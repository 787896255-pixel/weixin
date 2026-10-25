"""Discover local V2 image material; candidates and account IDs stay in RAM.

Independently implements the observed local-metadata flow. This module does not
read other processes, change permissions, contact a service, or log secrets.
"""
from __future__ import annotations

import hashlib
import os
import re
import struct
import time
from pathlib import Path


SKIP = {'.git', '.obsidian', 'node_modules', '__pycache__', '.wechatbrain',
        '.build-cache', 'runtime'}


def _account_names(root):
    result = []
    for path in (root, *list(root.parents)[:6]):
        name = path.name.strip()
        if name.lower().startswith('wxid_'):
            parts = name.split('_')
            values = ['_'.join(parts[:2]), name]
        elif path.parent.name.lower() in ('xwechat_files', 'wechat files'):
            values = [re.sub(r'_[A-Za-z0-9]{4}$', '', name), name]
        else:
            continue
        for value in values:
            if value and value not in result: result.append(value)
    return result[:32]


def _metadata_dirs(root):
    candidates = []
    for parent in (root, *list(root.parents)[:6]):
        candidates += [parent/'net'/'kvcomm', parent/'app_data'/'net'/'kvcomm',
                       parent/'xwechat'/'net'/'kvcomm']
    # Current-user locations only; no all-drive or other-user traversal.
    for variable in ('APPDATA', 'LOCALAPPDATA'):
        value = os.environ.get(variable)
        if not value: continue
        base = Path(value)/'Tencent'
        candidates += [base/'xwechat'/'net'/'kvcomm',
                       base/'Weixin'/'net'/'kvcomm',
                       base/'WeChat'/'net'/'kvcomm']
    seen = set()
    for path in candidates:
        try:
            identity = str(path.resolve())
            if identity not in seen and path.is_dir() and not path.is_symlink():
                seen.add(identity)
                yield path
        except OSError:
            continue


def discover_image_keys(root, progress=lambda s: None, cancelled=lambda: False):
    """Return header-validated candidates and a report containing only counts.

    Final image decoding still verifies padding and the complete image. No
    candidate is treated as a successfully decoded media file here.
    """
    root = Path(root)
    report = {'status': 'not_needed', 'method': 'local-account-metadata',
              'templates': 0, 'metadata_directories': 0, 'candidates_checked': 0}
    templates = []
    names = _account_names(root)
    deadline = time.monotonic()+8
    examined = 0
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if d not in SKIP and not (Path(directory)/d).is_symlink()]
        if not names and Path(directory).name.lower().startswith('wxid_'):
            names = _account_names(Path(directory))
        for name in files:
            if cancelled() or time.monotonic() >= deadline or examined >= 50000: break
            examined += 1
            if not name.lower().endswith('.dat'): continue
            path = Path(directory)/name
            try:
                if path.is_symlink(): continue
                with path.open('rb') as stream: head = stream.read(31)
                if head.startswith(b'\x07\x08V2\x08\x07') and len(head) == 31:
                    plain, tail = struct.unpack('<II', head[6:14])
                    count = plain//16*16+16
                    if plain >= 16 and count+tail+15 <= path.stat().st_size:
                        if head[15:] not in templates: templates.append(head[15:])
            except OSError:
                continue
            if len(templates) >= 3: break
        if cancelled() or len(templates) >= 3 or time.monotonic() >= deadline or examined >= 50000: break
    report['templates'] = len(templates)
    if not templates: return [], report
    if cancelled(): report['status'] = 'cancelled'; return [], report
    if not names: report['status'] = 'account_not_identified'; return [], report
    progress('正在从本地微信账号材料连接新版图片')
    codes, entries = set(), 0
    deadline = time.monotonic()+8
    for folder in _metadata_dirs(root):
        report['metadata_directories'] += 1
        pending = [(folder, 0)]
        while pending:
            current, depth = pending.pop()
            try:
                with os.scandir(current) as listing:
                    for entry in listing:
                        if cancelled() or time.monotonic() >= deadline or entries >= 8000: break
                        entries += 1
                        for digits in re.findall(r'(?<!\d)\d{1,20}(?!\d)', entry.name):
                            value = int(digits)
                            if 0 < value <= 0xffffffffffffffff: codes.add(value)
                        if depth < 1 and entry.is_dir(follow_symlinks=False):
                            pending.append((Path(entry.path), depth+1))
            except OSError:
                continue
            if cancelled() or time.monotonic() >= deadline or entries >= 8000: break
        if cancelled() or time.monotonic() >= deadline or entries >= 8000: break
    if cancelled(): report['status'] = 'cancelled'; return [], report
    from Crypto.Cipher import AES
    from .media_codec import image_format
    keys = []
    checked = set()
    deadline = time.monotonic()+8
    for code in sorted(codes):
        for name in names:
            if cancelled() or time.monotonic() >= deadline or len(checked) >= 16000: break
            key = hashlib.md5((str(code)+name).encode('utf-8')).hexdigest()[:16].encode('ascii')
            if key in checked: continue
            checked.add(key)
            report['candidates_checked'] += 1
            cipher = AES.new(key, AES.MODE_ECB)
            heads = [cipher.decrypt(block) for block in templates]
            # A selected parent can contain several accounts. Each file is
            # decoded and fully validated with candidates again during ingest.
            if any(image_format(h) or h.startswith(b'wxgf') for h in heads):
                keys.append(key)
                if len(keys) >= 8: break
        if cancelled() or time.monotonic() >= deadline or len(checked) >= 16000 or len(keys) >= 8: break
    report['status'] = ('cancelled' if cancelled() else 'header_matched' if keys
                        else 'metadata_missing' if not codes else 'no_match')
    progress('已连接新版图片候选，继续校验完整内容' if keys else
             '尚未从本地账号材料匹配到新版图片，继续读取其他媒体')
    return keys, report
