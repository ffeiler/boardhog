"""The STM controller check under --stm, against the fake ping and UDP in conftest.py and real flocks on lock files
under tmp_path, so no test reaches a controller.

IPs are RFC 5737 documentation addresses (192.0.2.0/24), not real boards.
"""

import fcntl
import io
import json
import os
import struct
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import boardhog as bh

CONFIG = """\
frame_1:
  type: 248
  n_boards: 3
  ETH_IP_START: 192.0.2.21
  STM_IP: 192.0.2.2

frame_2:
  type: 248
  n_boards: 1
  ETH_IP_START: 192.0.2.5
  STM_IP: 192.0.2.4
"""
LOCKS = (
    "BOARD_192_0_2_5",
    "BOARD_192_0_2_21",
    "BOARD_192_0_2_22",
    "BOARD_192_0_2_23",
    "STM_192_0_2_2",
    "STM_192_0_2_4",
)
REQUEST = struct.pack("<BBBBIII", 0, 0x64, 0, 0, 0, 0, 0x722ACCE7)
VALID = struct.pack("<BBBBIII", 0, 0x64, 0, 0, 0x42424D50, 0, 0x722ACCE7)


def ping_argv(ip):
    return ["ping", "-n", "-c", "1", "-W", "1", ip]


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
        (locks / f"{name}.lock").touch()
    monkeypatch.setattr(bh, "PROC", proc)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    where = ["--config-root", str(tmp_path), "--locks-dir", str(locks)]
    return SimpleNamespace(root=tmp_path, locks=locks, where=where, stm=locks / "STM_192_0_2_2.lock")


def hold(path):
    """The flock on `path` taken through another open file, as a loading process holds it; closing it lets go."""
    handle = open(path, "rb")
    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return handle


def stm_row(ip="192.0.2.2", frame="frame_1", held=None, probe=None):
    return bh.Row(
        bh.Board(ip=ip, machine=frame, is_stm=True), Path(f"STM_{ip.replace('.', '_')}.lock"), held, True, probe=probe
    )


def board_row(last, stm):
    return bh.Row(bh.Board(ip=f"192.0.2.{last}", machine=stm.board.machine), Path("x"), None, True, frame_stm=stm)


def result(state, udp_ms=None, age=0):
    return bh.StmProbe(state, state != "unreachable", udp_ms, time.time() - age)


def test_the_only_request_is_firmware_status_to_the_frames_own_controller():
    assert bh.STM_STATUS_REQUEST == REQUEST == bytes.fromhex("00640000 00000000 00000000 e7cc2a72")


def test_a_free_stm_lock_gates_one_firmware_status_read(world, network):
    os.utime(world.stm, (1_000_000_000, 1_000_000_000))
    held_during_send = []

    def check_lock():
        try:
            hold(world.stm).close()
        except BlockingIOError:
            held_during_send.append(True)

    network.on_send = check_lock
    before = time.time()
    probe = bh.probe_stm("192.0.2.2", world.stm)
    assert (probe.state, probe.icmp) == ("ok", True)
    assert isinstance(probe.udp_ms, float) and probe.udp_ms >= 0
    assert before <= probe.checked_at <= time.time()
    assert network.runs == [ping_argv("192.0.2.2")]
    assert network.sent == [(REQUEST, ("192.0.2.2", 8), 0.25)]
    assert held_during_send == [True]
    # The lock is let go after the one exchange, and the read-only open leaves the mtime hint alone.
    hold(world.stm).close()
    assert world.stm.stat().st_mtime == 1_000_000_000


def test_no_echo_reads_unreachable_and_sends_nothing(world, network):
    network.down.add("192.0.2.2")
    probe = bh.probe_stm("192.0.2.2", world.stm)
    assert (probe.state, probe.icmp, probe.udp_ms) == ("unreachable", False, None)
    assert network.sent == []


@pytest.mark.parametrize("error", [FileNotFoundError("ping"), subprocess.TimeoutExpired("ping", 5)])
def test_a_ping_that_cannot_run_reads_unreachable(world, network, error):
    network.ping_error = error
    assert bh.probe_stm("192.0.2.2", world.stm).state == "unreachable"
    assert network.sent == []


