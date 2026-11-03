from __future__ import annotations


class SyncCancelled(Exception):
    """Raised when an operator stops a running sync request."""
