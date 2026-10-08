#!/usr/bin/env python3
"""Monitor py-spinnaker2 board locks."""

from __future__ import annotations

import argparse
import glob
import ipaddress
import json
import math
import os
import pwd
import re
import select
import signal
import sys
import termios
import time
import tty
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path


PROC = Path("/proc")
CONFIG_POINTER = Path("/etc/opt/spinnaker/SPINNAKER_CONFIG_PATH")
DEFAULT_CONFIG_ROOT = Path("/mnt/spinnaker")
NETWORK_CONFIG = "spinnaker2_network_config.yml"
LOCKS_DIR = "locks"
LOCK_PREFIX = "BOARD_"
LOCK_SUFFIX = ".lock"

BOARD_TYPES = {
    "201": "single-chip",
    "248": "48-node",
}

SYMBOLS = {
    "free": "○",
    "short": "●",
    "medium": "●",
    "long": "●",
    "missing": "?",
}
STATE_WIDTH = max(len(state) for state in SYMBOLS)
SYMBOL_WIDTH = max(len(symbol) for symbol in SYMBOLS.values())
# A holder that exited without a usable open time has no age; it reads medium so an unknown age never lands in long.
DEAD_UNKNOWN_STATE = "medium"

# Terminal styles: bold, dim and one accent, #e46212, always bold so it survives a terminal without colour.
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
ACCENT = "\x1b[1;38;2;228;98;18m"
RESET = "\x1b[0m"

# Live view: the alternate screen with the cursor hidden and line wrap off, so a long line is clipped instead of
# scrolling the frame. LEAVE_SCREEN undoes each in reverse.
ENTER_SCREEN = "\x1b[?1049h\x1b[?25l\x1b[?7l"
LEAVE_SCREEN = "\x1b[?7h\x1b[?25h\x1b[?1049l"
HOME = "\x1b[H"
CLEAR_LINE = "\x1b[K"
CLEAR_BELOW = "\x1b[J"
# Key hints: a hint whose toggle is on shows bold, the rest dim.
LIVE_KEYS = (("a", "all"), ("d", "details"), ("w", "waiting"), ("l", "log"), ("q", "quit"))
CHANGE_LOG_LINES = 2
# Header names per network-config board type; another type shows as its raw value.
TYPE_NAMES = {"248": "spinn48", "201": "spinn1"}

# Boards cabled into one machine, in IP order. The network config does not mark them, so they are listed here.
CABLED_GROUPS = (("192.168.1.21", "192.168.1.22", "192.168.1.23"),)
# Top, middle and bottom of the bracket drawn over a cabled group held by one process, indexed by plain.
BRACKETS = {False: ("┌", "├", "└"), True: ("/", "|", "\\")}
# The bracket on a waiter line between a group's top and bottom rows, indexed by plain.
BRACKET_LINE = {False: "│", True: "|"}

DEV_INODE_RE = re.compile(r"^([0-9a-fA-F]+:[0-9a-fA-F]+):(\d+)$")
PID_RE = re.compile(r"^-?\d+$")


@dataclass(frozen=True)
class Board:
    ip: str
    machine: str | None = None
    board_id: int | None = None
    board_type: str | None = None
    n_boards: int = 1
    configured: bool = True

    @property
    def lock_name(self) -> str:
        return f"{LOCK_PREFIX}{self.ip.replace('.', '_')}{LOCK_SUFFIX}"

    @property
    def label(self) -> str:
        if not self.machine:
            return "unconfigured"
        if self.n_boards > 1 and self.board_id is not None:
            return f"{self.machine}[{self.board_id}]"
        return self.machine

    @property
    def type_label(self) -> str | None:
        return BOARD_TYPES.get(str(self.board_type), self.board_type) if self.board_type else None


@dataclass(frozen=True)
class Holder:
    pid: str
    user: str
    command: str
    age_seconds: int | None
    script: str | None = None
    alive: bool = True
    # proc: a PID /proc/locks records; fd: the reader's own process with the lock file open; none: a held lock no recorded PID or readable descriptor accounts for.
    source: str = "proc"
    dead_pid: str | None = None
    # The lock file's mtime, kept for a holder that exited when it is later than host boot.
    opened_at: float | None = None

    @property
    def hidden(self) -> bool:
        """The lock is held, yet neither a recorded PID nor a descriptor this reader can read holds it."""
        return self.source == "none"


