#!/usr/bin/env python3
"""BoardHog: Monitor who's using SpiNNaker2 boards.

This tool shows which boards are locked, who's using them, and for how long.
It scans for /tmp/s2*_lock files and presents the information in a clear format.

Requirements:
    - Python 3.6+
    - Access to /tmp/ directory with s2 lock files

Installation:
    1. Make the script executable:
       chmod +x boardhog.py

    2. Create a symlink in your PATH (optional):
       mkdir -p ~/.local/bin
       ln -sf $(pwd)/boardhog.py ~/.local/bin/boardhog

    3. Ensure ~/.local/bin is in your PATH:
       echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
       source ~/.bashrc

Usage:
    boardhog                     # Display current board usage
    boardhog --help              # Show help
    watch -n .5 boardhog         # Monitor in real-time

Status Indicators:
    😊 < 1 minute: Normal usage
    😐 1-5 minutes: OK
    😠 > 5 minutes: Getting long
    😡 > 30 minutes: Time to investigate!

Examples:
    # Check who's using boards right now
    boardhog

    # Monitor continuously (refresh every 0.5 seconds)
    watch -n .5 boardhog

    # Use in a script
    boardhog | grep "username"

    # Check specific board types
    boardhog | grep "ETH"
"""

import argparse
import re
import shutil
import subprocess
import sys
from datetime import datetime


def extract_ip_suffix(filepath):
    """Extract the last two octets of IP address from a filepath.

    Args:
        filepath: Path containing an IP address (e.g., '/tmp/s2_eth_lock_192.168.1.53').

    Returns:
        The last two octets (e.g., '1.53') or 'N/A' if not found.
    """
    match = re.search(r"(\d+)\.(\d+)\.(\d+)\.(\d+)", filepath)
    if match:
        return f"{match.group(3)}.{match.group(4)}"
    return "N/A"


def extract_board_type(filename):
    """Extract board type from a lock filename.

    Args:
        filename: Lock filename (e.g., 's2_eth_lock_192.168.1.53').

    Returns:
        Board type in uppercase (e.g., 'ETH', 'USB') or 'UNK' if not recognized.
    """
    match = re.search(r"s2_(\w+)_lock", filename)
    if match:
        return match.group(1).upper()
    return "UNK"


def get_terminal_width():
    """Get terminal width with sensible defaults.

    Returns:
        Terminal width between 60-120 columns, or 80 if detection fails.
    """
    try:
        # Get terminal size from shutil
        columns, _ = shutil.get_terminal_size()
        # Use at least 60 columns, at most 120 columns
        return max(60, min(columns, 120))
    except (AttributeError, ValueError, IOError):
        # Default terminal width if detection fails
        return 80


def get_time_since(date_str, time_str):
    """Calculate time elapsed since file creation.

    Args:
        date_str: Date string in format 'Mon DD' (e.g., 'Jul 7').
        time_str: Time string in format 'HH:MM' (e.g., '15:35').

    Returns:
        A tuple of (formatted_time_string, category) where:
        - formatted_time_string: Human-readable time (e.g., '5m ago')
        - category: One of 'seconds', 'minutes', 'hours', or 'days'
    """
    try:
        # Parse the date and time
        current_year = datetime.now().year
        file_datetime = datetime.strptime(f"{current_year} {date_str} {time_str}", "%Y %b %d %H:%M")

        # If the parsed date is in the future, it's probably from last year
        if file_datetime > datetime.now():
            file_datetime = file_datetime.replace(year=current_year - 1)

        time_diff = datetime.now() - file_datetime

        if time_diff.days > 0:
            return f"{time_diff.days}d ago", "days"
        elif time_diff.seconds > 3600:
            hours = time_diff.seconds // 3600
            return f"{hours}h ago", "hours"
        elif time_diff.seconds > 60:
            minutes = time_diff.seconds // 60
            return f"{minutes}m ago", "minutes"
        else:
            return "now", "seconds"
    except Exception:
        return "???", "unknown"


