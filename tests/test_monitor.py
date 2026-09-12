import copy
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import main


def phase(pid=1):
    return {"id": pid, "name": "本戦：加工フェーズ", "start": "2026-09-12T00:00:00Z", "end": "2026-09-25T00:00:00Z"}


def board():
    return {
        "id": 1, "count": 2, "next": None,
        "tasks": [{"id": 10, "columns": [{"key": "score_1", "title": "得点"}]}],
        "submissions": [
            {"id": sid, "owner": name, "slug_url": f"/user/{name}",
             "queue_name": f"{sid}_Studio", "created_when": "ignored",
             "scores": [{"task_id": 10, "column_key": "score_1", "score": score}]}
            for sid, name, score in [(100, "Alice", "0.9"), (200, "Bob", "0.8")]
        ],
    }


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.json"
        self.initial = main.snapshot(phase(), board())
        self.sent = []

    def run_monitor(self, current, sender=None):
        main.monitor(self.path, "not-a-real-secret", fetch=lambda: current,
                     send=sender or (lambda hook, msg: self.sent.append(msg)))

    def test_current_phase_boundary_and_gap(self):
        comp = {"phases": [phase()]}
        self.assertEqual(main.current_phase(comp, datetime(2026, 9, 12, tzinfo=timezone.utc))["id"], 1)
        self.assertIsNone(main.current_phase(comp, datetime(2026, 9, 25, tzinfo=timezone.utc)))

    def test_overlapping_phases_are_not_guessed(self):
        comp = {"phases": [phase(), phase(2)]}
        with self.assertRaises(main.MonitorError):
            main.current_phase(comp, datetime(2026, 9, 13, tzinfo=timezone.utc))
        comp["phases"][1]["status"] = "Current"
        self.assertEqual(main.current_phase(comp, datetime(2026, 9, 13, tzinfo=timezone.utc))["id"], 2)

    def test_empty_first_run_is_silent_and_new_team_notifies(self):
        empty = copy.deepcopy(self.initial)
        empty["rows"] = []
        self.run_monitor(empty)
        self.assertEqual(self.sent, [])
        self.run_monitor(self.initial)
        self.assertIn("参加：Alice", self.sent[0])

    def test_initial_and_unchanged_are_silent(self):
        self.run_monitor(self.initial)
        original = self.path.read_bytes()
        self.run_monitor(self.initial)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.path.read_bytes(), original)

    def test_time_and_numeric_format_ignored(self):
        other = board()
        other["submissions"][0].update(created_when="new")
        other["submissions"][0]["scores"][0]["score"] = "0.9000"
        changed = main.snapshot(phase(), other)
        self.assertEqual(main.changes(self.initial, changed), [])
        self.assertEqual(changed['rows'][0]['submission_id'], 100)

    def test_submission_id_change_alone_notifies_once(self):
        self.run_monitor(self.initial)
        other = board()
        other['submissions'][0].update(id=999, queue_name='999_Studio')
        changed = main.snapshot(phase(), other)
        self.run_monitor(changed)
        self.run_monitor(changed)
        self.assertEqual(len(self.sent), 1)
        self.assertIn('提出ID 100 → 999', self.sent[0])
        self.assertNotIn('参加：', self.sent[0])

    def test_cohort_team_and_current_submission_id_in_all_notices(self):
        raw = board()
        raw['submissions'][0].update(owner='zhiyan', slug_url='/profiles/user/zhiyan/')
        old = main.snapshot(phase(), raw)
        raw['submissions'][0].update(id=999, queue_name='999_Studio')
        raw['submissions'][0]['scores'][0]['score'] = '0.95'
        new = main.snapshot(phase(), raw)
        empty = copy.deepcopy(new)
        empty['rows'] = []
        for before, after in [(old, new), (empty, new), (new, empty)]:
            text = '\n'.join(main.changes(before, after))
            self.assertIn('【コホート5】ステテコは恥だが役に立つ', text)
            self.assertIn('CodaBench: zhiyan／提出ID: 999', text)

    def test_display_names_and_verified_account_aliases(self):
        for account, display, cohort in [('ryoga_sasaki','R',1), ('bank_san','harunaDan@GU',2),
                                         ('tani_shumma','Shum',3), ('unfrozen','unfrozen',18)]:
            raw = board()
            raw['submissions'][0].update(owner=display, slug_url=f'/profiles/user/{account}/')
            row = main.snapshot(phase(), raw)['rows'][0]
            self.assertIn(f'【コホート{cohort}】', main.participant_label(row))

    def test_old_saved_rows_upgrade_silently(self):
        old = copy.deepcopy(self.initial)
        for row in old['rows']:
            row.pop('submission_id')
        self.run_monitor(old)
        self.run_monitor(self.initial)
        self.assertEqual(self.sent, [])
        self.assertEqual(main.load_state(self.path)['snapshot'], self.initial)

    def test_unknown_participant_is_not_assigned_a_team(self):
        label = main.participant_label(self.initial['rows'][0])
        self.assertIn('コホート・チーム名未登録', label)
        self.assertIn('提出ID: 100', label)

    def test_score_change_notifies_once(self):
        self.run_monitor(self.initial)
        changed = copy.deepcopy(self.initial)
        changed["rows"][0]["scores"]["10:score_1"] = "0.95"
        self.run_monitor(changed)
        self.run_monitor(changed)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("0.9 → 0.95", self.sent[0])

    def test_rank_change_is_detected(self):
        raw = board()
        raw["submissions"].reverse()
        output = main.messages(self.initial, main.snapshot(phase(), raw))
        self.assertIn("順位 2位 → 1位", output[0])

    def test_removed_team(self):
        changed = copy.deepcopy(self.initial)
        changed["rows"].pop()
        self.assertIn("掲載終了：Bob", main.messages(self.initial, changed)[0])

    def test_no_active_phase_preserves_state(self):
        self.run_monitor(self.initial)
        original = self.path.read_bytes()
        self.run_monitor(None)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.sent, [])

    def test_phase_change_updates_baseline_without_false_empty_alert(self):
        empty = copy.deepcopy(self.initial)
        empty["rows"] = []
        self.run_monitor(empty)
        empty["phase_id"] = 2
        self.run_monitor(empty)
        self.assertEqual(self.sent, [])
        self.assertEqual(main.load_state(self.path)["snapshot"]["phase_id"], 2)

    def test_wrong_phase_and_incomplete_pages_rejected(self):
        for key, value in [("id", 7), ("count", 3), ("next", "next-page")]:
            raw = board()
            raw[key] = value
            with self.subTest(key=key), self.assertRaises(main.MonitorError):
                main.snapshot(phase(), raw)

    def test_failure_preserves_baseline_and_retries_pending(self):
        self.run_monitor(self.initial)
        changed = copy.deepcopy(self.initial)
        changed["rows"][0]["scores"]["10:score_1"] = "0.99"
        def fail(*args):
            raise main.MonitorError("simulated failure")
        with self.assertRaises(main.MonitorError):
            self.run_monitor(changed, fail)
        state = main.load_state(self.path)
        self.assertEqual(state["snapshot"], self.initial)
        self.assertEqual(state["pending"]["sent"], 0)
        self.run_monitor(changed)
        self.assertEqual(len(self.sent), 1)
        self.assertIsNone(main.load_state(self.path)["pending"])

    def test_partial_delivery_resumes_after_last_confirmed_message(self):
        main.save_state(self.path, {"version": 1, "snapshot": self.initial,
            "pending": {"snapshot": self.initial, "messages": ["first", "second"], "sent": 0}})
        def partial(hook, msg):
            if msg == "second":
                raise main.MonitorError("simulated failure")
            self.sent.append(msg)
        with self.assertRaises(main.MonitorError):
            self.run_monitor(self.initial, partial)
        self.run_monitor(self.initial)
        self.assertEqual(self.sent, ["first", "second"])

    def test_corrupt_state_is_not_replaced(self):
        self.path.write_text("broken")
        with self.assertRaises(main.MonitorError):
            self.run_monitor(self.initial)
        self.assertEqual(self.path.read_text(), "broken")

    def test_discord_limits_and_mentions(self):
        changed = copy.deepcopy(self.initial)
        changed["rows"][0]["name"] = "@everyone " + "😀" * 4000
        empty = copy.deepcopy(self.initial)
        empty["rows"] = []
        chunks = main.messages(empty, changed)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(m.encode("utf-16-le")) // 2 <= 2000 for m in chunks))
        with patch("main.request_json", return_value={"id": "1"}) as request:
            main.send_discord("https://discord.com/api/webhooks/123/fake-token", chunks[0])
        self.assertEqual(request.call_args.args[1]["allowed_mentions"], {"parse": []})

    def test_webhook_rejects_other_hosts_without_request(self):
        with patch("main.request_json") as request:
            with self.assertRaises(main.MonitorError):
                main.send_discord("https://example.org/webhooks/secret", "test")
        request.assert_not_called()

    def test_fetch_uses_current_phase_endpoint_only(self):
        with patch("main.request_json", side_effect=[{"phases": [phase()]}, board()]) as request:
            with patch("main.current_phase", return_value=phase()):
                main.fetch_snapshot()
        self.assertEqual(request.call_args.args[0], "https://www.codabench.org/api/phases/1/get_leaderboard/?page_size=all")


if __name__ == "__main__":
    unittest.main()
