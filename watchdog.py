"""Release stale, unassigned monitor jobs; never force-cancel a running job."""
import json
import os
import re
import sys
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.request import Request, build_opener, HTTPRedirectHandler

REPOSITORY = "Taisei-Tashiro/pws-cup-2026-monitor"
WORKFLOW_PATH = ".github/workflows/monitor-scheduled.yml"
MIN_WAIT_SECONDS = 600
PROBE_LABEL = "pws-cup-unassigned-recovery-probe"
ACTIVE_STATUSES = {"queued", "in_progress", "pending"}


class WatchdogError(RuntimeError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GitHub:
    def __init__(self, token):
        self.token = token
        self.opener = build_opener(NoRedirect())

    def request(self, path, method="GET"):
        if not path.startswith("/") or ".." in path:
            raise WatchdogError("Invalid API path")
        request = Request(
            "https://api.github.com/repos/" + REPOSITORY + path,
            method=method,
            data=b"{}" if method == "POST" else None,
            headers={"Authorization": "Bearer " + self.token,
                     "Accept": "application/vnd.github+json",
                     "Content-Type": "application/json",
                     "X-GitHub-Api-Version": "2026-03-10",
                     "User-Agent": "pws-cup-monitor-watchdog"})
        try:
            with self.opener.open(request, timeout=15) as response:
                raw = response.read()
                return json.loads(raw) if raw else None
        except HTTPError as exc:
            if method == "POST" and exc.code == 409:
                return {"conflict": True}
            # Do not include response bodies or credentials in logs.
            raise WatchdogError(f"GitHub API {method} returned HTTP {exc.code}") from None

    def collection(self, path, key):
        result = []
        for page in range(1, 11):
            sep = "&" if "?" in path else "?"
            payload = self.request(f"{path}{sep}per_page=100&page={page}")
            batch = payload.get(key)
            if not isinstance(batch, list):
                raise WatchdogError("Incomplete GitHub API response")
            result.extend(batch)
            if len(result) >= payload.get("total_count", len(result)):
                return result
            if not batch:
                raise WatchdogError(f"Incomplete GitHub API pagination: {path}; total={payload.get('total_count')}; received={len(result)}; page={page}")
        raise WatchdogError("GitHub API pagination limit exceeded")


def timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def unassigned(job):
    return (job.get("status") in {"queued", "pending", "waiting"}
            and job.get("runner_id") in (None, 0)
            and not job.get("runner_name") and job.get("steps") == [])


def candidate(run, jobs, current_run_id, now):
    """Fail closed on unknown jobs, assigned runners, started steps and retries."""
    if (run.get("id") == current_run_id or run.get("head_branch") != "main"
            or run.get("event") != "workflow_dispatch"
            or run.get("path") != WORKFLOW_PATH
            or run.get("status") not in ACTIVE_STATUSES
            or run.get("run_attempt") != 1 or not jobs):
        return False
    monitor = [job for job in jobs if job.get("name") == "monitor"]
    if len(monitor) != 1 or not unassigned(monitor[0]):
        return False
    try:
        threshold = 60 if monitor[0].get("labels") == [PROBE_LABEL] else MIN_WAIT_SECONDS
        if (now - timestamp(monitor[0]["created_at"])).total_seconds() < threshold:
            return False
    except (KeyError, TypeError, ValueError):
        return False
    for job in jobs:
        if job.get("name") not in {"monitor", "watchdog"}:
            return False
        if job.get("name") == "watchdog" and not (
                job.get("status") == "completed" or unassigned(job)):
            return False
    return True


def recover(api, current_run_id, now=None, dry_run=False):
    now = now or datetime.now(timezone.utc)
    runs = {}
    for status in sorted(ACTIVE_STATUSES):
        for run in api.collection(f"/actions/runs?branch=main&event=workflow_dispatch&status={status}", "workflow_runs"):
            if run.get("path") == WORKFLOW_PATH and run.get("id") != current_run_id:
                runs[run["id"]] = run
    cancelled = []
    for run_id, run in sorted(runs.items()):
        jobs_path = f"/actions/runs/{run_id}/jobs?filter=latest"
        jobs = api.collection(jobs_path, "jobs")
        if not candidate(run, jobs, current_run_id, now):
            continue
        # Recheck immediately before the normal cancellation request. The monitor
        # job also protects its transaction from normal cancellation after start.
        fresh_run = api.request(f"/actions/runs/{run_id}")
        fresh_jobs = api.collection(jobs_path, "jobs")
        if not candidate(fresh_run, fresh_jobs, current_run_id, now):
            print(f"Run {run_id}: state changed; left untouched.")
            continue
        if dry_run:
            print(f"Run {run_id}: stale unassigned monitor (read-only check).")
            continue
        result = api.request(f"/actions/runs/{run_id}/cancel", method="POST")
        if isinstance(result, dict) and result.get("conflict"):
            print(f"Run {run_id}: cancellation conflict; no retry or force cancel.")
            continue
        cancelled.append(run_id)
        print(f"Run {run_id}: requested cancellation of stale unassigned monitor (production: 10 minutes; isolated probe: 1 minute).")
        if len(cancelled) >= 3:
            break
    print(f"Watchdog checked {len(runs)} other active run(s); requested {len(cancelled)} cancellation(s).")
    return cancelled


def main():
    if os.environ.get("GITHUB_REPOSITORY") != REPOSITORY:
        raise WatchdogError("Unexpected repository")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    if not re.fullmatch(r"[0-9]+", run_id):
        raise WatchdogError("Invalid current run ID")
    token = os.environ.get("GH_TOKEN")
    if not token:
        raise WatchdogError("Missing GitHub token")
    cancelled = recover(GitHub(token), int(run_id), dry_run=os.environ.get("WATCHDOG_DRY_RUN") == "true")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as file:
            file.write("## 自動復旧チェック\n\n")
            file.write("解除要求: " + (", ".join(str(i) for i in cancelled) or "なし") + "\n")
            file.write("10分以上未開始・runner未割り当ての監視のみ対象。実行中の監視は対象外。\n")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        reason = str(exc) if isinstance(exc, WatchdogError) else type(exc).__name__
        print(f"Watchdog failed: {reason}", file=sys.stderr)
        sys.exit(1)
