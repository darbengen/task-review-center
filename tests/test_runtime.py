"""Same MCP connection, immutable UI/API releases, and no mutation replay."""
import importlib.util
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
spec = importlib.util.spec_from_file_location('publish_runtime', ROOT / 'scripts/publish_runtime.py')
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)

FIXTURE = '''import json, os, sys, time
from pathlib import Path
root = Path(__file__).resolve().parents[1]
version = json.loads((root / '.codex-plugin/plugin.json').read_text())['version']
html = (root / 'ui/board.html').read_text()
folder = Path(os.environ['TASK_REVIEW_STATE_DIR'])
initialized = False
for line in sys.stdin:
    req = json.loads(line)
    if 'id' not in req:
        continue
    method, params = req['method'], req.get('params', {})
    if method == 'initialize':
        if version == 'bad-start':
            sys.exit(2)
        initialized = True
        result = {'serverInfo': {'version': version}, 'capabilities': {}}
    else:
        assert initialized, 'initialize must be replayed'
        if method == 'resources/read':
            result = {'contents': [{'text': html}]}
        else:
            args = params.get('arguments', {})
            log = folder / 'mutations.json'
            records = json.loads(log.read_text()) if log.exists() else []
            if params.get('name') == 'set_review':
                records.append(args)
                log.write_text(json.dumps(records))
                if args.get('block'):
                    (folder / 'started').touch()
                    while not (folder / 'continue').exists():
                        time.sleep(.01)
                if args.get('crash'):
                    os._exit(3)
            result = {'structuredContent': {'version': version, 'records': records, 'childPid': os.getpid()}}
    print(json.dumps({'jsonrpc': '2.0', 'id': req['id'], 'result': result}), flush=True)
'''


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='task-center-runtime-')
        self.root = Path(self.temp.name)
        for folder in ('scripts', '.codex-plugin', 'ui', 'state'):
            (self.root / folder).mkdir()
        shutil.copy2(ROOT / 'scripts/launcher.py', self.root / 'scripts/launcher.py')
        (self.root / 'scripts/server.py').write_text(FIXTURE)
        for file in ('store.py', 'chat_catalog.py', 'core_bridge.py'):
            (self.root / 'scripts' / file).write_text('# fixture\n')
        self.process = None
        self.selector = selectors.DefaultSelector()
        self.serial = 0
        self.buffer = b''

    def tearDown(self):
        if self.process:
            self.process.stdin.close()
            try:
                self.process.wait(8)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
                self.fail('launcher must close its child when the MCP connection closes')
            self.assertEqual(self.process.returncode, 0, self.process.stderr.read().decode())
            self.process.stdout.close()
            self.process.stderr.close()
        self.selector.close()
        self.temp.cleanup()

    def publish(self, version):
        (self.root / '.codex-plugin/plugin.json').write_text(json.dumps({'version': version}))
        (self.root / 'ui/board.html').write_text('<p>' + version + '</p>')
        return publisher.publish(self.root)

    def start(self):
        env = dict(os.environ, TASK_REVIEW_STATE_DIR=str(self.root / 'state'))
        self.process = subprocess.Popen([sys.executable, str(self.root / 'scripts/launcher.py')], env=env,
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        self.request('initialize', {'protocolVersion': '2025-06-18'})
        self.process.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        self.process.stdin.flush()

    def send(self, method, params):
        self.serial += 1
        self.process.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': self.serial, 'method': method, 'params': params}).encode() + b'\n')
        self.process.stdin.flush()

    def receive(self):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if b'\n' in self.buffer:
                line, self.buffer = self.buffer.split(b'\n', 1)
                reply = json.loads(line)
                self.assertEqual(reply['id'], self.serial)
                return reply
            if self.selector.select(.1):
                chunk = os.read(self.process.stdout.fileno(), 65536)
                self.assertTrue(chunk, 'launcher exited before replying')
                self.buffer += chunk
        self.fail('launcher request timed out')

    def request(self, method, params):
        self.send(method, params)
        return self.receive()

    def listing(self):
        return self.request('tools/call', {'name': 'list_tasks'})['result']['structuredContent']

    def test_same_connection_changes_api_and_resource_only_after_publication(self):
        self.publish('v1'); self.start()
        self.assertEqual(self.listing()['version'], 'v1')
        (self.root / 'ui/board.html').write_text('<p>unpublished edit</p>')
        ui = self.request('resources/read', {})['result']['contents'][0]['text']
        self.assertEqual(ui, '<p>v1</p>')
        pid = self.process.pid
        self.publish('v2')
        self.assertEqual(self.listing()['version'], 'v2')
        self.assertEqual(self.request('resources/read', {})['result']['contents'][0]['text'], '<p>v2</p>')
        self.assertEqual(self.process.pid, pid, 'same host stdio connection stays alive')

    def test_release_does_not_interrupt_or_replay_an_inflight_review(self):
        self.publish('v1'); self.start()
        self.send('tools/call', {'name': 'set_review', 'arguments': {'block': True, 'note': 'keep me'}})
        deadline = time.monotonic() + 5
        while not (self.root / 'state/started').exists() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue((self.root / 'state/started').exists())
        self.publish('v2')
        (self.root / 'state/continue').touch()
        before = self.receive()['result']['structuredContent']
        self.assertEqual(before['version'], 'v1')
        after = self.listing()
        self.assertEqual(after['version'], 'v2')
        self.assertEqual(after['records'], [{'block': True, 'note': 'keep me'}])

    def test_uncertain_mutation_is_never_replayed(self):
        self.publish('v1'); self.start()
        result = self.request('tools/call', {'name': 'set_review', 'arguments': {'crash': True}})['result']
        self.assertTrue(result['isError'])
        self.assertIn('未确认', result['content'][0]['text'])
        self.assertEqual(self.listing()['records'], [{'crash': True}])

    def test_published_version_is_immutable_and_bad_release_cannot_replace_good_one(self):
        self.publish('v1'); self.start()
        pointer = (self.root / 'runtime/current.json').read_bytes()
        (self.root / 'ui/board.html').write_text('changed')
        with self.assertRaises(ValueError):
            publisher.publish(self.root)
        self.assertEqual((self.root / 'runtime/current.json').read_bytes(), pointer)
        self.publish('bad-start')
        failed = self.request('tools/call', {'name': 'list_tasks'})['result']
        self.assertTrue(failed['isError'])
        again = self.request('tools/call', {'name': 'list_tasks'})['result']
        self.assertTrue(again['isError'], 'unchanged broken publication stays latched')
        self.publish('v3')
        self.assertEqual(self.listing()['version'], 'v3')

    def test_checksum_failure_blocks_new_version_without_executing_it(self):
        self.publish('v1'); self.start()
        published = self.publish('v2')
        (self.root / 'runtime/releases' / published['version'] / 'scripts/server.py').write_text('raise RuntimeError("bad")')
        failed = self.request('tools/call', {'name': 'list_tasks'})['result']
        self.assertTrue(failed['isError'])
        self.assertIn('校验', failed['content'][0]['text'])
        self.publish('v3')
        self.assertEqual(self.listing()['version'], 'v3')

    def test_host_shutdown_reaps_plugin_child(self):
        self.publish('v1'); self.start()
        pid = self.listing()['childPid']
        self.process.terminate()
        self.process.wait(timeout=8)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)


if __name__ == '__main__':
    unittest.main()
