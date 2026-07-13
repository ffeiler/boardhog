# BoardHog

Small CLI for py-spinnaker2 board locks.

Board inventory comes from the SpiNNaker2 network config. `boardhog` shows unavailable boards by default.

## Status Indicators

🟢 `free` • 🟡 `short` (<1 min) • 🟠 `medium` (1-5 min) • 🔴 `long` (>5 min) • 🚫 `blocked` (own lock free, but frame STM held) • `?` `missing`

`boardhog` shows unavailable boards (locked or blocked) by default. Use `--all` to include free and missing boards plus STM controllers.

## Install

From this repository:

```bash
python -m pip install -e .
```

## Usage

Common checks:

```bash
boardhog                         # unavailable boards only (locked or blocked)
boardhog --all                   # include free/missing boards and STM controllers
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
2.22  🚫 alice        12m 04s  python     (frame_1[1]) via STM 2.2
```

`--all` also lists STM controllers as their own rows (`STM frame_N`).

```text
192.0.2.21      (BOARD_192_0_2_21.lock): locked by alice (PID=1234, CMD=python) for 12m 04s
192.0.2.22      (BOARD_192_0_2_22.lock): blocked by alice via STM 192.0.2.2 (PID=1234, CMD=python) for 12m 04s
```

```json
[{"ip":"192.0.2.22","state":"blocked","stm_ip":"192.0.2.2","holder":null,"stm_holder":{"pid":"1234","user":"alice","command":"python","age_seconds":724,"age":"12m 04s"}}]
```