@dataclass(frozen=True)
class LockPids:
    holders: list[str] = field(default_factory=list)
    waiters: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Row:
    board: Board
    lock_path: Path
    holder: Holder | None
    lock_file_exists: bool
    waiters: tuple[Holder, ...] = ()
    lock_mtime: float | None = None

    @property
    def state(self) -> str:
        if not self.lock_file_exists:
            return "missing"
        if self.holder is None:
            return "free"
        if self.holder.alive:
            return age_state(self.holder.age_seconds)
        if self.holder.opened_at is None:
            return DEAD_UNKNOWN_STATE
        return age_state(int(time.time() - self.holder.opened_at))


def read_config_root(pointer: Path = CONFIG_POINTER) -> Path:
    try:
        value = pointer.read_text().strip()
    except OSError:
        return DEFAULT_CONFIG_ROOT
    return Path(value) if value else DEFAULT_CONFIG_ROOT


def parse_network_config(path: Path) -> list[tuple[str, dict[str, str]]]:
    machines: list[tuple[str, dict[str, str]]] = []
    name: str | None = None
    data: dict[str, str] = {}

    try:
        lines = path.read_text().splitlines()
    except OSError:
        return machines

    for raw_line in lines:
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if not line.startswith((" ", "\t")):
            if name is not None:
                machines.append((name, data))
            # A top-level "name:" opens a machine; a top-level "key: value" closes the open one and belongs to none.
            name = line[:-1].strip() if line.endswith(":") else None
            data = {}
            continue
        if name is None or ":" not in line:
            continue
        key, value = line.strip().split(":", 1)
        data[key.strip()] = value.strip().strip("'\"")

    if name is not None:
        machines.append((name, data))
    return machines


def configured_boards(config_root: Path) -> list[Board]:
    boards: list[Board] = []

    for machine, data in parse_network_config(config_root / NETWORK_CONFIG):
        start_ip = data.get("ETH_IP_START")
        if not start_ip:
            continue
        try:
            n_boards = int(data.get("n_boards", "1"))
            # Board addresses and lock names are IPv4 across the SpiNNaker2 stack, so any other start is skipped.
            first_ip = ipaddress.IPv4Address(start_ip)
        except ValueError:
            continue

        for board_id in range(n_boards):
            boards.append(
                Board(
                    ip=str(first_ip + board_id),
                    machine=machine,
                    board_id=board_id,
                    board_type=data.get("type"),
                    n_boards=n_boards,
                )
            )

    return boards


def inventory(config_root: Path, locks_dir: Path, include_unconfigured: bool) -> list[Board]:
    boards = {board.ip: board for board in configured_boards(config_root)}

    if include_unconfigured:
        pattern = str(locks_dir / f"{LOCK_PREFIX}*{LOCK_SUFFIX}")
        for lock_file in glob.glob(pattern):
            ip = ip_from_lock_name(Path(lock_file).name)
            # A dangling link is no lock file: nothing can hold it, so it adds no board.
            if ip and ip not in boards and os.path.exists(lock_file):
                boards[ip] = Board(ip=ip, configured=False)

    return sorted(boards.values(), key=lambda board: ip_key(board.ip))


def ip_from_lock_name(name: str) -> str | None:
    if not name.startswith(LOCK_PREFIX) or not name.endswith(LOCK_SUFFIX):
        return None
    parts = name[len(LOCK_PREFIX) : -len(LOCK_SUFFIX)].split("_")
    if len(parts) != 4 or not all(part.isdigit() for part in parts):
        return None
    ip = ".".join(parts)
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        return None
    return ip


def ip_key(ip: str) -> tuple[int, ...]:
    return tuple(int(part) for part in ip.split("."))


def ip_suffix(ip: str) -> str:
    return ".".join(ip.split(".")[2:])


def proc_locks(path: Path | None = None) -> dict[tuple[str, str], LockPids]:
    locks: dict[tuple[str, str], LockPids] = {}

    try:
        lines = (path or PROC / "locks").read_text().splitlines()
    except OSError:
        return locks

    for line in lines:
        fields = line.split()
        for index, token in enumerate(fields[:-1]):
            if not PID_RE.match(token):
                continue
            match = DEV_INODE_RE.match(fields[index + 1])
            if match and token != "-1":
                entry = locks.setdefault((match.group(1).lower(), match.group(2)), LockPids())
                # A "->" line is a process blocked on the lock; every other line holds it.
                (entry.waiters if "->" in fields else entry.holders).append(token)
            break

    return locks


def lock_key(path: Path) -> tuple[str, str] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    device = f"{os.major(stat.st_dev):02x}:{os.minor(stat.st_dev):02x}"
    return (device.lower(), str(stat.st_ino))


def lock_mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def boot_time() -> float | None:
    for line in (read_text(PROC / "stat") or "").splitlines():
        if line.startswith("btime "):
            try:
                return float(line.split()[1])
            except (IndexError, ValueError):
                return None
    return None


