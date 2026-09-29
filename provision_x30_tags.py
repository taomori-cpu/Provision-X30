"""Provision Extronics iTAG X30 tags over BLE (XA JSON protocol), one tag or a batch.

Gen1 X30s are out of scope: they never advertise over BLE and are configured
through the TED only. A tag is reachable only after it is excited and flashes
blue: rear button hold, or --wake, which sends the TED's broadcast discovery
(read getters only, as read_x30_gen1_tags.py --detect) and wakes every X30 in its range.

    python provision_x30_tags.py --scan
    python provision_x30_tags.py --scan --wake
    python provision_x30_tags.py --mac 000ccc0f1522 --read
    python provision_x30_tags.py --mac 000ccc0f1522 --config site.json
    python provision_x30_tags.py --mac 000ccc0f1522 --set dfrm=7 --set txp=16
    python provision_x30_tags.py --mac 000ccc0f1522 --restore snapshots\\000ccc0f1522-20260928-101500.json
    python provision_x30_tags.py --batch --config site.json
    python provision_x30_tags.py --config site.json --dry-run
    python provision_x30_tags.py --list

--config is a JSON object of field names to values, e.g.

    {"dfrm": 7, "txp": 16, "bcnchmask": 2114, "ssid": "Site WLAN", "pass": "secret"}

Keys starting with "_" are ignored, so "_comment" is free. Prefer it to --set
for strings: PowerShell 5.1 strips double quotes out of native-command arguments.

Every write, per tag:
  1. reads every block and refuses a tag whose build is not iTAGX30 or whose
     pcc.mac disagrees with its advertised name or --mac;
  2. saves a snapshot to --snapshot-dir, the restore point for --restore;
  3. sends set* only for fields that differ, and stops without savecfg on the
     first rejection, so the tag reverts to its saved config on disconnect;
  4. sends savecfg (set* alone lands in RAM and is lost on disconnect);
  5. reconnects and reads every field back, because an unsaved value reads
     back correctly on the connection that wrote it, and res 0 does not mean
     a key was accepted.

--batch waits for excited X30s and provisions each once, appending one line
per tag to --log with a fingerprint of the target values; a later run skips
tags already logged ok under the same fingerprint. Without a config it just
snapshots each tag, which makes it an inventory pass, and an inventory never
counts as provisioned. With
--wake it re-sends the broadcast whenever no new tag shows up for a while, so
tags laid next to the TED are provisioned hands-free.

Exit code: 0 all verified, 1 any failure, 3 saved but some field unverifiable.

Needs bleak (`pip install bleak`); the WinRT backend negotiates the ATT MTU.
--wake also needs pyserial (`pip install pyserial`), read_x30_gen1_tags.py beside
this script, and EDM closed, since EDM holds the TED's port while it runs.

--wake reports "TED not detected" and stops, before any BLE scan, when no port
matches the TED's VID:PID 2B4F:0001 or "TED Device" description, when the port
will not open, or when it opens but nothing acknowledges the broadcast: the TED
acks every request, so an open port alone does not count as a TED. In --batch
the first wake must succeed; a later failure is reported and the batch keeps
waiting, since the rear button hold still wakes tags.
"""
import argparse
import asyncio
import csv
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime

from bleak import BleakClient, BleakScanner

XA_UUID = '51f1f198-4321-9abc-4321-0067eaa0da40'
NAME_PREFIX = 'iTAG X30'
BUILD_PREFIX = 'iTAGX30'
SCAN_TIMEOUT = 30.0
REWAKE_SECONDS = 60.0
# The TED answers every host message with TLV 0300 carrying the acknowledged
# type; 030B is the tag-request shell the broadcast travels in.
TED_ACK_OF_REQUEST = bytes.fromhex('03000002030B')
TED_ACK_WAIT = 1.5
SCAN_LIST_SECONDS = 12.0
# Keeps scanning after the first match so two excited tags are caught as
# ambiguous instead of silently picking whichever advertised first.
SETTLE_SECONDS = 3.0
REPLY_TIMEOUT = 10.0
CONNECT_TIMEOUT = 15.0
ATT_WRITE_OVERHEAD = 3
MIN_ATT_MTU = 23
MTU_WAIT_SECONDS = 3.0
MAX_REPLY_BYTES = 4096

