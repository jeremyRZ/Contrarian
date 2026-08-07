"""Test-process isolation for third-party libraries with import-time side effects."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


# futu-api creates its log directory while being imported. Keep that write out of
# the real user profile so collection is deterministic in CI and sandboxes.
_TEST_APPDATA = Path(tempfile.gettempdir()) / "contrarian-pytest-appdata"
_TEST_APPDATA.mkdir(parents=True, exist_ok=True)
os.environ["APPDATA"] = str(_TEST_APPDATA)
