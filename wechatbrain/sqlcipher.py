# SPDX-License-Identifier: Apache-2.0
"""Authenticated SQLCipher snapshots, adapted from Chatlog (Apache-2.0).

See THIRD_PARTY_NOTICES.txt. Never modify a source database or expose key bytes.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import sqlite3
import struct
import tempfile
import time
from functools import lru_cache
from dataclasses import dataclass
from pathlib import Path

from Crypto.Cipher import AES
from .wal import index_wal, WALError

SQLITE_HEADER = b'SQLite format 3\x00'


@dataclass(frozen=True)
class Profile:
    name: str
    page_size: int
    reserve: int
    digest: str
    iterations: int


PROFILES = (
    Profile('windows-v3', 4096, 48, 'sha1', 64000),
    Profile('windows-v4', 4096, 80, 'sha512', 256000),
    Profile('legacy-1024', 1024, 48, 'sha1', 64000),
)


@dataclass(repr=False)
class KeyInfo:
    profile: Profile
    encryption_key: bytes
    mac_key: bytes
    authenticated: bool
    salt: bytes = b''


def _secret(material):
    value = material.strip()
    if value.lower().startswith('0x'):
        value = value[2:]
    elif not re.fullmatch(r'[a-fA-F0-9]{64}(?:[a-fA-F0-9]{32})?', value):
        return material.encode('utf-8'), False, None
    try:
        raw = bytes.fromhex(value)
    except ValueError:
        return b'', True, None
    if len(raw) not in (32, 48):
        return b'', True, None
    return raw[:32], True, raw[32:] or None


def _header_ok(page, key, profile):
    end = profile.page_size - profile.reserve
    if len(page) < profile.page_size:
        return False
    decoded = AES.new(key, AES.MODE_CBC, page[end:end+16]).decrypt(page[16:48])
    size = int.from_bytes(decoded[:2], 'big')
    return (size == profile.page_size and decoded[2] in (1, 2)
            and decoded[3] in (1, 2) and decoded[4] == profile.reserve
            and decoded[5:8] == b'\x40\x20\x20')


def _valid_mac(page, number, info):
    p = info.profile
    end = p.page_size - p.reserve + 16
    length = hashlib.new(p.digest).digest_size
    stored = page[end:end+length]
    if not info.authenticated:
        # Compatibility with the project's older synthetic unsigned samples only.
        return stored == bytes(length)
    offset = 16 if number == 1 else 0
    actual = hmac.new(info.mac_key, page[offset:end] + struct.pack('<I', number), p.digest).digest()
    return hmac.compare_digest(actual, stored)


@lru_cache(maxsize=128)
def identify_key(page, material, hint=None, allow_unsigned=False):
    """Validate a password / raw process secret / derived SQLCipher hex key.

    A WeChat V3 process secret is PBKDF2 input, not the derived AES key. Try both
    representations and accept only a page with a valid HMAC and SQLite header.
    """
    if page.startswith(SQLITE_HEADER) or len(page) < 1024:
        return None
    secret, hexadecimal, expected_salt = _secret(material)
    if not secret or (expected_salt is not None and expected_salt != page[:16]):
        return None
    profiles = [p for p in PROFILES if not hint or p.name == hint]
    for p in profiles:
        if len(page) < p.page_size:
            continue
        # Direct keys are cheap to reject; actual WeChat process secrets use KDF.
        keys = [secret] if hexadecimal else []
        keys.append(None)
        for key in keys:
            if key is None:
                key = hashlib.pbkdf2_hmac(p.digest, secret, page[:16], p.iterations, 32)
            if not _header_ok(page, key, p):
                continue
            mac_salt = bytes(b ^ 0x3a for b in page[:16])
            mac_key = hashlib.pbkdf2_hmac(p.digest, key, mac_salt, 2, 32)
            info = KeyInfo(p, key, mac_key, True, page[:16])
            if _valid_mac(page[:p.page_size], 1, info):
                return info
            if allow_unsigned:
                info.authenticated = False
                if _valid_mac(page[:p.page_size], 1, info):
                    return info
    return None


def match_database(path, material, hint=None, allow_unsigned=False):
    try:
        with Path(path).open('rb') as source:
            return identify_key(source.read(4096), material, hint, allow_unsigned) is not None
    except (OSError, ValueError):
        return False


def decrypt_database(path, material, output_dir, notes=None, cancelled=lambda: False, diagnostics=None, progress=None):
    """Stream to a private temporary file; authenticate every page before publishing.

    Standard SQLite WAL frames are indexed up to the last complete transaction.
    Selected encrypted pages are authenticated with their database page number.
    The original database and WAL remain untouched.
    """
    path = Path(path)
    with path.open('rb') as source:
        info = identify_key(source.read(4096), material)
    if info is None:
        return None
    p = info.profile
    size = path.stat().st_size
    if size == 0 or size % p.page_size:
        raise ValueError('数据库页面不完整，可能尚未传完：' + path.name)
    journal = Path(str(path) + '-journal')
    if journal.exists() and journal.stat().st_size:
        raise WALError('存在回滚日志，请正常退出微信后重新读取：' + path.name)
    wal = index_wal(Path(str(path) + '-wal'), p.page_size, size // p.page_size, cancelled)
    if diagnostics is not None:
        diagnostics['wal'] = dict(wal.report)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='verified-', suffix='.db', dir=output_dir)
    output = Path(temporary)
    try:
        with os.fdopen(fd, 'wb') as target, path.open('rb') as source:
            # The WAL is already in a private, verified source copy. Frame hashes
            # are checked again while consuming offsets retained by the index.
            log = wal.path.open('rb') if wal.pages else None
            try:
                last_progress=0
                for number in range(1, wal.database_pages + 1):
                    if progress and (time.monotonic()-last_progress>.5 or number==wal.database_pages):
                        progress(number,wal.database_pages);last_progress=time.monotonic()
                    if cancelled():
                        raise ValueError('已取消读取')
                    from_wal = number in wal.pages
                    if from_wal:
                        page = wal.read_page(log, number)
                    else:
                        source.seek((number - 1) * p.page_size)
                        page = source.read(p.page_size)
                    if len(page) != p.page_size:
                        raise ValueError('数据库页面缺失，无法合并日志：' + path.name)
                    if number == 1 and page[:16] != info.salt:
                        raise WALError('日志与数据库的首页标识不匹配：' + path.name)
                    if number != 1 and not from_wal and not any(page):
                        target.write(page)
                        continue
                    if not _valid_mac(page, number, info):
                        raise ValueError(f'数据库第 {number} 页校验失败，保留原文件：{path.name}')
                    end = p.page_size - p.reserve
                    offset = 16 if number == 1 else 0
                    plain = AES.new(info.encryption_key, AES.MODE_CBC, page[end:end+16]).decrypt(page[offset:end])
                    if number == 1:
                        plain = SQLITE_HEADER + plain
                        plain = plain[:18] + b'\x01\x01' + plain[20:]
                        if wal.report['transactions']:
                            plain = plain[:28] + struct.pack('>I', wal.database_pages) + plain[32:]
                    target.write(plain + page[end:])
            finally:
                if log is not None:
                    log.close()
            target.flush()
            os.fsync(target.fileno())
        connection = sqlite3.connect(output.resolve().as_uri() + '?mode=ro&immutable=1', uri=True)
        try:
            result = connection.execute('PRAGMA quick_check').fetchall()
            if result != [('ok',)]:
                raise ValueError('解密结果未通过数据库完整性检查：' + path.name)
        finally:
            connection.close()
        if notes is not None:
            suffix = '逐页 HMAC 校验通过' if info.authenticated else '旧版无签名样本，未提供 HMAC 保证'
            notes.append(f'已解密 {path.name} · {p.name} · {suffix}')
            if wal.report['transactions']:
                notes.append(f'{path.name} 已合并日志中的 {wal.report["transactions"]} 次完整提交、{len(wal.pages)} 个页面')
            if wal.report['ignored_uncommitted_frames'] or wal.report['tail']:
                notes.append(f'{path.name} 只读取最后一次完整提交，未提交或旧代日志尾部未纳入记录')
        return output
    except Exception:
        output.unlink(missing_ok=True)
        raise