def rows(config_root: Path, locks_dir: Path, include_unconfigured: bool) -> list[Row]:
    locks = proc_locks()
    boot = boot_time()
    boards = inventory(config_root, locks_dir, include_unconfigured)
    entries: dict[str, LockPids | None] = {}
    for board in boards:
        key = lock_key(locks_dir / board.lock_name)
        entries[board.ip] = locks.get(key) if key else None
    # One pass over /proc serves every lock whose recorded holders have all exited.
    orphaned = [
        locks_dir / board.lock_name
        for board in boards
        if (entry := entries[board.ip]) and entry.holders and not any((PROC / pid).is_dir() for pid in entry.holders)
    ]
    fds = lock_fds(orphaned) if orphaned else {}

    def resolve(board: Board) -> Row:
        lock_path = locks_dir / board.lock_name
        entry = entries[board.ip]
        mtime = lock_mtime(lock_path)
        # Only a truncating open, such as a shell's `exec 9>FILE`, moves the mtime, so one from before boot says nothing.
        opened_at = mtime if mtime is not None and boot is not None and mtime > boot else None
        held = resolve_holder(entry, lock_path, opened_at, fds) if entry else None
        waiters = tuple(holder(pid) for pid in entry.waiters) if entry else ()
        return Row(board, lock_path, held, lock_path.exists(), waiters, mtime)

    return [resolve(board) for board in boards]


def resolve_holder(
    entry: LockPids, lock_path: Path, opened_at: float | None, fds: dict[str, list[str]] | None = None
) -> Holder | None:
    """The lock's holder: a recorded PID still running, else the reader's own process with the lock file open, else
    the first recorded PID marked exited. Another user's descriptors are unreadable, so only the reader's own
    processes can stand in for a PID that exited. `fds` is a lock_fds() result already read for this refresh."""
    if not entry.holders:
        return None
    for pid in entry.holders:
        if (PROC / pid).is_dir():
            return holder(pid)
    dead = entry.holders[0]
    owner = fd_holder(lock_path, exclude=set(entry.waiters), fds=fds)
    if owner is not None:
        return replace(holder(owner), source="fd", dead_pid=dead)
    return Holder(
        pid=dead,
        user="?",
        command="-",
        age_seconds=None,
        alive=False,
        source="none",
        dead_pid=dead,
        opened_at=opened_at,
    )


def fd_holder(lock_path: Path, exclude: set[str], fds: dict[str, list[str]] | None = None) -> str | None:
    """A process whose descriptor on the lock file holds the lock, as a shell's does after its flock helper exits."""
    key = real_path(lock_path)
    # A lock outside the refresh's pass had a live recorded holder at its scan, which has exited since; read it afresh.
    found = fds if fds is not None and key in fds else lock_fds([lock_path])
    return next((pid for pid in found.get(key, []) if pid not in exclude), None)


def real_path(path: Path) -> str:
    try:
        return str(path.resolve())
    except (OSError, RuntimeError):
        return str(path)


def lock_fds(lock_paths: list[Path]) -> dict[str, list[str]]:
    """The PIDs whose descriptor holds the lock on each file, keyed by real path, from one pass over /proc.

    Waiters and their shells also have the file open, but their fdinfo lists no granted lock."""
    found: dict[str, list[str]] = {real_path(path): [] for path in lock_paths}
    try:
        entries = sorted((entry for entry in PROC.iterdir() if entry.name.isdigit()), key=lambda e: int(e.name))
    except OSError:
        return found
    for entry in entries:
        try:
            for fd in (entry / "fd").iterdir():
                target = os.readlink(fd)
                if target in found and entry.name not in found[target] and holds_lock(entry / "fdinfo" / fd.name):
                    found[target].append(entry.name)
        except OSError:
            continue
    return found


def holds_lock(fdinfo: Path) -> bool:
    """Whether the open file behind a descriptor holds a lock: fdinfo lists each lock granted to it as a `lock:` line,
    so a shell that opened the file to wait for the lock does not count."""
    lines = (read_text(fdinfo) or "").splitlines()
    return any(line.startswith("lock:") and "->" not in line for line in lines)


def holder(pid: str) -> Holder:
    uid = process_uid(pid)
    user = user_from_uid(uid) if uid is not None else "unknown"
    command = read_text(PROC / pid / "comm") or "unknown"
    return Holder(pid=pid, user=user, command=command, age_seconds=process_age_seconds(pid), script=script_name(pid))


