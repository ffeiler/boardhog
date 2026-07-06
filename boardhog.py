#!/usr/bin/env python3
"""Monitor py-spinnaker2 board locks."""

from __future__ import annotations

import argparse
import glob
import ipaddress
import json
import os
import pwd
import re
import sys
from dataclasses import dataclass
from pathlib import Path


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
    "free": "🟢",
    "short": "🟡",
    "medium": "🟠",
    "long": "🔴",
    "missing": "?",
}

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


@dataclass(frozen=True)
class Row:
    board: Board
    lock_path: Path
    holder: Holder | None
    lock_file_exists: bool

    @property
    def state(self) -> str:
        if not self.lock_file_exists:
            return "missing"
        if self.holder is None:
            return "free"
        return age_state(self.holder.age_seconds)


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
        if not line.startswith((" ", "\t")) and line.endswith(":"):
            if name is not None:
                machines.append((name, data))
            name = line[:-1].strip()
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
            first_ip = ipaddress.ip_address(start_ip)
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
            if ip and ip not in boards:
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
    parts = ip.split(".")
    return f"{parts[2]}.{parts[3]}" if len(parts) == 4 else ip


def proc_locks(path: Path = Path("/proc/locks")) -> dict[tuple[str, str], str]:
    locks: dict[tuple[str, str], str] = {}

    try:
        lines = path.read_text().splitlines()
    except OSError:
        return locks

    for line in lines:
        fields = line.split()
        for index, field in enumerate(fields[:-1]):
            if not PID_RE.match(field):
                continue
            match = DEV_INODE_RE.match(fields[index + 1])
            if match and field != "-1":
                locks.setdefault((match.group(1).lower(), match.group(2)), field)
            break

    return locks


def lock_key(path: Path) -> tuple[str, str] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    device = f"{os.major(stat.st_dev):02x}:{os.minor(stat.st_dev):02x}"
    return (device.lower(), str(stat.st_ino))


def rows(config_root: Path, locks_dir: Path, include_unconfigured: bool) -> list[Row]:
    locks = proc_locks()
    result: list[Row] = []

    for board in inventory(config_root, locks_dir, include_unconfigured):
        lock_path = locks_dir / board.lock_name
        key = lock_key(lock_path)
        pid = locks.get(key) if key else None
        result.append(Row(board, lock_path, holder(pid) if pid else None, lock_path.exists()))

    return result


def holder(pid: str) -> Holder:
    uid = process_uid(pid)
    user = user_from_uid(uid) if uid is not None else f"PID={pid}"
    command = read_text(Path("/proc") / pid / "comm") or "unknown"
    return Holder(pid=pid, user=user, command=command, age_seconds=process_age_seconds(pid))


def process_uid(pid: str) -> int | None:
    try:
        with (Path("/proc") / pid / "status").open() as handle:
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
    stat = read_text(Path("/proc") / pid / "stat")
    uptime = read_text(Path("/proc/uptime"))
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


def compact(row: Row, full_ip: bool, show_pid: bool, plain: bool) -> str:
    label = row.board.ip if full_ip else ip_suffix(row.board.ip)
    label_width = 15 if full_ip else 5
    symbol = status_symbol(row.state, plain)

    if row.state == "missing":
        return f"{label:<{label_width}} {symbol} missing {row.board.lock_name}"
    if row.holder is None:
        return f"{label:<{label_width}} {symbol} {row.board.label}"

    pid = f" PID={row.holder.pid}" if show_pid else ""
    age = duration(row.holder.age_seconds)
    user = f"{row.holder.user:<12}"
    age_col = f"{age:<8}"
    command = f"{row.holder.command:<10}"
    return f"{label:<{label_width}} {symbol} {user} {age_col} {command}{pid} ({row.board.label})"


def detailed(row: Row) -> str:
    prefix = f"{row.board.ip:<15} ({row.board.lock_name}):"
    if row.state == "missing":
        return f"{prefix} missing lock file"
    if row.holder is None:
        return f"{prefix} free (no lock held)"
    age = duration(row.holder.age_seconds)
    return f"{prefix} locked by {row.holder.user} (PID={row.holder.pid}, CMD={row.holder.command}) for {age}"


def as_json(row: Row) -> dict[str, object]:
    holder_data = None
    if row.holder:
        holder_data = {
            "pid": row.holder.pid,
            "user": row.holder.user,
            "command": row.holder.command,
            "age_seconds": row.holder.age_seconds,
            "age": duration(row.holder.age_seconds),
        }

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
        "state": row.state,
        "holder": holder_data,
    }


def status_symbol(state: str, plain: bool) -> str:
    return state if plain else SYMBOLS[state]


def print_rows(args: argparse.Namespace) -> None:
    config_root = args.config_root or read_config_root()
    locks_dir = args.locks_dir or config_root / LOCKS_DIR
    data = rows(config_root, locks_dir, include_unconfigured=not args.config_only)
    visible = data if args.all else [row for row in data if row.holder is not None]

    if args.json:
        print(
            json.dumps(
                [as_json(row) for row in visible],
                indent=2 if args.pretty else None,
                separators=None if args.pretty else (",", ":"),
            )
        )
        return

    if not args.no_header:
        print(("Board Status" if args.all else "Locked Boards") + "\n")

    if not data:
        print(f"No boards found in {config_root / NETWORK_CONFIG} or {locks_dir}")
        return
    if not visible:
        print("No locked boards.")
        return

    for row in visible:
        if args.details:
            print(detailed(row))
        else:
            print(compact(row, full_ip=args.full_ip, show_pid=args.pid, plain=args.plain))


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description="Monitor py-spinnaker2 board locks")
    cli.add_argument("--config-root", type=Path, help="SpiNNaker config root")
    cli.add_argument("--locks-dir", type=Path, help="lock directory")
    cli.add_argument("--all", action="store_true", help="show free and missing boards too")
    cli.add_argument("--config-only", action="store_true", help="hide unconfigured lock files")
    cli.add_argument("--details", action="store_true", help="show lock file, PID, command, and age")
    cli.add_argument("--full-ip", action="store_true", help="show full IPs")
    cli.add_argument("--pid", action="store_true", help="show PIDs in compact output")
    cli.add_argument("--plain", "--no-emoji", action="store_true", help="use text states instead of emoji")
    cli.add_argument("--json", action="store_true", help="emit JSON")
    cli.add_argument("--pretty", action="store_true", help="pretty-print JSON")
    cli.add_argument("--no-header", action="store_true", help="omit compact/detail header")
    return cli


def main() -> None:
    try:
        print_rows(parser().parse_args())
    except KeyboardInterrupt:
        print("\nStopped.")
    except BrokenPipeError:
        sys.stderr.close()


if __name__ == "__main__":
    main()
