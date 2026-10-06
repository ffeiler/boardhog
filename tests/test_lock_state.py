"""Runnable checks for the pure logic: config parsing, lock naming, state derivation, compact layout, JSON, flags.

Live /proc access (proc_locks(), holder() for a running pid) is exercised by running boardhog live, not here.
IPs are RFC 5737 documentation ranges (192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24) and the RFC 3849 IPv6
documentation prefix (2001:db8::/32), not real boards.
"""

import contextlib
import io
import tempfile
from pathlib import Path

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


def test_board_lock_name():
    board = bh.Board(ip="192.0.2.21", machine="frame_1", board_id=0, n_boards=3)
    assert board.lock_name == "BOARD_192_0_2_21.lock"


def _row(*, board_held, exists=True):
    board = bh.Board(ip="192.0.2.21", machine="frame_1", board_id=0, n_boards=3)
    return bh.Row(board, Path("x"), board_held, exists)


def test_state_derivation():
    assert _row(board_held=None).state == "free"
    assert _row(board_held=_held()).state == "long"
    assert _row(board_held=None, exists=False).state == "missing"


def test_as_json():
    blob = bh.as_json(_row(board_held=None))
    assert blob["ip"] == "192.0.2.21"
    assert blob["state"] == "free"
    assert blob["holder"] is None


def test_holder_unreadable_pid():
    held = bh.holder("999999999")
    assert held.user == "unknown"
    assert held.command == "unknown"
    assert held.age_seconds is None


def test_status_symbols():
    assert bh.status_symbol("free", plain=False) == "○"
    assert all(bh.status_symbol(state, plain=False) == "●" for state in ("short", "medium", "long"))
    assert bh.status_symbol("long", plain=True) == "long"


STATES = ("free", "short", "medium", "long", "missing")
AGES = {"short": 5, "medium": 120, "long": 3600}


def _state_row(state):
    if state == "free":
        return _row(board_held=None)
    if state == "missing":
        return _row(board_held=None, exists=False)
    return _row(board_held=_held(AGES[state]))


def _body_column(line):
    """Column where the text after the IP and state fields starts."""
    _ip, _state, body = line.split(maxsplit=2)
    return len(line) - len(body)


def test_compact_columns_line_up_in_every_state():
    for plain in (False, True):
        for full_ip in (False, True):
            for show_pid in (False, True):
                lines = [bh.compact(_state_row(s), full_ip=full_ip, show_pid=show_pid, plain=plain) for s in STATES]
                assert len({_body_column(line) for line in lines}) == 1, lines
                held = [line for state, line in zip(STATES, lines) if state in AGES]
                assert len({line.index("(frame_1[0])") for line in held}) == 1, held


def test_plain_and_symbol_rows_differ_only_in_the_state_field():
    for state in STATES:
        row = _state_row(state)
        dot = bh.compact(row, full_ip=False, show_pid=True, plain=False)
        word = bh.compact(row, full_ip=False, show_pid=True, plain=True)
        assert dot.split()[1] == bh.SYMBOLS[state]
        assert word.split()[1] == state
        assert dot[_body_column(dot) :] == word[_body_column(word) :]


def test_missing_row_names_board_and_lock_file():
    line = bh.compact(_state_row("missing"), full_ip=False, show_pid=False, plain=True)
    assert line.split() == ["2.21", "missing", "frame_1[0]", "(no", "BOARD_192_0_2_21.lock)"]


def test_held_columns_fit_long_ages_commands_pids_and_unknown_holders():
    wide = bh.Holder(pid="4194304", user="someone", command="x" * 15, age_seconds=10 * 86400 - 1)
    narrow = bh.Holder(pid="1", user="someone", command="server", age_seconds=5)
    unknown = bh.Holder(pid="4194304", user="unknown", command="unknown", age_seconds=None)
    for plain in (False, True):
        for show_pid in (False, True):
            lines = [
                bh.compact(_row(board_held=h), full_ip=False, show_pid=show_pid, plain=plain)
                for h in (wide, narrow, unknown)
            ]
            assert len({line.index("(frame_1[0])") for line in lines}) == 1, lines
    assert bh.compact(_row(board_held=unknown), full_ip=False, show_pid=False, plain=True).split()[1] == "long"


def test_no_emoji_flag_rejected():
    assert bh.parser().parse_args(["--plain"]).plain
    for flag in ("--no-emoji", "--no-e"):
        with contextlib.redirect_stderr(io.StringIO()):
            try:
                bh.parser().parse_args([flag])
            except SystemExit as exc:
                assert exc.code == 2
            else:
                raise AssertionError(f"{flag} parsed")


GROUP = (("192.0.2.21", "192.0.2.22", "192.0.2.23"),)
BRACKET_CHARS = {False: ("┌", "├", "└"), True: ("/", "|", "\\")}


def _frame_row(last, holder=None, exists=True):
    board = bh.Board(ip=f"192.0.2.{last}", machine="frame_1", board_id=last - 20, n_boards=5)
    return bh.Row(board, Path("x"), holder, exists)


def _pid(pid, user="someone"):
    return bh.Holder(pid=pid, user=user, command="python", age_seconds=1576)


def _marks(lines, full_ip):
    return [line[15 if full_ip else 5] for line in lines]