def test_a_held_stm_lock_skips_the_read(world, network):
    with hold(world.stm):
        probe = bh.probe_stm("192.0.2.2", world.stm)
    assert (probe.state, probe.icmp, probe.udp_ms) == ("lock held", True, None)
    assert bh.probe_stm("192.0.2.2", world.locks / "STM_192_0_2_9.lock").state == "lock held"
    assert not (world.locks / "STM_192_0_2_9.lock").exists()
    assert network.sent == []


@pytest.mark.parametrize(
    "reply, source",
    [
        (None, None),
        (VALID[:12], None),
        (VALID + b"\0", None),
        (struct.pack("<BBBBIII", 0, 0x64, 0, 0, 0, 0, 0xDEADBEEF), None),
        (struct.pack("<BBBBIII", 0, 0x65, 0, 0, 0, 0, 0x722ACCE7), None),
        (struct.pack("<BBBBIII", 1, 0x64, 0, 0, 0, 0, 0x722ACCE7), None),
        (VALID, ("192.0.2.9", 8)),
        (VALID, ("192.0.2.2", 9)),
    ],
)
def test_a_timeout_or_an_invalid_reply_reads_no_reply(world, network, reply, source):
    network.replies["192.0.2.2"] = reply
    if source:
        network.sources["192.0.2.2"] = source
    probe = bh.probe_stm("192.0.2.2", world.stm)
    assert (probe.state, probe.icmp, probe.udp_ms) == ("no reply", True, None)
    assert len(network.sent) == 1


def test_the_stm_line_names_controller_faults_in_the_accent():
    args = bh.parser().parse_args(["--stm"])
    alice = bh.Holder(pid="500", user="alice", command="python", age_seconds=60, script="run.py")
    quiet = [
        board_row(21, stm_row(probe=result("ok", 0.14))),
        board_row(5, stm_row("192.0.2.4", "frame_2", probe=result("lock held"))),
    ]
    assert bh.stm_line(args, quiet, True) is None
    down = stm_row(probe=result("unreachable"))
    hung = stm_row("192.0.2.4", "frame_2", held=alice, probe=result("no reply"))
    data = [board_row(21, down), board_row(5, hung)]
    assert bh.stm_line(args, data, False) == "stm  alice (frame_2)  frame_1 STM unreachable  frame_2 STM no reply"
    assert bh.stm_line(args, data, True) == (
        bh.DIM
        + "stm  alice (frame_2)"
        + bh.RESET
        + "  "
        + bh.ACCENT
        + "frame_1 STM unreachable"
        + bh.RESET
        + "  "
        + bh.ACCENT
        + "frame_2 STM no reply"
        + bh.RESET
    )
    assert bh.stm_line(args, [board_row(21, down)], True) == (
        bh.DIM + "stm" + bh.RESET + "  " + bh.ACCENT + "frame_1 STM unreachable" + bh.RESET
    )
    assert bh.stm_line(bh.parser().parse_args([]), data, False) is None
    assert bh.stm_line(bh.parser().parse_args(["--stm", "--details"]), data, False) is None


def test_details_ends_every_checked_frames_entry_with_its_last_check(world, network, capsys):
    network.down.add("192.0.2.4")
    bh.print_rows(bh.parser().parse_args(["--stm", "--details", *world.where]))
    lines = capsys.readouterr().out.splitlines()
    assert lines[:3] == ["Unavailable Boards", "", "STM 192.0.2.2   (STM_192_0_2_2.lock): free (no lock held)"]
    assert lines[3].startswith("    probe: ok in ") and lines[3].endswith(" ms, 0s ago")
    assert lines[4:] == [
        "STM 192.0.2.4   (STM_192_0_2_4.lock): free (no lock held)",
        "    probe: unreachable, 0s ago",
        "",
        "No unavailable boards.",
    ]
    assert bh.detailed(stm_row(probe=result("ok", 0.141, age=12))) == (
        "STM 192.0.2.2   (STM_192_0_2_2.lock): free (no lock held)\n    probe: ok in 0.14 ms, 12s ago"
    )
    assert bh.detailed(stm_row(probe=result("lock held", age=125))).endswith("\n    probe: lock held, 2m 05s ago")


