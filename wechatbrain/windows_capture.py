# SPDX-License-Identifier: MIT
"""Optional WeChat 4.x startup capture with authenticated in-memory candidates.

The SHA-512 fallback is adapted from wechat-decrypt; direct object capture is
independently implemented from the documented static observations.
The user explicitly starts this mode. A caller may supply an explicitly
confirmed restart callback after capture setup. This adapter never changes the
client signature or stores/prints key bytes.
Only candidates authenticated against the selected databases enter RAM cache.
"""
from __future__ import annotations

import hashlib
import os
import queue
import re
import threading
import time
from pathlib import Path

from .sqlcipher import identify_key


def capture_keys(databases, keys, progress, cancelled=lambda: False, seconds=240,
                 ready=None, status=None):
    if os.name != 'nt':
        raise ValueError('新版微信连接目前需要在 Windows 运行；请复制读取诊断确认当前运行环境。')
    try:
        import frida
        import psutil
    except ImportError:
        raise ValueError('新版微信连接组件缺失，请使用完整便携包。') from None
    targets = []
    page_ids = set()
    ordered = sorted(databases, key=lambda p: (0 if Path(p).name.lower() == 'message_0.db'
                                              else 1 if Path(p).name.lower() in ('contact.db', 'micromsg.db', 'msg0.db') else 2))
    for path in ordered:
        try:
            with Path(path).open('rb') as source:
                page = source.read(4096)
            if len(page) == 4096 and not page.startswith(b'SQLite format 3\x00'):
                page_id = hashlib.sha256(page).digest()
                if page_id not in page_ids:
                    targets.append(page)
                    page_ids.add(page_id)
        except OSError:
            continue
    if not targets:
        raise ValueError('所选目录没有完整的加密数据库首页，请先选择实际的微信记录目录。')

    owner = psutil.Process().username().casefold()
    def own_processes():
        result = set()
        for p in psutil.process_iter(['pid', 'name']):
            if str(p.info.get('name', '')).casefold() != 'weixin.exe':
                continue
            try:
                if p.username().casefold() == owner:
                    result.add(p.pid)
            except psutil.Error:
                continue
        return result

    baseline = own_processes()
    device = frida.get_local_device()
    script_source = (Path(__file__).parent/'capture_v4.js').read_text(encoding='utf-8')
    script_source = 'if (Process.arch !== "x64") { throw new Error("UNSUPPORTED_TARGET_ARCH"); }\n' + script_source
    diag = {'mode':'windows-v4-startup', 'status':'waiting_for_restart', 'candidates_checked':0,
            'matched_targets':0, 'target_count':len(targets), 'sha512_tables':0, 'hooked_entries':0,
            'signature_matches':0, 'direct_key_entries':0, 'sha512_entries':0,
            'capture_method':'pending',
            'attached_processes':0,
            'message':'请保持本软件打开，手动退出微信，再从桌面重新打开并登录。'}
    def emit(message):
        diag['message'] = message
        progress(message)
        if status:
            status(dict(diag))

    emit(diag['message'])
    pending = queue.Queue(maxsize=512)
    failed = threading.Event()
    seen = set()
    matched = set()
    found = []
    sessions = []
    scripts = []
    attached = set()
    started = time.monotonic()

    def on_message(message, data):
        if message.get('type') == 'error':
            diag['status'] = 'script_error'
            diag['message'] = ('该连接方式目前仅适配 x64 微信进程。' if 'UNSUPPORTED_TARGET_ARCH' in message.get('description', '')
                               else '新版微信连接脚本未能正常运行，需进一步核对客户端版本。')
            failed.set()
            return
        if message.get('type') != 'send':
            return
        payload = message.get('payload')
        if not isinstance(payload, str):
            return
        if payload.startswith('KEY:'):
            candidate = payload[4:].lower()
            if re.fullmatch(r'[0-9a-f]{64}', candidate):
                try:
                    pending.put_nowait(candidate)
                except queue.Full:
                    diag['queue_full'] = True
            return
        # Only public counters/status are retained; no unfiltered script messages.
        match = re.search(r'SHA-512 K-tables found: (\d+)', payload)
        if match:
            diag['sha512_tables'] = int(match.group(1))
        match = re.search(r'hooked (\d+) SHA-512 entries', payload)
        if match:
            diag['sha512_entries'] = int(match.group(1))
            diag['hooked_entries'] = diag['sha512_entries'] + diag['direct_key_entries']
            diag['capture_method'] = 'combined' if diag['direct_key_entries'] else 'sha512-fallback'
            diag['status'] = 'waiting_for_login'
            emit('已连接新启动的微信，请完成微信登录；正在校验所选数据库。')
        match = re.search(r'direct-key entries matched: (\d+)', payload)
        if match:
            diag['signature_matches'] = int(match.group(1))
        match = re.search(r'hooked (\d+) direct-key entries', payload)
        if match:
            diag['direct_key_entries'] = int(match.group(1))
            diag['hooked_entries'] = diag['direct_key_entries'] + diag['sha512_entries']
            diag['capture_method'] = 'combined' if diag['sha512_entries'] else 'direct-key-object'
            diag['status'] = 'waiting_for_login'
            emit('已连接微信数据库入口，请完成登录；正在校验所选记录。')
        if payload.startswith('ERR:'):
            diag.update(status='version_not_supported', message='未能定位当前版本的数据库连接入口，需要继续适配该微信版本。')
            failed.set()

    try:
        if ready and not cancelled():
            ready()
        while time.monotonic()-started < seconds and not cancelled() and not failed.is_set():
            for pid in own_processes()-baseline-attached:
                try:
                    session = device.attach(pid)
                    sessions.append(session)
                    script = session.create_script(script_source)
                    scripts.append(script)
                    script.on('message', on_message)
                    script.load()
                    attached.add(pid)
                    diag['attached_processes'] += 1
                    diag['status'] = 'waiting_for_login'
                    emit('已连接新启动的微信，等待登录及数据库打开。')
                except Exception as exc:
                    # Public exception class only; never print Frida payloads or private state.
                    diag['attach_error'] = type(exc).__name__
                    attached.add(pid)
            try:
                candidate = pending.get(timeout=.08)
            except queue.Empty:
                continue
            if candidate in seen:
                continue
            seen.add(candidate)
            diag['candidates_checked'] += 1
            material = '0x' + candidate
            for number, page in enumerate(targets):
                if number in matched:
                    continue
                if cancelled() or time.monotonic()-started >= seconds:
                    break
                if identify_key(page, material) is not None:
                    matched.add(number)
                    if material not in found:
                        found.append(material)
                    if material not in keys:
                        keys.append(material)
            diag['matched_targets'] = len(matched)
            if matched:
                emit(f'已校验 {len(matched)} / {len(targets)} 个数据库，继续接收其余记录的连接信息。')
            if len(matched) == len(targets):
                break
    finally:
        for script in scripts:
            try:
                script.unload()
            except Exception:
                pass
        for session in sessions:
            try:
                session.detach()
            except Exception:
                pass
    diag['elapsed_seconds'] = round(time.monotonic()-started, 2)
    if found:
        diag.update(status='matched', message=f'已取得 {len(matched)} / {len(targets)} 个数据库的有效连接信息，正在尝试读取。')
    elif cancelled():
        diag.update(status='cancelled', message='新版微信连接已停止。')
    elif not failed.is_set():
        diag.update(status='restart_timeout' if not sessions else 'capture_timeout',
                    message='未检测到新启动的微信，请保持软件打开并按提示手动重开微信。' if not sessions
                    else '已连接微信，但在等待期间未取得能通过所选库校验的信息；需要根据诊断继续适配。')
    emit(diag['message'])
    return diag
