#!/usr/bin/env python3
"""BoardHog: Monitor SpiNNaker2 board usage with traffic light indicators.

This tool shows which boards are locked, who's using them, and for how long.
It scans /tmp/s2*_lock files and displays board status in a clean format.

Monitored boards:
    - Fixed boards: 1.53, 2.52, 3.24
    - Dynamic boards: 4.xx (placeholder when free) or actual 4.* IPs when in use

Requirements:
    - Python 3.6+
    - Access to /tmp/ directory with s2 lock files

Installation:
    1. Make the script executable:
       chmod +x ~/boardhog/boardhog.py

    2. Create a symlink in your PATH (optional):
       mkdir -p ~/.local/bin
       ln -sf ~/boardhog/boardhog.py ~/.local/bin/boardhog

    3. Ensure ~/.local/bin is in your PATH:
       echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
       source ~/.bashrc

Usage:
    ~/boardhog/boardhog.py       # Run directly
    boardhog                     # If symlinked
    watch -n .5 boardhog         # Monitor in real-time

Status Indicators:
    🟢 Board is free
    🟡 Used < 1 minute
    🟠 Used 1-5 minutes
    🔴 Used > 5 minutes

Output Format:
    <ip_suffix> <status_indicator> [username]

Examples:
    # Check current board status
    ~/boardhog/boardhog.py
    # Output: 1.53 🟢
    #         2.52 🟠 username
    #         4.xx 🟢

    # Monitor continuously (refresh every 0.5 seconds)
    watch -n .5 boardhog

    # Filter for specific users
    boardhog | grep "username"
"""

import argparse
import re
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


def get_status_indicator(time_category, time_since):
    """Get traffic light status indicator based on usage time.

    Args:
        time_category: Category from get_time_since ('seconds', 'minutes', 'hours', 'days').
        time_since: Time string from get_time_since (e.g., '5m ago').

    Returns:
        Traffic light emoji: 🟡 < 1min, 🟠 1-5min, 🔴 > 5min.
    """
    if (
        time_category == "days"
        or (time_category == "hours")
        or (time_category == "minutes" and int(time_since.split("m")[0]) > 5)
    ):
        return "🔴"  # Red - over 5 minutes
    elif time_category == "minutes" and int(time_since.split("m")[0]) > 1:
        return "🟠"  # Orange - 1-5 minutes
    else:
        return "🟡"  # Yellow - less than 1 minute


def get_all_board_ips():
    """Get all board IPs to monitor.

    Returns:
        List of IP suffixes. Fixed boards (1.53, 2.52, 3.24) plus either:
        - '4.xx' placeholder when no 4.* boards are in use, or
        - Actual 4.* IP suffixes when boards are locked (e.g., '4.15')
    """
    fixed_boards = ["1.53", "2.52", "3.24"]

    # For 4.* boards, discover them by checking for any existing lock files
    dynamic_boards = set()

    try:
        # Check for any 4.* lock files that might exist
        import glob

        lock_files = glob.glob("/tmp/s2*_lock_192.168.4.*")
        for lock_file in lock_files:
            ip_suffix = extract_ip_suffix(lock_file)
            if ip_suffix.startswith("4."):
                dynamic_boards.add(ip_suffix)
    except Exception:
        pass

    # If no 4.* boards are in use, show placeholder
    if not dynamic_boards:
        dynamic_boards.add("4.xx")

    return fixed_boards + sorted(list(dynamic_boards))


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
        filename, date, time, ip_suffix, time_since, time_category, username
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

    # Extract IP suffix
    ip_suffix = extract_ip_suffix(filepath)
    time_since, time_category = get_time_since(f"{month} {day}", time)

    return {
        "filename": filename,
        "date": f"{month} {day}",
        "time": time,
        "ip_suffix": ip_suffix,
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
    """Display board status with traffic light indicators.

    Shows each board's IP suffix, status (🟢🟡🟠🔴), and username if locked.
    Format: <ip_suffix> <status_indicator> [username]

    Args:
        lines: Optional list of ls output lines. If None, fetches current status.
    """
    if lines is None:
        lines = get_board_status()

    # Simple functional header
    print("Board Status")
    print()

    # Parse all lock files
    locked_boards = {}
    for line in lines:
        if not line.strip() or line.startswith("total"):
            continue

        parsed = parse_ls_line(line)
        if parsed:
            locked_boards[parsed["ip_suffix"]] = parsed

    # Get known board IPs and check status
    known_ips = get_all_board_ips()

    # Also include any 4.* boards found in lock files
    for ip_suffix in locked_boards.keys():
        if ip_suffix.startswith("4.") and ip_suffix not in known_ips:
            known_ips.append(ip_suffix)

    # Sort IPs for consistent display
    known_ips.sort()

    # Display each board
    for ip_suffix in known_ips:
        if ip_suffix in locked_boards:
            # Board is locked
            parsed = locked_boards[ip_suffix]
            status = get_status_indicator(parsed["time_category"], parsed["time_since"])
            hogger = parsed["username"]
            print(f"{ip_suffix} {status} {hogger}")
        else:
            # Board is free
            print(f"{ip_suffix} 🟢")

    # Also check for any other 4.* boards in lock files that we might have missed
    for ip_suffix, parsed in locked_boards.items():
        if ip_suffix.startswith("4.") and ip_suffix not in known_ips:
            status = get_status_indicator(parsed["time_category"], parsed["time_since"])
            hogger = parsed["username"]
            print(f"{ip_suffix} {status} {hogger}")


def main():
    """Main function to handle command line arguments and display board status."""
    parser = argparse.ArgumentParser(
        description="BoardHog: Monitor SpiNNaker2 board usage with traffic light indicators",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    boardhog                     # Show current board status
    watch -n .5 boardhog         # Monitor in real-time
    
Status indicators:
    🟢 Free    🟡 < 1min    🟠 1-5min    🔴 > 5min
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
        print("\nMonitoring stopped.")
    except BrokenPipeError:
        # Handle broken pipe gracefully when piping to other commands
        pass


if __name__ == "__main__":
    main()
