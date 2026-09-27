#!/usr/bin/env python3
"""Publish one immutable plugin runtime, then atomically select it for launchers."""
import argparse
import ast
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

FILES = ('scripts/server.py', 'scripts/store.py', 'scripts/chat_catalog.py',
         'scripts/core_bridge.py', 'ui/board.html', '.codex-plugin/plugin.json')


def publish(root):
    root = Path(root).resolve()
    runtime = root / 'runtime'
    releases = runtime / 'releases'
    releases.mkdir(parents=True, exist_ok=True)
    with (runtime / 'publish.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        sources = {name: (root / name).read_bytes() for name in FILES}
        version = json.loads(sources['.codex-plugin/plugin.json'])['version']
        if not isinstance(version, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.+_-]{0,127}', version):
            raise ValueError('Invalid runtime version')
        for name, data in sources.items():
            if name.endswith('.py'):
                ast.parse(data, filename=name)
        descriptor = {'schema': 1, 'version': version,
                      'files': {name: hashlib.sha256(data).hexdigest() for name, data in sources.items()}}
        target = releases / version
        if target.exists():
            if any(not (target / name).is_file() or (target / name).read_bytes() != data for name, data in sources.items()):
                raise ValueError('Published versions are immutable; bump the plugin version first')
        else:
            staging = Path(tempfile.mkdtemp(prefix='.staging-', dir=releases))
            try:
                for name, data in sources.items():
                    dest = staging / name
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with dest.open('wb') as stream:
                        stream.write(data)
                        stream.flush()
                        os.fsync(stream.fileno())
                # Development edits must never slip into a half-published release.
                if any((root / name).read_bytes() != data for name, data in sources.items()):
                    raise ValueError('Source files changed during publication; nothing was activated')
                staging.rename(target)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
        fd, temporary = tempfile.mkstemp(prefix='.current-', dir=runtime)
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump(descriptor, stream, ensure_ascii=False, indent=2)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, runtime / 'current.json')
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return descriptor


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    print(json.dumps(publish(args.root), ensure_ascii=False))
