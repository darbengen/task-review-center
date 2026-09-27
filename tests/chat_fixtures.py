import json
from contextlib import closing
from pathlib import Path
import sqlite3

ACCOUNT_A = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
ACCOUNT_B = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'
CHAT_A = '11111111-1111-1111-1111-111111111111'
CHAT_B = '22222222-2222-2222-2222-222222222222'


class ChatFixture:
    def __init__(self, directory):
        self.database = Path(directory) / 'chat-fixture.db'
        self.global_state = Path(directory) / 'chat-account.json'
        with closing(sqlite3.connect(self.database)) as db, db:
            db.executescript('''
                CREATE TABLE local_thread_catalog_hosts (host_id TEXT PRIMARY KEY, host_kind TEXT);
                CREATE TABLE local_thread_catalog_sync_state (host_id TEXT PRIMARY KEY, initial_build_complete INTEGER, watermark_updated_at REAL);
                CREATE TABLE local_thread_catalog (host_id TEXT, thread_id TEXT, display_title TEXT,
                    source_updated_at REAL, project_id TEXT, source_kind TEXT, missing_candidate INTEGER, conversation_origin TEXT,
                    PRIMARY KEY(host_id, thread_id));
            ''')
            for account in (ACCOUNT_A, ACCOUNT_B):
                db.execute('INSERT INTO local_thread_catalog_hosts VALUES (?,?)', (self.host(account), 'chatgpt'))
                db.execute('INSERT INTO local_thread_catalog_sync_state VALUES (?,?,?)', (self.host(account), 1, 100))
        self.account(ACCOUNT_A)
        self.add(CHAT_A)

    @staticmethod
    def host(account=ACCOUNT_A):
        return 'chatgpt:' + account + ':user-fixture'

    @classmethod
    def key(cls, cid=CHAT_A, account=ACCOUNT_A):
        return cls.host(account) + ':' + cid

    def account(self, account):
        self.global_state.write_text(json.dumps({'electron-persisted-atom-state': {'mcp-extension-sidebar-catalog': {'accountId': account}}}))

    def add(self, cid, account=ACCOUNT_A, title='隔离 Chat 样本', origin=None, missing=0, kind='chatgpt', project=None, updated=100):
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute('INSERT OR REPLACE INTO local_thread_catalog VALUES (?,?,?,?,?,?,?,?)',
                       (self.host(account), cid, title, updated, project, kind, missing, origin))

    def execute(self, query, params=()):
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute(query, params)
