# BoardHog

Small CLI for py-spinnaker2 board locks.

Board inventory comes from the SpiNNaker2 network config, whose `ETH_IP_START` must be IPv4, plus every `BOARD_<ip>.lock` file the config does not list; a dangling link adds no board. `boardhog` shows unavailable boards by default.

## Status Indicators

`●` held • `○` free • `?` missing lock file. `--plain` prints the state as a word padded to one width, splitting held by age into `short` (<1 min), `medium` (1-5 min) and `long` (>5 min or unknown).

`boardhog` shows locked boards by default. Use `--all` to include free and missing boards. A board is unavailable when its own `BOARD_<ip>` lock is held.

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

2.21  ● alice        12m 04s    python          (frame_1[0])
```

`--all --plain`:

```text
Board Status

2.21  long    alice        12m 04s    python          (frame_1[0])
2.22  free    frame_1[1]
2.23  missing frame_1[2] (no BOARD_192_0_2_23.lock)
```

Boards cabled into one machine (`CABLED_GROUPS` in `boardhog.py`) get a bracket when one process holds them all, drawn `/`, `|`, `\` under `--plain`:

```text
2.21 ┌● alice        12m 04s    python          (frame_1[0])
2.22 │● alice        12m 04s    python          (frame_1[1])
2.23 └● alice        12m 04s    python          (frame_1[2])
```

`--details`:

```text
192.0.2.21      (BOARD_192_0_2_21.lock): locked by alice (PID=1234, CMD=python) for 12m 04s
```

`--json`:

```json
[{"ip":"192.0.2.21","state":"long","holder":{"pid":"1234","user":"alice","command":"python","age_seconds":724,"age":"12m 04s"}}]
```


