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
