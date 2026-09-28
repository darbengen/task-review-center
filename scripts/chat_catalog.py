"""Read only the desktop's Chat directory metadata, never messages or credentials."""
import atexit
import json
from contextlib import closing
import os
import re
import sqlite3
import threading
import time
from pathlib import Path

from store import Store, timestamp

UUID = re.compile(r'^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$', re.I)


class ChatCatalog:
    def __init__(self, database=None, global_state=None, account_reader=None):
        self.database = Path(database or os.environ.get('TASK_REVIEW_CHAT_DB',
                            str(Path.home() / '.codex/sqlite/codex-dev.db')))
        self.global_state = Path(global_state or os.environ.get('TASK_REVIEW_CHAT_GLOBAL_STATE',
                                str(Path.home() / '.codex/.codex-global-state.json')))
        self.account_core = None
        # Explicit offline catalogs must never consult the real user's account.
        configured = database is not None or global_state is not None or any(
            key in os.environ for key in ('TASK_REVIEW_CHAT_DB', 'TASK_REVIEW_CHAT_GLOBAL_STATE'))
        self.account_reader = account_reader if account_reader is not None else (
            None if configured else self.official_account)

    def official_account(self):
        # The desktop removed mcp-extension-sidebar-catalog on restart/update.
        # Ask its official core for the active account instead of guessing from
        # cached sidebar keys, which can still contain a previous account.
        if self.account_core is None:
            from server import Core
            self.account_core = Core()
            atexit.register(self.account_core.close)
        core = self.account_core
        with core.mutex:
            try:
                core.connect()
                result = core.call('account/read', {'refreshToken': False})
                account = result.get('account') if isinstance(result, dict) else None
                routing = result.get('workspaceRouting') if isinstance(result, dict) else None
                if not isinstance(account, dict) or account.get('type') != 'chatgpt' or not isinstance(routing, dict):
                    raise ValueError('No active ChatGPT account')
                return routing.get('chatgptAccountId')
            except (OSError, ValueError, RuntimeError):
                core.close()
                raise ValueError('暂时无法读取当前聊天账号，已保留记录，请稍后刷新。') from None

    def account(self):
        try:
            if self.account_reader is not None:
                account = self.account_reader()
            else:
                atoms = json.loads(self.global_state.read_text())['electron-persisted-atom-state']
                account = atoms['mcp-extension-sidebar-catalog']['accountId']
            if not isinstance(account, str) or not UUID.fullmatch(account):
                raise ValueError()
            return account
        except (OSError, ValueError, KeyError, TypeError):
            raise ValueError('暂时无法确定聊天所属账号，请重新打开任务中心。') from None

    def read(self):
        account = self.account()
        # mode=ro refuses missing paths and cannot create or change the official database.
        with closing(sqlite3.connect(self.database.as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            hosts = db.execute("SELECT host_id FROM local_thread_catalog_hosts "
                               "WHERE host_kind = 'chatgpt'").fetchall()
            matches = [r['host_id'] for r in hosts if r['host_id'].startswith('chatgpt:' + account + ':')]
            if len(matches) != 1:
                raise ValueError('当前账号的 Chat 聊天目录尚未就绪，请先打开应用的聊天列表。')
            host = matches[0]
            sync = db.execute('SELECT initial_build_complete, watermark_updated_at '
                              'FROM local_thread_catalog_sync_state WHERE host_id = ?', (host,)).fetchone()
            if sync is None or sync['initial_build_complete'] not in (0, 1):
                raise ValueError('无法确认 Chat 聊天目录的同步状态。')
            rows = db.execute("SELECT thread_id, display_title, source_updated_at, project_id "
                              "FROM local_thread_catalog WHERE host_id = ? AND source_kind = 'chatgpt' "
                              "AND missing_candidate = 0 AND conversation_origin IS NOT 'tpp' "
                              "ORDER BY source_updated_at DESC, thread_id LIMIT 50001", (host,)).fetchall()
            if len(rows) > 50000:
                raise ValueError('聊天目录超过当前支持数量，已保留上次列表。')
            tasks = []
            for row in rows:
                cid, title, updated = row['thread_id'], row['display_title'], row['source_updated_at']
                if not isinstance(cid, str) or not UUID.fullmatch(cid) or not isinstance(title, str) or not timestamp(updated):
                    raise ValueError('Chat 聊天目录格式发生变化，已保留上次列表。')
                tasks.append({'id': host + ':' + cid, 'conversationId': cid, 'source': 'chatgpt',
                              'sourceHost': host, 'title': title.strip()[:300] or '新聊天',
                              'updatedAt': updated, 'cwd': '', 'project': 'ChatGPT Chat',
                              'projectId': row['project_id'], 'archived': False})
        if self.account() != account:
            raise ValueError('账号正在切换，请稍后刷新聊天栏。')
        source = {'accountId': account, 'hostId': host, 'complete': sync['initial_build_complete'] == 1,
                  'kind': 'desktop-chat-catalog', 'sourceUpdatedAt': sync['watermark_updated_at']}
        return tasks, source


class ChatService:
    """Separate store so Work refresh/archival can never erase or archive Chat entries."""
    def __init__(self, path, catalog=None):
        self.store = Store(path)
        self.catalog = catalog or ChatCatalog()
        self.mutex = threading.Lock()
        self.last_error = None

    def refresh(self):
        started = time.time()
        with self.mutex:
            try:
                tasks, source = self.catalog.read()
                self.store.sync(tasks, started, source=source)
                self.last_error = None
            except (sqlite3.Error, OSError, ValueError, TypeError) as error:
                # No database paths, queries or account data are needed in UI errors.
                message = str(error) if isinstance(error, ValueError) else '应用聊天目录暂时无法读取。'
                self.last_error = message
                self.store.sync_failed(message, started)

    def snapshot(self):
        result = self.store.snapshot()
        result['accountCheckedAt'] = time.time()
        if self.last_error:
            result['syncError'] = self.last_error
        try:
            account = self.catalog.account()
        except ValueError as error:
            return {'tasks': [], 'syncedAt': None, 'syncError': str(error), 'source': None, 'accountCheckedAt': result['accountCheckedAt']}
        # Never display the last account's cache while a new account is syncing.
        if (result.get('source') or {}).get('accountId') != account:
            return {'tasks': [], 'syncedAt': None, 'syncError': result['syncError'] or '正在等待当前账号的聊天目录。', 'source': None, 'accountCheckedAt': result['accountCheckedAt']}
        return result

    def mutate(self, method, thread_id, *args):
        with self.mutex:
            snapshot = self.snapshot()
            if not any(t['id'] == thread_id for t in snapshot['tasks']):
                raise ValueError('聊天不属于当前账号的列表，请刷新后再操作。')
            return getattr(self.store, method)(thread_id, *args)
