"""Holder resolution, waiters, styling and the free summary, read from a fake /proc tree.

Each test that touches /proc writes /proc/locks and /proc/<pid> files under tmp_path and points bh.PROC at them.
IPs are RFC 5737 documentation addresses (192.0.2.0/24, 198.51.100.0/24), not real boards.
"""

import os
import pwd
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import boardhog as bh

CONFIG = """\
frame_1:
  type: 248
  n_boards: 3
  ETH_IP_START: 192.0.2.21
"""
ME = pwd.getpwuid(os.getuid()).pw_name


@pytest.fixture
def world(tmp_path, monkeypatch):
    proc = tmp_path / "proc"
    proc.mkdir()
    locks = tmp_path / "locks"
    locks.mkdir()
    (tmp_path / bh.NETWORK_CONFIG).write_text(CONFIG)
    for last in (21, 22, 23):
        (locks / f"BOARD_192_0_2_{last}.lock").touch()
    monkeypatch.setattr(bh, "PROC", proc)
    return SimpleNamespace(root=tmp_path, proc=proc, locks=locks)


def lock_file(world, last):
    return world.locks / f"BOARD_192_0_2_{last}.lock"


def dev_inode(path):
    stat = path.stat()
    return f"{os.major(stat.st_dev):02x}:{os.minor(stat.st_dev):02x}:{stat.st_ino}"


def write_locks(world, *entries):
    """One /proc/locks line per (board's last octet, pid, waiting) entry."""
    lines = []
    for number, (last, pid, waiting) in enumerate(entries, 1):
        arrow = "-> " if waiting else ""
        lines.append(f"{number}: {arrow}FLOCK  ADVISORY  WRITE {pid} {dev_inode(lock_file(world, last))} 0 EOF")
    (world.proc / "locks").write_text("\n".join(lines) + "\n")


def write_boot(world, btime):
    (world.proc / "stat").write_text(f"cpu  1 2 3 4\nbtime {int(btime)}\nprocesses 9\n")


def add_process(world, pid, argv, comm="python", fds=(), locked=False):
    root = world.proc / str(pid)
    (root / "fd").mkdir(parents=True)
    (root / "fdinfo").mkdir()
    (root / "status").write_text(f"Name:\t{comm}\nUid:\t{os.getuid()}\t{os.getuid()}\t0\t0\n")
    (root / "comm").write_text(comm + "\n")
    (root / "cmdline").write_bytes(b"".join(arg.encode() + b"\0" for arg in argv))
    for number, target in enumerate(fds, 3):
        (root / "fd" / str(number)).symlink_to(target.resolve())
        lock = f"lock:\t1: FLOCK  ADVISORY  WRITE 1 {dev_inode(target)} 0 EOF\n" if locked else ""
        (root / "fdinfo" / str(number)).write_text(f"pos:\t0\nflags:\t0100001\n{lock}")


def board_rows(world):
    return {row.board.ip: row for row in bh.rows(world.root, world.locks, include_unconfigured=True)}


def _row(holder=None, waiters=()):
    board = bh.Board(ip="192.0.2.21", machine="frame_1", board_id=0, n_boards=3)
    return bh.Row(board, Path("x"), holder, True, waiters)


def _live(age=3600, script="hw_full.py"):
    return bh.Holder(pid="500", user="someone", command="python", age_seconds=age, script=script)


def _dead(opened_at=None):
    return bh.Holder(
        pid="777",
        user="?",
        command="-",
        age_seconds=None,
        alive=False,
        source="none",
        dead_pid="777",
        opened_at=opened_at,
    )


def test_proc_locks_splits_holders_and_waiters(tmp_path):
    locks = tmp_path / "locks"
    locks.write_text(
        "1: FLOCK  ADVISORY  WRITE 100 103:02:5 0 EOF\n"
        "1: -> FLOCK  ADVISORY  WRITE 101 103:02:5 0 EOF\n"
        "1: -> FLOCK  ADVISORY  WRITE 102 103:02:5 0 EOF\n"
        "2: POSIX  ADVISORY  READ 200 00:5A:7 0 EOF\n"
        "3: OFDLCK ADVISORY  WRITE -1 103:02:9 0 EOF\n"
    )
    found = bh.proc_locks(locks)
    assert found == {
        ("103:02", "5"): bh.LockPids(holders=["100"], waiters=["101", "102"]),
        ("00:5a", "7"): bh.LockPids(holders=["200"], waiters=[]),
    }


def test_a_running_holder_names_user_and_script(world):
    add_process(world, 500, ["/usr/bin/python3", "-u", "hw_full.py", "--ip", "192.0.2.21"])
    write_locks(world, (21, 500, False))
    held = board_rows(world)["192.0.2.21"].holder
    assert (held.pid, held.user, held.command, held.script) == ("500", ME, "python", "hw_full.py")
    assert (held.alive, held.source, held.dead_pid) == (True, "proc", None)


def test_every_recorded_pid_is_tried(world):
    add_process(world, 500, ["python", "run.py"])
    write_locks(world, (21, 600, False), (21, 500, False))
    held = board_rows(world)["192.0.2.21"].holder
    assert (held.pid, held.source, held.alive) == ("500", "proc", True)


