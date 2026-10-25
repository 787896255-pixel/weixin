"""SQLite WAL frame verification and committed-page indexing, without writes.

Independent implementation from sqlite.org/fileformat2.html#walformat.
Page payloads may be encrypted; this module does not hold decryption keys.
"""
from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass
from pathlib import Path

MAX_FRAMES = 262144


class WALError(ValueError):
    status = 'wal_validation_failed'


def _checksum(data, state, endian):
    first, second = state
    for left, right in struct.iter_unpack(endian + 'II', data):
        first = (first + left + second) & 0xffffffff
        second = (second + right + first) & 0xffffffff
    return first, second


@dataclass(frozen=True)
class Frame:
    offset: int
    digest: bytes


@dataclass
class WALIndex:
    path: Path
    page_size: int
    pages: dict
    database_pages: int
    report: dict

    def read_page(self, stream, number):
        frame = self.pages[number]
        stream.seek(frame.offset)
        raw = stream.read(24 + self.page_size)
        if (len(raw) != 24 + self.page_size
                or not hashlib.sha256(raw).digest() == frame.digest
                or int.from_bytes(raw[:4], 'big') != number):
            raise WALError('日志在读取期间发生变化，请重新读取：' + self.path.name)
        return raw[24:]


def index_wal(path, page_size, base_pages, cancelled=lambda: False):
    """Retain latest pages at the last complete commit; validate rolling checksums."""
    path = Path(path)
    report = {'status': 'absent', 'frames': 0, 'transactions': 0,
              'overlay_pages': 0, 'ignored_uncommitted_frames': 0, 'tail': None}
    index = WALIndex(path, page_size, {}, base_pages, report)
    if not path.exists() or not path.stat().st_size:
        return index
    with path.open('rb') as stream:
        header = stream.read(32)
        if len(header) != 32:
            raise WALError('日志头不完整，请重新复制记录与日志：' + path.name)
        magic, version, size, _, salt1, salt2, check1, check2 = struct.unpack('>8I', header)
        endian = {0x377f0682: '<', 0x377f0683: '>'}.get(magic)
        if endian is None or version != 3007000 or size != page_size:
            raise WALError('日志格式或页面大小不匹配：' + path.name)
        state = _checksum(header[:24], (0, 0), endian)
        if state != (check1, check2):
            raise WALError('日志头校验失败：' + path.name)
        report['status'] = 'no_commit'
        pending, pending_frames = {}, 0
        while True:
            if cancelled():
                raise WALError('已取消读取')
            offset = stream.tell()
            frame_header = stream.read(24)
            if not frame_header:
                break
            if len(frame_header) != 24:
                report['tail'] = 'incomplete_frame'
                break
            number, commit_size, first_salt, second_salt, first_check, second_check = struct.unpack('>6I', frame_header)
            if (first_salt, second_salt) != (salt1, salt2):
                # After a WAL reset, unused frames from an older generation remain.
                report['tail'] = 'previous_generation'
                break
            page = stream.read(page_size)
            if len(page) != page_size:
                report['tail'] = 'incomplete_frame'
                break
            if not 0 < number <= 0xfffffffe or commit_size > 0xfffffffe:
                raise WALError('日志页号无效：' + path.name)
            state = _checksum(frame_header[:8], state, endian)
            state = _checksum(page, state, endian)
            if state != (first_check, second_check):
                raise WALError('日志帧校验失败，请重新复制记录与日志：' + path.name)
            report['frames'] += 1
            if report['frames'] > MAX_FRAMES:
                raise WALError('日志超过本次读取上限，请正常退出微信、等待日志合并后重试：' + path.name)
            pending[number] = Frame(offset, hashlib.sha256(frame_header + page).digest())
            pending_frames += 1
            if commit_size:
                index.pages.update((n, frame) for n, frame in pending.items() if n <= commit_size)
                if commit_size < index.database_pages:
                    index.pages = {n: frame for n, frame in index.pages.items() if n <= commit_size}
                index.database_pages = commit_size
                report['transactions'] += 1
                report['last_commit_frame']=report['frames']
                report['status'] = 'committed'
                pending.clear()
                pending_frames = 0
        report['ignored_uncommitted_frames'] = pending_frames
        report['overlay_pages'] = len(index.pages)
        report['database_pages'] = index.database_pages
        # Prevent a corrupt commit marker from creating enormous sparse output.
        if index.database_pages > base_pages + len(index.pages) + 1:
            raise WALError('日志声明的数据库页数与现有页面不一致：' + path.name)
    return index
