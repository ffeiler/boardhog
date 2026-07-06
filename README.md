# BoardHog

Small CLI for py-spinnaker2 board locks.

Reads the config root from `/etc/opt/spinnaker/SPINNAKER_CONFIG_PATH`, loads `spinnaker2_network_config.yml`, and checks `/mnt/spinnaker/locks/BOARD_*.lock` through `/proc/locks`.

Lock files are placeholders. A board is busy only while a process holds the file lock.

## Install

```bash
python -m pip install -e ~/boardhog
```

Or keep the existing symlink:

```bash
ln -sf ~/boardhog/boardhog.py ~/.local/bin/boardhog
```

## Use

```bash
boardhog                         # locked boards only, emoji
boardhog --all                   # include free and missing boards
boardhog --plain --no-header     # script-friendly text
boardhog --details               # full IP, lock file, PID, command, age
boardhog --json                  # locked boards as compact JSON
boardhog --json --pretty         # readable JSON
watch_locks                      # zsh alias
```

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

## Alias

```zsh
alias watch_locks='watch -n 1 -d boardhog'
```

## States

`boardhog` shows `short`, `medium`, and `long` by default. Use `--all` to include `free` and `missing`.
