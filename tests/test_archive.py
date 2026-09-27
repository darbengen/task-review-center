"""Archive contracts use isolated stores and explicit host acknowledgements."""
import concurrent.futures
import importlib.util
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('archive_server', Path(__file__).parents[1] / 'scripts/server.py')
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = server.Store(self.temp.name)
        self.task = server.normalize({'id': 'fixture', 'name': '待审查样本', 'updatedAt': 3})
        self.store.sync([self.task])
        self.calls = []

    def tearDown(self):
        self.temp.cleanup()

    def archive(self, **overrides):
        params = dict(thread_id='fixture', revision=0, updated_at=3, confirmed=True, commit=self.calls.append)
        params.update(overrides)
        return self.store.archive(**params)

    def test_explicit_second_confirmation_required(self):
        for value in (False, None, 'true', 1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.archive(confirmed=value)
        self.assertEqual(self.calls, [])
        self.assertFalse(self.store.snapshot()['tasks'][0]['archived'])

    def test_archive_preserves_review_note_star_and_restart(self):
        self.store.update('fixture', 0, note='已保存的备注', starred=True)
        before = self.store.read()['reviews']
        result = self.archive(revision=1)
        self.assertTrue(result['archived'])
        self.assertEqual(self.calls, [self.task])
        after = server.Store(self.temp.name).snapshot()['tasks'][0]
        self.assertTrue(after['archived'])
        before['fixture']['revision'] += 1
        self.assertEqual(self.store.read()['reviews'], before)
        self.assertEqual(after['review']['status'], 'pending')
        self.assertEqual(after['review']['note'], '已保存的备注')
        self.assertTrue(after['review']['starred'])

    def test_concurrent_duplicate_archive_calls_host_once(self):
        def run(_):
            return server.Store(self.temp.name).archive('fixture', 0, 3, True, self.calls.append)
        with concurrent.futures.ThreadPoolExecutor() as pool:
            results = list(pool.map(run, range(6)))
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(all(r['archived'] for r in results))

    def test_stale_revision_and_completed_task_are_rejected(self):
        self.store.mark('fixture', 'done', 0)
        for revision in (0, 1, True):
            with self.subTest(revision=revision), self.assertRaises(ValueError):
                self.archive(revision=revision)
        self.assertEqual(self.calls, [])

    def test_missing_task_and_changed_timestamp_are_rejected(self):
        with self.assertRaises(ValueError):
            self.archive(updated_at=2)
        self.store.sync([])
        with self.assertRaises(ValueError):
            self.archive()
        self.assertEqual(self.calls, [])

    def test_failed_or_uncertain_remote_call_does_not_hide_task(self):
        before = self.store.file.read_bytes()
        for exception in (RuntimeError('归档失败'), TimeoutError('连接超时')):
            def fail(task):
                raise exception
            with self.subTest(exception=exception), self.assertRaises(type(exception)):
                self.archive(commit=fail)
            self.assertEqual(self.store.file.read_bytes(), before)
            self.assertFalse(self.store.snapshot()['tasks'][0]['archived'])

    def test_late_catalog_cannot_resurrect_archived_task(self):
        started = time.time()
        self.archive()
        self.store.sync([self.task], started)
        self.assertTrue(self.store.snapshot()['tasks'][0]['archived'])
        # A genuinely later host unarchive must remain visible and keep its review.
        self.store.sync([self.task], time.time())
        self.assertFalse(self.store.snapshot()['tasks'][0]['archived'])

    def test_archive_invalidates_approval_queued_from_another_window(self):
        self.archive()
        with self.assertRaisesRegex(ValueError, '其他窗口更新'):
            self.store.mark('fixture', 'done', 0)
        self.assertEqual(self.store.snapshot()['tasks'][0]['review']['status'], 'pending')

    def test_disk_failure_after_host_success_reports_actual_outcome(self):
        before = self.store.file.read_bytes()
        with patch.object(self.store, 'write', side_effect=OSError('disk failure')):
            with self.assertRaisesRegex(OSError, '聊天已归档.*保存失败'):
                self.archive()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.store.file.read_bytes(), before)
        self.store.sync([dict(self.task, archived=True)])
        self.assertTrue(self.store.snapshot()['tasks'][0]['archived'])

    def test_service_exposes_archive_only_to_app_with_confirmation_schema(self):
        class Host:
            archive = self.calls.append
        service = server.Service(self.store, Host())
        tool = next(t for t in service.tools() if t['name'] == 'archive_task')
        self.assertEqual(tool['_meta']['ui']['visibility'], ['app'])
        self.assertTrue(tool['inputSchema']['properties']['confirmed']['const'])
        args = dict(threadId='fixture', expectedRevision=0, expectedUpdatedAt=3)
        self.assertTrue(service.handle('tools/call', {'name': 'archive_task', 'arguments': args})['isError'])
        result = service.handle('tools/call', {'name': 'archive_task', 'arguments': dict(args, confirmed=True)})
        self.assertTrue(result['structuredContent']['archived'])
        self.assertEqual(len(self.calls), 1)


class CoreArchiveTests(unittest.TestCase):
    def host(self, **changes):
        class Host(server.Core):
            def connect(self):
                pass
            def close(self):
                self.closed = True
            def call(self, method, params):
                self.calls.append((method, params))
                if method == 'thread/read':
                    return {'thread': self.raw}
                if self.failure:
                    raise self.failure
                return self.ack
        host = Host()
        host.raw = dict(id='fixture', name='样本', updatedAt=3, status={'type': 'idle'}, **changes)
        host.calls, host.failure, host.ack = [], None, {}
        self.addCleanup(host.close)
        return host

    def test_metadata_read_then_official_archive_without_reading_messages(self):
        host = self.host()
        host.archive(server.normalize(host.raw))
        self.assertEqual(host.calls, [('thread/read', {'threadId': 'fixture', 'includeTurns': False}),
                                      ('thread/archive', {'threadId': 'fixture'})])

    def test_active_unknown_or_updated_threads_are_not_archived(self):
        for field, value in [('status', {'type': 'active'}), ('status', {'type': 'future'}),
                             ('updatedAt', 4), ('name', '新标题'), ('id', 'another')]:
            host = self.host()
            task = server.normalize(host.raw)
            host.raw[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                host.archive(task)
            self.assertEqual([m for m, p in host.calls], ['thread/read'])

    def test_remote_failure_or_invalid_ack_never_returns_success(self):
        host = self.host()
        task = server.normalize(host.raw)
        host.failure = RuntimeError('host rejected')
        with self.assertRaisesRegex(RuntimeError, 'host rejected'):
            host.archive(task)
        host.failure = None
        for ack in (None, [], True):
            host.ack = ack
            with self.subTest(ack=ack), self.assertRaisesRegex(ValueError, '尚未确认'):
                host.archive(task)


if __name__ == '__main__':
    unittest.main()
