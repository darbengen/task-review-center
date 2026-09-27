#!/usr/bin/env python3
"""Keep the host MCP pipe stable; swap only published runtimes between requests.

Only this plugin's child is managed. A sent request is never replayed, including
when its result is unknown. Official app processes and databases are untouched.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
FILES = {'scripts/server.py', 'scripts/store.py', 'scripts/chat_catalog.py',
         'scripts/core_bridge.py', 'ui/board.html', '.codex-plugin/plugin.json'}


def emit(message):
    print(json.dumps(message, ensure_ascii=False), flush=True)


class Child:
    def __init__(self, root):
        self.process = subprocess.Popen([sys.executable, '-B', str(root / 'scripts/server.py')],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=sys.stderr)
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        self.buffer = b''

    def send(self, request):
        self.process.stdin.write(json.dumps(request, ensure_ascii=False).encode() + b'\n')
        self.process.stdin.flush()

    def exchange(self, request, timeout=90):
        self.send(request)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            while b'\n' in self.buffer:
                line, self.buffer = self.buffer.split(b'\n', 1)
                response = json.loads(line)
                if response.get('id') == request['id'] and 'method' not in response:
                    return response
                if 'method' in response and 'id' not in response:
                    emit(response)
                else:
                    raise ConnectionError('Unexpected child response')
            if not self.selector.select(max(.01, deadline - time.monotonic())):
                break
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                raise ConnectionError('Child connection closed')
            self.buffer += chunk
        raise TimeoutError('Child request timed out')

    def close(self):
        try:
            self.process.stdin.close()
        except (OSError, BrokenPipeError):
            pass
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.selector.close()
        self.process.stdout.close()


class Runtime:
    def __init__(self):
        self.child = None
        self.current = None
        self.failed = None
        self.initialize = None
        self.initialized = False

    def ensure(self):
        raw = (ROOT / 'runtime/current.json').read_bytes()
        if raw == self.failed:
            raise ValueError('新版连接或文件校验未通过，请修复插件安装；本次操作未执行。')
        if raw == self.current and self.child and self.child.process.poll() is None:
            return
        candidate = None
        try:
            release = json.loads(raw)
            version = release['version']
            if release.get('schema') != 1 or not isinstance(version, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.+_-]{0,127}', version):
                raise ValueError('Invalid release')
            directory = ROOT / 'runtime/releases' / version
            if set(release['files']) != FILES:
                raise ValueError('Incomplete release')
            if any(hashlib.sha256((directory / name).read_bytes()).hexdigest() != digest for name, digest in release['files'].items()):
                raise ValueError('Runtime checksum mismatch')
            if json.loads((directory / '.codex-plugin/plugin.json').read_text())['version'] != version:
                raise ValueError('Runtime version mismatch')
            candidate = Child(directory)
            if self.initialize:
                reply = candidate.exchange(self.initialize, timeout=10)
                if reply.get('result', {}).get('serverInfo', {}).get('version') != version:
                    raise ValueError('New runtime initialization failed')
                if self.initialized:
                    candidate.send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        except Exception as error:
            if candidate:
                candidate.close()
            self.failed = raw
            raise ValueError('新版连接或文件校验未通过，请修复插件安装；本次操作未执行。') from error
        previous = self.child
        self.child, self.current, self.failed = candidate, raw, None
        if previous:
            previous.close()

    def forward(self, request):
        try:
            self.ensure()
        except (ValueError, OSError) as error:
            self.failure(request, str(error) if isinstance(error, ValueError) else '已发布的任务中心版本暂不可用，请检查插件安装。')
            return
        try:
            if 'id' not in request:
                self.child.send(request)
                if request.get('method') == 'notifications/initialized':
                    self.initialized = True
                return
            response = self.child.exchange(request)
            if request.get('method') == 'initialize' and 'result' in response:
                self.initialize = request
            emit(response)
        except (OSError, ValueError, TimeoutError):
            self.child.close()
            self.child = None
            # May have committed before losing the pipe. Never retry the request.
            self.failure(request, '本次请求结果未确认。请刷新核对状态后再操作；系统没有自动重试。')

    @staticmethod
    def failure(request, message):
        if 'id' not in request:
            return
        reply = {'jsonrpc': '2.0', 'id': request['id']}
        if request.get('method') == 'tools/call':
            reply['result'] = {'isError': True, 'content': [{'type': 'text', 'text': message}]}
        else:
            reply['error'] = {'code': -32603, 'message': message}
        emit(reply)

    def close(self):
        if self.child:
            self.child.close()


def main():
    runtime = Runtime()
    def stop(_signum, _frame):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        # Eager start retains automatic registration without opening the board.
        runtime.ensure()
        for line in sys.stdin:
            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(request, dict):
                runtime.forward(request)
    finally:
        runtime.close()


if __name__ == '__main__':
    main()