BLOCKS = {
    'wifi': ('getwificfg', 'setwificfg'),
    'platform': ('getplatformcfg', 'setplatformcfg'),
    'beacon': ('getbeaconcfg', 'setbeaconcfg'),
    'motion': ('getmotioncfg', 'setmotioncfg'),
    'ble': ('getblecfg', 'setblecfg'),
    'gps': ('getgpscfg', 'setgpscfg'),
    'status': ('getstatus', None),
}
CONFIG_BLOCKS = [b for b, (_, setter) in BLOCKS.items() if setter]
SAVE_COMMAND = 'savecfg'
SECRET_KEYS = {'pass'}

EXIT_OK, EXIT_FAILED, EXIT_UNVERIFIED = 0, 1, 3


@dataclass
class Field:
    block: str
    write: str
    read: str | None
    kind: str  # int, bool, str, or mac (12 hex digits, compared case-insensitively)
    lo: int | None = None
    hi: int | None = None
    note: str = ''


FIELDS = {
    # Bit 0 IBSS, bit 1 WDS, bit 2 CCX. Write "dfrm", not EDM's "fmt": this
    # firmware answers res 0 to "fmt" and leaves the mask alone.
    'dfrm': Field('wifi', 'bcn.dfrm', 'bcn.dfrm', 'int', 1, 7, 'beacon format mask: 1 IBSS, 2 WDS, 4 CCX'),
    'oui': Field('wifi', 'bcn.oui', 'bcn.oui', 'str'),
    'txp': Field('wifi', 'bcn.txp', 'bcn.txp', 'int'),
    'rep': Field('wifi', 'bcn.rep', 'bcn.rep', 'int', 1),
    'intms': Field('wifi', 'bcn.intms', 'bcn.intms', 'int', 0),
    'bcmacccx': Field('wifi', 'bcn.bcmacccx', 'bcn.bcmacccx', 'mac'),
    'bcmacibsswds': Field('wifi', 'bcn.bcmacibsswds', 'bcn.bcmacibsswds', 'mac'),
    'bletlvmax': Field('wifi', 'bcn.bletlvmax', 'bcn.bletlvmax', 'int', 0),
    # Bit N selects 2.4 GHz channel N, so 2114 is channels 1, 6, 11.
    'bcnchmask': Field('wifi', 'bcn.chmask', 'bcn.chmask', 'int', 2, 0x7FFE, 'channels beaconed on'),
    'ssid': Field('wifi', 'sta.ssid', 'sta.ssid', 'str'),
    'pass': Field('wifi', 'sta.pass', 'sta.pass', 'str'),
    'url': Field('wifi', 'sta.url', 'sta.url', 'str', note='firmware update URL'),
    # getwificfg returns no "scan" object, so these cannot be read back.
    'scanchmask': Field('wifi', 'scan.chmask', None, 'int', 2, 0x7FFE, 'channels scanned'),
    'ascn': Field('wifi', 'scan.ascn', None, 'bool'),
    'ascnmax': Field('wifi', 'scan.ascnmax', None, 'int', 0),
    'ascnmin': Field('wifi', 'scan.ascnmin', None, 'int', 0),
    'pscn': Field('wifi', 'scan.pscn', None, 'int', 0),
    'act': Field('platform', 'pcc.act', 'pcc.act', 'int', note='activation state'),
    # EDM writes these as booleans; the tag reports and accepts milliseconds.
    'itvinmotion': Field('beacon', 'itvinmotion', 'itvinmotion', 'int', 1),
    'itvstatic': Field('beacon', 'itvstatic', 'itvstatic', 'int', 1),
    'itvputdown': Field('beacon', 'itvputdown', 'itvputdown', 'int', 1),
    'itvonchg': Field('beacon', 'itvonchg', 'itvonchg', 'int', 1),
    'sampthresh': Field('motion', 'sampthresh', 'sampthresh', 'int', 0),
    'stimeoutms': Field('motion', 'stimeoutms', 'stimeoutms', 'int', 0),
    'pdtimeoutms': Field('motion', 'pdtimeoutms', 'pdtimeoutms', 'int', 0),
    'bledur': Field('ble', 'scan.dur', 'scan.dur', 'int', 0),
    'blerssi': Field('ble', 'scan.rssi', 'scan.rssi', 'int', -127, 0),
    'gpsfixtimeoutms': Field('gps', 'fixtimeoutms', 'fixtimeoutms', 'int', 0),
}