def parse_ls_line(line):
    """Parse a single line from ls -lct output.

    Args:
        line: A line from 'ls -lct' (e.g., '-rw-rw-rw- 1 username username 0 Jul 7 15:35 /tmp/s2_eth_lock_192.168.1.53').

    Returns:
        Dictionary with extracted information or None if the line format is invalid.

    Keys in returned dictionary:
        filename, date, time, ip_suffix, board_type,
        time_since, time_category, username
    """
    # Example: -rw-rw-rw- 1 username     username     0 Jul  7 15:35 /tmp/s2_eth_lock_192.168.1.53
    parts = line.strip().split()
    if len(parts) < 9:
        return None

    # Extract filename (last part)
    filepath = parts[-1]
    filename = filepath.split("/")[-1]

    # Extract username (owner) - 3rd part
    username = parts[2] if len(parts) >= 3 else "unknown"

    # Extract date and time (parts 5, 6, 7)
    month = parts[5]
    day = parts[6]
    time = parts[7]

    # Extract IP suffix and board type
    ip_suffix = extract_ip_suffix(filepath)
    board_type = extract_board_type(filename)
    time_since, time_category = get_time_since(f"{month} {day}", time)

    return {
        "filename": filename,
        "date": f"{month} {day}",
        "time": time,
        "ip_suffix": ip_suffix,
        "board_type": board_type,
        "time_since": time_since,
        "time_category": time_category,
        "username": username,
    }


def get_board_status():
    """Get current board status by listing S2 lock files.

    Returns:
        List of strings from ls command output or empty list if none found.
    """
    try:
        # Use shell=True to handle glob expansion properly
        result = subprocess.run("ls /tmp/s2* -h -lct 2>/dev/null", shell=True, capture_output=True, text=True)
        if result.returncode == 0 and result.stdout.strip():
            return [line for line in result.stdout.strip().split("\n") if line.strip()]
        else:
            return []
    except Exception:
        return []


