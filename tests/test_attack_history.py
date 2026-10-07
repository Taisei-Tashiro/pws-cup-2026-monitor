import copy
import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import main


def attack(sid=100, pid=900, account='gghatano', display=None, queue='Studio'):
    return main.snapshot({'id': pid, 'name': '本戦：攻撃フェーズ'}, {
        'id': pid, 'count': 1, 'next': None, 'tasks': [],
        'submissions': [{'id': sid, 'owner': display or account,
                         'slug_url': f'/profiles/user/{account}/',
                         'queue_name': f'{sid}_{queue}', 'scores': []}]})


class AttackHistoryTests(unittest.TestCase):
    def setUp(self):
        self.seed = main.load_attack_seed()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'state.json'
        self.sent = []
        self.seed_patch = patch('main.load_attack_seed', return_value={})
        self.seed_patch.start()
        self.addCleanup(self.seed_patch.stop)

    def run_monitor(self, snap, send=None):
        main.monitor(self.path, 'fake', fetch=lambda: snap,
                     send=send or (lambda hook, msg, **kw: self.sent.append(msg)))

    def history(self):
        return main.load_state(self.path)['attack_history']

    def test_new_id_increments_once_and_score_changes_keep_same_ordinal(self):
        self.run_monitor(attack())
        second = attack(200)
        self.run_monitor(second)
        self.run_monitor(second)
        changed = copy.deepcopy(second)
        changed['rows'][0]['scores'] = {'10:score_1': '0.99'}
        self.run_monitor(changed)
        self.assertEqual(self.history()['900']['team:gghatano'], [100, 200])
        self.assertEqual(len(self.sent), 2)
        self.assertTrue(all('攻撃提出：累計2件目' in msg for msg in self.sent))

    def test_withdrawal_and_old_id_reappearance_keep_count(self):
        self.run_monitor(attack())
        self.run_monitor(attack(200))
        empty = attack(200)
        empty['rows'] = []
        self.run_monitor(empty)
        self.run_monitor(attack())
        self.assertEqual(self.history()['900']['team:gghatano'], [100, 200])
        self.assertIn('1件目の再掲載・継続／累計2件', self.sent[-1])

    def test_aliases_multiple_queues_and_repeated_rows_share_team_count(self):
        snap = attack(100, account='ryoga_sasaki', display='R')
        other = attack(200, account='ryoga_sasaki', display='renamed', queue='Other')
        snap['rows'].extend([other['rows'][0], copy.deepcopy(snap['rows'][0])])
        history = {}
        main.record_attack_submissions(history, snap)
        self.assertEqual(history['900'], {'team:r': [100, 200]})
        self.assertEqual(main.attack_progress(other['rows'][0], 900, history), (2, 2))

    def test_seed_backfills_legacy_state_silently_and_next_id_is_fourth(self):
        current = attack(965694, pid=29526)
        main.save_state(self.path, {'version': 1, 'snapshot': current, 'pending': None})
        with patch('main.load_attack_seed', return_value=self.seed):
            self.run_monitor(current)
            self.assertEqual(self.sent, [])
            self.assertEqual(self.history()['29526']['team:gghatano'], [950932, 961110, 965694])
            self.run_monitor(attack(999999, pid=29526))
        self.assertIn('攻撃提出：累計4件目', self.sent[0])
        self.assertEqual(self.history()['29526']['team:gghatano'], [950932, 961110, 965694, 999999])

    def test_delivery_failure_and_retry_preserve_count_and_original_message(self):
        self.run_monitor(attack())
        def fail(*args, **kwargs):
            raise main.MonitorError('simulated delivery failure')
        with self.assertRaises(main.MonitorError):
            self.run_monitor(attack(200), send=fail)
        state = main.load_state(self.path)
        pending = state['pending']['messages'][:]
        self.assertEqual(state['attack_history']['900']['team:gghatano'], [100, 200])
        self.run_monitor(attack(200))
        self.assertEqual(self.sent, pending)
        self.assertEqual(self.history()['900']['team:gghatano'], [100, 200])

    def test_phase_counts_are_separate_and_processing_does_not_contribute(self):
        self.run_monitor(attack())
        processing = attack(200)
        processing.update(phase_id=899, phase_name='本戦：加工フェーズ')
        self.run_monitor(processing)
        self.run_monitor(attack(300, pid=901))
        self.assertNotIn('899', self.history())
        self.assertEqual(self.history()['900']['team:gghatano'], [100])
        self.assertEqual(self.history()['901']['team:gghatano'], [300])
        self.assertIn('攻撃提出：累計1件目', self.sent[-1])

    def test_current_document_includes_ordinal_and_removed_team_totals(self):
        current = attack(200)
        history = {'900': {'team:gghatano': [100, 200], 'team:r': [150]}}
        doc = main.leaderboard_document(attack(), current,
            datetime(2026, 10, 7, tzinfo=timezone.utc), attack_history=history)['text']
        self.assertIn('攻撃提出（件目/累計）', doc)
        self.assertIn('2/2', doc)
        self.assertIn('12 ｜ Ritz ｜ 1件', doc)
        self.assertIn('15 ｜ さいふぉん-3/siphon-3 ｜ 2件', doc)
        self.assertIn('28 ｜ ステテコは恥だが役に立つ ｜ 0件', doc)
        self.assertIn('アカウント未確認', doc)

    def test_corrupt_history_stops_without_replacing_saved_state(self):
        state = {'version': 1, 'snapshot': attack(), 'pending': None,
                 'attack_history': {'900': {'team:gghatano': [100, 100]}}}
        self.path.write_text(json.dumps(state))
        original = self.path.read_bytes()
        with self.assertRaises(main.MonitorError):
            self.run_monitor(attack(200))
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.sent, [])

    def test_unknown_account_is_counted_without_guessing_team(self):
        snap = attack(account='unknown')
        self.run_monitor(snap)
        self.run_monitor(attack(200, account='unknown', display='new name'))
        self.assertEqual(self.history()['900']['participant:/profiles/user/unknown/'], [100, 200])
        self.assertIn('コホート・チーム名未登録', self.sent[-1])
        self.assertIn('攻撃提出：累計2件目', self.sent[-1])


if __name__ == '__main__':
    unittest.main()