PROFILES = {
    'all-formats': {'dfrm': 7},
    'aeroscout-only': {'dfrm': 3},
    'ccx-only': {'dfrm': 4},
    'ibss-only': {'dfrm': 1},
    'wds-only': {'dfrm': 2},
}


class Refused(Exception):
    pass


def log(text=''):
    print(text, flush=True)


def dig(tree, path):
    for step in path.split('.'):
        if not isinstance(tree, dict) or step not in tree:
            return None
        tree = tree[step]
    return tree


def plant(tree, path, value):
    steps = path.split('.')
    for step in steps[:-1]:
        tree = tree.setdefault(step, {})
    tree[steps[-1]] = value


def leaves(tree, prefix=''):
    for key, value in tree.items():
        path = prefix + key
        if isinstance(value, dict):
            yield from leaves(value, path + '.')
        else:
            yield path, value


def redact(tree):
    if isinstance(tree, dict):
        return {k: ('***' if k in SECRET_KEYS and v else redact(v)) for k, v in tree.items()}
    return tree


def shown(name, value):
    return '***' if name in SECRET_KEYS and value else value


def normalize_mac(text):
    digits = re.sub(r'[^0-9A-Fa-f]', '', text or '')
    return digits.lower() if len(digits) == 12 else None


def name_mac(name):
    match = re.search(r'([0-9A-Fa-f]{12})\s*$', name or '')
    return match.group(1).lower() if match else None


def validate(name, value):
    field = FIELDS[name]
    kind = field.kind
    if kind == 'int':
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError('%s wants an integer, got %r' % (name, value))
        if field.lo is not None and value < field.lo or field.hi is not None and value > field.hi:
            raise ValueError('%s=%d is outside %s..%s' % (name, value, field.lo, field.hi))
    elif kind == 'bool':
        if not isinstance(value, bool):
            raise ValueError('%s wants true or false, got %r' % (name, value))
    else:
        if not isinstance(value, str):
            raise ValueError('%s wants a string, got %r' % (name, value))
        if kind == 'mac':
            if not re.fullmatch(r'[0-9A-Fa-f]{12}', value):
                raise ValueError('%s wants 12 hex digits, got %r' % (name, value))
            value = value.upper()
    return value


def from_cli(name, raw):
    kind = FIELDS[name].kind
    if kind == 'int':
        try:
            return int(raw)
        except ValueError:
            raise ValueError('%s wants an integer, got %r' % (name, raw)) from None
    if kind == 'bool':
        if raw.lower() not in ('true', 'false'):
            raise ValueError('%s wants true or false, got %r' % (name, raw))
        return raw.lower() == 'true'
    return raw


def same(name, want, now):
    if FIELDS[name].kind == 'mac':
        return isinstance(now, str) and now.upper() == want
    return type(now) is type(want) and now == want


def group(targets):
    out = {}
    for name, value in targets.items():
        field = FIELDS[name]
        plant(out.setdefault(field.block, {}), field.write, value)
    return out


def encode(name, params=None):
    body = {'name': name}
    if params:
        body['params'] = params
    return json.dumps({'cmd': body}, separators=(',', ':')).encode('utf-8')


def split_params(setter, params, limit):
    """One write if it fits the MTU, else one per leaf; set* merges, so partial blocks are safe."""
    if limit is None or len(encode(setter, params)) <= limit:
        return [params]
    chunks = []
    for path, value in leaves(params):
        chunk = {}
        plant(chunk, path, value)
        if len(encode(setter, chunk)) > limit:
            raise Refused('%s %s does not fit one %d-byte write' % (setter, path, limit))
        chunks.append(chunk)
    return chunks


