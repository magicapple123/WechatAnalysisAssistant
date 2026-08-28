"""Shared timestamp formatting helpers."""

from datetime import datetime


def format_timestamp(timestamp: int) -> str:
    """Format a Unix timestamp for chat display and exports."""
    if not timestamp or timestamp <= 0:
        return ""
    try:
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, OverflowError, TypeError, ValueError):
        return str(timestamp)
