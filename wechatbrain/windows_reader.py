# SPDX-License-Identifier: Apache-2.0
"""Read-only Windows adapter adapted from Chatlog's V3 module/pointer search.

Apache-2.0; see THIRD_PARTY_NOTICES.txt. Only same-user WeChat processes are read.
No injection, privilege changes, process writes, or secret logging are performed.
"""
from __future__ import annotations

import ctypes
import hashlib
import os
import re
import struct
import time
from pathlib import Path

from .sqlcipher import identify_key


def acquire_keys(databases, progress, cancelled=lambda: False, seconds=45, diagnostics=None):
    diag = diagnostics if diagnostics is not None else {}
    diag.update(platform=os.name, python_bits=struct.calcsize('P') * 8,
                processes=[], candidates_checked=0, scanned_bytes=0,
                read_failures=0, query_failures=0, matched_targets=0, status='starting')
    if os.name != 'nt':
        diag.update(status='unsupported_host', message='当前运行环境不是 Windows，未执行 Windows 微信连接。')
        progress(diag['message'])
        return []
    if struct.calcsize('P') != 8:
        diag.update(status='unsupported_runtime', message='当前软件运行环境不是 64 位，请使用 Windows x64 便携版。')
        progress('请使用 64 位 Windows 便携版读取')
        return []
    import psutil
    from ctypes import wintypes as w

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenProcess.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.CloseHandle.restype = w.BOOL
    kernel.ReadProcessMemory.argtypes = [w.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                        ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    kernel.ReadProcessMemory.restype = w.BOOL

    class MemoryInfo(ctypes.Structure):
        _fields_ = [('base', ctypes.c_void_p), ('allocation_base', ctypes.c_void_p),
                    ('allocation_protect', w.DWORD), ('partition', w.WORD),
                    ('size', ctypes.c_size_t), ('state', w.DWORD),
                    ('protect', w.DWORD), ('type', w.DWORD)]

    class ModuleInfo(ctypes.Structure):
        _fields_ = [('size', w.DWORD), ('module_id', w.DWORD), ('process_id', w.DWORD),
                    ('global_usage', w.DWORD), ('process_usage', w.DWORD),
                    ('base', ctypes.c_void_p), ('bytes', w.DWORD), ('handle', w.HANDLE),
                    ('name', w.WCHAR * 256), ('path', w.WCHAR * 260)]

    kernel.VirtualQueryEx.argtypes = [w.HANDLE, ctypes.c_void_p, ctypes.POINTER(MemoryInfo), ctypes.c_size_t]
    kernel.VirtualQueryEx.restype = ctypes.c_size_t
    kernel.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = w.HANDLE
    for name in ('Module32FirstW', 'Module32NextW'):
        fn = getattr(kernel, name)
        fn.argtypes = [w.HANDLE, ctypes.POINTER(ModuleInfo)]
        fn.restype = w.BOOL

    deadline = time.monotonic() + seconds
    budget = 512 * 1024 * 1024
    scanned = 0
    tested = set()
    found = []
    heads = []
    matched_heads = set()
    head_ids = set()
    # Prioritize real conversation/contact files over indexes and auxiliary data.
    ordered = sorted(databases, key=lambda p: (0 if Path(p).name.lower() in ('msg0.db', 'message_0.db')
                                               else 1 if Path(p).name.lower() in ('micromsg.db', 'contact.db') else 2))
    for path in ordered:
        try:
            with Path(path).open('rb') as source:
                page = source.read(4096)
            if len(page) >= 1024 and not page.startswith(b'SQLite format 3\x00'):
                identity=hashlib.sha256(page).digest()
                if identity not in head_ids:
                    heads.append(page)
                    head_ids.add(identity)
        except OSError:
            continue
    if not heads:
        diag.update(status='no_usable_database', message='没有找到完整的加密数据库首页，需检查所选文件是否完整。')
        return []
    hint = 'windows-v4' if any('db_storage' in Path(p).parts or re.fullmatch(r'message_\d+\.db', Path(p).name.lower()) for p in databases) else 'windows-v3'
    diag['source_layout'] = hint
    diag['target_count'] = len(heads)

    def finish():
        diag.update(scanned_bytes=scanned, elapsed_seconds=round(time.monotonic() - (deadline-seconds), 2),
                    stopped_by=('cancelled' if cancelled() else 'time_limit' if time.monotonic() >= deadline
                                else 'byte_limit' if scanned >= budget else 'completed'))
        if found:
            complete=len(matched_heads)==len(heads)
            diag.update(status='matched' if complete else 'matched_partial',
                message=f'已校验 {len(matched_heads)} / {len(heads)} 个数据库首页；'+
                    ('正在读取聊天记录。' if complete else '将先读取通过校验的记录，其余库仍需继续连接。'))
        elif cancelled():
            diag.update(status='cancelled', message='连接检查已取消。')
        elif not any(p.get('opened') for p in diag['processes']):
            diag.update(status='process_access_failed', message='检测到微信进程，但未能读取。请查看进程访问错误码；尚不能判断是否为账号或版本问题。')
        elif diag['read_failures'] and not scanned:
            diag.update(status='memory_read_failed', message='已打开微信进程，但 Windows 未返回可读数据。请查看进程权限及错误码。')
        else:
            diag.update(status='no_matching_material', message='已检测并扫描微信，但没有获得能通过所选数据库校验的连接信息；需要核对微信版本与数据格式。')
        progress(diag['message'])
        return found

    def stopped():
        return cancelled() or time.monotonic() >= deadline or scanned >= budget

    def read(handle, address, length):
        nonlocal scanned
        if stopped() or length <= 0:
            return b''
        length = min(length, budget-scanned)
        data = ctypes.create_string_buffer(length)
        got = ctypes.c_size_t()
        kernel.ReadProcessMemory(handle, address, data, length, ctypes.byref(got))
        scanned += got.value
        if not got.value:
            diag['read_failures'] += 1
            diag['last_read_error'] = ctypes.get_last_error()
        return data.raw[:got.value]

    def accept(raw):
        if stopped() or len(raw) not in (32, 48):
            return False
        identity = hashlib.sha256(raw).digest()
        if identity in tested:
            return False
        tested.add(identity)
        diag['candidates_checked'] += 1
        material = '0x' + raw.hex()
        for number,head in enumerate(heads):
            if stopped():break
            if number in matched_heads:continue
            if identify_key(head, material, hint=hint) is not None:
                matched_heads.add(number)
                if material not in found:found.append(material)
                diag['matched_targets']=len(matched_heads)
                progress(f'已连接 {len(matched_heads)} / {len(heads)} 个数据库，继续校验其余记录')
        # A positive candidate is retained, but ends the scan only after every
        # distinct database header is covered. V4 may have one key per database.
        return len(matched_heads)==len(heads)

    def module(pid, wanted, details):
        handle = kernel.CreateToolhelp32Snapshot(0x08 | 0x10, pid)
        if handle in (None, 0, ctypes.c_void_p(-1).value):
            details['module_error'] = ctypes.get_last_error()
            return None
        try:
            info = ModuleInfo()
            info.size = ctypes.sizeof(info)
            okay = kernel.Module32FirstW(handle, ctypes.byref(info))
            while okay:
                if info.name.lower() == wanted.lower():
                    return int(info.base), int(info.bytes)
                okay = kernel.Module32NextW(handle, ctypes.byref(info))
            details['module_search_error'] = ctypes.get_last_error()
        finally:
            kernel.CloseHandle(handle)
        return None

    def pointer_size(handle):
        if hasattr(kernel, 'IsWow64Process2'):
            fn = kernel.IsWow64Process2
            fn.argtypes = [w.HANDLE, ctypes.POINTER(w.WORD), ctypes.POINTER(w.WORD)]
            fn.restype = w.BOOL
            process_machine, native = w.WORD(), w.WORD()
            if fn(handle, ctypes.byref(process_machine), ctypes.byref(native)):
                return 4 if process_machine.value in (0x014c, 0x01c4) else 8
        fn = kernel.IsWow64Process
        fn.argtypes = [w.HANDLE, ctypes.POINTER(w.BOOL)]
        fn.restype = w.BOOL
        wow = w.BOOL()
        if not fn(handle, ctypes.byref(wow)):
            raise ValueError('无法确认微信进程位数')
        return 4 if wow.value else 8

    def regions(handle, begin, end, writable=False, private=False):
        address = begin
        while address < end and not stopped():
            info = MemoryInfo()
            if not kernel.VirtualQueryEx(handle, address, ctypes.byref(info), ctypes.sizeof(info)):
                diag['query_failures'] += 1
                diag['last_query_error'] = ctypes.get_last_error()
                break
            base, size = int(info.base or 0), int(info.size)
            following = base + size
            if following <= address:
                break
            address = following
            protection = info.protect & 0xff
            allowed = protection in ((0x04, 0x08, 0x40, 0x80) if writable else (0x02, 0x04, 0x08, 0x20, 0x40, 0x80))
            if info.state != 0x1000 or info.protect & 0x100 or not allowed:
                continue
            if private and info.type != 0x20000:
                continue
            left, right = max(begin, base), min(end, following)
            for offset in range(left, right, 1024 * 1024):
                if stopped():
                    break
                data = read(handle, offset, min(1024 * 1024 + 128, right-offset))
                if data:
                    yield data

    def file_version(path):
        if not path:
            return ''
        version = ctypes.WinDLL('version', use_last_error=True)
        version.GetFileVersionInfoSizeW.argtypes = [w.LPCWSTR, ctypes.POINTER(w.DWORD)]
        version.GetFileVersionInfoSizeW.restype = w.DWORD
        version.GetFileVersionInfoW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p]
        version.GetFileVersionInfoW.restype = w.BOOL
        version.VerQueryValueW.argtypes = [ctypes.c_void_p, w.LPCWSTR, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(w.UINT)]
        version.VerQueryValueW.restype = w.BOOL
        ignored = w.DWORD()
        size = version.GetFileVersionInfoSizeW(path, ctypes.byref(ignored))
        if not size or size > 1024 * 1024:
            return ''
        data = ctypes.create_string_buffer(size)
        if not version.GetFileVersionInfoW(path, 0, size, data):
            return ''
        pointer, length = ctypes.c_void_p(), w.UINT()
        if not version.VerQueryValueW(data, '\\', ctypes.byref(pointer), ctypes.byref(length)) or length.value < 16:
            return ''
        values = (w.DWORD * 4).from_address(pointer.value)
        if values[0] != 0xFEEF04BD:
            return ''
        return '.'.join(str(x) for x in (values[2] >> 16, values[2] & 65535, values[3] >> 16, values[3] & 65535))

    owner = psutil.Process().username().lower()
    processes = []
    for process in psutil.process_iter(['pid', 'name', 'username']):
        if str(process.info.get('name', '')).lower() not in ('wechat.exe', 'weixin.exe'):
            continue
        details = {'pid':process.pid, 'name':process.info['name'],
                   'same_user':str(process.info.get('username', '')).lower() == owner, 'opened':False}
        if details['same_user']:
            try:
                details['version'] = file_version(process.exe())
            except (psutil.Error, OSError, ValueError):
                details['version'] = ''
            processes.append((process, details))
        diag['processes'].append(details)
    if not processes:
        diag.update(status='no_same_user_process' if diag['processes'] else 'no_process',
                    message='没有检测到当前 Windows 用户可访问的微信主进程；这不代表记录不在本机。')
        progress(diag['message'])
        return []
    pattern = re.compile(rb"(?:x'|0x)([0-9a-fA-F]{64}(?:[0-9a-fA-F]{32})?)(?:'|\x00)")
    for process, details in processes:
        if stopped():
            break
        handle = kernel.OpenProcess(0x0400 | 0x0010, False, process.pid)
        if not handle:
            details['open_error'] = ctypes.get_last_error()
            continue
        details['opened'] = True
        try:
            details['pointer_bits'] = pointer_size(handle) * 8
            v3_module = module(process.pid, 'WeChatWin.dll', details)
            v4_module = module(process.pid, 'Weixin.dll', details)
            details['modules'] = [name for name, value in [('WeChatWin.dll', v3_module), ('Weixin.dll', v4_module)] if value]
            if hint=='windows-v3' and v4_module and not v3_module:
                diag['layout_warning']='所选目录为微信 3.x 布局，运行中检测到 4.x 模块；记录是否与当前客户端关联需要核对。'
            if v3_module:
                if hint == 'windows-v3':
                    progress('正在使用 Chatlog 3.x 读取方式连接本人微信')
                    width = pointer_size(handle)
                    marker = (32).to_bytes(width, 'little')
                    begin, size = v3_module
                    for data in regions(handle, begin, begin+size, writable=True):
                        at = len(data)
                        while not stopped():
                            at = data.rfind(marker, 0, at)
                            if at < width:
                                break
                            address = int.from_bytes(data[at-width:at], 'little')
                            maximum = 0x100000000 if width == 4 else 0x7fffffffffff
                            if 0x10000 < address < maximum:
                                if accept(read(handle, address, 32)):
                                    return finish()
            if v4_module or (not v3_module and process.info['name'].lower() == 'weixin.exe'):
                progress('正在使用微信 4.x 专用读取方式校验所选数据')
                marker = bytes(8) + (32).to_bytes(8, 'little') + (47).to_bytes(8, 'little')
                pointers = set()
                for data in regions(handle, 0x10000, 0x7fffffffffff, writable=True, private=True):
                    at = len(data)
                    while not stopped():
                        at = data.rfind(marker, 0, at)
                        if at < 8:
                            break
                        address = int.from_bytes(data[at-8:at], 'little')
                        if address in pointers or not 0x10000 < address < 0x7fffffffffff:
                            continue
                        pointers.add(address)
                        if accept(read(handle, address, 32)):
                            return finish()
            # Bounded compatibility fallback for textual SQLCipher keys and salt-adjacent material.
            progress('正在校验本人微信与所选文件夹的连接信息')
            for data in regions(handle, 0, 0x7fffffffffff):
                for view in (data, data.replace(b'\x00', b'')):
                    for match in pattern.finditer(view):
                        if accept(bytes.fromhex(match.group(1).decode('ascii'))):
                            return finish()
                        if stopped():
                            break
                for head in heads:
                    at = data.find(head[:16])
                    if at < 0:
                        continue
                    for pos in range(max(0, at-128), min(len(data)-31, at+128), 8):
                        if accept(data[pos:pos+32]):
                            return finish()
                        if stopped():
                            break
        finally:
            kernel.CloseHandle(handle)
    return finish()