def script_name(pid: str) -> str | None:
    """What a process runs: the first argument ending .py or .sh, the module after `-m`, the first argument of a
    Python interpreter (a console script such as pytest), else argv[0]'s basename. None for an unreadable or empty
    command line."""
    try:
        raw = (PROC / pid / "cmdline").read_bytes()
    except OSError:
        return None
    argv = [part.decode(errors="replace") for part in raw.split(b"\0") if part]
    if not argv:
        return None
    program = os.path.basename(argv[0])
    if program.startswith("python"):
        # The interpreter's options come before the module, script or console script it runs; what follows is theirs.
        rest = iter(argv[1:])
        for arg in rest:
            if arg == "-m":
                return next(rest, program)
            if arg == "-c":
                return program
            if arg in ("-W", "-X"):
                next(rest, None)
            elif not arg.startswith("-"):
                return os.path.basename(arg)
        return program
    return next((os.path.basename(arg) for arg in argv if arg.endswith((".py", ".sh"))), program)


def display_command(holder: Holder) -> str:
    return holder.script or holder.command


def process_uid(pid: str) -> int | None:
    try:
        with (PROC / pid / "status").open() as handle:
            for line in handle:
                if line.startswith("Uid:"):
                    return int(line.split()[1])
    except (OSError, IndexError, ValueError):
        return None
    return None


def user_from_uid(uid: int) -> str:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return f"uid={uid}"


def read_text(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def process_age_seconds(pid: str) -> int | None:
    stat = read_text(PROC / pid / "stat")
    uptime = read_text(PROC / "uptime")
    if not stat or not uptime:
        return None
    try:
        tail = stat.rsplit(")", 1)[1].split()
        started_at = int(tail[19]) / os.sysconf("SC_CLK_TCK")
        return max(0, int(float(uptime.split()[0]) - started_at))
    except (IndexError, OSError, TypeError, ValueError):
        return None


def age_state(seconds: int | None) -> str:
    if seconds is None or seconds > 300:
        return "long"
    if seconds >= 60:
        return "medium"
    return "short"


def duration(seconds: int | None) -> str:
    if seconds is None:
        return "unknown"
    minutes, seconds = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}d {hours:02d}h {minutes:02d}m"
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


def opened_clock(holder: Holder) -> str | None:
    return None if holder.opened_at is None else time.strftime("%H:%M", time.localtime(holder.opened_at))


def age_cell(holder: Holder) -> str:
    if holder.alive:
        return duration(holder.age_seconds)
    clock = opened_clock(holder)
    # The lock file's last truncating open, a hint at when an exited holder took the lock.
    return "-" if clock is None else f"@{clock}?"


def holder_columns(holder: Holder, show_pid: bool) -> str:
    # Widths fit an age under ten days or "@HH:MM?", 15 characters of the script or kernel comm name and a PID up to
    # pid_max. A longer script name is cut to 15, as the kernel cuts comm.
    pid = f" PID={holder.pid:<7}" if show_pid else ""
    user = "hidden" if holder.hidden else holder.user
    return f"{user:<12} {age_cell(holder):<10} {display_command(holder)[:15]:<15}{pid}"


def paint(text: str, style: str, color: bool) -> str:
    return f"{style}{text}{RESET}" if color and text else text


def use_color(plain: bool, stream=None, mode: str = "auto") -> bool:
    """Whether to style: never under --plain; else --color always or never; else a non-empty NO_COLOR turns styles off,
    a non-empty FORCE_COLOR turns them on, and otherwise only a terminal gets them."""
    if plain or mode == "never":
        return False
    if mode == "always":
        return True
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return (stream or sys.stdout).isatty()


def compact(row: Row, full_ip: bool, show_pid: bool, plain: bool, mark: str = " ", color: bool = False) -> str:
    label = row.board.ip if full_ip else ip_suffix(row.board.ip)
    label_width = 15 if full_ip else 5
    symbol_width = STATE_WIDTH if plain else SYMBOL_WIDTH
    symbol = f"{status_symbol(row.state, plain):<{symbol_width}}"
    if row.holder is not None and (not row.holder.alive or row.state == "long"):
        symbol = paint(symbol, ACCENT, color)
    head = f"{label:<{label_width}}{mark}{symbol}"

    if row.state == "missing":
        return f"{head} {row.board.label} (no {row.board.lock_name})"
    if row.holder is None:
        return f"{head} {row.board.label}"
    line = f"{head} {holder_columns(row.holder, show_pid)} {paint(f'({row.board.label})', DIM, color)}"
    if not row.holder.alive:
        line += "  " + paint(f"pid {row.holder.dead_pid} exited, lock held", ACCENT, color)
    if row.waiters:
        line += "  " + paint(f"+{len(row.waiters)} waiting", DIM, color)
    return line


