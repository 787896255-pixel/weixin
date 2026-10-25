"""Bounded, read-only copies of a database and its changing sidecars.

Metadata and a second content pass detect ordinary concurrent writes. This is
not an atomic filesystem snapshot; cryptographic/page and SQLite checks still
run on the private copy. No SQLite connection is opened against the original.
"""
from __future__ import annotations

import hashlib
import stat
import time
from pathlib import Path


class SnapshotError(ValueError):
    status = 'snapshot_changed'


class RollbackJournalError(SnapshotError):
    status = 'rollback_journal'


def _inventory(source, suffixes):
    result = {}
    for suffix in suffixes:
        path = Path(str(source) + suffix)
        try:
            item = path.lstat()
        except FileNotFoundError:
            if not suffix:
                raise SnapshotError('记录文件已移动：' + source.name)
            result[suffix] = None
            continue
        if not stat.S_ISREG(item.st_mode):
            raise SnapshotError('记录或相邻日志不是普通文件：' + path.name)
        result[suffix] = (item.st_dev, item.st_ino, item.st_size,
                          item.st_mtime_ns, item.st_ctime_ns)
    return result


def _hash_copy(source, target=None, cancelled=lambda: False, progress=None, phase='copy',limit=None):
    checksum = hashlib.sha256()
    output = target.open('wb') if target is not None else None
    try:
        done=0;last=0;total=source.stat().st_size if limit is None else limit
        with source.open('rb') as stream:
            while True:
                if cancelled():
                    raise SnapshotError('已取消读取')
                if limit is not None and done>=limit:break
                chunk = stream.read(min(1024*1024,limit-done) if limit is not None else 1024*1024)
                if not chunk:
                    if limit is not None and done<limit:raise SnapshotError('读取期间文件被截短：'+source.name)
                    break
                checksum.update(chunk)
                if output is not None:
                    output.write(chunk)
                done+=len(chunk)
                if progress and (time.monotonic()-last>.5 or done>=total):
                    progress(done,total,phase);last=time.monotonic()
    finally:
        if output is not None:
            output.close()
    return checksum.digest()


def _compatible(before,after,database):
    for suffix,old in before.items():
        current=after.get(suffix)
        if database and suffix=='-wal' and old and current:
            # The prefix is independently hashed twice; new frames after the
            # captured boundary belong to the next synchronization pass.
            if current[:2]!=old[:2] or current[2]<old[2]:return False
        elif current!=old:return False
    return True


def copy_snapshot(source, target, database=False, cancelled=lambda: False, attempts=3, progress=None):
    """Copy to caller-owned staging paths; remove partial copies on every failure."""
    source, target = Path(source), Path(target)
    suffixes = ('', '-wal', '-journal') if database else ('',)
    if source.resolve() == target.resolve():
        raise SnapshotError('临时副本不能写入原记录位置')
    target.parent.mkdir(parents=True, exist_ok=True)
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            before = _inventory(source, suffixes)
            # SHM is a transient index. SQLite rebuilds it in the private folder.
            for suffix in (*suffixes, '-shm'):
                Path(str(target) + suffix).unlink(missing_ok=True)
            copied = {}
            for suffix, metadata in before.items():
                if metadata is not None:
                    copied[suffix] = _hash_copy(Path(str(source) + suffix),
                        Path(str(target) + suffix), cancelled, progress, 'copy',metadata[2] if suffix=='-wal' else None)
            if not _compatible(before,_inventory(source,suffixes),database):
                raise SnapshotError('读取期间记录或日志发生变化：' + source.name)
            for suffix, checksum in copied.items():
                if _hash_copy(Path(str(source) + suffix), cancelled=cancelled, progress=progress, phase='verify',
                              limit=before[suffix][2] if suffix=='-wal' else None) != checksum:
                    raise SnapshotError('读取期间记录内容发生变化：' + source.name)
            if not _compatible(before,_inventory(source,suffixes),database):
                raise SnapshotError('校验期间记录或日志发生变化：' + source.name)
            journal = before.get('-journal')
            if journal is not None and journal[2]:
                raise RollbackJournalError('存在回滚日志，请正常退出微信后重新读取：' + source.name)
            return {'status': 'copied', 'attempts': attempt,
                    'bytes': sum(m[2] for m in before.values() if m is not None),
                    'wal_bytes': (before.get('-wal') or (0, 0, 0))[2],
                    'verification': 'metadata-and-double-content-pass'}
        except (OSError, SnapshotError) as exc:
            last_error = exc
            for suffix in (*suffixes, '-shm'):
                Path(str(target) + suffix).unlink(missing_ok=True)
            if cancelled() or isinstance(exc, RollbackJournalError):
                raise
    raise SnapshotError('无法取得稳定副本，请正常退出微信后重试：' + source.name) from last_error
