import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from chat_catalog import ChatCatalog, ChatService
from server import Service, normalize
from store import Store
from chat_fixtures import ChatFixture, ACCOUNT_A, ACCOUNT_B, CHAT_A, CHAT_B


class ChatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.fixture = ChatFixture(self.temp.name)
        self.catalog = ChatCatalog(self.fixture.database, self.fixture.global_state)
        self.chat = ChatService(Path(self.temp.name) / 'reviews', self.catalog)

    def tearDown(self):
        self.temp.cleanup()

    def test_whole_directory_includes_unpinned_and_project_chats_only_current_account(self):
        self.fixture.add(CHAT_B, project='g-p-test', title='未置顶项目聊天')
        self.fixture.add(CHAT_A, account=ACCOUNT_B, title='另一个账号不能出现')
        for i, values in enumerate([{'origin': 'tpp'}, {'missing': 1}, {'kind': 'vscode'}], 3):
            self.fixture.add(f'{i:08d}-0000-0000-0000-000000000000', **values)
        before = hashlib.sha256(self.fixture.database.read_bytes()).hexdigest()
        tasks, source = self.catalog.read()
        self.assertEqual({t['conversationId'] for t in tasks}, {CHAT_A, CHAT_B})
        self.assertTrue(all(t['id'].startswith(self.fixture.host() + ':') for t in tasks))
        self.assertTrue(source['complete'])
        self.assertEqual(hashlib.sha256(self.fixture.database.read_bytes()).hexdigest(), before)

    def test_registration_review_undo_notes_and_process_restart(self):
        self.chat.refresh()
        key = self.fixture.key()
        self.chat.mutate('update', key, 0, '保留备注', True)
        result = self.chat.mutate('mark', key, 'done', 1)
        self.fixture.add(CHAT_B)
        restarted = ChatService(self.chat.store.path, self.catalog)
        restarted.refresh()
        tasks = {t['id']: t for t in restarted.snapshot()['tasks']}
        self.assertEqual(tasks[key]['review']['status'], 'done')
        self.assertEqual(tasks[key]['review']['note'], '保留备注')
        self.assertTrue(tasks[key]['review']['starred'])
        self.assertEqual(tasks[self.fixture.key(CHAT_B)]['review']['status'], 'pending')
        restarted.mutate('undo', key, result['undo']['token'], 2)
        self.assertEqual(restarted.snapshot()['tasks'][0]['review']['status'], 'pending')

    def test_account_switch_masks_old_cache_and_rejects_old_mutations(self):
        self.chat.refresh()
        self.chat.mutate('mark', self.fixture.key(), 'done', 0)
        self.fixture.account(ACCOUNT_B)
        self.assertEqual(self.chat.snapshot()['tasks'], [])
        with self.assertRaises(ValueError):
            self.chat.mutate('update', self.fixture.key(), 1, '旧窗口的备注', None)
        self.fixture.add(CHAT_A, account=ACCOUNT_B)
        self.chat.refresh()
        self.assertEqual(self.chat.snapshot()['tasks'][0]['review']['status'], 'pending')
        self.fixture.account(ACCOUNT_A)
        self.chat.refresh()
        self.assertEqual(self.chat.snapshot()['tasks'][0]['review']['status'], 'done')

    def test_unknown_account_hides_all_chats(self):
        self.chat.refresh()
        for contents in ['{}', '{bad', 'null']:
            self.fixture.global_state.write_text(contents)
            self.assertEqual(self.chat.snapshot()['tasks'], [])
            self.assertTrue(self.chat.snapshot()['syncError'])

    def test_schema_failure_preserves_previous_account_cache_and_recovers(self):
        self.chat.refresh()
        self.fixture.execute('ALTER TABLE local_thread_catalog RENAME TO renamed_catalog')
        self.chat.refresh()
        snapshot = self.chat.snapshot()
        self.assertEqual(len(snapshot['tasks']), 1)
        self.assertTrue(snapshot['syncError'])
        self.fixture.execute('ALTER TABLE renamed_catalog RENAME TO local_thread_catalog')
        self.chat.refresh()
        self.assertIsNone(self.chat.snapshot()['syncError'])

    def test_bad_row_is_not_mistaken_for_empty_directory(self):
        self.chat.refresh()
        self.fixture.execute('UPDATE local_thread_catalog SET source_updated_at = NULL')
        self.chat.refresh()
        self.assertEqual(len(self.chat.snapshot()['tasks']), 1)
        self.assertTrue(self.chat.snapshot()['syncError'])

    def test_incomplete_directory_is_labeled(self):
        self.fixture.execute('UPDATE local_thread_catalog_sync_state SET initial_build_complete = 0')
        self.chat.refresh()
        self.assertFalse(self.chat.snapshot()['source']['complete'])
        self.assertEqual(len(self.chat.snapshot()['tasks']), 1)

    def test_missing_database_does_not_create_file(self):
        catalog = ChatCatalog(Path(self.temp.name) / 'absent.db', self.fixture.global_state)
        with self.assertRaises(sqlite3.Error):
            catalog.read()
        self.assertFalse(catalog.database.exists())

    def test_disappeared_chat_retains_review_for_restore(self):
        self.chat.refresh()
        self.chat.mutate('mark', self.fixture.key(), 'done', 0)
        self.fixture.execute('UPDATE local_thread_catalog SET missing_candidate = 1')
        self.chat.refresh()
        self.assertEqual(self.chat.snapshot()['tasks'], [])
        self.fixture.execute('UPDATE local_thread_catalog SET missing_candidate = 0')
        self.chat.refresh()
        self.assertEqual(self.chat.snapshot()['tasks'][0]['review']['status'], 'done')

    def test_multiple_user_hosts_fail_closed(self):
        self.fixture.execute('INSERT INTO local_thread_catalog_hosts VALUES (?,?)', ('chatgpt:' + ACCOUNT_A + ':other-user', 'chatgpt'))
        self.chat.refresh()
        self.assertEqual(self.chat.snapshot()['tasks'], [])
        self.assertTrue(self.chat.snapshot()['syncError'])

    def test_source_race_never_commits_wrong_account(self):
        original = self.catalog.account
        count = 0
        def switching():
            nonlocal count
            count += 1
            return original() if count == 1 else ACCOUNT_B
        self.catalog.account = switching
        self.chat.refresh()
        self.assertEqual(self.chat.snapshot()['tasks'], [])

    def test_independent_work_sync_failure_and_chat_archive_rejection(self):
        class BrokenCore:
            def catalog(self):
                raise ConnectionError('Work offline')
            def archive(self, *_):
                raise AssertionError('Chat must never use Work archive')
        work = Store(Path(self.temp.name) / 'work')
        work.sync([normalize({'id': CHAT_A, 'name': 'Work sample', 'updatedAt': 1})])
        service = Service(work, BrokenCore(), self.chat)
        result = service.listing(True)
        self.assertEqual(len(result['tasks']), 1)
        self.assertEqual(len(result['chats']), 1)
        args = {'threadId': self.fixture.key(), 'expectedRevision': 0, 'status': 'done'}
        result = service.handle('tools/call', {'name': 'set_review', 'arguments': args})
        self.assertEqual(result['structuredContent']['record']['status'], 'done')
        self.assertEqual(work.snapshot()['tasks'][0]['review']['status'], 'pending')
        archived = service.handle('tools/call', {'name': 'archive_task', 'arguments': dict(args, confirmed=True, expectedUpdatedAt=100)})
        self.assertTrue(archived['isError'])

    def test_fresh_work_background_snapshot_does_not_prevent_chat_registration(self):
        work = Store(Path(self.temp.name) / 'work')
        work.sync([normalize({'id': 'work', 'name': 'Work sample', 'updatedAt': 1})])
        # A still-running old Work watcher can keep syncedAt fresh indefinitely.
        service = Service(work, object(), self.chat)
        self.assertEqual(len(service.listing()['chats']), 1)
        self.fixture.add(CHAT_B)
        data = self.chat.store.read()
        data['syncedAt'] = time.time() - 20
        self.chat.store.write(data)
        work.sync(work.snapshot()['tasks'])
        self.assertEqual(len(service.listing()['chats']), 2)


if __name__ == '__main__':
    unittest.main()