def waiter_line(
    waiter: Holder, full_ip: bool, show_pid: bool, plain: bool, mark: str = " ", color: bool = False
) -> str:
    """One process waiting for a board, under its holder row: `↳` in the dot column (`waiting` in the state column
    under --plain), the waiter's user, age and script in the holder columns, all dim; `mark` carries a bracket on."""
    tag = "waiting" if plain else "↳"
    cells = f"{tag:<{STATE_WIDTH if plain else SYMBOL_WIDTH}} {holder_columns(waiter, show_pid)}".rstrip()
    return f"{'':<{15 if full_ip else 5}}{mark}{paint(cells, DIM, color)}"


def group_marks(visible: list[Row], plain: bool, groups: tuple[tuple[str, ...], ...] = CABLED_GROUPS) -> list[str]:
    """Separator between label and state per row: a bracket over a group one PID holds, else a space."""
    marks = [" "] * len(visible)
    position = {row.board.ip: index for index, row in enumerate(visible)}
    top, middle, bottom = BRACKETS[plain]
    for group in groups:
        indices = [position[ip] for ip in group if ip in position]
        # Every member present and adjacent, so the bracket never spans a row outside the group.
        if len(indices) != len(group) or indices != list(range(indices[0], indices[0] + len(group))):
            continue
        # Compare PIDs only: a holder whose process has gone reads as user "hidden" and still counts.
        pids = {visible[index].holder.pid if visible[index].holder else None for index in indices}
        if len(pids) != 1 or None in pids:
            continue
        marks[indices[0]] = top
        for index in indices[1:-1]:
            marks[index] = middle
        marks[indices[-1]] = bottom
    return marks


def compact_lines(
    visible: list[Row],
    full_ip: bool,
    show_pid: bool,
    plain: bool,
    groups: tuple[tuple[str, ...], ...] = CABLED_GROUPS,
    color: bool = False,
    waiters: int = 0,
) -> list[str]:
    """One line per row, then up to `waiters` dim waiter lines in all, under their holder rows, the top board's first."""
    marks = group_marks(visible, plain, groups)
    top, middle, _ = BRACKETS[plain]
    lines: list[str] = []
    for row, mark in zip(visible, marks):
        lines.append(compact(row, full_ip, show_pid, plain, mark, color))
        shown = row.waiters[: max(waiters, 0)]
        waiters -= len(shown)
        # Under a bracket's top or middle row the bracket runs on through the waiter lines to the next board.
        carry = BRACKET_LINE[plain] if mark in (top, middle) else " "
        lines += [waiter_line(waiter, full_ip, show_pid, plain, carry, color) for waiter in shown]
    return lines


def free_summary(visible: list[Row], full_ip: bool) -> str | None:
    """One line naming the free boards, consecutive addresses joined into a range."""
    runs: list[list[str]] = []
    for row in visible:
        if row.state != "free":
            continue
        ip = row.board.ip
        if runs and ip_key(ip)[:-1] == ip_key(runs[-1][-1])[:-1] and ip_key(ip)[-1] == ip_key(runs[-1][-1])[-1] + 1:
            runs[-1].append(ip)
        else:
            runs.append([ip])
    if not runs:
        return None

    def label(ip: str) -> str:
        return ip if full_ip else ip_suffix(ip)

    return "free  " + " ".join(label(run[0]) if len(run) == 1 else f"{label(run[0])}-{label(run[-1])}" for run in runs)


def detailed(row: Row) -> str:
    prefix = f"{row.board.ip:<15} ({row.board.lock_name}):"
    if row.state == "missing":
        return f"{prefix} missing lock file"
    if row.holder is None:
        return f"{prefix} free (no lock held)"
    held = row.holder
    if held.alive:
        line = f"{prefix} locked by {held.user} (PID={held.pid}, CMD={display_command(held)}) for {duration(held.age_seconds)}"
        if held.dead_pid:
            line += f", recorded PID={held.dead_pid} exited"
    else:
        clock = opened_clock(held)
        opened = "open time unknown" if clock is None else f"lock file opened {clock}"
        held_by = "lock held by no fd this reader can read"
        line = f"{prefix} locked, holder PID={held.dead_pid} exited, {held_by}, {opened}"
    for waiter in row.waiters:
        line += (
            f"\n    waiting: {waiter.user} (PID={waiter.pid}, CMD={display_command(waiter)})"
            f" for {duration(waiter.age_seconds)}"
        )
    return line


def iso(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp).astimezone().isoformat(timespec="seconds")


