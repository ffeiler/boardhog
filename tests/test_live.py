"""The live view (-n/--watch) and --color, against a fake /proc tree, a pty for stdin and a fake terminal for stdout.

IPs are RFC 5737 documentation addresses (192.0.2.0/24, 198.51.100.0/24), not real boards.
"""

import contextlib
import fcntl
import io
import os
import pwd
import shutil
import signal
import struct
import sys
import termios
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import boardhog as bh

CONFIG = """\
frame_1:
  type: 248
  n_boards: 3
  ETH_IP_START: 192.0.2.21

b201_1:
  type: 201
  n_boards: 1
  ETH_IP_START: 198.51.100.2
"""
ME = pwd.getpwuid(os.getuid()).pw_name
LOCKS = ("192_0_2_21", "192_0_2_22", "192_0_2_23", "198_51_100_2")


class Screen(io.StringIO):
    """A terminal on stdout without a size, so the view falls back to 24 lines."""

    def isatty(self):
        return True


@pytest.fixture
def world(tmp_path, monkeypatch):
    proc = tmp_path / "proc"
    proc.mkdir()
    locks = tmp_path / "locks"
    locks.mkdir()
    (tmp_path / bh.NETWORK_CONFIG).write_text(CONFIG)
    for name in LOCKS:
        (locks / f"BOARD_{name}.lock").touch()
    monkeypatch.setattr(bh, "PROC", proc)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    return SimpleNamespace(root=tmp_path, proc=proc, locks=locks)


@pytest.fixture
def terminal():
    master, slave = os.openpty()
    stdin = os.fdopen(slave, "rb", buffering=0)
    yield SimpleNamespace(master=master, stdin=stdin, saved=termios.tcgetattr(slave))
    stdin.close()
    os.close(master)


def lock_file(world, name):
    return world.locks / f"BOARD_{name}.lock"


def dev_inode(path):
    stat = path.stat()
    return f"{os.major(stat.st_dev):02x}:{os.minor(stat.st_dev):02x}:{stat.st_ino}"


def write_locks(world, *entries):
    """One /proc/locks line per (lock name, pid) holder."""
    lines = [
        f"{number}: FLOCK  ADVISORY  WRITE {pid} {dev_inode(lock_file(world, name))} 0 EOF"
        for number, (name, pid) in enumerate(entries, 1)
    ]
    (world.proc / "locks").write_text("\n".join(lines) + "\n")


def add_process(world, pid, argv, fds=()):
    root = world.proc / str(pid)
    (root / "fd").mkdir(parents=True)
    (root / "fdinfo").mkdir()
    (root / "status").write_text(f"Name:\tbash\nUid:\t{os.getuid()}\t{os.getuid()}\t0\t0\n")
    (root / "comm").write_text("bash\n")
    (root / "cmdline").write_bytes(b"".join(arg.encode() + b"\0" for arg in argv))
    for number, target in enumerate(fds, 3):
        (root / "fd" / str(number)).symlink_to(target.resolve())
        (root / "fdinfo" / str(number)).write_text(
            f"pos:\t0\nlock:\t1: FLOCK  ADVISORY  WRITE 1 {dev_inode(target)} 0 EOF\n"
        )


def live_args(world, *extra):
    return bh.parser().parse_args(
        ["-n", "60", "--config-root", str(world.root), "--locks-dir", str(world.locks), *extra]
    )


def holder(pid, user="alice", script="run.py", age=60, alive=True):
    if alive:
        return bh.Holder(pid=pid, user=user, command="python", age_seconds=age, script=script)
    return bh.Holder(pid=pid, user="?", command="-", age_seconds=None, alive=False, source="none", dead_pid=pid)


def snapshot(**held):
    """Rows for 2.21-2.23 keyed by last octet, e.g. snapshot(b21=holder("500"))."""
    return [
        bh.Row(
            bh.Board(ip=f"192.0.2.{last}", machine="frame_1", board_id=last - 21, board_type="248", n_boards=3),
            Path("x"),
            held.get(f"b{last}"),
            True,
        )
        for last in (21, 22, 23)
    ]


def clock(now):
    return time.strftime("%H:%M:%S", time.localtime(now))