def ok(rsp):
    return rsp is not None and rsp.get('res') == 0


class Tag:
    def __init__(self, client):
        self.client = client
        self.buffer = bytearray()
        self.pending = None

    @property
    def max_write(self):
        """Bytes per write, or None while the stack still reports the pre-exchange MTU."""
        mtu = self.client.mtu_size or MIN_ATT_MTU
        return mtu - ATT_WRITE_OVERHEAD if mtu > MIN_ATT_MTU else None

    def _on_notify(self, _sender, data):
        self.buffer += data
        if len(self.buffer) > MAX_REPLY_BYTES:
            self.buffer.clear()
            return
        try:
            parsed = json.loads(self.buffer.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            return  # A reply longer than one notification arrives in pieces.
        self.buffer.clear()
        rsp = parsed.get('rsp') if isinstance(parsed, dict) else None
        if not isinstance(rsp, dict) or self.pending is None:
            return
        name, future = self.pending
        if rsp.get('name') != name:
            log('  <- ignored a reply to %r while waiting for %r' % (rsp.get('name'), name))
        elif not future.done():
            future.set_result(rsp)

    async def start(self):
        await self.client.start_notify(XA_UUID, self._on_notify)

    async def command(self, name, params=None):
        payload = encode(name, params)
        limit = self.max_write
        if limit is not None and len(payload) > limit:
            raise Refused('%s is %d bytes; this link carries %d per write'
                          % (name, len(payload), limit))
        self.buffer.clear()
        future = asyncio.get_running_loop().create_future()
        self.pending = (name, future)
        log('  -> %s' % json.dumps(redact(json.loads(payload)), separators=(',', ':')))
        try:
            await self.client.write_gatt_char(XA_UUID, payload, response=False)
            rsp = await asyncio.wait_for(future, REPLY_TIMEOUT)
        except asyncio.TimeoutError:
            log('  <- no reply within %.0fs' % REPLY_TIMEOUT)
            return None
        finally:
            self.pending = None
        log('  <- res=%s %s' % (rsp.get('res'), json.dumps(redact(rsp.get('params', {})))))
        return rsp

    async def read_blocks(self, blocks):
        out = {}
        for block in blocks:
            rsp = await self.command(BLOCKS[block][0])
            out[block] = rsp.get('params', {}) if ok(rsp) else None
        return out


async def open_tag(client):
    if client.services.get_characteristic(XA_UUID) is None:
        raise Refused('no XA config characteristic; not an XA-protocol X30')
    tag = Tag(client)
    await tag.start()
    # WinRT reports the default 23-byte MTU at connect and raises it once the
    # exchange completes; every XA command is longer than 20 bytes.
    for _ in range(int(MTU_WAIT_SECONDS / 0.1)):
        if tag.max_write is not None:
            break
        await asyncio.sleep(0.1)
    if tag.max_write is None:
        log('ATT write limit: unknown (stack still reports MTU %s); sending unsplit'
            % client.mtu_size)
    else:
        log('ATT write limit: %d bytes' % tag.max_write)
    return tag


async def wait_for_tags(skip=(), mac=None, timeout=SCAN_TIMEOUT):
    """Return {ble_address: name} of excited X30s, after the first match plus a settle period."""
    seen = {}
    first = asyncio.Event()

    def on_adv(device, adv):
        name = adv.local_name or device.name or ''
        if not name.startswith(NAME_PREFIX):
            return
        key = name_mac(name) or device.address
        if key in skip or (mac and key != mac):
            return
        seen[device.address] = name
        first.set()

    async with BleakScanner(on_adv):
        try:
            await asyncio.wait_for(first.wait(), timeout)
        except asyncio.TimeoutError:
            return {}
        await asyncio.sleep(SETTLE_SECONDS)
    return seen


def ted_wake(port):
    """Send the TED's broadcast discovery; raise Refused unless a TED acknowledges it."""
    try:
        import read_x30_gen1_tags as gen1
    except ImportError as exc:
        raise Refused('--wake needs pyserial and read_x30_gen1_tags.py beside this script (%s)'
                      % exc)
    port = port or gen1.find_port()
    if port is None:
        raise Refused('TED not detected: no serial port with VID:PID 2B4F:0001 or a '
                      '"TED Device" description. Check its USB cable, or pass --ted-port.')
    try:
        ted = gen1.Ted(port)
    except gen1.serial.SerialException as exc:
        raise Refused('TED not usable: %s could not be opened (%s). If EDM is running, close '
                      'it: it holds the port.' % (port, exc))
    request = gen1.tag_request(gen1.BROADCAST_MAC, gen1.DETECT_GETTERS)
    log('Waking X30s through the TED on %s...' % port)
    try:
        ted.start()
        time.sleep(0.5)
        since = time.monotonic()
        ted.send(request)
        deadline = since + TED_ACK_WAIT
        while time.monotonic() < deadline:
            if any(TED_ACK_OF_REQUEST in f for f in ted.take(since)):
                break
            time.sleep(0.1)
        else:
            raise Refused('TED not detected: %s opened but nothing acknowledged the broadcast. '
                          'Replug the TED, or pass --ted-port if this is the wrong port.' % port)
        for _ in range(gen1.DETECT_REPEATS - 1):
            ted.send(request)
            time.sleep(gen1.DETECT_DELAY)
    except gen1.serial.SerialException as exc:
        raise Refused('TED on %s stopped responding (%s)' % (port, exc))
    finally:
        try:
            ted.stop()
        except gen1.serial.SerialException:
            pass
    log('TED acknowledged; X30s in its range should start advertising.')


async def wake(args):
    """Wake through the TED when --wake is set; False, with the reason logged, if it failed."""
    if not args.wake:
        return True
    try:
        await asyncio.to_thread(ted_wake, args.ted_port)
    except Refused as exc:
        log('!! %s' % exc)
        return False
    return True


async def scan(args):
    if not await wake(args):
        return EXIT_FAILED
    seen = {}

    def on_adv(device, adv):
        name = adv.local_name or device.name or ''
        if name.startswith(NAME_PREFIX):
            seen[device.address] = (name, adv.rssi)

    log('Scanning for %.0fs...' % SCAN_LIST_SECONDS)
    async with BleakScanner(on_adv):
        await asyncio.sleep(SCAN_LIST_SECONDS)
    if not seen:
        log('No X30 advertising. Excite the tag (it flashes blue) and retry.')
        return EXIT_FAILED
    log('\n%-18s %5s  %-14s %s' % ('ble address', 'rssi', 'wifi mac', 'name'))
    for address, (name, rssi) in sorted(seen.items(), key=lambda kv: kv[1][0]):
        log('%-18s %5s  %-14s %s' % (address, rssi, name_mac(name) or '?', name))
    return EXIT_OK


def check_identity(blocks, expected_mac):
    platform = blocks.get('platform') or {}
    build = dig(platform, 'pce_ro.build')
    mac = normalize_mac(str(dig(platform, 'pcc.mac') or ''))
    if not (isinstance(build, str) and build.startswith(BUILD_PREFIX)):
        raise Refused('build is %r, not %s' % (build, BUILD_PREFIX))
    if mac is None:
        raise Refused('getplatformcfg carries no pcc.mac')
    if expected_mac and mac != expected_mac:
        raise Refused('pcc.mac is %s, expected %s' % (mac, expected_mac))
    return mac


def write_snapshot(directory, mac, address, name, blocks):
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, '%s-%s.json' % (mac, datetime.now().strftime('%Y%m%d-%H%M%S')))
    doc = {'mac': mac, 'address': address, 'name': name,
           'taken': datetime.now().isoformat(timespec='seconds'), 'blocks': blocks}
    with open(path, 'w', encoding='utf-8') as out:
        json.dump(doc, out, indent=2)
    log('Snapshot: %s' % path)
    return path


