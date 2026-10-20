"""MATE_READ_ONLY: Mate reads the car and never commands it.

For an install that should only watch — a dashboard, a second instance, an owner who keeps the
commands in the official app. The switch is an environment variable and not a setting on purpose:
a page that could turn it off would be the very thing it is meant to keep out.
"""
import os

_ON = ("1", "true", "yes", "on")


def read_only() -> bool:
    return os.environ.get("MATE_READ_ONLY", "").strip().lower() in _ON
