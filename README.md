# Provision X30

Provisions Extronics iTAG X30 tags (Gen2, XA JSON protocol) over BLE, one tag or a batch.
Gen1 X30s never advertise over BLE and are out of scope.

## Setup

```
pip install -r requirements.txt
```

`--wake` additionally needs `read_x30_gen1_tags.py` next to the script (it drives the TED's
broadcast discovery over serial) and EDM closed, since EDM holds the TED's port.

## Typical use

```
python provision_x30_tags.py --list                       # fields and profiles
python provision_x30_tags.py --scan --wake                 # wake tags via the TED, list them
python provision_x30_tags.py --mac 000ccc0f1522 --read     # snapshot only
python provision_x30_tags.py --mac 000ccc0f1522 --config site.json
python provision_x30_tags.py --batch --wake --config site.json
python provision_x30_tags.py --mac 000ccc0f1522 --restore snapshots\000ccc0f1522-<stamp>.json
```

A tag must be excited (flashing blue) to be reachable: hold the rear button, or use `--wake`.

Copy `site.example.json` to `site.json` and edit it. Prefer `--config` over `--set` for strings;
PowerShell 5.1 strips double quotes out of native-command arguments.

## What a write does

1. Reads every block; refuses a tag whose build is not iTAGX30 or whose `pcc.mac` disagrees.
2. Saves a snapshot to `snapshots/` (the restore point for `--restore`).
3. Sends `set*` only for fields that differ; stops before `savecfg` on the first rejection.
4. Sends `savecfg`.
5. Reconnects and reads every field back.

`--batch` logs one line per tag to `provision-log.csv` with a fingerprint of the config, and a
later run skips tags already logged ok under the same fingerprint (`--redo` to include them).

Exit codes: 0 all verified, 1 any failure, 3 saved but some field could not be read back.

`snapshots/` and `provision-log.csv` are site data and are git-ignored.

## Phone / browser version

`x30_provisioner.html` does the same read, snapshot, write, savecfg and read-back over Web
Bluetooth, so a phone can provision a tag without the PC. It needs Chrome or Edge on Android or
Windows (iPhone: the Bluefy browser), and Web Bluetooth only runs from HTTPS or localhost.

Quickest way to use it from a phone on the same LAN:

```
python -m http.server 8000
```

then on the phone open `chrome://flags/#unsafely-treat-insecure-origin-as-secure`, add
`http://<pc-ip>:8000`, relaunch Chrome and browse to `http://<pc-ip>:8000/x30_provisioner.html`.
Any HTTPS host (GitHub Pages, an internal web server) works without the flag.

Web Bluetooth must be told the service UUID in advance and the Python script only records the
characteristic. The page searches a range of likely UUIDs on connect and remembers the one that
works; if that fails, run `python dump_x30_services.py` on the PC while a tag is excited and paste
the service UUID under Link settings.

Snapshots taken from the page live in that browser's local storage (last 20) and can be
downloaded as the same JSON the Python script writes.