def holder_json(holder: Holder | None) -> dict[str, object] | None:
    if holder is None:
        return None
    return {
        "pid": holder.pid,
        "user": holder.user,
        "command": holder.command,
        "script": holder.script,
        "age_seconds": holder.age_seconds,
        "age": duration(holder.age_seconds),
        "alive": holder.alive,
        "source": holder.source,
        "dead_pid": holder.dead_pid,
        "hidden": holder.hidden,
    }


def as_json(row: Row) -> dict[str, object]:
    return {
        "ip": row.board.ip,
        "ip_suffix": ip_suffix(row.board.ip),
        "machine": row.board.machine,
        "board_id": row.board.board_id,
        "board_type": row.board.type_label,
        "configured": row.board.configured,
        "lock_name": row.board.lock_name,
        "lock_path": str(row.lock_path),
        "lock_file_exists": row.lock_file_exists,
        "lock_mtime": iso(row.lock_mtime),
        "state": row.state,
        "holder": holder_json(row.holder),
        "waiters": [holder_json(waiter) for waiter in row.waiters],
    }


def status_symbol(state: str, plain: bool) -> str:
    return state if plain else SYMBOLS[state]


def is_visible(row: Row, show_all: bool) -> bool:
    if show_all:
        return True
    # Default view: only unavailable (locked) boards; free/missing rows appear under --all.
    return row.state not in ("free", "missing")


def sources(args: argparse.Namespace) -> tuple[Path, Path]:
    config_root = args.config_root or read_config_root()
    return config_root, args.locks_dir or config_root / LOCKS_DIR


def frame_lines(
    args: argparse.Namespace, data: list[Row], color: bool, where: str, title: bool = True, waiters: int = 0
) -> list[str]:
    """The text a static run prints, line by line; the live view takes it without the title and, under its `w` key,
    with up to `waiters` waiter lines."""
    visible = [row for row in data if is_visible(row, args.all)]
    lines: list[str] = []
    if title and not args.no_header:
        lines += [paint("Board Status" if args.all else "Unavailable Boards", BOLD, color), ""]

    if not data:
        return lines + [f"No boards found in {where}"]
    if not visible:
        return lines + ["No unavailable boards."]

    if args.details:
        return lines + [line for row in visible for line in detailed(row).split("\n")]
    # Under --all the free boards fold into one summary line; --plain keeps one row per board for scripts.
    collapse = args.all and not args.plain
    listed = [row for row in visible if not (collapse and row.state == "free")]
    lines += compact_lines(
        listed, full_ip=args.full_ip, show_pid=args.pid, plain=args.plain, color=color, waiters=waiters
    )
    summary = free_summary(visible, args.full_ip) if collapse else None
    if summary:
        if listed:
            lines.append("")
        lines.append(paint(summary, DIM, color))
    return lines


def print_rows(args: argparse.Namespace) -> None:
    config_root, locks_dir = sources(args)
    data = rows(config_root, locks_dir, include_unconfigured=not args.config_only)

    if args.json:
        visible = [row for row in data if is_visible(row, args.all)]
        print(
            json.dumps(
                [as_json(row) for row in visible],
                indent=2 if args.pretty else None,
                separators=None if args.pretty else (",", ":"),
            )
        )
        return

    color = use_color(args.plain, mode=args.color)
    print("\n".join(frame_lines(args, data, color, f"{config_root / NETWORK_CONFIG} or {locks_dir}")))


def type_counts(data: list[Row]) -> list[tuple[str, int, int]]:
    """Free and total boards per board type, in board order. A board whose holder exited counts as taken, a board
    without a lock file as not free, and a board without a type is left out."""
    counts: dict[str, list[int]] = {}
    for row in data:
        if not row.board.board_type:
            continue
        tally = counts.setdefault(TYPE_NAMES.get(str(row.board.board_type), str(row.board.board_type)), [0, 0])
        tally[0] += row.state == "free"
        tally[1] += 1
    return [(name, free, total) for name, (free, total) in counts.items()]


def live_header(data: list[Row], now: float, color: bool) -> str:
    cells = [paint("boardhog", BOLD, color), paint(time.strftime("%H:%M:%S", time.localtime(now)), DIM, color)]
    cells += [f"{name} {free}/{total} free" for name, free, total in type_counts(data)]
    return "  ".join(cells)


def live_footer(
    interval: float, color: bool, show_all: bool = False, details: bool = False, waiting: bool = False, log: bool = True
) -> str:
    cells = [paint(f"every {interval:g}s", DIM, color)]
    active = {"a": show_all, "d": details, "w": waiting, "l": log, "q": False}
    cells += [paint(f"{key} {word}", BOLD if active[key] else DIM, color) for key, word in LIVE_KEYS]
    return "  ".join(cells)


