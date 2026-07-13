"""Runnable checks for the pure logic: STM parsing, entity discovery, state derivation, rendering.

Filesystem/proc access (holder(), proc_locks()) is exercised by running boardhog live, not here.
IPs are RFC 5737 documentation ranges (192.0.2.0/24, 198.51.100.0/24), not real boards.
"""

import tempfile
from pathlib import Path

import boardhog as bh

CONFIG = """\
frame_1:
  type: 248
  n_boards: 3
  STM_IP: 192.0.2.2
  ETH_IP_START: 192.0.2.21

b201_1:
  type: 201
  n_boards: 1
  STM_IP: None
  ETH_IP_START: 198.51.100.2
"""


def _held(age=3600):
    return bh.Holder(pid="999", user="someone", command="server", age_seconds=age)


def test_normalize_stm_ip():
    assert bh.normalize_stm_ip("None") is None
    assert bh.normalize_stm_ip("none") is None
    assert bh.normalize_stm_ip("") is None
    assert bh.normalize_stm_ip(None) is None
    assert bh.normalize_stm_ip("not-an-ip") is None
    assert bh.normalize_stm_ip(" 192.0.2.2 ") == "192.0.2.2"


def test_configured_boards_and_stm_entity():
    with tempfile.TemporaryDirectory() as root:
        (Path(root) / bh.NETWORK_CONFIG).write_text(CONFIG)
        boards = bh.configured_boards(Path(root))

    frame_boards = [b for b in boards if b.machine == "frame_1" and not b.is_stm]
    assert [b.ip for b in frame_boards] == ["192.0.2.21", "192.0.2.22", "192.0.2.23"]
    assert all(b.stm_ip == "192.0.2.2" for b in frame_boards)

    single = next(b for b in boards if b.machine == "b201_1" and not b.is_stm)
    assert single.stm_ip is None  # STM_IP: None -> no frame gating

    stms = [b for b in boards if b.is_stm]
    assert len(stms) == 1  # one entity for the shared frame_1 STM, none for single-node
    assert stms[0].ip == "192.0.2.2"
    assert stms[0].lock_name == "STM_192_0_2_2.lock"
    assert stms[0].label == "STM frame_1"


def test_board_lock_name():
    board = bh.Board(ip="192.0.2.21", machine="frame_1", board_id=0, n_boards=3, stm_ip="192.0.2.2")
    assert board.lock_name == "BOARD_192_0_2_21.lock"


def _row(*, board_held, stm_held, exists=True, is_stm=False, stm_ip="192.0.2.2"):
    board = bh.Board(ip="192.0.2.21", machine="frame_1", board_id=0, n_boards=3, stm_ip=stm_ip, is_stm=is_stm)
    return bh.Row(board, Path("x"), board_held, exists, stm_held)


def test_state_derivation():
    assert _row(board_held=None, stm_held=None).state == "free"
    assert _row(board_held=None, stm_held=_held()).state == "blocked"  # <- the failure mode
    assert _row(board_held=_held(), stm_held=_held()).state == "long"  # own lock wins over STM
    assert _row(board_held=None, stm_held=None, exists=False).state == "missing"
    # An STM entity has no parent STM, so it never reports blocked.
    assert _row(board_held=None, stm_held=None, is_stm=True, stm_ip=None).state == "free"


def test_blocked_rendering_names_the_stm():
    row = _row(board_held=None, stm_held=_held())
    assert "via STM 2.2" in bh.compact(row, full_ip=False, show_pid=False, plain=False)
    detail = bh.detailed(row)
    assert detail.startswith("192.0.2.21")
    assert "blocked by someone via STM 192.0.2.2" in detail


def test_json_carries_stm_fields():
    blob = bh.as_json(_row(board_held=None, stm_held=_held()))
    assert blob["state"] == "blocked"
    assert blob["stm_ip"] == "192.0.2.2"
    assert blob["holder"] is None
    assert blob["stm_holder"]["user"] == "someone"
    assert blob["is_stm"] is False


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all passed")
