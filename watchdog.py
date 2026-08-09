#!/usr/bin/env python3
"""External dead-man's switch for the wallet sweeper server.

Runs on GitHub Actions every 5 minutes (public repo = free scheduled jobs).
Probes the server TCP port. Sends a Telegram message ONLY on state changes:
- ok -> down:  "server is unreachable"
- down -> ok:  "server is back online"

State is persisted in watchdog_state.json committed back to the repo. The state
file is force re-committed every KEEPALIVE_DAYS even if nothing changed: GitHub
disables scheduled workflows in public repos after 60 days without repository
activity, and a commit to the default branch counts as activity, so this keeps
the schedule alive indefinitely.
"""

import base64
import json
import os
import socket
import urllib.error
import urllib.request
from datetime import date, datetime, timezone

STATE_PATH = "watchdog_state.json"
API_BASE = "https://api.github.com"
PROBE_TIMEOUT = 15
KEEPALIVE_DAYS = 30


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def probe(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=PROBE_TIMEOUT) as sock:
            return True
    except OSError as exc:
        print(f"probe failed: {exc}")
        return False


def send_telegram(text: str) -> None:
    token = os.environ.get("TG_BOT_TOKEN", "")
    chat_id = os.environ.get("TG_CHAT_ID", "")
    if not token or not chat_id:
        print("telegram not configured, skipping: " + text)
        return
    payload = json.dumps({"chat_id": chat_id, "text": text, "disable_web_page_preview": True}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status == 200:
                print("telegram sent")
    except (urllib.error.URLError, OSError) as exc:
        print(f"telegram send failed: {exc}")


def read_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
            if isinstance(data, dict):
                return data
            return {"state": str(data)}
    except (FileNotFoundError, json.JSONDecodeError):
        return {"state": "unknown"}


def write_state(state: dict) -> None:
    token = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    branch = os.environ.get("GITHUB_BRANCH", "main")
    if not token or not repo:
        print("no GITHUB_TOKEN/REPOSITORY, skipping state write")
        return

    content = base64.b64encode(json.dumps(state, ensure_ascii=False).encode()).decode()
    url = f"{API_BASE}/repos/{repo}/contents/{STATE_PATH}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "wallet-watchdog",
    }

    sha = None
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            sha = json.loads(resp.read().decode()).get("sha")
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            print(f"failed to read state file: {exc}")
            return

    body = json.dumps(
        {
            "message": f"watchdog state: {state.get('state')}",
            "content": content,
            "branch": branch,
            **({"sha": sha} if sha else {}),
        }
    ).encode()
    try:
        req = urllib.request.Request(url, data=body, headers=headers, method="PUT")
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
        print(f"state written: {state}")
    except urllib.error.HTTPError as exc:
        print(f"failed to write state file: {exc}")


def keepalive_due(state: dict) -> bool:
    today = datetime.now(timezone.utc).date()
    last = state.get("last_activity")
    if not last:
        return True
    try:
        last_date = datetime.strptime(last, "%Y-%m-%d").astimezone(timezone.utc).date()
    except ValueError:
        return True
    return (today - last_date).days >= KEEPALIVE_DAYS


def main() -> None:
    host = os.environ.get("SERVER_HOST", "172.86.119.96")
    port = int(os.environ.get("SERVER_PORT", "22"))
    ts = now_utc()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    state = read_state()
    up = probe(host, port)
    prev_state = state.get("state", "unknown")

    if up:
        if prev_state == "down":
            print(f"recovery detected at {ts}")
            send_telegram(f"🟢 Сервер {host}:{port} снова в сети ({ts})")
            state = {"state": "up", "last_activity": today}
            write_state(state)
        elif prev_state != "up":
            state = {"state": "up", "last_activity": today}
            write_state(state)
        elif keepalive_due(state):
            state["last_activity"] = today
            write_state(state)
        print(f"OK {host}:{port} reachable ({ts})")
    else:
        if prev_state == "up":
            print(f"server went down at {ts}")
            send_telegram(f"🔴 Сервер {host}:{port} НЕДОСТУПЕН ({ts})")
            state = {"state": "down", "last_activity": today}
            write_state(state)
        else:
            print(f"still down ({ts}), no repeat alert")


if __name__ == "__main__":
    main()