def test_json_carries_each_frames_check_only_under_stm(world, network, capsys):
    bh.print_rows(bh.parser().parse_args(["--json", "--all", *world.where]))
    assert [board["stm_probe"] for board in json.loads(capsys.readouterr().out)] == [None] * 4
    assert network.runs == []
    network.down.add("192.0.2.4")
    bh.print_rows(bh.parser().parse_args(["--json", "--all", "--stm", *world.where]))
    boards = {board["ip"]: board["stm_probe"] for board in json.loads(capsys.readouterr().out)}
    ok = boards["192.0.2.21"]
    assert set(ok) == {"state", "icmp", "udp_ms", "checked_at"}
    assert (ok["state"], ok["icmp"]) == ("ok", True) and isinstance(ok["udp_ms"], float)
    assert datetime.fromisoformat(ok["checked_at"]).utcoffset() == timedelta(0)
    assert boards["192.0.2.22"] == boards["192.0.2.23"] == ok
    down = boards["192.0.2.5"]
    assert (down["state"], down["icmp"], down["udp_ms"]) == ("unreachable", False, None)
    assert network.runs == [ping_argv("192.0.2.2"), ping_argv("192.0.2.4")]
    assert network.sent == [(REQUEST, ("192.0.2.2", 8), 0.25)]


def test_a_frame_without_an_stm_lock_file_is_not_checked(world, network, capsys):
    (world.locks / "STM_192_0_2_4.lock").unlink()
    bh.print_rows(bh.parser().parse_args(["--json", "--all", "--stm", *world.where]))
    boards = {board["ip"]: board["stm_probe"] for board in json.loads(capsys.readouterr().out)}
    assert boards["192.0.2.5"] is None and boards["192.0.2.21"]["state"] == "ok"
    assert network.runs == [ping_argv("192.0.2.2")]


def test_the_prober_runs_one_round_at_a_time_at_most_every_interval(world, network):
    prober = bh.StmProber(world.locks)
    controllers = [bh.Board(ip="192.0.2.2", machine="frame_1", is_stm=True)]
    prober.poll([], 0.0)
    assert prober.thread is None and prober.results == {}
    network.gate = threading.Event()
    prober.poll(controllers, 100.0)
    assert prober.busy()
    prober.poll(controllers, 100.0 + bh.STM_PROBE_INTERVAL)
    network.gate.set()
    prober.thread.join(5)
    assert not prober.busy() and len(network.runs) == 1
    first = prober.results
    assert first["192.0.2.2"].state == "ok"
    prober.poll(controllers, 100.0 + bh.STM_PROBE_INTERVAL - 1)
    assert prober.results is first and len(network.runs) == 1
    prober.poll(controllers, 100.0 + bh.STM_PROBE_INTERVAL)
    prober.thread.join(5)
    assert prober.results is not first and len(network.runs) == 2
    prober.reset()
    assert prober.results == {}
    prober.poll(controllers, 100.0 + bh.STM_PROBE_INTERVAL + bh.STM_PROBE_MIN_GAP - 1)
    prober.thread.join(5)
    assert len(network.runs) == 2
    prober.poll(controllers, 100.0 + bh.STM_PROBE_INTERVAL + bh.STM_PROBE_MIN_GAP)
    prober.thread.join(5)
    assert len(network.runs) == 3


def test_the_live_view_checks_when_s_turns_on_and_shows_a_fault(world, network, monkeypatch):
    network.down.add("192.0.2.2")
    master, slave = os.openpty()
    stdin = os.fdopen(slave, "rb", buffering=0)
    frames = []

    def typing_draw(stream, lines, height):
        frames.append(lines)
        if len(frames) == 1:
            assert network.runs == []
            os.write(master, b"s")
        elif any("STM unreachable" in line for line in lines) or len(frames) > 100:
            os.write(master, b"q")

    monkeypatch.setattr(bh, "draw", typing_draw)
    args = bh.parser().parse_args(["-n", "60", *world.where])
    try:
        assert bh.run_live(args, stdin, Screen()) == 0
    finally:
        stdin.close()
        os.close(master)
    assert frames[0][1] == ""
    assert frames[-1][1] == bh.DIM + "stm" + bh.RESET + "  " + bh.ACCENT + "frame_1 STM unreachable" + bh.RESET
    assert network.runs == [ping_argv("192.0.2.2"), ping_argv("192.0.2.4")]
    assert network.sent == [(REQUEST, ("192.0.2.4", 8), 0.25)]
