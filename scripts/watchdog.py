"""Independent one-shot watchdog; intended for Windows Task Scheduler."""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import hk_calendar, notify  # noqa: E402
from app.futu_client import load_config  # noqa: E402

STATE_FILE = ROOT / ".runtime" / "watchdog.json"
HEALTH_URL = "http://127.0.0.1:8000/health"
DAILY_URL = "http://127.0.0.1:8000/internal/daily-jobs"
LOCK_PORT = 18777


def _http(url: str, method: str = "GET", timeout: int = 8) -> dict:
    request = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _port(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return True
    except OSError:
        return False


def _heartbeat(url: str) -> None:
    """Ping an external dead-man endpoint; it alerts when these pings stop."""
    with urllib.request.urlopen(url, timeout=8) as response:
        if response.status >= 400:
            raise OSError(f"heartbeat HTTP {response.status}")


def _state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(value: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = STATE_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(STATE_FILE)


def run(recover: bool = True) -> dict:
    now = datetime.now()
    config = load_config()
    webhook = (os.environ.get("CONTRARIAN_WECOM_WEBHOOK", "")
               or config.get("wecom", {}).get("webhook", ""))
    heartbeat_url = (os.environ.get("CONTRARIAN_HEARTBEAT_URL", "")
                     or config.get("watchdog", {}).get("heartbeat_url", ""))
    state = _state()
    web_ok = False
    try:
        health = _http(HEALTH_URL)
        web_ok = bool(health.get("ok"))
    except Exception:  # noqa: BLE001
        health = {}
    open_d_ok = _port(11111)
    if recover and (not web_ok or not open_d_ok):
        try:
            subprocess.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                 str(ROOT / "start_contrarian.ps1"), "-NoBrowser"],
                cwd=ROOT, capture_output=True, timeout=60, check=False)
            health = _http(HEALTH_URL)
            web_ok, open_d_ok = bool(health.get("ok")), _port(11111)
        except Exception:  # noqa: BLE001
            pass
    if not web_ok or not open_d_ok:
        message = ("**Contrarian 独立守护报警**\n"
                   f"网站={'正常' if web_ok else '故障'}；"
                   f"OpenD={'正常' if open_d_ok else '故障'}；时间{now:%Y-%m-%d %H:%M:%S}")
        notify.push_if_new(f"watchdog:health:{now:%Y-%m-%d-%H}", message, webhook,
                           min_interval=3600)
    else:
        state.pop("last_error", None)
    periods = hk_calendar.periods(now.date())
    if periods and now >= datetime.combine(now.date(), periods[-1][1]) + timedelta(minutes=30):
        if web_ok and state.get("last_daily_catchup") != str(now.date()):
            try:
                _http(DAILY_URL, "POST", timeout=180)
                state["last_daily_catchup"] = str(now.date())
            except Exception as exc:  # noqa: BLE001
                state["last_error"] = type(exc).__name__
    state.update({"checked_at": now.isoformat(timespec="seconds"),
                  "website_ok": web_ok, "opend_ok": open_d_ok})
    if heartbeat_url:
        try:
            _heartbeat(heartbeat_url)
            state["external_heartbeat_ok"] = True
            state["external_heartbeat_at"] = now.isoformat(timespec="seconds")
            state.pop("external_heartbeat_error", None)
        except Exception as exc:  # noqa: BLE001
            state["external_heartbeat_ok"] = False
            state["external_heartbeat_error"] = type(exc).__name__
    _save(state)
    notify.retry_outbox(webhook)
    return state


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="run one health/catch-up cycle")
    parser.add_argument("--loop", action="store_true", help="run independently every five minutes")
    parser.add_argument("--no-recover", action="store_true")
    args = parser.parse_args()
    if args.loop:
        lock = socket.socket()
        try:
            lock.bind(("127.0.0.1", LOCK_PORT))
            lock.listen(1)
        except OSError:
            sys.exit(0)
        while True:
            try:
                run(not args.no_recover)
            except Exception as exc:  # noqa: BLE001
                state = _state()
                state.update({"checked_at": datetime.now().isoformat(timespec="seconds"),
                              "last_error": type(exc).__name__})
                _save(state)
            time.sleep(300)
    else:
        print(json.dumps(run(not args.no_recover), ensure_ascii=False))
