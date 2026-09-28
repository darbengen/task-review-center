"""Private review records, independent of Codex's conversation database."""
import contextlib
import fcntl
import json
import math
import os
import time
import uuid
from pathlib import Path


def timestamp(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def validate(data):
    invalid = ValueError('任务记录格式异常，原文件已保留；请恢复有效备份。')
    if not isinstance(data, dict) or data.get('schema') != 1:
        raise invalid
    if not isinstance(data.get('reviews'), dict) or not isinstance(data.get('tasks'), list):
        raise invalid
    if data.get('syncedAt') is not None and not timestamp(data['syncedAt']):
        raise invalid
    for key, record in data['reviews'].items():
        if not isinstance(key, str) or not isinstance(record, dict):
            raise invalid
        if record.get('status') not in ('pending', 'done') or type(record.get('revision')) is not int or record['revision'] < 0:
            raise invalid
        if not timestamp(record.get('registeredAt')):
            raise invalid
        for field in ('confirmedAt', 'reviewedUpdatedAt', 'pendingSince'):
            if record.get(field) is not None and not timestamp(record[field]):
                raise invalid
        if not isinstance(record.get('note', ''), str) or type(record.get('starred', False)) is not bool:
            raise invalid
    ids = set()
    for task in data['tasks']:
        if not isinstance(task, dict) or not isinstance(task.get('id'), str) or task['id'] in ids:
            raise invalid
        ids.add(task['id'])
        if task['id'] not in data['reviews'] or not isinstance(task.get('title'), str) or not timestamp(task.get('updatedAt')):
            raise invalid
    return data


def atomic(path, data):
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.file = self.path / 'state.json'

    @contextlib.contextmanager
    def lock(self):
        fd = os.open(self.path / 'state.lock', os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def read(self):
        if not self.file.exists():
            return {'schema': 1, 'reviews': {}, 'tasks': [], 'syncedAt': None}
        return validate(json.loads(self.file.read_text()))

    def write(self, data):
        validate(data)
        raw = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False).encode()
        if self.file.exists():
            previous = self.file.read_bytes()
            old = validate(json.loads(previous))
            # Preserve review changes independently of the frequently refreshed crash backup.
            # New task registration alone does not create a review-history snapshot.
            if any(data['reviews'].get(key) != record for key, record in old['reviews'].items()):
                history = self.path / 'backups' / 'review-history'
                history.mkdir(parents=True, exist_ok=True, mode=0o700)
                atomic(history / ('review-%020d-%s.json' % (time.time_ns(), uuid.uuid4().hex)), previous)
            atomic(self.file.with_suffix('.json.bak'), previous)
        atomic(self.file, raw)
        self._prune_review_history()

    def _prune_review_history(self):
        history = self.path / 'backups' / 'review-history'
        # Only generated history files are eligible; manual backups remain untouched.
        import re
        try:
            owned = sorted(p for p in history.glob('review-*.json')
                           if re.fullmatch(r'review-\d{20}-[0-9a-f]{32}\.json', p.name))
            for path in owned[:-30]:
                path.unlink()
        except OSError:
            # Retention cleanup must not turn an acknowledged atomic save into a failed action.
            pass

    def sync(self, tasks, started_at=None, *, source=None, workflow=False):
        with self.lock():
            data = self.read()
            # An older catalog request must not replace a newer successful catalog.
            if started_at is not None and data.get('syncStartedAt', 0) > started_at:
                return
            now = time.time()
            for task in tasks:
                data['reviews'].setdefault(task['id'], {
                    'status': 'pending', 'revision': 0, 'registeredAt': now,
                    'confirmedAt': None, 'reviewedUpdatedAt': None, 'note': '', 'starred': False,
                    **({'pendingSince': max(task['updatedAt'], task.get('execution', {}).get('endedAt', 0))} if workflow else {}),
                })
            if workflow:
                self._workflow(data, tasks, now)
            data['tasks'] = tasks
            data['syncedAt'] = now
            data['syncStartedAt'] = started_at if started_at is not None else now
            if source is not None:
                data['source'] = source
            data.pop('syncError', None)
            self.write(data)

    @staticmethod
    def _workflow(data, tasks, now):
        previous = {task['id']: task for task in data['tasks']}
        for task in tasks:
            review = data['reviews'][task['id']]
            before = dict(review)
            # Reverse only the retired automatic rule; manual confirmations have no such marker.
            if review.get('completionReason') == 'aged_30_days':
                review.update(status='pending', confirmedAt=None, reviewedUpdatedAt=None)
                review.pop('completionReason', None)
            execution = task.get('execution', {})
            state = execution.get('state', 'unknown')
            old_execution = previous.get(task['id'], {}).get('execution', {})
            activity = max(task['updatedAt'], execution.get('endedAt', 0))
            # Manual reopen grants a fresh review window. Initial imports use actual activity.
            pending_since = max(review.get('pendingSince', 0), activity)
            run_at = execution.get('startedAt', execution.get('eventAt', 0))
            if review['status'] == 'done' and state != 'unknown' and (state == 'running' or review.get('awaitingRunEnd') or run_at > (review.get('confirmedAt') or 0)):
                review.update(status='pending', confirmedAt=None, reviewedUpdatedAt=None)
                review.pop('completionReason', None)
                pending_since = max(pending_since, run_at)
            if (old_execution.get('state') == 'running' or review.get('awaitingRunEnd')) and state == 'idle':
                # Includes an interrupted/crashed run; its result still needs review.
                pending_since = max(pending_since, execution.get('endedAt') or now)
            if state == 'running':
                review['awaitingRunEnd'] = True
            elif state == 'idle':
                review.pop('awaitingRunEnd', None)
            if review['status'] == 'pending':
                review['pendingSince'] = pending_since
            if review != before:
                review['revision'] += 1
                review.pop('undo', None)

    def sync_failed(self, message, started_at):
        with self.lock():
            data = self.read()
            if data.get('syncStartedAt', 0) > started_at:
                return
            if data.get('syncError') != message:
                data['syncError'] = message
                self.write(data)

    def snapshot(self):
        with self.lock():
            return self._snapshot(self.read())

    def _snapshot(self, data):
        tasks = []
        for task in data['tasks']:
            review = self._public(data['reviews'][task['id']])
            review.setdefault('note', '')
            review.setdefault('starred', False)
            baseline = review.get('reviewedUpdatedAt')
            if baseline is None:
                baseline = review.get('confirmedAt')
            changed = review['status'] == 'done' and baseline is not None and task['updatedAt'] > baseline
            tasks.append(dict(task, review=review, changedSinceReview=changed))
        result = {'tasks': tasks, 'syncedAt': data.get('syncedAt'), 'syncError': data.get('syncError')}
        if 'source' in data:
            result['source'] = data['source']
        return result

    def archive(self, thread_id, revision, updated_at, confirmed, commit):
        if confirmed is not True:
            raise ValueError('请在二次确认窗口点击“确认归档”。')
        if not timestamp(updated_at):
            raise ValueError('无效的任务更新时间，请刷新列表。')
        # Serialize approvals and archives across plugin processes. A catalog read that
        # started before the acknowledged archive cannot overwrite the committed state.
        with self.lock():
            data = self.read()
            if not isinstance(thread_id, str) or type(revision) is not int or revision < 0:
                raise ValueError('无效的任务编号或版本。')
            review = data['reviews'].get(thread_id)
            if review is None:
                raise ValueError('未找到该任务，请刷新列表。')
            if review['status'] != 'pending':
                raise ValueError('任务已不在待审查，请核对最新状态。')
            task = next((t for t in data['tasks'] if t['id'] == thread_id), None)
            if task is None:
                raise ValueError('当前任务来源不可用，请刷新列表。')
            if task.get('execution', {}).get('state') == 'running':
                raise ValueError('任务仍在运行，请等它结束后再归档。')
            if not task.get('archived'):
                self._record(data, thread_id, revision)
                if task['updatedAt'] != updated_at:
                    raise ValueError('任务有新进展，请刷新并重新确认归档。')
                commit(dict(task))
                task['archived'] = True
                # Invalidate approvals queued from another window before this archive.
                review['revision'] += 1
                review.pop('undo', None)
                now = time.time()
                data['syncedAt'] = now
                data['syncStartedAt'] = now
                try:
                    self.write(data)
                except OSError as e:
                    raise OSError('聊天已归档，但本地列表保存失败。请刷新核对；原审查记录仍保留。') from e
            return dict(self._snapshot(data), threadId=thread_id, archived=True)

    def _record(self, data, thread_id, revision):
        if not isinstance(thread_id, str):
            raise ValueError('无效的任务编号。')
        old = data['reviews'].get(thread_id)
        if old is None:
            raise ValueError('未找到该任务，请刷新列表。')
        if type(revision) is not int or old['revision'] != revision:
            raise ValueError('该任务已在其他窗口更新，请核对最新状态后重试。')
        return old

    def mark(self, thread_id, status, revision):
        if status not in ('pending', 'done'):
            raise ValueError('无效的任务状态。')
        with self.lock():
            data = self.read()
            old = self._record(data, thread_id, revision)
            if old['status'] == status:
                return {'record': self._public(old), 'undo': None}
            current = next((t for t in data['tasks'] if t['id'] == thread_id), None)
            if current is None:
                raise ValueError('当前任务来源不可用，请同步后再确认。')
            if current.get('execution', {}).get('state') == 'running':
                raise ValueError('任务仍在运行，结束后会自动进入待审查。')
            token = uuid.uuid4().hex
            value = dict(old, status=status, revision=revision + 1,
                         confirmedAt=time.time() if status == 'done' else None,
                         reviewedUpdatedAt=current['updatedAt'] if status == 'done' else None)
            value.pop('completionReason', None)
            if status == 'pending':
                value['pendingSince'] = time.time()
            # Server-owned token restores the exact previous record, including confirmation time.
            value['undo'] = {'token': token, 'previous': {k: v for k, v in old.items() if k != 'undo'}}
            data['reviews'][thread_id] = value
            self.write(data)
            return {'record': self._public(value), 'undo': {'threadId': thread_id, 'token': token, 'expectedRevision': value['revision']}}

    @staticmethod
    def _public(record):
        return {k: v for k, v in record.items() if k != 'undo'}

    def undo(self, thread_id, token, revision):
        with self.lock():
            data = self.read()
            current = self._record(data, thread_id, revision)
            task = next((t for t in data['tasks'] if t['id'] == thread_id), {})
            if task.get('execution', {}).get('state') == 'running':
                raise ValueError('任务已开始运行，不能恢复旧审查状态。')
            action = current.get('undo')
            if not action or action['token'] != token:
                raise ValueError('此操作已失效，不能覆盖更新后的审查记录。')
            restored = dict(action['previous'], revision=revision + 1)
            data['reviews'][thread_id] = restored
            self.write(data)
            return {'record': self._public(restored), 'undo': None}

    def update(self, thread_id, revision, note=None, starred=None):
        if note is not None and (not isinstance(note, str) or len(note) > 2000):
            raise ValueError('审查备注最多 2000 字。')
        if starred is not None and type(starred) is not bool:
            raise ValueError('无效的重点标记。')
        if note is None and starred is None:
            raise ValueError('没有需要保存的内容。')
        with self.lock():
            data = self.read()
            old = self._record(data, thread_id, revision)
            value = dict(old)
            if note is not None:
                value['note'] = note
            if starred is not None:
                value['starred'] = starred
            if value != old:
                value.pop('undo', None)
                value['revision'] += 1
                data['reviews'][thread_id] = value
                self.write(data)
            return {'record': self._public(value)}