def test_watch_takes_positive_seconds():
    assert bh.parser().parse_args(["-n", "1"]).watch == 1.0
    assert bh.parser().parse_args(["--watch", "0.5"]).watch == 0.5
    assert bh.parser().parse_args([]).watch is None
    for bad in ("0", "-1", "nan", "inf", "x"):
        with contextlib.redirect_stderr(io.StringIO()), pytest.raises(SystemExit) as exc:
            bh.parser().parse_args(["-n", bad])
        assert exc.value.code == 2, bad


def test_color_precedence(monkeypatch):
    tty = SimpleNamespace(isatty=lambda: True)
    pipe = SimpleNamespace(isatty=lambda: False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    assert bh.use_color(False, tty) and not bh.use_color(False, pipe)
    assert bh.use_color(False, pipe, "always") and not bh.use_color(False, tty, "never")
    assert not bh.use_color(True, tty, "always")
    monkeypatch.setenv("FORCE_COLOR", "1")
    assert bh.use_color(False, pipe)
    monkeypatch.setenv("NO_COLOR", "1")
    assert not bh.use_color(False, pipe) and not bh.use_color(False, tty)
    assert bh.use_color(False, pipe, "always")
    monkeypatch.setenv("NO_COLOR", "")
    monkeypatch.setenv("FORCE_COLOR", "")
    assert not bh.use_color(False, pipe)


def test_color_always_styles_piped_static_output(world, capsys):
    where = ["--config-root", str(world.root), "--locks-dir", str(world.locks)]
    bh.print_rows(bh.parser().parse_args(["--color", "always", *where]))
    assert capsys.readouterr().out.startswith(bh.BOLD + "Unavailable Boards" + bh.RESET + "\n\n")
    bh.print_rows(bh.parser().parse_args(where))
    assert "\x1b" not in capsys.readouterr().out


def test_header_counts_free_boards_per_type():
    data = snapshot(b22=holder("500"), b23=holder("600", alive=False))
    data.append(bh.Row(bh.Board(ip="198.51.100.2", machine="b", board_type="201"), Path("x"), None, True))
    data.append(bh.Row(bh.Board(ip="198.51.100.3", machine="c", board_type="201"), Path("x"), None, False))
    data.append(bh.Row(bh.Board(ip="203.0.113.9", configured=False), Path("x"), None, True))
    assert bh.type_counts(data) == [("spinn48", 1, 3), ("spinn1", 1, 2)]
    now = time.time()
    assert bh.live_header(data, now, False) == f"boardhog  {clock(now)}  spinn48 1/3 free  spinn1 1/2 free"
    styled = bh.live_header(data, now, True)
    assert styled == bh.BOLD + "boardhog" + bh.RESET + "  " + bh.DIM + clock(now) + bh.RESET + (
        "  spinn48 1/3 free  spinn1 1/2 free"
    )
    assert bh.live_footer(0.5, True).startswith(bh.DIM + "every 0.5s" + bh.RESET + "  ")


def test_key_hints_show_which_toggles_are_on():
    def hints(show_all, details):
        styled = bh.live_footer(1.0, True, show_all, details)
        return styled.split(bh.DIM + "every 1s" + bh.RESET + "  ", 1)[1]

    def dim(text):
        return bh.DIM + text + bh.RESET

    def bold(text):
        return bh.BOLD + text + bh.RESET

    assert hints(False, False) == "  ".join(
        [dim("a all"), dim("d details"), dim("w waiting"), bold("l log"), dim("q quit")]
    )
    assert hints(True, False) == "  ".join(
        [bold("a all"), dim("d details"), dim("w waiting"), bold("l log"), dim("q quit")]
    )
    assert hints(True, True) == "  ".join(
        [bold("a all"), bold("d details"), dim("w waiting"), bold("l log"), dim("q quit")]
    )
    assert bh.live_footer(1.0, True, waiting=True).endswith(
        bold("w waiting") + "  " + bold("l log") + "  " + dim("q quit")
    )
    assert bh.live_footer(1.0, False, True, True, True) == "every 1s  a all  d details  w waiting  l log  q quit"
    view = bh.LiveView(bh.parser().parse_args(["-n", "1", "--details"]), "nowhere")
    assert bh.BOLD + "d details" in view.screen([], 0.0, True)[-1]
    view.key("d")
    assert bh.DIM + "d details" in view.screen([], 0.0, True)[-1]
    assert bh.DIM + "w waiting" in view.screen([], 0.0, True)[-1]
    view.key("w")
    assert bh.BOLD + "w waiting" in view.screen([], 0.0, True)[-1]
    view.key("l")
    assert bh.DIM + "l log" in view.screen([], 0.0, True)[-1]


def test_a_short_window_cuts_the_change_log_then_rows_and_keeps_the_footer():
    view = bh.LiveView(bh.parser().parse_args(["-n", "1", "--all", "--plain"]), "nowhere")
    data = snapshot(b21=holder("500"), b22=holder("501"), b23=holder("502"))
    view.changes.extend(["12:00:00  2.21 taken by alice run.py", "12:00:01  2.22 taken by alice run.py"])
    footer = "every 1s  a all  d details  w waiting  l log  q quit"
    full = view.screen(data, 0.0, False)
    header, rows, changes = full[0], full[2:5], full[6:8]
    assert full == [header, "", *rows, "", *changes, "", footer]
    assert view.screen(data, 0.0, False, height=24) == full
    assert view.screen(data, 0.0, False, height=9) == [header, "", *rows, "", changes[1], "", footer]
    assert view.screen(data, 0.0, False, height=7) == [header, "", *rows, "", footer]
    assert view.screen(data, 0.0, False, height=6) == [header, "", *rows[:2], "", footer]
    view.key("l")
    assert view.screen(data, 0.0, False) == [header, "", *rows, "", footer]
    assert len(view.changes) == 2
    view.key("l")
    assert view.screen(data, 0.0, False) == full
    for height in range(1, 12):
        lines = view.screen(data, 0.0, False, height=height)
        assert len(lines) <= height and lines[-1] == footer, height


GROUP = (("192.0.2.21", "192.0.2.22", "192.0.2.23"),)


def waited(data, **waiters):
    """The rows of `data` with waiters added by last octet, e.g. waited(data, b21=[holder("601")])."""
    return [replace(row, waiters=tuple(waiters.get(f"b{row.board.ip.split('.')[-1]}", ()))) for row in data]


def test_w_folds_waiters_out_under_their_holder_row():
    bob, carol = holder("601", user="bob", script="flock", age=156), holder("602", user="carol", script="t.sh")
    data = waited(snapshot(b21=holder("500"), b23=holder("600", user="dave")), b21=[bob, carol], b23=[bob])
    view = bh.LiveView(bh.parser().parse_args(["-n", "1"]), "nowhere")
    folded = view.screen(data, 0.0, False)
    assert [line.split()[0] for line in folded[2:-2]] == ["2.21", "2.23"]
    assert folded[2].endswith("+2 waiting") and folded[3].endswith("+1 waiting")
    assert not view.key("w")
    lines = view.screen(data, 0.0, False)[2:-2]
    assert lines[1:3] == [
        "      ↳ bob          2m 36s     flock",
        "      ↳ carol        1m 00s     t.sh",
    ]
    assert [line.split()[0] for line in lines] == ["2.21", "↳", "↳", "2.23", "↳"]
    assert view.screen(data, 0.0, True)[3] == "      " + bh.DIM + "↳ bob          2m 36s     flock" + bh.RESET
    view.key("d")
    assert not any("↳" in line for line in view.screen(data, 0.0, False))


def test_waiter_lines_line_up_under_the_holder_columns():
    data = waited(
        snapshot(b21=holder("500"), b22=holder("500"), b23=holder("500")),
        b21=[holder("601", user="bob", script="flock", age=156)],
        b22=[holder("4194304", user="carol", script="t.sh", age=9)],
        b23=[holder("603", user="erin", script="x.py", age=5)],
    )
    expected = {"bob": ("2m 36s", "flock"), "carol": ("9s", "t.sh"), "erin": ("5s", "x.py")}
    for full_ip in (False, True):
        for show_pid in (False, True):
            for plain in (False, True):
                lines = bh.compact_lines(data, full_ip, show_pid, plain, GROUP, waiters=9)
                r21, w21, r22, w22, r23, w23 = lines
                mark, tag = 15 if full_ip else 5, "waiting" if plain else "↳"
                # The bracket runs on through the waiter lines between its top and bottom rows.
                top, middle, bottom = bh.BRACKETS[plain]
                carry = bh.BRACKET_LINE[plain]
                assert [line[mark] for line in lines] == [top, carry, middle, carry, bottom, " "], lines
                for row, line, user in ((r21, w21, "bob"), (r22, w22, "carol"), (r23, w23, "erin")):
                    columns = [row.index(cell) for cell in ("alice", "1m 00s", "run.py")]
                    assert [line.index(cell) for cell in (user, *expected[user])] == columns, (row, line)
                    assert line.index(tag) == mark + 1 and line[:mark].strip() == ""
                    assert line.find("PID=") == row.find("PID=")


def test_a_short_window_folds_waiters_back_before_rows():
    data = waited(
        snapshot(b21=holder("500"), b22=holder("501"), b23=holder("502")),
        b21=[holder("601", user="bob"), holder("602", user="carol")],
        b22=[holder("603", user="erin")],
    )
    view = bh.LiveView(bh.parser().parse_args(["-n", "1", "--plain"]), "nowhere")
    view.key("w")
    view.changes.append("12:00:00  2.23 taken by alice run.py")
    full = view.screen(data, 0.0, False)
    header, footer = full[0], full[-1]
    r21, w1, w2, r22, w3, r23 = full[2:8]
    names = ["2.21", "bob", "carol", "2.22", "erin", "2.23"]
    assert [r21[:4], w1.split()[1], w2.split()[1], r22[:4], w3.split()[1], r23[:4]] == names
    assert full == [header, "", r21, w1, w2, r22, w3, r23, "", full[9], "", footer]
    assert view.screen(data, 0.0, False, height=10) == [header, "", r21, w1, w2, r22, w3, r23, "", footer]
    assert view.screen(data, 0.0, False, height=9) == [header, "", r21, w1, w2, r22, r23, "", footer]
    assert view.screen(data, 0.0, False, height=8) == [header, "", r21, w1, r22, r23, "", footer]
    assert view.screen(data, 0.0, False, height=7) == [header, "", r21, r22, r23, "", footer]
    assert view.screen(data, 0.0, False, height=6) == [header, "", r21, r22, "", footer]
    for height in range(1, 14):
        lines = view.screen(data, 0.0, False, height=height)
        assert len(lines) <= height and lines[-1] == footer, height


def test_change_log_notes_taken_freed_and_exited():
    view = bh.LiveView(bh.parser().parse_args(["-n", "1"]), "nowhere")
    start = time.time()
    view.update(snapshot(b21=holder("500", age=60), b23=holder("600", user="bob", script="x.py")), start)
    assert list(view.changes) == []
    later = start + 10
    view.update(snapshot(b22=holder("700", user="carol"), b23=holder("600", alive=False)), later)
    assert list(view.changes) == [
        f"{clock(later)}  2.22 taken by carol run.py",
        f"{clock(later)}  2.23 holder pid 600 exited, lock held (bob x.py)",
    ]
    view = bh.LiveView(bh.parser().parse_args(["-n", "1"]), "nowhere")
    view.update(snapshot(b21=holder("500", age=60)), start)
    view.update(snapshot(b21=holder("500", age=70), b22=holder("700", user="carol")), start + 10)
    view.update(snapshot(), start + 15)
    assert list(view.changes) == [
        f"{clock(start + 15)}  2.21 freed (alice run.py, held 1m 15s)",
        f"{clock(start + 15)}  2.22 freed (carol run.py, held 5s)",
    ]


def test_change_log_ignores_finding_the_shell_behind_an_exited_pid():
    view = bh.LiveView(bh.parser().parse_args(["-n", "1"]), "nowhere")
    found = bh.Holder(pid="4321", user=ME, command="bash", age_seconds=5, source="fd", dead_pid="777")
    view.update(snapshot(b21=holder("777", alive=False)), time.time())
    view.update(snapshot(b21=found), time.time())
    view.update(snapshot(b22=holder("800", alive=False), b21=found), time.time())
    assert [change.split("  ", 1)[1] for change in view.changes] == ["2.22 taken, holder pid 800 exited, lock held"]


def test_change_log_names_the_own_process_that_let_go():
    view = bh.LiveView(bh.parser().parse_args(["-n", "1"]), "nowhere")
    found = bh.Holder(pid="4321", user=ME, command="bash", age_seconds=5, source="fd", dead_pid="777")
    view.update(snapshot(b21=found), time.time())
    view.update(snapshot(b21=holder("777", alive=False)), time.time())
    assert [change.split("  ", 1)[1] for change in view.changes] == [
        f"2.21 holder pid 4321 let go, lock held ({ME} bash)"
    ]


def test_change_log_names_a_hidden_holder():
    view = bh.LiveView(bh.parser().parse_args(["-n", "1"]), "nowhere")
    start = time.time()
    view.update(snapshot(b21=holder("777", alive=False), b22=holder("800", alive=False)), start)
    view.update(snapshot(b21=holder("500")), start + 10)
    assert [change.split("  ", 1)[1] for change in view.changes] == [
        "2.21 hidden pid 777 -> alice run.py (held 10s)",
        "2.22 freed (hidden pid 800, held 10s)",
    ]


def test_a_handover_is_one_line_naming_both_holders():
    view = bh.LiveView(bh.parser().parse_args(["-n", "1"]), "nowhere")
    start = time.time()
    view.update(snapshot(b21=holder("500", user="ekagupta", script="ring_chip_mc", age=2520)), start)
    view.update(snapshot(b21=holder("600", user="carol", script="pytest", age=1)), start + 10)
    view.update(snapshot(b21=holder("700", alive=False)), start + 15)
    assert list(view.changes) == [
        f"{clock(start + 10)}  2.21 ekagupta ring_chip_mc -> carol pytest (held 42m 10s)",
        f"{clock(start + 15)}  2.21 carol pytest -> hidden pid 700 (held 5s)",
    ]


def test_one_proc_pass_serves_every_exited_holder(world, monkeypatch):
    add_process(world, 4321, ["bash", "a.sh"], fds=[lock_file(world, "192_0_2_21")])
    add_process(world, 4322, ["bash", "b.sh"], fds=[lock_file(world, "192_0_2_22")])
    write_locks(world, ("192_0_2_21", 901), ("192_0_2_22", 902))
    calls = []
    original = bh.lock_fds

    def counted(paths):
        calls.append(len(paths))
        return original(paths)

    monkeypatch.setattr(bh, "lock_fds", counted)
    data = {row.board.ip: row.holder for row in bh.rows(world.root, world.locks, include_unconfigured=True)}
    assert calls == [2]
    assert (data["192.0.2.21"].pid, data["192.0.2.21"].dead_pid) == ("4321", "901")
    assert (data["192.0.2.22"].pid, data["192.0.2.22"].dead_pid) == ("4322", "902")
    assert bh.fd_holder(lock_file(world, "192_0_2_21"), exclude=set()) == "4321"


def test_a_holder_exiting_after_the_proc_pass_still_finds_the_shell(world, monkeypatch):
    # 901 still runs when rows() looks for exited holders and exits before its row resolves; 902 exited before.
    add_process(world, 901, ["flock", "9"])
    add_process(world, 4321, ["bash", "a.sh"], fds=[lock_file(world, "192_0_2_21")])
    write_locks(world, ("192_0_2_21", 901), ("192_0_2_22", 902))
    original = bh.lock_fds

    def exit_901(paths):
        shutil.rmtree(world.proc / "901", ignore_errors=True)
        return original(paths)

    monkeypatch.setattr(bh, "lock_fds", exit_901)
    data = {row.board.ip: row.holder for row in bh.rows(world.root, world.locks, include_unconfigured=True)}
    held = data["192.0.2.21"]
    assert (held.pid, held.source, held.dead_pid, held.alive) == ("4321", "fd", "901", True)


def test_one_frame_keys_and_draw(world):
    add_process(world, 500, ["python", "run.py"])
    write_locks(world, ("192_0_2_22", 500))
    view = bh.LiveView(live_args(world), "nowhere")
    data = bh.rows(world.root, world.locks, include_unconfigured=True)
    now = time.time()
    view.update(data, now)
    lines = view.screen(data, now, False)
    assert lines[0] == f"boardhog  {clock(now)}  spinn48 2/3 free  spinn1 1/1 free"
    assert lines[1] == "" and [line.split()[0] for line in lines[2:-2]] == ["2.22"]
    assert lines[-2:] == ["", "every 60s  a all  d details  w waiting  l log  q quit"]
    assert not view.key("a") and view.screen(data, now, False)[-3] == "free  2.21 2.23 100.2"
    assert not view.key("d") and view.screen(data, now, False)[2].startswith(
        "192.0.2.21      (BOARD_192_0_2_21.lock): free"
    )
    assert not view.key("x") and view.key("q")
    master, slave = os.openpty()
    try:
        assert bh.terminal_lines(os.fdopen(slave, closefd=False)) == 24
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
        assert bh.terminal_lines(os.fdopen(slave, closefd=False)) == 40
    finally:
        os.close(master)
        os.close(slave)
    screen = Screen()
    assert bh.terminal_lines(screen) == 24
    bh.draw(screen, ["a", "b", "c", "d"], height=3)
    assert screen.getvalue() == bh.HOME + "a\x1b[K\nb\x1b[K\nc\x1b[K\x1b[J"


def test_live_loop_reads_keys_and_restores_the_terminal(world, terminal, monkeypatch):
    add_process(world, 500, ["python", "run.py"])
    write_locks(world, ("192_0_2_22", 500))
    frames = []
    original = bh.draw

    def typing_draw(stream, lines, height):
        frames.append(lines)
        os.write(terminal.master, b"a" if len(frames) == 1 else b"q")
        original(stream, lines, height)

    monkeypatch.setattr(bh, "draw", typing_draw)
    screen = Screen()
    term_handler = signal.getsignal(signal.SIGTERM)
    assert bh.run_live(live_args(world), terminal.stdin, screen) == 0
    assert len(frames) == 2
    # stdout is a terminal, so the frames are styled.
    summary = bh.DIM + "free  2.21 2.23 100.2" + bh.RESET
    assert summary not in frames[0] and frames[1][-3] == summary
    assert frames[1][-1].startswith(bh.DIM + "every 60s" + bh.RESET)
    assert frames[0][0].startswith(bh.BOLD + "boardhog" + bh.RESET)
    assert screen.getvalue().startswith(bh.ENTER_SCREEN + bh.HOME) and screen.getvalue().endswith(bh.LEAVE_SCREEN)
    assert termios.tcgetattr(terminal.stdin.fileno()) == terminal.saved
    assert signal.getsignal(signal.SIGTERM) == term_handler


def test_live_loop_restores_the_terminal_on_ctrl_c_and_sigterm(world, terminal, monkeypatch):
    def interrupted(stream, lines, height):
        raise KeyboardInterrupt

    monkeypatch.setattr(bh, "draw", interrupted)
    screen = Screen()
    assert bh.run_live(live_args(world), terminal.stdin, screen) == 130
    assert screen.getvalue() == bh.ENTER_SCREEN + bh.LEAVE_SCREEN
    assert termios.tcgetattr(terminal.stdin.fileno()) == terminal.saved

    def terminated(stream, lines, height):
        os.kill(os.getpid(), signal.SIGTERM)

    monkeypatch.setattr(bh, "draw", terminated)
    term_handler = signal.getsignal(signal.SIGTERM)
    screen = Screen()
    with pytest.raises(SystemExit) as exc:
        bh.run_live(live_args(world), terminal.stdin, screen)
    assert exc.value.code == 128 + signal.SIGTERM
    assert screen.getvalue().endswith(bh.LEAVE_SCREEN)
    assert termios.tcgetattr(terminal.stdin.fileno()) == terminal.saved
    assert signal.getsignal(signal.SIGTERM) == term_handler


def test_watch_without_a_terminal_prints_one_static_frame(world, capsys, monkeypatch):
    add_process(world, 500, ["python", "run.py"])
    write_locks(world, ("192_0_2_22", 500))
    where = ["--config-root", str(world.root), "--locks-dir", str(world.locks)]
    monkeypatch.setattr(bh, "run_live", lambda *args, **kwargs: pytest.fail("live view without a terminal"))
    monkeypatch.setattr(sys, "argv", ["boardhog", *where])
    bh.main()
    static = capsys.readouterr().out
    monkeypatch.setattr(sys, "argv", ["boardhog", "-n", "1", *where])
    bh.main()
    assert capsys.readouterr().out == static
    assert static.startswith("Unavailable Boards\n\n2.22")
