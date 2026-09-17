import copy
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, MagicMock, patch
from urllib.error import HTTPError, URLError
import watchdog as w

NOW = datetime(2026, 9, 13, 7, tzinfo=timezone.utc)
RUN = dict(id=12, head_branch='main', event='workflow_dispatch', path=w.WORKFLOW_PATH,
           status='in_progress', run_attempt=1)
JOB = dict(name='monitor', status='queued', runner_id=0, runner_name='', steps=[],
           created_at=(NOW-timedelta(minutes=11)).isoformat())

class WatchdogTests(unittest.TestCase):
    def test_eligible_with_completed_watchdog(self):
        self.assertTrue(w.candidate(RUN, [JOB, dict(name='watchdog', status='completed')], 99, NOW))

    def test_age_boundary(self):
        for seconds, expected in [(599, False), (600, True)]:
            self.assertEqual(w.candidate(RUN, [dict(JOB, created_at=(NOW-timedelta(seconds=seconds)).isoformat())], 99, NOW), expected)

    def test_started_assigned_or_incomplete_jobs_are_protected(self):
        for change in [dict(runner_id=42), dict(runner_name='runner'), dict(status='in_progress'),
                       dict(steps=[dict(name='Set up job')]), dict(steps=None), dict(status='completed')]:
            with self.subTest(change=change):
                self.assertFalse(w.candidate(RUN, [dict(JOB, **change)], 99, NOW))

    def test_scope_attempt_current_and_completed_runs_excluded(self):
        for change in [dict(id=99), dict(head_branch='other'), dict(path='other.yml'), dict(event='push'),
                       dict(run_attempt=2), dict(status='completed')]:
            self.assertFalse(w.candidate(dict(RUN, **change), [JOB], 99, NOW))

    def test_unknown_missing_running_sibling_jobs_fail_closed(self):
        for jobs in [[], [JOB, JOB], [dict(JOB, name='other')],
                     [JOB, dict(name='other', status='completed')],
                     [JOB, dict(name='watchdog', status='in_progress')]]:
            self.assertFalse(w.candidate(RUN, jobs, 99, NOW))

    def test_isolated_probe_uses_one_minute_only_for_exact_label(self):
        for seconds, expected in [(59, False), (60, True)]:
            job = dict(JOB, labels=[w.PROBE_LABEL], created_at=(NOW-timedelta(seconds=seconds)).isoformat())
            self.assertEqual(w.candidate(RUN, [job], 99, NOW), expected)
        self.assertFalse(w.candidate(RUN, [dict(JOB, labels=["ubuntu-latest"], created_at=(NOW-timedelta(seconds=60)).isoformat())], 99, NOW))

    def test_invalid_date(self):
        self.assertFalse(w.candidate(RUN, [dict(JOB, created_at='bad')], 99, NOW))

    def api(self, fresh_job=None, fresh_run=None, post=None):
        api = Mock()
        reads = []
        def collection(path, key):
            if key == 'workflow_runs':
                return [RUN] if 'status=in_progress' in path else []
            reads.append(path)
            return [copy.deepcopy(JOB if len(reads)==1 else (fresh_job or JOB))]
        api.collection.side_effect = collection
        api.request.side_effect = lambda path, method='GET': post if method=='POST' else copy.deepcopy(fresh_run or RUN)
        return api

    def posts(self, api):
        return [c for c in api.request.call_args_list if c.kwargs.get('method')=='POST']

    def test_normal_cancel_once_no_force_or_redispatch(self):
        api = self.api()
        self.assertEqual(w.recover(api, 99, NOW), [12])
        self.assertEqual(len(self.posts(api)), 1)
        self.assertEqual(self.posts(api)[0].args[0], '/actions/runs/12/cancel')

    def test_start_between_checks_protected(self):
        api = self.api(fresh_job=dict(JOB, runner_id=5, status='in_progress'))
        self.assertEqual(w.recover(api, 99, NOW), [])
        self.assertEqual(self.posts(api), [])

    def test_retry_between_checks_protected(self):
        api = self.api(fresh_run=dict(RUN, run_attempt=2))
        self.assertEqual(w.recover(api, 99, NOW), [])
        self.assertEqual(self.posts(api), [])

    def test_dry_run_read_only(self):
        api = self.api()
        self.assertEqual(w.recover(api, 99, NOW, dry_run=True), [])
        self.assertEqual(self.posts(api), [])

    def test_conflict_not_retried(self):
        api = self.api(post=dict(conflict=True))
        self.assertEqual(w.recover(api, 99, NOW), [])
        self.assertEqual(len(self.posts(api)), 1)

    def test_read_failure_no_mutation(self):
        api = self.api()
        api.request.side_effect = w.WatchdogError('read failed')
        with self.assertRaises(w.WatchdogError):
            w.recover(api, 99, NOW)
        self.assertEqual(self.posts(api), [])

    def test_pagination_and_incomplete_results(self):
        api = w.GitHub('unused')
        api.request = Mock(side_effect=[dict(total_count=2, jobs=[dict(id=1)]), dict(total_count=2, jobs=[dict(id=2)])])
        self.assertEqual(len(api.collection('/actions/runs/12/jobs?filter=latest', 'jobs')), 2)
        api.request = Mock(return_value=dict(total_count=2, jobs=[]))
        with self.assertRaises(w.WatchdogError):
            api.collection('/actions/runs/12/jobs', 'jobs')

    def test_run_finishes_while_status_list_is_assembled(self):
        api = w.GitHub('unused')
        api.request = Mock(return_value=dict(total_count=1, workflow_runs=[]))
        self.assertEqual(api.collection('/actions/runs?status=in_progress', 'workflow_runs'), [])
        self.assertEqual(api.request.call_count, 1)

    def test_changing_run_count_does_not_discard_discovered_runs(self):
        api = w.GitHub('unused')
        api.request = Mock(return_value=dict(total_count=2, workflow_runs=[RUN]))
        self.assertEqual(api.collection('/actions/runs?status=queued', 'workflow_runs'), [RUN])
        self.assertEqual(api.request.call_count, 1)

    def test_full_run_page_does_not_trust_stale_count(self):
        api = w.GitHub('unused')
        page = [dict(RUN, id=i) for i in range(100)]
        api.request = Mock(side_effect=[dict(total_count=1, workflow_runs=page),
                                       dict(total_count=101, workflow_runs=[dict(RUN, id=100)])])
        runs = api.collection('/actions/runs?status=queued', 'workflow_runs')
        self.assertEqual(len(runs), 101)
        self.assertIn('page=2', api.request.call_args.args[0])

    def test_malformed_run_list_is_still_an_error(self):
        api = w.GitHub('unused')
        api.request = Mock(return_value=dict(total_count=1, workflow_runs=None))
        with self.assertRaises(w.WatchdogError):
            api.collection('/actions/runs?status=queued', 'workflow_runs')

    @patch('watchdog.time.sleep')
    def test_transient_reads_retry_then_succeed(self, sleep):
        api = w.GitHub('private-test-token')
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"jobs": []}'
        api.opener.open = Mock(side_effect=[HTTPError('https://api.github.com', 503, 'unavailable', {}, None),
                                           URLError('temporary'), response])
        self.assertEqual(api.request('/actions/runs/12/jobs'), {'jobs': []})
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [1, 2])

    @patch('watchdog.time.sleep')
    def test_persistent_failure_still_fails_without_leaking_details(self, sleep):
        api = w.GitHub('private-test-token')
        api.opener.open = Mock(side_effect=URLError('private-test-token'))
        with self.assertRaises(w.WatchdogError) as error:
            api.request('/actions/runs')
        self.assertNotIn('private-test-token', str(error.exception))
        self.assertEqual(api.opener.open.call_count, 3)

    @patch('watchdog.time.sleep')
    def test_no_retry_for_cancel_authentication_or_rate_limit(self, sleep):
        for method, code in [('POST', 503), ('GET', 401), ('GET', 403), ('GET', 429)]:
            api = w.GitHub('unused')
            api.opener.open = Mock(side_effect=HTTPError('https://api.github.com', code, 'error', {}, None))
            with self.subTest(method=method, code=code), self.assertRaises(w.WatchdogError):
                api.request('/actions/runs/12/cancel' if method == 'POST' else '/actions/runs', method=method)
            self.assertEqual(api.opener.open.call_count, 1)
        sleep.assert_not_called()

if __name__ == '__main__':
    unittest.main()