def print_board_hoggers(lines=None):
    """Print board usage information in a formatted table.

    Args:
        lines: Optional list of ls output lines. If None, fetches current status.
    """
    if lines is None:
        lines = get_board_status()

    # Get current terminal width
    term_width = get_terminal_width()

    # Header with emoji but no colors
    print("=" * term_width)
    title = "🐷  BOARD HOG SHAME BOARD  🐷"
    padding = " " * ((term_width - len(title)) // 2)
    print(f"{padding}{title}")
    print("=" * term_width)

    # Get column widths based on terminal width
    term_width = get_terminal_width()

    # Calculate proportional column widths - adjust ratios as needed
    board_width = max(8, int(term_width * 0.10))  # 10% of width, at least 8 chars
    hogged_width = max(12, int(term_width * 0.15))  # 15% of width, at least 12 chars
    since_width = max(12, int(term_width * 0.15))  # 15% of width, at least 12 chars
    hogger_width = max(25, term_width - board_width - hogged_width - since_width - 5)  # Remaining space

    if not lines:
        print()

        no_boards_msg = "🎉 ALL BOARDS ARE FREE! NOBODY IS HOGGING! 🎉"
        hooray_msg = "🌈 EVERYONE IS SHARING NICELY TODAY! 🌈"

        # Center the messages
        padding1 = " " * ((term_width - len(no_boards_msg)) // 2)
        padding2 = " " * ((term_width - len(hooray_msg)) // 2)

        print(f"{padding1}{no_boards_msg}")
        print(f"{padding2}{hooray_msg}")
        print()
        return

    # Table header
    print()
    print(
        f"{'BOARD':<{board_width}} "
        f"{'HOGGED FOR':<{hogged_width}} "
        f"{'SINCE':<{since_width}} "
        f"{'HOGGER':<{hogger_width}}"
    )
    print("-" * term_width)

    board_count = 0
    for line in lines:
        if not line.strip() or line.startswith("total"):
            continue

        parsed = parse_ls_line(line)
        if parsed:
            board_count += 1

            # Shame level determination based on tolerance levels:
            # < 1 minute: completely fine (green emoji)
            # 1-5 minutes: ok (yellow emoji)
            # > 5 minutes: concerning (red emoji)
            # > 30 minutes: angry (red emoji with angry face)

            time_category = parsed["time_category"]
            time_since = parsed["time_since"]

            if (
                time_category == "days"
                or (time_category == "hours")
                or (time_category == "minutes" and int(time_since.split("m")[0]) > 30)
            ):
                # Over 30 minutes = ANGRY
                shame_emoji = "😡"
            elif time_category == "minutes" and int(time_since.split("m")[0]) > 5:
                # Over 5 minutes = concerning
                shame_emoji = "😠"
            elif time_category == "minutes" and int(time_since.split("m")[0]) > 1:
                # 1-5 minutes = ok
                shame_emoji = "😐"
            else:
                # Less than 1 minute = completely fine
                shame_emoji = "😊"

            # Get username for display
            hogger_name = parsed["username"]

            # Create the blame message with status emoji + username
            blame_msg = f"{shame_emoji} {hogger_name}"

            # Use IP suffix as the board identifier
            board_id = f"{parsed['ip_suffix']}"

            print(
                f"{board_id:<{board_width}} "
                f"{parsed['time_since']:<{hogged_width}} "
                f"{parsed['time']:<{since_width}} "
                f"{blame_msg:<{hogger_width}}"
            )

    # Footer with statistics
    print("=" * term_width)
    if board_count > 0:
        if board_count > 3:
            # Critical situation - lots of hogged boards
            alert_border = "🚨 " + "!" * (term_width - 6) + " 🚨"

            alert_msg = f"🔥 WHOA! {board_count} boards?! Someone's building their own supercomputer! 🔥"
            urgent_msg = "🔪 Hide your boards! The board vigilante is on the hunt! 🔪"

            # Center the messages
            padding1 = " " * ((term_width - len(alert_msg)) // 2)
            padding2 = " " * ((term_width - len(urgent_msg)) // 2)

            print(alert_border)
            print(f"{padding1}{alert_msg}")
            print(f"{padding2}{urgent_msg}")
            print(alert_border)
        else:
            # Normal situation - some boards hogged
            hogged_msg = f"{board_count} board{'s' if board_count > 1 else ''} being hogged!"
            remind_msg = "🚪 Knock knock... it's the board liberation squad! 🔪"

            # Center the messages
            padding1 = " " * ((term_width - len(hogged_msg)) // 2)
            padding2 = " " * ((term_width - len(remind_msg)) // 2)

            print(f"{padding1}{hogged_msg}")
            print(f"{padding2}{remind_msg}")
    else:
        # No boards hogged - celebrate!
        celebration_msg = "🎉 AMAZING! NO ACTIVE BOARD HOGGERS DETECTED! 🎉"
        nice_msg = "🌟 Everyone's being considerate today! 🌟"

        # Center the messages
        padding1 = " " * ((term_width - len(celebration_msg)) // 2)
        padding2 = " " * ((term_width - len(nice_msg)) // 2)

        print(f"{padding1}{celebration_msg}")
        print(f"{padding2}{nice_msg}")


def main():
    """Main function to handle command line arguments."""
    parser = argparse.ArgumentParser(
        description="BoardHog: Monitor who's using SpiNNaker2 boards",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    boardhog                     # Show current board status
    watch -n .5 boardhog         # Monitor in real-time
    
Status indicators:
    😊 < 1 min    😐 1-5 min    😠 > 5 min    😡 > 30 min
        """,
    )

    parser.parse_args()  # Parse arguments for help functionality

    try:
        # Check if we're reading from stdin (piped input)
        if not sys.stdin.isatty():
            lines = [line.strip() for line in sys.stdin]
            print_board_hoggers(lines)
        else:
            print_board_hoggers()
    except KeyboardInterrupt:
        print("\n🏃 Board hogger hunt cancelled!")
    except BrokenPipeError:
        # Handle broken pipe gracefully when piping to other commands
        pass


if __name__ == "__main__":
    main()