def holder_key(held: Holder | None) -> str | None:
    """The PID /proc/locks records for a holder, so finding the reader's own shell behind an exited PID is no change."""
    return None if held is None else held.dead_pid or held.pid


def change_note(label: str, before: Holder | None, after: Holder | None, held_for: int | None) -> str | None:
    """One change-log entry for a board between two refreshes, or None when its holder did not change."""
    if holder_key(before) == holder_key(after):
        if before is not None and after is not None and before.alive and not after.alive:
            # A recorded PID that exited, or the reader's own process whose descriptor no longer holds the lock.
            gone = "exited" if before.source == "proc" else "let go"
            return f"{label} holder pid {before.pid} {gone}, lock held ({before.user} {display_command(before)})"
        return None
    if after is None:
        return f"{label} freed ({who_holds(before)}, held {duration(held_for)})"
    if before is not None:
        # A handover between two refreshes: one line naming both holders and how long the first held the board.
        return f"{label} {who_holds(before)} -> {who_holds(after)} (held {duration(held_for)})"
    if after.alive:
        return f"{label} taken by {after.user} {display_command(after)}"
    return f"{label} taken, holder pid {after.dead_pid} exited, lock held"


def who_holds(held: Holder) -> str:
    return f"{held.user} {display_command(held)}" if held.alive else f"hidden pid {held.dead_pid}"


class LiveView:
    """What the live view keeps between refreshes: the flags its keys toggle, the holders it last saw, when each hold
    began, and the last changes. It lives in memory only while the view runs."""

    def __init__(self, args: argparse.Namespace, where: str) -> None:
        self.args = argparse.Namespace(**vars(args))
        self.where = where
        self.seen: dict[str, Holder | None] | None = None
        self.taken_at: dict[str, float] = {}
        self.changes: deque[str] = deque(maxlen=CHANGE_LOG_LINES)
        # The `w` key: waiter lines under the holder rows. The live view alone has it; --details lists waiters too.
        self.waiting = False
        self.log = True

    def key(self, char: str) -> bool:
        """Apply one key press; True when it quits."""
        if char == "q":
            return True
        if char == "a":
            self.args.all = not self.args.all
        elif char == "d":
            self.args.details = not self.args.details
        elif char == "w":
            self.waiting = not self.waiting
        elif char == "l":
            self.log = not self.log
        return False

    def update(self, data: list[Row], now: float) -> None:
        current = {row.board.ip: row.holder for row in data}
        if self.seen is None:
            # A hold already running when the view starts dates from its process's start, as the age column does.
            for ip, held in current.items():
                if held is not None:
                    self.taken_at[ip] = now - held.age_seconds if held.alive and held.age_seconds is not None else now
            self.seen = current
            return
        clock = time.strftime("%H:%M:%S", time.localtime(now))
        for ip, after in current.items():
            before = self.seen.get(ip)
            held_for = int(now - self.taken_at.get(ip, now))
            note = change_note(ip if self.args.full_ip else ip_suffix(ip), before, after, held_for)
            if note:
                self.changes.append(f"{clock}  {note}")
            if after is None:
                self.taken_at.pop(ip, None)
            elif holder_key(before) != holder_key(after):
                self.taken_at[ip] = now
        self.seen = current

    def screen(self, data: list[Row], now: float, color: bool, height: int | None = None) -> list[str]:
        """Header, rows, change log and footer, each after a blank line. Given a height, the frame fits it: the change
        log is cut first, oldest entry first, then the waiter lines from the last board up, then the rows from the
        bottom; the header and footer stay."""
        waiters = sum(len(row.waiters) for row in data) if self.waiting else 0
        body = frame_lines(self.args, data, color, self.where, title=False, waiters=waiters)
        log = [paint(change, DIM, color) for change in self.changes] if self.log else []
        if height is not None:
            # Four lines go to the header, the footer and the blank line under and over each.
            room = height - 4
            shown = len(body) - len(frame_lines(self.args, data, color, self.where, title=False)) if waiters else 0
            if len(body) > room and shown:
                body = frame_lines(
                    self.args, data, color, self.where, title=False, waiters=max(shown - (len(body) - room), 0)
                )
            if len(body) > room:
                body = body[: max(room, 0)]
                while body and not body[-1]:
                    body.pop()
            # The log needs a blank line above it as well.
            keep = min(max(room - len(body) - 1, 0), len(log))
            log = log[len(log) - keep :]
        lines = [live_header(data, now, color), "", *body]
        if log:
            lines += ["", *log]
        lines += ["", live_footer(self.args.watch, color, self.args.all, self.args.details, self.waiting, self.log)]
        return lines if height is None else lines[-max(height, 1) :]