def load_restore(path):
    with open(path, encoding='utf-8') as src:
        doc = json.load(src)
    targets = {}
    for name, field in FIELDS.items():
        if field.read is None:
            continue
        value = dig(doc.get('blocks', {}).get(field.block) or {}, field.read)
        if value is not None:
            targets[name] = validate(name, value)
    return normalize_mac(doc.get('mac')), targets


def load_config(path):
    with open(path, encoding='utf-8') as src:
        doc = json.load(src)
    if not isinstance(doc, dict):
        raise ValueError('%s must hold a JSON object' % path)
    targets = {}
    for name, value in doc.items():
        if name.startswith('_'):
            continue
        if name not in FIELDS:
            raise ValueError('%s: unknown field %r; --list shows them' % (path, name))
        targets[name] = validate(name, value)
    return targets


def build_targets(args):
    restore_mac, targets = load_restore(args.restore) if args.restore else (None, {})
    if args.profile:
        targets.update(PROFILES[args.profile])
    if args.config:
        targets.update(load_config(args.config))
    for assignment in args.set or []:
        name, sep, raw = assignment.partition('=')
        if not sep:
            raise ValueError('--set wants FIELD=VALUE, got %r' % assignment)
        if name not in FIELDS:
            raise ValueError('unknown field %r; --list shows them' % name)
        targets[name] = validate(name, from_cli(name, raw))
    return restore_mac, targets