def test_an_exited_holder_shows_its_open_time(world):
    opened = time.time() - 30
    os.utime(lock_file(world, 21), (opened, opened))
    write_boot(world, opened - 86400)
    write_locks(world, (21, 777, False))
    row = board_rows(world)["192.0.2.21"]
    assert row.holder == _dead(opened_at=opened)
    assert row.state == "short"
    line = bh.compact(row, full_ip=False, show_pid=False, plain=True)
    clock = time.strftime("%H:%M", time.localtime(opened))
    assert f" hidden{' ' * 7}@{clock}? " in line
    assert bh.detailed(row).endswith(f"exited, lock held by no fd this reader can read, lock file opened {clock}")
    assert line.split()[1] == "short"
    assert line.endswith("(frame_1[0])  pid 777 exited, lock held")


def test_an_exited_holder_never_reads_long_for_an_unknown_age(world):
    opened = time.time() - 3600
    os.utime(lock_file(world, 21), (opened, opened))
    write_boot(world, opened + 60)
    write_locks(world, (21, 777, False))
    row = board_rows(world)["192.0.2.21"]
    assert row.holder == _dead(opened_at=None)
    assert row.state == bh.DEAD_UNKNOWN_STATE != "long"
    assert bh.compact(row, full_ip=False, show_pid=False, plain=True).split()[2:5] == ["hidden", "-", "-"]
    assert "open time unknown" in bh.detailed(row)


def test_own_open_fd_stands_in_for_an_exited_pid(world):
    add_process(world, 4321, ["bash"], comm="bash", fds=[lock_file(world, 21)], locked=True)
    write_locks(world, (21, 2373047, False))
    row = board_rows(world)["192.0.2.21"]
    held = row.holder
    assert (held.pid, held.user, held.script, held.source, held.dead_pid) == ("4321", ME, "bash", "fd", "2373047")
    assert held.alive
    assert bh.detailed(row).endswith(", recorded PID=2373047 exited")


# Both descriptor orders, so the closed one is listed before the lock's in one of them whatever order /proc lists.
@pytest.mark.parametrize("closed_first", [False, True])
def test_a_descriptor_closed_mid_scan_skips_only_itself(world, monkeypatch, closed_first):
    other = world.root / "other"
    other.touch()
    fds = [other, lock_file(world, 21)] if closed_first else [lock_file(world, 21), other]
    add_process(world, 4321, ["bash"], comm="bash", fds=fds, locked=True)
    write_locks(world, (21, 2373047, False))
    real = os.readlink

    def closing(path):
        target = real(path)
        if target == str(other.resolve()):
            raise FileNotFoundError(path)
        return target

    monkeypatch.setattr(bh.os, "readlink", closing)
    held = board_rows(world)["192.0.2.21"].holder
    assert (held.pid, held.source) == ("4321", "fd")


def test_a_waiter_with_the_file_open_is_no_holder(world):
    add_process(world, 4321, ["flock", "-w", "900", "9"], comm="flock", fds=[lock_file(world, 21)])
    write_locks(world, (21, 2373047, False), (21, 4321, True))
    row = board_rows(world)["192.0.2.21"]
    assert (row.holder.alive, row.holder.source, row.holder.dead_pid) == (False, "none", "2373047")
    assert [waiter.pid for waiter in row.waiters] == ["4321"]
    assert bh.compact(row, full_ip=False, show_pid=False, plain=True).endswith(
        "pid 2373047 exited, lock held  +1 waiting"
    )
    assert bh.detailed(row).splitlines()[1] == f"    waiting: {ME} (PID=4321, CMD=flock) for unknown"


def test_a_waiting_shell_with_the_file_open_is_no_holder(world):
    add_process(world, 4000, ["bash", "wait.sh"], comm="bash", fds=[lock_file(world, 21)])
    add_process(world, 4321, ["bash", "hold.sh"], comm="bash", fds=[lock_file(world, 21)], locked=True)
    add_process(world, 4322, ["flock", "8"], comm="flock", fds=[lock_file(world, 21)])
    write_locks(world, (21, 2373047, False), (21, 4322, True))
    held = board_rows(world)["192.0.2.21"].holder
    assert (held.pid, held.source, held.script) == ("4321", "fd", "hold.sh")


def test_script_name_reads_the_command_line(world):
    cases = {
        501: (["/env/bin/python", "-m", "pytest", "tests/test_x.py"], "pytest"),
        502: (["/env/bin/python", "/env/bin/pytest", "-k", "spinn1"], "pytest"),
        503: (["bash", "/home/u/run_tier.sh", "--x"], "run_tier.sh"),
        504: (["/opt/app/ring_chip_mc", "--x"], "ring_chip_mc"),
        505: (["python", "-u", "hw_full.py", "-m", "3"], "hw_full.py"),
        506: ([], None),
        507: (["/env/bin/python", "/env/bin/pytest", "-m", "not hardware"], "pytest"),
        508: (["/opt/app/ring_chip_mc", "-m", "3"], "ring_chip_mc"),
        509: (["python3.11", "-W", "ignore", "-X", "dev", "x.py"], "x.py"),
        510: (["python", "-c", "import time"], "python"),
    }
    for pid, (argv, expected) in cases.items():
        add_process(world, pid, argv, comm="kworker")
        assert bh.script_name(str(pid)) == expected, argv
    assert bh.display_command(bh.holder("506")) == "kworker"


