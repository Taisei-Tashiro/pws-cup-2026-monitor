"""Monitor only the public, current-phase CodaBench leaderboard."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
import unicodedata
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import Request, urlopen

COMPETITION_ID = 17698
BASE = "https://www.codabench.org"
PAGE = f"{BASE}/competitions/{COMPETITION_ID}/#results"


class MonitorError(Exception):
    pass


def request_json(url, payload=None, attachment=None):
    """Retry transient failures without including request URLs/secrets in errors."""
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
    content_type = "application/json"
    if attachment:
        boundary = "pws-" + uuid.uuid4().hex
        filename = attachment["filename"]
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", filename):
            raise MonitorError("Invalid attachment filename")
        data = (f'--{boundary}\r\nContent-Disposition: form-data; name="payload_json"\r\n'
                'Content-Type: application/json\r\n\r\n').encode() + data
        data += (f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="files[0]"; '
                 f'filename="{filename}"\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n').encode()
        data += attachment["text"].encode("utf-8") + f"\r\n--{boundary}--\r\n".encode()
        content_type = f"multipart/form-data; boundary={boundary}"
    for attempt in range(4):
        req = Request(url, data=data, headers={
            "User-Agent": "PWS-Cup-2026-Leaderboard-Monitor/1.0",
            "Accept": "application/json", "Content-Type": content_type,
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


def team_metadata(row, phase_name=""):
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
    stage = "final" if "本戦" in phase_name else "preliminary" if "予備戦" in phase_name else None
    return team, team.get("cohorts", {}).get(stage) if team else None


def participant_label(row, previous=None, phase_name=""):
    team, cohort = team_metadata(row, phase_name)
    sid = row.get("submission_id")
    submission = str(sid) if sid is not None else "不明（旧保存データ）"
    if previous and previous.get("submission_id") is not None and previous["submission_id"] != sid:
        submission_line = f"提出ID {previous['submission_id']} → {submission}"
    else:
        submission_line = f"提出ID: {submission}"
    cohort_label = f"コホート{cohort}" if cohort is not None else "コホート未確認"
    title = (f"【{cohort_label}】{safe_text(team['team'])}" if team
             else f"{safe_text(row['name'])}（コホート・チーム名未登録）")
    return f"**{title}**\nCodaBench: {safe_text(row['name'])}\n{submission_line}"


def metric_title(snapshot, key):
    if "加工" in snapshot["phase_name"]:
        short = {"score_1": "総合U", "score_2": "U_gen", "score_3": "U_spec",
                 "score_4": "U_rare", "score_5": "U_valid", "score_6": "保護"}
        if key.rsplit(":", 1)[-1] in short:
            return safe_text(short[key.rsplit(":", 1)[-1]])
    return safe_text(snapshot["columns"].get(key, key))


def align_rows(before, after):
    """Match surviving submissions first when a participant has multiple rows."""
    old = {r["key"]: r for r in before["rows"]}
    available = dict(old)
    assigned = {}
    for index, row in enumerate(after["rows"]):
        base = row["key"].rsplit(":", 1)[0]
        for key, prior in list(available.items()):
            if (key.rsplit(":", 1)[0] == base and row.get("submission_id") is not None
                    and row["submission_id"] == prior.get("submission_id")):
                assigned[index] = key
                del available[key]
                break
    new = {}
    for index, row in enumerate(after["rows"]):
        base = row["key"].rsplit(":", 1)[0]
        key = assigned.get(index)
        if key is None:
            key = next((k for k in available if k.rsplit(":", 1)[0] == base), None)
            if key is not None:
                del available[key]
            else:
                key = f"{base}:new:{index}"
        new[key] = row
    return old, new


def changes(before, after):
    old, new = align_rows(before, after)
    blocks = []
    rank_only = 0
    for key, row in new.items():
        if key not in old:
            scores = [f"• {metric_title(after, k)}: {v if v is not None else '—'}"
                      for k, v in row["scores"].items()]
            blocks.append(f"🆕 参加：{participant_label(row, phase_name=after['phase_name'])}\n• 順位 {row['rank']}位\n" + "\n".join(scores))
            continue
        prev = old[key]
        id_changed = prev.get("submission_id") is not None and prev["submission_id"] != row.get("submission_id")
        rank_changed = prev["rank"] != row["rank"]
        details = []
        for col in sorted(prev["scores"].keys() | row["scores"].keys()):
            a, b = prev["scores"].get(col), row["scores"].get(col)
            if a != b:
                source = after if col in after["columns"] else before
                details.append(f"• {metric_title(source, col)}: {a if a is not None else '—'} → {b if b is not None else '—'}")
        if rank_changed and not id_changed and not details:
            rank_only += 1
        if id_changed or details:
            rank = f"• 順位 {prev['rank']}位 → {row['rank']}位" if rank_changed else f"• 順位 {row['rank']}位（変更なし）"
            if not details:
                details.append("• スコア：変更なし")
            blocks.append(f"🔄 更新：{participant_label(row, prev, after['phase_name'])}\n{rank}\n" + "\n".join(details))
    for key, row in old.items():
        if key not in new:
            same_phase = before["phase_id"] == after["phase_id"]
            reason = "取り下げ・掲載終了" if same_phase else "フェーズ移行により監視対象外"
            scores = [f"• 前回 {metric_title(before, k)}: {v if v is not None else '—'}"
                      for k, v in row["scores"].items()]
            note = "\nLeaderboardから掲載がなくなりました（理由はAPIでは判別できません）。" if same_phase else ""
            blocks.append(f"📤 {reason}：{participant_label(row, phase_name=before['phase_name'])}"
                          f"\n• 前回順位 {row['rank']}位\n" + "\n".join(scores) + note)
    if rank_only:
        blocks.append(f"📊 順位のみの変更：{rank_only}件（添付の全体一覧で確認できます）")
    return blocks


def leaderboard_document(before, after, checked_at):
    """Full current standings with display-width aligned Japanese pipe columns."""
    old, new = align_rows(before, after)
    columns = list(after["columns"])
    for row in after["rows"]:
        columns.extend(k for k in row["scores"] if k not in columns)
    plain = lambda v: " ".join(str(v).replace("|", "／").replace("｜", "／").split())
    width = lambda v: sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in v)
    def clip(value, limit):
        value = plain(value)
        if width(value) <= limit:
            return value
        result = ""
        for c in value:
            if width(result + c) > limit - 1:
                break
            result += c
        return result + "…"
    lines = ["PWS Cup 2026｜Leaderboard 全体一覧",
             f"フェーズ：{after['phase_name']}",
             f"確認時刻：{checked_at.astimezone(timezone(timedelta(hours=9))):%Y/%m/%d %H:%M:%S} JST（日本時間）",
             f"掲載件数：{len(after['rows'])}", "",
             "変動は前回確認時との比較。↑＝順位上昇、↓＝順位下降。新規＝新たに掲載された提出。", ""]
    table = [["順位", "変動", "更新", "コホート", "チーム名", "CodaBench", "提出ID"] +
             [metric_title(after, k).replace("\\", "") for k in columns]]
    legends = []
    for key, row in new.items():
        prior = old.get(key) if before["phase_id"] == after["phase_id"] else None
        team, cohort = team_metadata(row, after["phase_name"])
        name = team["team"] if team else "未登録"
        delta = prior["rank"] - row["rank"] if prior else None
        movement = "新規" if delta is None else f"↑{delta}" if delta > 0 else f"↓{-delta}" if delta < 0 else "—"
        updated = prior and (prior.get("submission_id") != row.get("submission_id") or prior["scores"] != row["scores"])
        status = "新規" if not prior else "提出/得点" if updated else "順位のみ" if delta else "—"
        short_name, short_account = clip(name, 28), clip(row['name'], 22)
        table.append([str(row['rank']), movement, status, str(cohort) if cohort is not None else "未確認",
                      short_name, short_account, str(row.get('submission_id', '不明'))] +
                     [row['scores'].get(k) if row['scores'].get(k) is not None else "—" for k in columns])
        if short_name != plain(name) or short_account != plain(row['name']):
            legends.append(f"順位{row['rank']}：{plain(name)}（CodaBench: {plain(row['name'])}）")
    widths = [max(width(plain(row[i])) for row in table) for i in range(len(table[0]))]
    for i, row in enumerate(table):
        lines.append(" ｜ ".join(plain(v) + " " * (widths[j] - width(plain(v))) for j, v in enumerate(row)))
        if i == 0:
            lines.append("─┼─".join("─" * w for w in widths))
    if not after['rows']:
        lines.append("現在掲載されている提出はありません。")
    removed = [row for key, row in old.items() if key not in new]
    if removed:
        lines += ["", "前回から掲載がなくなった提出（掲載終了理由は不明／フェーズ移行時は監視対象の変更）："]
        for row in removed:
            team, cohort = team_metadata(row, before['phase_name'])
            lines.append(f"前回{row['rank']}位 ｜ コホート{cohort if cohort is not None else '未確認'} ｜ "
                         f"{plain(team['team'] if team else row['name'])} ｜ 提出ID {row.get('submission_id', '不明')}")
    if legends:
        lines += ["", "省略した名称の全文：", *legends]
    lines += ["", f"Leaderboard：{PAGE}"]
    stamp = checked_at.astimezone(timezone(timedelta(hours=9))).strftime('%Y%m%d-%H%M%S')
    return {"filename": f"leaderboard-{stamp}-JST.txt", "text": "\n".join(lines) + "\n"}


def messages(before, after, checked_at=None):
    blocks = changes(before, after)
    if not blocks:
        return []
    checked_at = checked_at or datetime.now(timezone.utc)
    if checked_at.tzinfo is None:
        raise MonitorError("Check timestamp must include a timezone")
    checked_jst = checked_at.astimezone(timezone(timedelta(hours=9)))
    event_id = hashlib.sha256(json.dumps(after, sort_keys=True).encode()).hexdigest()[:10]
    heading = (f"**PWS Cup 2026｜Leaderboard更新**\n"
               f"フェーズ：{safe_text(after['phase_name'])}\n"
               f"確認時刻：{checked_jst:%Y/%m/%d %H:%M:%S} JST（日本時間）\n")
    if before["phase_id"] != after["phase_id"]:
        heading += f"フェーズ移行：{safe_text(before['phase_name'])} → {safe_text(after['phase_name'])}\n"
    heading += "\n"
    footer = f"\n\n📎 全チームの順位・スコアは添付一覧へ（分割時は最後の通知に添付）。\nLeaderboard：{PAGE}\n更新ID: {event_id}"
    units = lambda text: len(text.encode("utf-16-le")) // 2
    budget = 1900 - units(heading + footer)
    if budget < 100:
        raise MonitorError("Notification header too long")
    chunks, current = [], ""
    for block in blocks:
        # Keep each team together unless it alone exceeds Discord's limit.
        pieces = []
        if units(block) <= budget:
            pieces = [block]
        else:
            piece = ""
            for line in block.splitlines(keepends=True):
                if units(piece + line) <= budget:
                    piece += line
                    continue
                if piece:
                    pieces.append(piece.rstrip("\n"))
                    piece = ""
                for char in line:
                    if units(piece + char) > budget:
                        pieces.append(piece.rstrip("\n"))
                        piece = ""
                    piece += char
            if piece:
                pieces.append(piece.rstrip("\n"))
        for piece in pieces:
            candidate = current + ("\n\n" if current else "") + piece
            if units(candidate) > budget:
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


def send_discord(webhook, content, attachment=None):
    if not re.fullmatch(r"https://(?:discord\.com|discordapp\.com)/api(?:/v\d+)?/webhooks/\d+/[A-Za-z0-9._-]+", webhook):
        raise MonitorError("DISCORD_WEBHOOK_URL is missing or invalid")
    result = request_json(webhook + "?wait=true", {
        "content": content, "allowed_mentions": {"parse": []},
        "username": "PWS Cup 2026 Monitor",
    }, attachment=attachment)
    if not isinstance(result, dict) or not result.get("id"):
        raise MonitorError("Discord did not confirm message creation")


def monitor(path, webhook, fetch=fetch_snapshot, send=send_discord):
    state = load_state(path)

    def flush():
        pending = state["pending"]
        if pending is None:
            return
        while pending["sent"] < len(pending["messages"]):
            message = pending["messages"][pending["sent"]]
            if pending.get("attachment") and pending["sent"] == len(pending["messages"]) - 1:
                send(webhook, message, attachment=pending["attachment"])
            else:
                send(webhook, message)
            pending["sent"] += 1
            save_state(path, state)
        state["snapshot"] = pending["snapshot"]
        state["pending"] = None
        save_state(path, state)

    flush()
    current = fetch()
    checked_at = datetime.now(timezone.utc)
    if current is None:
        print("No active phase. Saved state preserved; no notification.")
        return
    print(f"Current phase: {current['phase_name']} ({current['phase_id']}); leaderboard rows: {len(current['rows'])}")
    if state["snapshot"] is None:
        state["snapshot"] = current
        save_state(path, state)
        print("Initial baseline saved. No Discord notification.")
        return
    outgoing = messages(state["snapshot"], current, checked_at=checked_at)
    if not outgoing:
        if state["snapshot"] != current:
            state["snapshot"] = current
            save_state(path, state)
        print("No score, rank, participant, or submission ID changes. No notification.")
        return
    state["pending"] = {"snapshot": current, "messages": outgoing, "sent": 0,
                        "attachment": leaderboard_document(state["snapshot"], current, checked_at)}
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
