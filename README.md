# BoardHog

Small CLI for py-spinnaker2 board locks.

Board inventory comes from the SpiNNaker2 network config. `boardhog` shows unavailable boards by default.

## Status Indicators

🟢 `free` • 🟡 `short` (<1 min) • 🟠 `medium` (1-5 min) • 🔴 `long` (>5 min) • `?` `missing`

`boardhog` shows locked boards by default. Use `--all` to include free and missing boards.

Availability is per board: a board is unavailable only when its own `BOARD_<ip>` lock is held. A frame's STM lock does not make its sibling boards unavailable (boards under one STM co-allocate), so `boardhog` does not track it.

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
Unavailable Boards

2.21  🔴 alice        12m 04s  python     (frame_1[0])
```

`--details`:

```text
192.0.2.21      (BOARD_192_0_2_21.lock): locked by alice (PID=1234, CMD=python) for 12m 04s
```

`--json`:

```json
[{"ip":"192.0.2.21","state":"long","holder":{"pid":"1234","user":"alice","command":"python","age_seconds":724,"age":"12m 04s"}}]
```