def test_bracket_over_a_group_one_pid_holds():
    data = [_frame_row(20), *(_frame_row(n, _pid("999")) for n in (21, 22, 23)), _frame_row(24, _pid("999"))]
    for show_all in (False, True):
        visible = [row for row in data if bh.is_visible(row, show_all)]
        for plain in (False, True):
            for full_ip in (False, True):
                for show_pid in (False, True):
                    lines = bh.compact_lines(visible, full_ip, show_pid, plain, GROUP)
                    marks = _marks(lines, full_ip)
                    triad = marks[1:4] if show_all else marks[0:3]
                    assert triad == list(BRACKET_CHARS[plain]), lines
                    assert marks.count(" ") == len(marks) - 3, lines
                    for row, line in zip(visible, lines):
                        width = 15 if full_ip else 5
                        bare = bh.compact(row, full_ip, show_pid, plain)
                        assert line[:width] + " " + line[width + 1 :] == bare, (line, bare)
                        assert line[width + 1 :].startswith(bh.status_symbol(row.state, plain)), line


def test_bracket_counts_a_gone_holder_by_pid():
    gone = [_pid("4194304", user="unknown"), _pid("4194304", user="unknown"), _pid("4194304")]
    data = [_frame_row(n, h) for n, h in zip((21, 22, 23), gone)]
    assert _marks(bh.compact_lines(data, False, False, True, GROUP), False) == ["/", "|", "\\"]


def test_no_bracket_unless_one_pid_holds_every_adjacent_member():
    cases = {
        "split pids": [_frame_row(21, _pid("999")), _frame_row(22, _pid("999")), _frame_row(23, _pid("1000"))],
        "all free": [_frame_row(21), _frame_row(22), _frame_row(23)],
        "two of three": [_frame_row(21, _pid("999")), _frame_row(22, _pid("999"))],
        "free member": [_frame_row(21, _pid("999")), _frame_row(22, _pid("999")), _frame_row(23)],
        "missing member": [_frame_row(21, _pid("999")), _frame_row(22, _pid("999")), _frame_row(23, exists=False)],
        "interleaved": [
            _frame_row(21, _pid("999")),
            _frame_row(24, _pid("999")),
            _frame_row(22, _pid("999")),
            _frame_row(23, _pid("999")),
        ],
    }
    for name, data in cases.items():
        for plain in (False, True):
            for full_ip in (False, True):
                lines = bh.compact_lines(data, full_ip, True, plain, GROUP)
                assert lines == [bh.compact(row, full_ip, True, plain) for row in data], (name, lines)


def test_top_level_scalars_belong_to_no_machine():
    text = (
        "SPINNAKER_CONFIG_PATH: /mnt/spinnaker\n"
        "frame_1:\n  type: 248\n  ETH_IP_START: 192.0.2.21\n"
        "n_boards: 3\n  type: 201\n"
        "b201_1:\n  type: 201\n  ETH_IP_START: 198.51.100.2\n"
    )
    with tempfile.TemporaryDirectory() as root:
        (Path(root) / bh.NETWORK_CONFIG).write_text(text)
        machines = bh.parse_network_config(Path(root) / bh.NETWORK_CONFIG)
        boards = bh.configured_boards(Path(root))
    assert machines == [
        ("frame_1", {"type": "248", "ETH_IP_START": "192.0.2.21"}),
        ("b201_1", {"type": "201", "ETH_IP_START": "198.51.100.2"}),
    ]
    assert [(b.ip, b.board_type) for b in boards] == [("192.0.2.21", "248"), ("198.51.100.2", "201")]


def test_an_ipv6_machine_is_skipped():
    v6 = "v6_frame:\n  type: 248\n  n_boards: 2\n  ETH_IP_START: 2001:db8::15\n\n"
    with tempfile.TemporaryDirectory() as root:
        (Path(root) / bh.NETWORK_CONFIG).write_text(v6 + CONFIG)
        boards = bh.inventory(Path(root), Path(root) / "locks", include_unconfigured=True)
    assert [b.ip for b in boards] == ["192.0.2.21", "192.0.2.22", "192.0.2.23", "198.51.100.2"]


def test_lock_links_dangling_and_valid():
    with tempfile.TemporaryDirectory() as root:
        (Path(root) / bh.NETWORK_CONFIG).write_text(CONFIG)
        locks = Path(root) / "locks"
        locks.mkdir()
        for name in ("192_0_2_21", "192_0_2_22", "198_51_100_2", "203_0_113_9"):
            (locks / f"BOARD_{name}.lock").touch()
        (locks / "BOARD_192_0_2_23.lock").symlink_to(locks / "gone")
        (locks / "BOARD_203_0_113_7.lock").symlink_to(locks / "gone")
        (locks / "BOARD_203_0_113_8.lock").symlink_to(locks / "BOARD_203_0_113_9.lock")
        states = [(row.board.ip, row.state) for row in bh.rows(Path(root), locks, include_unconfigured=True)]
    assert states == [
        ("192.0.2.21", "free"),
        ("192.0.2.22", "free"),
        ("192.0.2.23", "missing"),
        ("198.51.100.2", "free"),
        ("203.0.113.8", "free"),
        ("203.0.113.9", "free"),
    ], states


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all passed")
