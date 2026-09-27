"""Old cached MCP launch commands must still follow published runtime updates."""
import json
import os
from pathlib import Path
import selectors
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from publish_runtime import FILES, publish
from store import Store
from chat_fixtures import ChatFixture


class LegacyEntryTests(unittest.TestCase):
    def test_cached_server_command_upgrades_same_connection_and_preserves_both_stores(self):
        with tempfile.TemporaryDirectory(prefix='task-center-legacy-') as temp:
            folder = Path(temp)
            root, state = folder / 'plugin', folder / 'state'
            for name in (*FILES, 'scripts/launcher.py'):
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / name, target)
            manifest = root / '.codex-plugin/plugin.json'
            metadata = json.loads(manifest.read_text())
            html = (root / 'ui/board.html').read_text()
            def release(version):
                metadata['version'] = version
                manifest.write_text(json.dumps(metadata))
                (root / 'ui/board.html').write_text(html + '\n<!-- ' + version + ' -->\n')
                publish(root)
            release('compat-v1')
            Store(state).sync([{'id': 'work-fixture', 'title': 'Work fixture', 'project': 'fixture', 'cwd': '/tmp/fixture', 'updatedAt': 1, 'archived': False}])
            chat = ChatFixture(temp)
            env = dict(os.environ, TASK_REVIEW_STATE_DIR=str(state), TASK_REVIEW_CORE_SOCKET=str(folder / 'absent.sock'),
                       TASK_REVIEW_CHAT_DB=str(chat.database), TASK_REVIEW_CHAT_GLOBAL_STATE=str(chat.global_state))
            # Reproduce the exact old entry point retained by the official app.
            process = subprocess.Popen([sys.executable, str(root / 'scripts/server.py'), '--generation', 'old-cached-command'],
                                       env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            selector = selectors.DefaultSelector()
            selector.register(process.stdout, selectors.EVENT_READ)
            serial, buffer = 0, b''
            def request(method, params):
                nonlocal serial, buffer
                serial += 1
                process.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': serial, 'method': method, 'params': params}).encode() + b'\n')
                process.stdin.flush()
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if b'\n' in buffer:
                        line, buffer = buffer.split(b'\n', 1)
                        response = json.loads(line)
                        self.assertEqual(response['id'], serial)
                        self.assertNotIn('error', response)
                        self.assertFalse(response['result'].get('isError'), response['result'].get('content'))
                        return response['result']
                    if selector.select(.1):
                        chunk = os.read(process.stdout.fileno(), 65536)
                        self.assertTrue(chunk)
                        buffer += chunk
                self.fail('legacy entry stopped responding')
            def tool(name, arguments=None):
                return request('tools/call', {'name': name, 'arguments': arguments or {}})
            try:
                self.assertEqual(request('initialize', {'protocolVersion': '2025-06-18'})['serverInfo']['version'], 'compat-v1')
                process.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
                process.stdin.flush()
                snapshot = tool('list_tasks', {'refresh': True})['structuredContent']
                self.assertEqual(len(snapshot['chats']), 1)
                tool('set_review', {'threadId': 'work-fixture', 'status': 'done', 'expectedRevision': 0})
                tool('update_task', {'threadId': chat.key(), 'note': 'keep chat note', 'expectedRevision': 0})
                release('compat-v2')
                actual = tool('list_tasks', {'refresh': True})
                self.assertEqual(actual['_meta']['taskReviewVersion'], 'compat-v2', 'cached server.py command must enter the stable launcher')
                self.assertEqual(actual['structuredContent']['tasks'][0]['review']['status'], 'done')
                self.assertEqual(actual['structuredContent']['chats'][0]['review']['note'], 'keep chat note')
                ui = request('resources/read', {'uri': 'ui://task-review-center/board.html'})['contents'][0]['text']
                self.assertIn('<!-- compat-v2 -->', ui)
            finally:
                process.stdin.close()
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait()
                    self.fail('legacy entry did not shut down')
                selector.close()
                error = process.stderr.read().decode()
                process.stdout.close(); process.stderr.close()
                self.assertEqual(process.returncode, 0, error)
                self.assertFalse(error)


if __name__ == '__main__':
    unittest.main()
