"""Telling the operator that a run failed.

The log is the record; this package is the tap on the shoulder. The router
hands every final failure to `Notifier.failed`, which decides in-process whether
anyone should hear about it (muted codes, muted apps, one message per failure
kind per window) and then writes to the configured channels — Telegram first,
e-mail second — without the failing request ever waiting for either.
"""

from router_mcp.notify.service import Failure, Notifier, build_notifier

__all__ = ["Failure", "Notifier", "build_notifier"]
