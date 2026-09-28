"""Exercise the actual MCP subprocess and socket bridge against an isolated host fixture."""
import base64
import hashlib
import json
import os
from pathlib import Path
import selectors
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from core_bridge import decode
from chat_fixtures import ChatFixture, CHAT_A, CHAT_B
from publish_runtime import publish, FILES


class FixtureHost:
    def __init__(self, path):
        self.path = path
        self.tasks = [{'id': 'fixture-a', 'name': '隔离样本 A', 'updatedAt': 1, 'source': 'appServer'}]
        self.invalid = False
        self.clients = []
        self.archive_calls = []

    def start(self):
        self.stopped = threading.Event()
        if self.path.exists():
            self.path.unlink()
        self.listener = socket.socket(socket.AF_UNIX)
        self.listener.bind(str(self.path))
        self.listener.listen()
        self.listener.settimeout(.1)
        self.worker = threading.Thread(target=self.accept, daemon=True)
        self.worker.start()

    def accept(self):
        while not self.stopped.is_set():
            try:
                conn, _ = self.listener.accept()
            except (socket.timeout, OSError):
                continue
            self.clients.append(conn)
            threading.Thread(target=self.serve, args=(conn,), daemon=True).start()

    def serve(self, conn):
        try:
            raw = b''
            while b'\r\n\r\n' not in raw:
                part = conn.recv(8192)
                if not part:
                    return
                raw += part
            header, buffer = raw.split(b'\r\n\r\n', 1)
            key = next(line.split(b':', 1)[1].strip() for line in header.split(b'\r\n') if line.lower().startswith(b'sec-websocket-key:'))
            accept = base64.b64encode(hashlib.sha1(key + b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest())
            conn.sendall(b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ' + accept + b'\r\n\r\n')
            while not self.stopped.is_set():
                message = decode(buffer)
                if message is None:
                    part = conn.recv(65536)
                    if not part:
                        return
                    buffer += part
                    continue
                _, _, payload, buffer = message
                request = json.loads(payload)
                if 'id' not in request:
                    continue
                if request['method'] == 'initialize':
                    result = {'userAgent': 'isolated-lifecycle-fixture'}
                elif request['method'] == 'thread/list':
                    result = {} if self.invalid else {'data': [t for t in self.tasks if bool(t.get('archived')) == bool(request['params'].get('archived'))], 'nextCursor': None}
                elif request['method'] == 'thread/read':
                    result = {'thread': dict(next(t for t in self.tasks if t['id'] == request['params']['threadId']), status={'type': 'idle'})}
                elif request['method'] == 'thread/archive':
                    task = next(t for t in self.tasks if t['id'] == request['params']['threadId'])
                    self.archive_calls.append(task['id'])
                    task['archived'] = True
                    result = {}
                else:
                    raise AssertionError('Unexpected fixture request: ' + request['method'])
                body = json.dumps({'id': request['id'], 'result': result}).encode()
                prefix = bytes([129, len(body)]) if len(body) < 126 else bytes([129, 126]) + struct.pack('!H', len(body))
                conn.sendall(prefix + body)
        except (ConnectionError, OSError):
            pass
        finally:
            conn.close()

    def stop(self):
        self.stopped.set()
        self.listener.close()
        for conn in self.clients:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            conn.close()
        self.clients = []
        self.worker.join(1)
        if self.path.exists():
            self.path.unlink()


class Plugin:
    def __init__(self, directory, socket_path, chat_fixture=None, plugin_root=ROOT):
        self.serial = 0
        self.buffer = b''
        env = dict(os.environ, TASK_REVIEW_STATE_DIR=str(directory), TASK_REVIEW_CORE_SOCKET=str(socket_path))
        env.update(TASK_REVIEW_CHAT_DB=str(chat_fixture.database if chat_fixture else directory / 'absent-chat.db'),
                   TASK_REVIEW_CHAT_GLOBAL_STATE=str(chat_fixture.global_state if chat_fixture else directory / 'absent-account.json'))
        self.process = subprocess.Popen([sys.executable, str(plugin_root / 'scripts/launcher.py')], env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)

    def call(self, tool, arguments=None):
        self.serial += 1
        self.process.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': self.serial, 'method': 'tools/call', 'params': {'name': tool, 'arguments': arguments or {}}}).encode() + b'\n')
        self.process.stdin.flush()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if b'\n' in self.buffer:
                line, self.buffer = self.buffer.split(b'\n', 1)
                response = json.loads(line)
                assert response['id'] == self.serial, response
                result = response['result']
                assert not result.get('isError'), result
                return result['structuredContent']
            if self.selector.select(.2):
                self.buffer += os.read(self.process.stdout.fileno(), 65536)
        raise AssertionError('Plugin fixture request timed out')

    def stop(self):
        self.process.stdin.close()
        try:
            self.process.wait(5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
            raise AssertionError('Plugin did not exit cleanly')
        self.selector.close()
        error = self.process.stderr.read().decode()
        assert self.process.returncode == 0, error
        self.process.stdout.close()
        self.process.stderr.close()


def wait_for_record(directory, task_id, timeout=13):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            state = json.loads((directory / 'state.json').read_text())
            if task_id in state['reviews']:
                return state
        except FileNotFoundError:
            pass
        time.sleep(.1)
    raise AssertionError('Background watcher failed to register ' + task_id)


def main():
    findings = []
    with tempfile.TemporaryDirectory(prefix='task-review-lifecycle-', dir='/tmp') as temp:
        plugin_root = Path(temp) / 'plugin'
        for name in (*FILES, 'scripts/launcher.py'):
            target = plugin_root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, target)
        publish(plugin_root)
        directory = Path(temp) / 'state'
        host = FixtureHost(Path(temp) / 'host.sock')
        chat_fixture = ChatFixture(temp)
        host.start()
        plugin = None
        try:
            plugin = Plugin(directory, host.path, chat_fixture, plugin_root)
            wait_for_record(directory, 'fixture-a')
            wait_for_record(directory / 'chat', chat_fixture.key())
            plugin.call('update_task', {'threadId': chat_fixture.key(), 'expectedRevision': 0, 'note': 'Chat 重启后保留', 'starred': True})
            chat_action = plugin.call('set_review', {'threadId': chat_fixture.key(), 'expectedRevision': 1, 'status': 'done'})
            plugin.call('update_task', {'threadId': 'fixture-a', 'expectedRevision': 0, 'note': '重启后保留', 'starred': True})
            action = plugin.call('set_review', {'threadId': 'fixture-a', 'expectedRevision': 1, 'status': 'done'})
            # Publish a second runtime while this same host connection stays open.
            manifest = plugin_root / '.codex-plugin/plugin.json'
            metadata = json.loads(manifest.read_text())
            metadata['version'] += '.lifecycle'
            manifest.write_text(json.dumps(metadata))
            publish(plugin_root)
            upgraded = plugin.call('list_tasks', {'refresh': True})
            assert next(t for t in upgraded['tasks'] if t['id'] == 'fixture-a')['review']['status'] == 'done'
            assert next(t for t in upgraded['chats'] if t['id'] == chat_fixture.key())['review']['note'] == 'Chat 重启后保留'
            findings.append({'check': 'Same MCP connection switches a published runtime and keeps Work / Chat approvals and notes'})
            start = time.monotonic()
            host.tasks.append({'id': 'fixture-b', 'name': '运行期间新增', 'updatedAt': 2})
            chat_fixture.add(CHAT_B, title='Chat 运行期间新增')
            state = wait_for_record(directory, 'fixture-b')
            assert state['reviews']['fixture-b']['status'] == 'pending'
            assert state['reviews']['fixture-a']['status'] == 'done'
            findings.append({'check': 'New task registers without a UI/tool refresh call', 'seconds': round(time.monotonic() - start, 2)})
            chat_state = wait_for_record(directory / 'chat', chat_fixture.key(CHAT_B))
            assert chat_state['reviews'][chat_fixture.key(CHAT_B)]['status'] == 'pending'
            findings.append({'check': 'New Chat index entry registers automatically without a UI/tool refresh call', 'seconds': round(time.monotonic() - start, 2)})
            plugin.stop()
            plugin = None
            host.tasks.append({'id': 'fixture-c', 'name': '停机期间新增', 'updatedAt': 3})
            assert 'fixture-c' not in json.loads((directory / 'state.json').read_text())['reviews']
            plugin = Plugin(directory, host.path, chat_fixture, plugin_root)
            state = wait_for_record(directory, 'fixture-c')
            assert state['reviews']['fixture-c']['status'] == 'pending'
            assert state['reviews']['fixture-a']['status'] == 'done'
            assert state['reviews']['fixture-a']['note'] == '重启后保留'
            assert state['reviews']['fixture-a']['starred'] is True
            findings.append({'check': 'Process restart preserves approvals and catches up tasks created while stopped'})
            chat_state = plugin.call('list_tasks', {'refresh': True})
            chat_review = next(t['review'] for t in chat_state['chats'] if t['id'] == chat_fixture.key())
            assert chat_review['status'] == 'done' and chat_review['starred'] and chat_review['note'] == 'Chat 重启后保留'
            chat_undo = plugin.call('undo_review', chat_action['undo'])
            assert chat_undo['record']['status'] == 'pending'
            findings.append({'check': 'Chat review, note, star and exact undo survive plugin process restart'})
            host.stop()
            cached = plugin.call('list_tasks', {'refresh': True})
            assert cached['syncError'] and len(cached['tasks']) == 3
            host.tasks.append({'id': 'fixture-d', 'name': '重新连接后新增', 'updatedAt': 4})
            host.start()
            recovered = plugin.call('list_tasks', {'refresh': True})
            assert not recovered['syncError'] and len(recovered['tasks']) == 4
            findings.append({'check': 'Host disconnection retains the list; reconnect catches up without restarting the plugin'})
            host.invalid = True
            invalid = plugin.call('list_tasks', {'refresh': True})
            assert invalid['syncError'] and len(invalid['tasks']) == 4
            host.invalid = False
            assert not plugin.call('list_tasks', {'refresh': True})['syncError']
            findings.append({'check': 'Changed catalog schema shows an error and retains all tasks; compatible responses recover'})
            plugin.stop()
            plugin = Plugin(directory, host.path, chat_fixture, plugin_root)
            restored = plugin.call('undo_review', action['undo'])
            assert restored['record']['status'] == 'pending' and restored['record']['note'] == '重启后保留'
            assert list((directory / 'backups/review-history').glob('review-*.json'))
            findings.append({'check': 'Another restart retains the exact undo token and persistent review-history backup'})
            args = {'threadId': 'fixture-b', 'expectedRevision': 0, 'expectedUpdatedAt': 2, 'confirmed': True}
            archived = plugin.call('archive_task', args)
            assert archived['archived'] and archived['threadId'] == 'fixture-b'
            assert next(t for t in archived['tasks'] if t['id'] == 'fixture-b')['review']['status'] == 'pending'
            assert plugin.call('archive_task', args)['archived']
            assert host.archive_calls == ['fixture-b']
            plugin.stop()
            plugin = Plugin(directory, host.path, chat_fixture, plugin_root)
            restarted = plugin.call('list_tasks', {'refresh': True})
            task = next(t for t in restarted['tasks'] if t['id'] == 'fixture-b')
            assert task['archived'] and task['review']['status'] == 'pending'
            assert len(restarted['chats']) == 2 and not restarted['chatSyncError']
            findings.append({'check': 'Official archive request crosses the real bridge once; repeat is idempotent and restart retains archive without marking done'})
        finally:
            if plugin:
                plugin.stop()
            host.stop()
    report = {'passed': True, 'scope': 'Actual plugin subprocess and socket bridge with isolated fixture host/data; official desktop was not restarted', 'findings': findings}
    (ROOT / 'evidence').mkdir(exist_ok=True)
    (ROOT / 'evidence/workflow-lifecycle-checks.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
