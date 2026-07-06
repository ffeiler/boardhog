# BoardHog

Small CLI for py-spinnaker2 board locks.

Reads the config root from `/etc/opt/spinnaker/SPINNAKER_CONFIG_PATH`, loads `spinnaker2_network_config.yml`, and checks `/mnt/spinnaker/locks/BOARD_*.lock` through `/proc/locks`.

Lock files are placeholders. A board is busy only while a process holds the file lock.

## Status Indicators

🟢 `free` • 🟡 `short` (<1 min) • 🟠 `medium` (1-5 min) • 🔴 `long` (>5 min) • `?` `missing`

`boardhog` shows locked boards by default. Use `--all` to include free and missing boards.

## Install

From this repository:

```bash
python -m pip install -e .
```

## Usage

Common checks:

```bash
boardhog                         # locked boards only
boardhog --all                   # include free and missing boards
boardhog --details               # lock file, PID, command, age
```

Script output:

```bash
boardhog --plain --no-header     # text states, no title
boardhog --json                  # compact JSON
boardhog --json --pretty         # readable JSON
```

Run `boardhog --help` for all flags.

## Output

```text
Locked Boards

1.11            🔴 alice 12m 04s python (frame_5)
```

```text
192.168.1.11    (BOARD_192_168_1_11.lock): locked by alice (PID=1234, CMD=python) for 12m 04s
```

```json
[{"ip":"192.168.1.11","state":"long","holder":{"pid":"1234","user":"alice","command":"python","age_seconds":724,"age":"12m 04s"}}]
```


