"""Runnable checks for the pure logic: config parsing, state derivation, rendering, JSON.

Filesystem/proc access (holder(), proc_locks()) is exercised by running boardhog live, not here.
IPs are RFC 5737 documentation ranges (192.0.2.0/24, 198.51.100.0/24), not real boards.
"""

import tempfile
from pathlib import Path

import boardhog as bh

# STM_IP lines are present in the real config; boardhog tracks per-board locks and ignores them.
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


def test_configured_boards():
    with tempfile.TemporaryDirectory() as root:
        (Path(root) / bh.NETWORK_CONFIG).write_text(CONFIG)
        boards = bh.configured_boards(Path(root))

    frame_boards = [b for b in boards if b.machine == "frame_1"]
    assert [b.ip for b in frame_boards] == ["192.0.2.21", "192.0.2.22", "192.0.2.23"]

    single = next(b for b in boards if b.machine == "b201_1")
    assert single.ip == "198.51.100.2" and single.n_boards == 1

    # No STM entities: boardhog only tracks per-board BOARD_ locks.
    assert all(b.lock_name.startswith(bh.LOCK_PREFIX) for b in boards)


def test_board_lock_name():
    board = bh.Board(ip="192.0.2.21", machine="frame_1", board_id=0, n_boards=3)
    assert board.lock_name == "BOARD_192_0_2_21.lock"


def _row(*, board_held, exists=True):
    board = bh.Board(ip="192.0.2.21", machine="frame_1", board_id=0, n_boards=3)
    return bh.Row(board, Path("x"), board_held, exists)


def test_state_derivation():
    # Availability is per-board: only this board's own lock holder matters (no STM gating).
    assert _row(board_held=None).state == "free"
    assert _row(board_held=_held()).state == "long"
    assert _row(board_held=None, exists=False).state == "missing"


def test_json_has_no_stm_fields():
    blob = bh.as_json(_row(board_held=None))
    assert blob["state"] == "free"
    assert blob["holder"] is None
    assert not any(k in blob for k in ("is_stm", "stm_ip", "stm_holder"))


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all passed")