def terminal_lines(stream) -> int:
    """The terminal's height, or 24 when it has none to report: not a terminal, or a pty never given a size (0)."""
    try:
        return os.get_terminal_size(stream.fileno()).lines or 24
    except (AttributeError, OSError, ValueError):
        return 24


def draw(stream, lines: list[str], height: int) -> None:
    """Overwrite the screen in place: home, each line cleared to its end, then everything below cleared, so nothing
    flickers. Lines past the terminal's height are cut; LiveView.screen already fits the frame to it."""
    shown = lines[: max(height, 1)]
    stream.write(HOME + (CLEAR_LINE + "\n").join(shown) + CLEAR_LINE + CLEAR_BELOW)
    stream.flush()


def terminate(signum: int, frame: object) -> None:
    """SIGTERM ends the live view through its finally block, which restores the terminal."""
    raise SystemExit(128 + signum)


def run_live(args: argparse.Namespace, stdin=None, stdout=None) -> int:
    """Redraw every args.watch seconds until q (exit 0), Ctrl+C (exit 130) or SIGTERM (exit 143), then restore the
    terminal. One select() on stdin is both the wait and the key reader; without a terminal on stdin it only waits."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    config_root, locks_dir = sources(args)
    view = LiveView(args, f"{config_root / NETWORK_CONFIG} or {locks_dir}")
    color = use_color(args.plain, stdout, args.color)
    fd = stdin.fileno() if stdin.isatty() else None
    saved = termios.tcgetattr(fd) if fd is not None else None
    previous = signal.signal(signal.SIGTERM, terminate)
    data: list[Row] | None = None
    due = 0.0
    try:
        if fd is not None:
            tty.setcbreak(fd)
        stdout.write(ENTER_SCREEN)
        while True:
            if data is None or time.monotonic() >= due:
                data = rows(config_root, locks_dir, include_unconfigured=not args.config_only)
                view.update(data, time.time())
                due = time.monotonic() + args.watch
            height = terminal_lines(stdout)
            draw(stdout, view.screen(data, time.time(), color, height), height)
            wait = max(due - time.monotonic(), 0.0)
            if fd is None:
                time.sleep(wait)
                continue
            ready, _, _ = select.select([fd], [], [], wait)
            if ready:
                char = os.read(fd, 1).decode(errors="replace")
                if not char or view.key(char):
                    return 0
    except KeyboardInterrupt:
        return 130
    finally:
        stdout.write(LEAVE_SCREEN)
        stdout.flush()
        if saved is not None:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        signal.signal(signal.SIGTERM, previous)


def positive_seconds(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number of seconds: {text!r}") from None
    if not (math.isfinite(value) and value > 0):
        raise argparse.ArgumentTypeError(f"must be a positive number of seconds: {text!r}")
    return value


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description="Monitor py-spinnaker2 board locks")
    cli.add_argument("--config-root", type=Path, help="SpiNNaker config root")
    cli.add_argument("--locks-dir", type=Path, help="lock directory")
    cli.add_argument("--all", action="store_true", help="show free and missing boards too")
    cli.add_argument("--config-only", action="store_true", help="hide unconfigured lock files")
    cli.add_argument("--details", action="store_true", help="show lock file, PID, command, and age")
    cli.add_argument("--full-ip", action="store_true", help="show full IPs")
    cli.add_argument("--pid", action="store_true", help="show PIDs in compact output")
    cli.add_argument("--plain", action="store_true", help="print states as words: free, short, medium, long, missing")
    cli.add_argument("--json", action="store_true", help="emit JSON")
    cli.add_argument("--pretty", action="store_true", help="pretty-print JSON")
    cli.add_argument("--no-header", action="store_true", help="omit compact/detail header")
    cli.add_argument(
        "-n",
        "--watch",
        type=positive_seconds,
        metavar="SECS",
        help="on a terminal, redraw every SECS seconds; keys a (--all), d (--details), w (waiters), l (log), q (quit)",
    )
    cli.add_argument(
        "--color",
        choices=("auto", "always", "never"),
        default="auto",
        help="styling: auto (a terminal, unless NO_COLOR; FORCE_COLOR forces it), always or never",
    )
    return cli


def main() -> None:
    args = parser().parse_args()
    try:
        # Without a terminal there is nothing to redraw, so --watch prints one static frame.
        if args.watch is not None and not args.json and sys.stdout.isatty():
            sys.exit(run_live(args))
        print_rows(args)
    except KeyboardInterrupt:
        print("\nStopped.")
    except BrokenPipeError:
        sys.stderr.close()


if __name__ == "__main__":
    main()