def test_dead_live_and_long_script_rows_line_up():
    rows = [
        _row(_live()),
        _row(_live(age=10 * 86400 - 1, script="x" * 30)),
        _row(_dead()),
        _row(_dead(opened_at=time.time() - 30)),
    ]
    for plain in (False, True):
        for show_pid in (False, True):
            lines = [bh.compact(row, full_ip=False, show_pid=show_pid, plain=plain) for row in rows]
            assert len({line.index("(frame_1[0])") for line in lines}) == 1, lines


def test_json_adds_holder_waiter_and_mtime_fields(world):
    add_process(world, 500, ["python", "run.py"])
    add_process(world, 501, ["flock", "9"], comm="flock")
    write_locks(world, (21, 500, False), (21, 501, True))
    data = board_rows(world)
    blob = bh.as_json(data["192.0.2.21"])
    assert {key: blob["holder"][key] for key in ("script", "alive", "source", "dead_pid", "hidden")} == {
        "script": "run.py",
        "alive": True,
        "source": "proc",
        "dead_pid": None,
        "hidden": False,
    }
    assert [(waiter["pid"], waiter["hidden"]) for waiter in blob["waiters"]] == [("501", False)]
    mtime = lock_file(world, 21).stat().st_mtime
    assert datetime.fromisoformat(blob["lock_mtime"]).timestamp() == int(mtime)
    free = bh.as_json(data["192.0.2.22"])
    assert (free["holder"], free["waiters"], free["state"]) == (None, [], "free")


def test_json_marks_a_hidden_holder_and_keeps_its_user(world):
    write_locks(world, (21, 777, False))
    held = bh.as_json(board_rows(world)["192.0.2.21"])["holder"]
    assert (held["user"], held["hidden"], held["source"], held["dead_pid"]) == ("?", True, "none", "777")


def test_free_summary_joins_consecutive_boards():
    def row(ip, holder=None):
        return bh.Row(bh.Board(ip=ip, machine="m"), Path("x"), holder, True)

    data = [row(f"192.0.2.{last}", _live() if last == 24 else None) for last in range(21, 27)]
    data.append(row("198.51.100.2"))
    assert bh.free_summary(data, full_ip=False) == "free  2.21-2.23 2.25-2.26 100.2"
    assert bh.free_summary(data, full_ip=True) == "free  192.0.2.21-192.0.2.23 192.0.2.25-192.0.2.26 198.51.100.2"
    assert bh.free_summary([row("192.0.2.24", _live())], full_ip=False) is None


def test_all_folds_free_boards_unless_plain(world, capsys):
    add_process(world, 500, ["python", "run.py"])
    write_locks(world, (22, 500, False))
    where = ["--config-root", str(world.root), "--locks-dir", str(world.locks)]

    bh.print_rows(bh.parser().parse_args(["--all", *where]))
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "Board Status" and lines[-2:] == ["", "free  2.21 2.23"]
    assert [line.split()[0] for line in lines[2:-2]] == ["2.22"]

    bh.print_rows(bh.parser().parse_args(["--all", "--plain", *where]))
    out = capsys.readouterr().out
    assert [line.split()[:2] for line in out.splitlines()[2:]] == [["2.21", "free"], ["2.22", "long"], ["2.23", "free"]]

    bh.print_rows(bh.parser().parse_args(where))
    out = capsys.readouterr().out
    assert "free" not in out and "\x1b" not in out


def test_colour_only_on_a_terminal(monkeypatch):
    tty = SimpleNamespace(isatty=lambda: True)
    pipe = SimpleNamespace(isatty=lambda: False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert bh.use_color(False, tty)
    assert not bh.use_color(True, tty)
    assert not bh.use_color(False, pipe)
    monkeypatch.setenv("NO_COLOR", "")
    assert bh.use_color(False, tty)
    monkeypatch.setenv("NO_COLOR", "1")
    assert not bh.use_color(False, tty)


def test_accent_marks_dead_and_long_holds_only():
    long_row, short_row, dead_row = _row(_live(age=3600)), _row(_live(age=5)), _row(_dead())
    assert bh.compact(long_row, False, False, False, color=True).count(bh.ACCENT) == 1
    assert bh.ACCENT + "●" in bh.compact(long_row, False, False, False, color=True)
    short = bh.compact(short_row, False, False, False, color=True)
    assert bh.ACCENT not in short and bh.DIM + "(frame_1[0])" + bh.RESET in short
    dead = bh.compact(dead_row, False, False, False, color=True)
    assert dead.count(bh.ACCENT) == 2 and dead.endswith(bh.ACCENT + "pid 777 exited, lock held" + bh.RESET)
    for row in (long_row, short_row, dead_row):
        assert "\x1b" not in bh.compact(row, False, False, False)
