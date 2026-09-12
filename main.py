"""Monitor only the public, current-phase CodaBench leaderboard."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import Request, urlopen

COMPETITION_ID = 17698
BASE = "https://www.codabench.org"
PAGE = f"{BASE}/competitions/{COMPETITION_ID}/#results"


class MonitorError(Exception):
    pass


def request_json(url, payload=None):
    """Retry transient failures without including request URLs/secrets in errors."""
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
    for attempt in range(4):
        req = Request(url, data=data, headers={
            "User-Agent": "PWS-Cup-2026-Leaderboard-Monitor/1.0",
            "Accept": "application/json", "Content-Type": "application/json",
        })
        try:
            with urlopen(req, timeout=25) as response:
                raw = response.read()
                return json.loads(raw) if raw else None
        except HTTPError as exc:
            status = exc.code
            delay = 2 ** attempt
            if status == 429:
                try:
                    delay = min(30, max(1, float(json.loads(exc.read()).get("retry_after", delay))))
                except (ValueError, TypeError, AttributeError):
                    pass
            if status != 429 and status < 500:
                raise MonitorError(f"HTTP {status}; request rejected") from None
            if attempt == 3:
                raise MonitorError(f"HTTP {status}; retries exhausted") from None
        except (URLError, TimeoutError, OSError):
            if attempt == 3:
                raise MonitorError("Network request failed; retries exhausted") from None
            delay = 2 ** attempt
        except (ValueError, UnicodeError):
            raise MonitorError("Server returned invalid JSON") from None
        time.sleep(delay)


def current_phase(competition, now=None):
    phases = competition.get("phases")
    if not isinstance(phases, list):
        raise MonitorError("Competition response has no phase list")
    now = now or datetime.now(timezone.utc)
    candidates = []
    for phase in phases:
        try:
            start = datetime.fromisoformat(phase["start"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(phase["end"].replace("Z", "+00:00")) if phase.get("end") else None
        except (KeyError, TypeError, ValueError):
            raise MonitorError("Invalid phase dates") from None
        if start <= now and (end is None or now < end):
            candidates.append(phase)
    if len(candidates) > 1:
        marked = [p for p in candidates if p.get("status") == "Current"]
        if len(marked) == 1:
            return marked[0]
        raise MonitorError("Multiple active phases; refusing to select an arbitrary phase")
    return candidates[0] if candidates else None


def number(value):
    if value is None:
        return None
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise MonitorError("Invalid leaderboard score") from None
    if not result.is_finite():
        raise MonitorError("Non-finite leaderboard score")
    return "0" if result == 0 else format(result.normalize(), "f")


def snapshot(phase, board):
    if board.get("id") != phase["id"]:
        raise MonitorError("Leaderboard phase does not match the current phase")
    rows = board.get("submissions")
    if not isinstance(rows, list):
        raise MonitorError("Leaderboard response has no row list")
    if board.get("next") or board.get("count", len(rows)) != len(rows):
        raise MonitorError("Incomplete leaderboard response; state preserved")
    columns = {}
    for task in board.get("tasks", []):
        for col in task.get("columns", []):
            columns[f"{task['id']}:{col['key']}"] = col["title"]
    normalized = []
    occurrences = {}
    for rank, row in enumerate(rows, 1):
        org = row.get("organization")
        if org:
            identity = str(org.get("url") or org.get("id") or org["name"])
            name = org["name"]
        else:
            identity = row.get("slug_url") or row["owner"]
            name = row["owner"]
        # CodaBench prefixes queue labels with a changing submission ID.
        group = re.sub(r"^\d+_", "", row.get("queue_name") or "")
        base_key = json.dumps([identity, group], ensure_ascii=False)
        occurrence = occurrences.get(base_key, 0) + 1
        occurrences[base_key] = occurrence
        scores = {}
        for score in row["scores"]:
            key = f"{score['task_id']}:{score['column_key']}"
            if key in scores:
                raise MonitorError("Duplicate leaderboard score key")
            scores[key] = number(score["score"])
        normalized.append({
            "key": f"{base_key}:{occurrence}", "name": name,
            "rank": rank, "scores": scores, "submission_id": row["id"],
        })
    return {"phase_id": phase["id"], "phase_name": phase["name"],
            "columns": columns, "rows": normalized}


def fetch_snapshot():
    competition = request_json(f"{BASE}/api/competitions/{COMPETITION_ID}/")
    phase = current_phase(competition)
    if phase is None:
        return None
    # Use the same phase-specific endpoint as the official leaderboard UI.
    board = request_json(f"{BASE}/api/phases/{phase['id']}/get_leaderboard/?page_size=all")
    return snapshot(phase, board)


def safe_text(value):
    return re.sub(r"([\\`*_~>|])", r"\\\1", str(value)).replace("\n", " ")


def participant_label(row):
    """Enrich notification text without changing participant identity or diffing."""
    teams = json.loads(Path(__file__).with_name("teams.json").read_text())
    lookup = {}
    for team in teams:
        for account in [team.get("codabench"), *team.get("aliases", [])]:
            if account:
                lookup[account.casefold()] = team
    # Also support snapshots saved before team metadata was introduced.
    identity = json.loads(row["key"].rsplit(":", 1)[0])[0]
    account = unquote(urlsplit(identity).path.rstrip("/").rsplit("/", 1)[-1])
    team = lookup.get(account.casefold()) or lookup.get(row["name"].casefold())
    sid = row.get("submission_id")
    submission = str(sid) if sid is not None else "不明（旧保存データ）"
    if team:
        return (f"【コホート{team['cohort']}】{safe_text(team['team'])}"
                f"（CodaBench: {safe_text(row['name'])}／提出ID: {submission}）")
    return f"{safe_text(row['name'])}（コホート・チーム名未登録／提出ID: {submission}）"


def changes(before, after):
    old = {r["key"]: r for r in before["rows"]}
    new = {r["key"]: r for r in after["rows"]}
    lines = []
    for key, row in new.items():
        name = participant_label(row)
        if key not in old:
            scores = ", ".join(f"{safe_text(after['columns'].get(k, k))}: {v}" for k, v in row["scores"].items())
            lines.append(f"参加：{name}（{row['rank']}位） {scores}")
            continue
        prev = old[key]
        details = []
        if prev.get("submission_id") is not None and prev["submission_id"] != row.get("submission_id"):
            details.append(f"提出ID {prev['submission_id']} → {row['submission_id']}")
        if prev["rank"] != row["rank"]:
            details.append(f"順位 {prev['rank']}位 → {row['rank']}位")
        for col in sorted(prev["scores"].keys() | row["scores"].keys()):
            a, b = prev["scores"].get(col), row["scores"].get(col)
            if a != b:
                title = after["columns"].get(col, before["columns"].get(col, col))
                details.append(f"{safe_text(title)}: {a if a is not None else '—'} → {b if b is not None else '—'}")
        if details:
            lines.append(f"更新：{name}／" + "、".join(details))
    for key, row in old.items():
        if key not in new:
            lines.append(f"掲載終了：{participant_label(row)}（前回 {row['rank']}位）")
    return lines


def messages(before, after):
    lines = changes(before, after)
    if not lines:
        return []
    event_id = hashlib.sha256(json.dumps(after, sort_keys=True).encode()).hexdigest()[:10]
    heading = f"PWS Cup 2026｜{safe_text(after['phase_name'])}\n"
    if before["phase_id"] != after["phase_id"]:
        heading += f"フェーズ移行：{safe_text(before['phase_name'])} → {safe_text(after['phase_name'])}\n"
    footer = f"\n{PAGE}\n更新ID: {event_id}"
    chunks, current = [], ""
    # Bound by UTF-16 units as well as Unicode characters for Discord's limit.
    for line in lines:
        for start in range(0, len(line), 550):
            piece = line[start:start + 550]
            candidate = current + ("\n" if current else "") + piece
            if len((heading + candidate + footer).encode("utf-16-le")) // 2 > 1900:
                if current:
                    chunks.append(heading + current + footer)
                current = piece
            else:
                current = candidate
    if current:
        chunks.append(heading + current + footer)
    return chunks


def load_state(path):
    if not path.exists():
        return {"version": 1, "snapshot": None, "pending": None}
    try:
        state = json.loads(path.read_text())
        if state["version"] != 1 or not {"snapshot", "pending"} <= state.keys():
            raise ValueError()
        return state
    except (ValueError, KeyError, TypeError):
        raise MonitorError("Invalid saved state; refusing to replace baseline") from None


def save_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    temp.replace(path)


def send_discord(webhook, content):
    if not re.fullmatch(r"https://(?:discord\.com|discordapp\.com)/api(?:/v\d+)?/webhooks/\d+/[A-Za-z0-9._-]+", webhook):
        raise MonitorError("DISCORD_WEBHOOK_URL is missing or invalid")
    result = request_json(webhook + "?wait=true", {
        "content": content, "allowed_mentions": {"parse": []},
        "username": "PWS Cup 2026 Monitor",
    })
    if not isinstance(result, dict) or not result.get("id"):
        raise MonitorError("Discord did not confirm message creation")


def monitor(path, webhook, fetch=fetch_snapshot, send=send_discord):
    state = load_state(path)

    def flush():
        pending = state["pending"]
        if pending is None:
            return
        while pending["sent"] < len(pending["messages"]):
            send(webhook, pending["messages"][pending["sent"]])
            pending["sent"] += 1
            save_state(path, state)
        state["snapshot"] = pending["snapshot"]
        state["pending"] = None
        save_state(path, state)

    flush()
    current = fetch()
    if current is None:
        print("No active phase. Saved state preserved; no notification.")
        return
    print(f"Current phase: {current['phase_name']} ({current['phase_id']}); leaderboard rows: {len(current['rows'])}")
    if state["snapshot"] is None:
        state["snapshot"] = current
        save_state(path, state)
        print("Initial baseline saved. No Discord notification.")
        return
    outgoing = messages(state["snapshot"], current)
    if not outgoing:
        if state["snapshot"] != current:
            state["snapshot"] = current
            save_state(path, state)
        print("No score, rank, participant, or submission ID changes. No notification.")
        return
    state["pending"] = {"snapshot": current, "messages": outgoing, "sent": 0}
    save_state(path, state)
    flush()
    print(f"Discord confirmed {len(outgoing)} change notification(s). State saved.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, default=Path(".monitor/state.json"))
    parser.add_argument("--dry-run", action="store_true", help="Fetch and validate only; no writes or messages")
    args = parser.parse_args()
    try:
        if args.dry_run:
            current = fetch_snapshot()
            print(json.dumps(current, ensure_ascii=False, indent=2))
            print("Dry run OK. No state writes or Discord messages.")
        else:
            webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
            if not webhook:
                raise MonitorError("Set the DISCORD_WEBHOOK_URL Actions secret before starting the monitor")
            monitor(args.state, webhook)
        return 0
    except (MonitorError, KeyError, TypeError, ValueError, OSError) as exc:
        # Unexpected data errors must not dump remote response bodies or secrets.
        print(f"Monitor failed: {str(exc) if isinstance(exc, MonitorError) else type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
