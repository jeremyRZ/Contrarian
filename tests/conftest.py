"""Test-process isolation for third-party libraries with import-time side effects."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest


# futu-api creates its log directory while being imported. Keep that write out of
# the real user profile so collection is deterministic in CI and sandboxes.
_TEST_APPDATA = Path(tempfile.gettempdir()) / "contrarian-pytest-appdata"
_TEST_APPDATA.mkdir(parents=True, exist_ok=True)
os.environ["APPDATA"] = str(_TEST_APPDATA)


@pytest.fixture(autouse=True)
def isolate_runtime_databases(tmp_path, monkeypatch):
    """No test may write the user's live forward or notification ledger."""
    from app.modules import forward_ledger, notification_ledger

    monkeypatch.setattr(forward_ledger, "DB_PATH", tmp_path / "forward.sqlite3")
    monkeypatch.setattr(notification_ledger, "DB_PATH", tmp_path / "notifications.sqlite3")