def report(rows):
    width = max(len(r[0]) for r in rows)
    fmt = '%-*s  %-16s %-16s %-16s %s'
    log('\n' + fmt % (width, 'field', 'before', 'target', 'after', 'result'))
    for name, before, target, after, verdict in rows:
        log(fmt % (width, name, shown(name, before), shown(name, target), shown(name, after), verdict))


async def provision(address, name, expected_mac, targets, args):
    """Return (status, wifi mac, savecfg crc, detail) for one tag."""
    log('\n=== %s [%s] ===' % (name, address))
    expected_mac = expected_mac or name_mac(name)
    mac, crc = expected_mac, None
    try:
        async with BleakClient(address, timeout=CONNECT_TIMEOUT) as client:
            tag = await open_tag(client)
            log('\n--- before ---')
            before = await tag.read_blocks(BLOCKS)
            unread = [b for b in CONFIG_BLOCKS if before[b] is None]
            if unread:
                raise Refused('could not read %s; nothing written' % ', '.join(unread))
            mac = check_identity(before, expected_mac)
            if args.restore_mac and mac != args.restore_mac:
                raise Refused('snapshot is from %s, not this tag %s' % (args.restore_mac, mac))
            write_snapshot(args.snapshot_dir, mac, address, name, before)
            if not targets:
                return 'read', mac, None, ''

            changes = {k: v for k, v in targets.items()
                       if FIELDS[k].read is None
                       or not same(k, v, dig(before[FIELDS[k].block], FIELDS[k].read))}
            if changes:
                log('\n--- writing ---')
                for block, params in group(changes).items():
                    setter = BLOCKS[block][1]
                    for chunk in split_params(setter, params, tag.max_write):
                        if not ok(await tag.command(setter, chunk)):
                            raise Refused('%s rejected; not saved, the tag reverts on disconnect'
                                          % setter)
                log('\n--- saving ---')
                rsp = await tag.command(SAVE_COMMAND)
                if not ok(rsp):
                    raise Refused('%s failed; the tag reverts on disconnect' % SAVE_COMMAND)
                crc = (rsp.get('params') or {}).get('crc')
            else:
                log('\nAlready at target; nothing written.')
    except Refused as exc:
        log('!! %s' % exc)
        return 'FAILED', mac, crc, str(exc)
    except Exception as exc:
        log('!! %s: %s' % (type(exc).__name__, exc))
        return 'FAILED', mac, crc, '%s: %s' % (type(exc).__name__, exc)

    if not changes:
        after = before
    else:
        log('\n--- after (new connection) ---')
        try:
            async with BleakClient(address, timeout=CONNECT_TIMEOUT) as client:
                tag = await open_tag(client)
                after = await tag.read_blocks(sorted({FIELDS[k].block for k in targets}))
        except Exception as exc:
            detail = 'saved (crc %s) but read-back failed: %s' % (crc, exc)
            log('!! ' + detail)
            return 'UNVERIFIED', mac, crc, detail

    rows, failed, unverified = [], [], []
    for name_, want in sorted(targets.items()):
        field = FIELDS[name_]
        if field.read is None:
            rows.append((name_, '?', want, '?', 'written, cannot be read back'))
            unverified.append(name_)
            continue
        was = dig(before[field.block], field.read)
        now = dig(after.get(field.block) or {}, field.read)
        if same(name_, want, now):
            verdict = 'ok' if name_ in changes else 'ok (already set)'
        else:
            verdict = 'UNCHANGED - key not accepted' if now == was else 'DIVERGED'
            failed.append(name_)
        rows.append((name_, was, want, now, verdict))
    report(rows)
    if crc:
        log('savecfg crc: %s' % crc)
    if failed:
        return 'FAILED', mac, crc, 'not applied: ' + ', '.join(failed)
    if unverified:
        return 'UNVERIFIED', mac, crc, 'write-only: ' + ', '.join(unverified)
    return 'ok', mac, crc, ''


