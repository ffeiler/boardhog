# BoardHog

Small CLI for py-spinnaker2 board locks.

Board inventory comes from the SpiNNaker2 network config, whose `ETH_IP_START` must be IPv4, plus every `BOARD_<ip>.lock` file the config does not list; a dangling link adds no board. `boardhog` shows unavailable boards by default.

## Status Indicators

`●` held • `○` free • `?` missing lock file. `--plain` prints the state as a word padded to one width, splitting held by age into `short` (<1 min), `medium` (1-5 min) and `long` (>5 min or unknown).

The age column is the holding process's age: a process that waited for the lock, or a long-lived kernel, reads older than its hold.

`boardhog` shows locked boards by default. Use `--all` to include free and missing boards; the free ones fold into one `free` line, consecutive addresses joined into a range, while `--all --plain` keeps one row per board. A board is unavailable when its own `BOARD_<ip>` lock is held.

The command column names the script from the process's command line: the first argument ending `.py` or `.sh`, the module after `-m`, the first argument of a Python interpreter (a console script such as `pytest`), else the program name, cut to 15 characters. It falls back to the kernel's `comm` when the command line is unreadable.

## Holders that exited

`/proc/locks` records the PID that took a lock. When that process has exited and a child or a shell still holds the descriptor, as `exec 9>FILE; flock 9` leaves it, `boardhog` tries every PID the lock records, then the reader's own processes whose descriptor on the lock file holds the lock (`lock:` in `/proc/<pid>/fdinfo`), so a shell waiting for the lock never counts. Another user's descriptors are unreadable, so their row reads:

```text
1.5   ● ?            @12:59?    -               (frame_2)  pid 2373047 exited
```

`@HH:MM?` is the lock file's mtime, shown only when it is later than host boot. Only a truncating open such as `exec 9>FILE` moves it, and a shell that waits for the lock moves it too, so it is a hint; otherwise the cell reads `-`. Such a row takes its state from that open time, or `medium` when there is none, never `long` for an unknown age.

## Waiters

Processes blocked on a board's lock (`->` lines in `/proc/locks`) show as a count after the row, `+1 waiting`; `--details` lists each one with its user, PID, command and age. Flock waiters race, so the list implies no order.

## Styling

On a terminal the title is bold, frame labels and notes are dim, and one accent, bold `#e46212`, marks the dot and note of a row whose holder exited and the dot of a hold in the `long` age bucket. Piped output, `--plain` and a non-empty `NO_COLOR` print no escape codes.

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

2.21  ● alice        12m 04s    run_tier.sh     (frame_1[0])  +1 waiting
2.24  ● ?            @12:59?    -               (frame_1[3])  pid 2373047 exited
```

`--all`:

```text
Board Status

2.21  ● alice        12m 04s    run_tier.sh     (frame_1[0])  +1 waiting
2.23  ? frame_1[2] (no BOARD_192_0_2_23.lock)

free  2.22 2.25-2.29
```

`--all --plain`:

```text
Board Status

2.21  long    alice        12m 04s    run_tier.sh     (frame_1[0])  +1 waiting
2.22  free    frame_1[1]
2.23  missing frame_1[2] (no BOARD_192_0_2_23.lock)
```

Boards cabled into one machine (`CABLED_GROUPS` in `boardhog.py`) get a bracket when one process holds them all, drawn `/`, `|`, `\` under `--plain`:

```text
2.21 ┌● alice        12m 04s    run_tier.sh     (frame_1[0])
2.22 │● alice        12m 04s    run_tier.sh     (frame_1[1])
2.23 └● alice        12m 04s    run_tier.sh     (frame_1[2])
```

`--details`:

```text
192.0.2.21      (BOARD_192_0_2_21.lock): locked by alice (PID=1234, CMD=run_tier.sh) for 12m 04s
    waiting: bob (PID=1301, CMD=flock) for 2m 36s
192.0.2.24      (BOARD_192_0_2_24.lock): locked, holder PID=2373047 exited, lock file opened 12:59
```

`--json`, one object per board (abridged here). `lock_mtime` is ISO time with the local offset. A holder carries `script`, `alive`, `source` (`proc` a PID `/proc/locks` records, `fd` the reader's own process with the file open, `none` no live holder found) and `dead_pid` (the recorded PID that exited, else `null`); `waiters` lists blocked processes in the holder's shape:

```json
[{"ip":"192.0.2.21","lock_mtime":"2026-10-06T12:59:54+02:00","state":"long","holder":{"pid":"1234","user":"alice","command":"bash","script":"run_tier.sh","age_seconds":724,"age":"12m 04s","alive":true,"source":"proc","dead_pid":null},"waiters":[{"pid":"1301","user":"bob","command":"flock","script":"flock","age_seconds":156,"age":"2m 36s","alive":true,"source":"proc","dead_pid":null}]}]
```