STATUS_EXIT = {'ok': EXIT_OK, 'read': EXIT_OK, 'UNVERIFIED': EXIT_UNVERIFIED, 'FAILED': EXIT_FAILED}
# Exit codes are not ordered by severity: any FAILED outranks any UNVERIFIED.
EXIT_PRIORITY = [EXIT_FAILED, EXIT_UNVERIFIED, EXIT_OK]
LOG_HEADER = ['time', 'wifi_mac', 'ble_address', 'status', 'crc', 'config', 'detail']


def config_id(targets):
    """Short fingerprint of the target values, so a log row says which config it applied."""
    blob = json.dumps(targets, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(blob).hexdigest()[:12]


def log_layout_ok(path):
    if not os.path.exists(path):
        return True
    with open(path, newline='', encoding='utf-8') as src:
        return next(csv.reader(src), None) == LOG_HEADER


def append_log(path, address, status, mac, crc, config, detail):
    new = not os.path.exists(path)
    with open(path, 'a', newline='', encoding='utf-8') as out:
        writer = csv.writer(out)
        if new:
            writer.writerow(LOG_HEADER)
        writer.writerow([datetime.now().isoformat(timespec='seconds'), mac or '', address,
                         status, crc or '', config, detail])


def logged_done(path, config):
    """Wi-Fi MACs already logged ok (or read, for an inventory) under this same config."""
    if not os.path.exists(path):
        return set()
    with open(path, newline='', encoding='utf-8') as src:
        return {row['wifi_mac'] for row in csv.DictReader(src)
                if row.get('config') == config and row['status'] in ('ok', 'read')}


async def run_one(args, targets):
    if not await wake(args):
        return EXIT_FAILED
    if args.address:
        address, name = args.address, args.address
    else:
        log('Scanning for %s...' % (args.mac or 'an excited X30'))
        seen = await wait_for_tags(mac=args.mac)
        if not seen:
            log('ERROR: no X30 found. Excite the tag (it flashes blue) and retry.')
            return EXIT_FAILED
        if len(seen) > 1:
            log('ERROR: %d X30s advertising; pick one with --mac:' % len(seen))
            for address, name in sorted(seen.items()):
                log('  %s  %s' % (address, name))
            return EXIT_FAILED
        address, name = next(iter(seen.items()))
    status, mac, crc, detail = await provision(address, name, args.mac, targets, args)
    append_log(args.log, address, status, mac, crc, config_id(targets), detail)
    log('\nResult: %s %s' % (status, detail))
    return STATUS_EXIT[status]


async def run_batch(args, targets):
    config = config_id(targets)
    skip = set() if args.redo else logged_done(args.log, config)
    attempted = set()
    tally = {}
    if skip:
        log('Skipping %d tag(s) already logged ok with this config (%s) in %s '
            '(--redo to include them).' % (len(skip), config, args.log))
    if not await wake(args):
        return EXIT_FAILED
    try:
        while True:
            log('\nWaiting for an excited X30 (Ctrl-C to stop)...')
            seen = await wait_for_tags(skip=skip | attempted,
                                       timeout=REWAKE_SECONDS if args.wake else None)
            if not seen:
                if not await wake(args):
                    log('   Still waiting; the rear button hold wakes a tag without the TED.')
                continue
            address, name = sorted(seen.items(), key=lambda kv: kv[1])[0]
            status, mac, crc, detail = await provision(address, name, None, targets, args)
            append_log(args.log, address, status, mac, crc, config, detail)
            attempted.add(mac or name_mac(name) or address)
            tally[status] = tally.get(status, 0) + 1
            log('\nResult for %s: %s %s' % (mac or address, status, detail))
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    log('\nBatch done: %s' % (', '.join('%s %d' % kv for kv in sorted(tally.items())) or 'no tags'))
    codes = {STATUS_EXIT[s] for s in tally}
    return next((c for c in EXIT_PRIORITY if c in codes), EXIT_OK)


def list_fields():
    log('fields (for --set and --config):')
    for name, field in FIELDS.items():
        span = '' if field.lo is None and field.hi is None else '%s..%s' % (
            '' if field.lo is None else field.lo, '' if field.hi is None else field.hi)
        log('  %-16s %-5s %-12s %-8s %-18s %s' % (
            name, field.kind, span, field.block, field.read or '(write-only)', field.note))
    log('\nprofiles (for --profile):')
    for name, values in PROFILES.items():
        log('  %-16s %s' % (name, values))


def dry_run(targets):
    for block, params in group(targets).items():
        payload = encode(BLOCKS[block][1], params)
        log('%s  (%d bytes)' % (json.dumps(redact(json.loads(payload)), separators=(',', ':')),
                                len(payload)))
    log(SAVE_COMMAND)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter,
                                     epilog='\n'.join(__doc__.splitlines()[2:]))
    parser.add_argument('--scan', action='store_true', help='list advertising X30s and stop')
    parser.add_argument('--list', action='store_true', help='show fields and profiles')
    parser.add_argument('--mac', help="the tag's Wi-Fi MAC, as in its advertised name")
    parser.add_argument('--address', help='connect to this BLE address, skipping the scan')
    parser.add_argument('--batch', action='store_true', help='provision every excited X30 in turn')
    parser.add_argument('--redo', action='store_true', help='with --batch, include tags logged ok')
    parser.add_argument('--wake', action='store_true',
                        help='wake X30s through the TED before scanning')
    parser.add_argument('--ted-port', metavar='PORT', help='TED serial port, e.g. COM9; found if omitted')
    parser.add_argument('--read', action='store_true', help='read and snapshot, write nothing')
    parser.add_argument('--config', metavar='FILE', help='JSON object of field values')
    parser.add_argument('--profile', choices=sorted(PROFILES))
    parser.add_argument('--set', action='append', metavar='FIELD=VALUE')
    parser.add_argument('--restore', metavar='FILE', help="write back one tag's snapshot")
    parser.add_argument('--dry-run', action='store_true', help='print payloads, send nothing')
    parser.add_argument('--snapshot-dir', default='snapshots')
    parser.add_argument('--log', default='provision-log.csv')
    args = parser.parse_args()

    if args.list:
        list_fields()
        return EXIT_OK
    if args.scan:
        return asyncio.run(scan(args))

    if args.mac:
        args.mac = normalize_mac(args.mac)
        if args.mac is None:
            parser.error('--mac wants 12 hex digits')
    try:
        args.restore_mac, targets = build_targets(args)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))

    if args.dry_run:
        if not targets:
            parser.error('--dry-run needs --config, --profile, --set or --restore')
        dry_run(targets)
        return EXIT_OK
    if args.read and targets:
        parser.error('--read writes nothing; drop it or the write options')
    if not log_layout_ok(args.log):
        parser.error('%s has an older column layout; move it aside or pass --log' % args.log)
    if args.batch:
        if args.mac or args.address or args.restore:
            parser.error('--batch picks tags itself; drop --mac, --address and --restore')
        return asyncio.run(run_batch(args, targets))
    if not (args.mac or args.address):
        parser.error('pick a tag with --mac or --address (--scan lists them), or use --batch')
    if not (args.read or targets):
        parser.error('nothing to do: pass --read, --config, --profile, --set or --restore')
    return asyncio.run(run_one(args, targets))


if __name__ == '__main__':
    sys.exit(main())
