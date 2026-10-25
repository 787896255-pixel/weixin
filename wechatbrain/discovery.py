"""Discover native WeChat accounts from configuration and bounded directory roots.

Discovery reads directory metadata and small WeChat path configuration files.
It never opens chat databases or walks attachment/document trees.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

CONTAINERS = {'wechat files', 'xwechat_files'}
SKIP = {'all users', 'all_users', 'applet', 'filestorage', 'msgattach', 'attach',
        'image', 'video', 'file', 'cache', 'backup', 'temp', 'db_storage', 'msg'}
CONFIG_FOLDERS = ('Tencent/WeChat/All Users/config', 'Tencent/Weixin/All Users/config',
                  'Tencent/xwechat/config')


def native_layout(path):
    try:
        if (path/'db_storage').is_dir(): return 'Windows 微信 4.x'
        if (path/'Msg').is_dir(): return 'Windows 微信 3.x'
    except OSError:
        pass
    return ''


def _documents():
    roots = [Path.home()/'Documents']
    if os.name != 'nt': return roots
    import winreg
    for key_name in (r'Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders',
                     r'Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders'):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_name) as key:
                value = winreg.QueryValueEx(key, 'Personal')[0]
                if isinstance(value, str) and value: roots.insert(0, Path(os.path.expandvars(value)))
        except OSError:
            pass
    # Read-only known-folder lookup, not a file-picker dialog.
    try:
        import ctypes
        from ctypes import wintypes as w
        shell = ctypes.WinDLL('shell32'); buffer = ctypes.create_unicode_buffer(32768)
        shell.SHGetFolderPathW.argtypes = [w.HWND, ctypes.c_int, w.HANDLE, w.DWORD, w.LPWSTR]
        shell.SHGetFolderPathW.restype = ctypes.c_long
        if shell.SHGetFolderPathW(None, 5, None, 0, buffer) == 0 and buffer.value:
            roots.insert(0, Path(buffer.value))
    except (OSError, ValueError):
        pass
    for variable in ('OneDrive', 'OneDriveConsumer', 'OneDriveCommercial'):
        if os.environ.get(variable): roots.append(Path(os.environ[variable])/'Documents')
    return roots


def _registry_roots():
    if os.name != 'nt': return []
    import winreg
    roots = []
    for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
        for key_name in (r'Software\Tencent\WeChat', r'Software\Tencent\Weixin',
                         r'Software\Tencent\xwechat'):
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_name, 0, winreg.KEY_READ | view) as key:
                    for name in ('FileSavePath', 'DataPath', 'DataDir', 'SavePath'):
                        try:
                            value = winreg.QueryValueEx(key, name)[0]
                            if isinstance(value, str) and value and value.casefold() != 'mydocument:':
                                roots.append((os.path.expandvars(value.strip('" ')), '微信配置'))
                        except OSError:
                            pass
            except OSError:
                pass
    return roots


def _path_values(raw):
    # Only absolute path-shaped values are returned; unrelated configuration
    # is neither retained nor included in diagnostics.
    encodings = ('utf-16', 'utf-16-le', 'utf-8-sig') if raw.startswith((b'\xff\xfe', b'\xfe\xff')) or raw.count(b'\x00') > len(raw)//5 else ('utf-8-sig',)
    for encoding in encodings:
        try: text = raw.decode(encoding)
        except UnicodeError: continue
        if '\\\\' in text and '"' in text:
            try:
                import json
                parsed = json.loads(text)
                def strings(value):
                    if isinstance(value, str): yield value
                    elif isinstance(value, dict):
                        for item in value.values(): yield from strings(item)
                    elif isinstance(value, list):
                        for item in value[:128]: yield from strings(item)
                text = '\n'.join(strings(parsed))
            except (ValueError, RecursionError):
                pass
        for match in re.finditer(r'(?:[A-Za-z]:[\\/]|\\\\[^\\/\x00\r\n"<>|]+[\\/])[^\x00\r\n"<>|;]*', text):
            value = match.group().strip().rstrip(',\t ')
            if len(value) <= 2048: yield value
        if os.name != 'nt' and text.strip().startswith('/') and '\n' not in text.strip():
            yield text.strip()


def _configured_roots(documents, stopped, diagnostics):
    directories = []
    if os.environ.get('APPDATA'):
        directories += [Path(os.environ['APPDATA'])/name for name in CONFIG_FOLDERS]
    for document in documents:
        for container in ('WeChat Files', 'xwechat_files'):
            directories.append(document/container/'All Users/config')
            directories.append(document/container/'all_users/config')
    result = []; seen = set(); total = 0
    for directory in directories:
        if stopped(): break
        try:
            key = os.path.normcase(str(directory.resolve()))
            if key in seen or not directory.is_dir(): continue
            seen.add(key)
            with os.scandir(directory) as entries:
                for index, entry in enumerate(entries):
                    if stopped() or index >= 64: break
                    if not entry.is_file(follow_symlinks=False) or Path(entry.name).suffix.casefold() not in ('.ini', '.txt', '.conf', '.json', '.xml', '.config', '.dat'): continue
                    size = entry.stat(follow_symlinks=False).st_size
                    if size > 256*1024 or total+size > 1024*1024: continue
                    with open(entry.path, 'rb') as stream: raw = stream.read(256*1024+1)
                    total += len(raw); diagnostics['config_files'] += 1
                    result.extend((value, '微信配置') for value in _path_values(raw))
        except (OSError, ValueError):
            continue
    return result


def _drive_roots():
    if os.name != 'nt': return []
    import ctypes
    from ctypes import wintypes as w
    api = ctypes.WinDLL('kernel32')
    api.GetLogicalDrives.restype = w.DWORD
    api.GetDriveTypeW.argtypes = [w.LPCWSTR]; api.GetDriveTypeW.restype = w.UINT
    mask = api.GetLogicalDrives(); result = []
    # Fixed/removable drive metadata only. Configured network paths are admitted
    # above; unconfigured network drives are not enumerated.
    for index in range(26):
        drive = chr(65+index)+':\\'
        if mask & (1 << index) and api.GetDriveTypeW(drive) in (2, 3):
            for name in ('WeChat Files', 'xwechat_files', '微信聊天记录', '微信文件', 'WeChat', 'Weixin'):
                result.append((str(Path(drive)/name), '磁盘常用位置'))
    return result


def detect_sources(known_paths=None, cancelled=lambda: False, seconds=8, include_caches=False):
    started = time.monotonic(); deadline = started+seconds
    stopped = lambda: cancelled() or time.monotonic() >= deadline
    diagnostics = {'roots_checked': 0, 'config_files': 0, 'complete': True}
    documents = _documents()
    roots = [(str(path), '已有账号') for path in (known_paths or [])]
    roots += _registry_roots()
    roots += _configured_roots(documents, stopped, diagnostics)
    for folder in documents:
        roots += [(str(folder/name), '系统文档') for name in ('WeChat Files', 'xwechat_files')]
    roots += [(str(Path.home()/'xwechat_files'), '用户目录')]
    roots += _drive_roots()
    result = []; seen = set(); inspected = set(); queue = []
    for raw, origin in roots:
        path = Path(os.path.expandvars(str(raw))).expanduser()
        for candidate in (path, *list(path.parents)[:5]):
            if candidate.name.casefold() in ('db_storage', 'msg'):
                path = candidate.parent; break
        queue.extend((candidate, origin, 0) for candidate in (path, path/'WeChat Files', path/'xwechat_files'))
    index = 0
    while index < len(queue):
        if stopped(): diagnostics['complete'] = False; break
        path, origin, depth = queue[index]; index += 1
        try:
            key = os.path.normcase(str(path))
            if key in inspected or path.name.casefold() in SKIP: continue
            inspected.add(key); diagnostics['roots_checked'] += 1
            if not path.is_dir() or path.is_symlink(): continue
            layout = native_layout(path)
            if layout:
                resolved = str(path.resolve()); identity = os.path.normcase(resolved)
                if identity not in seen:
                    seen.add(identity); result.append({'path': resolved, 'account': path.name,
                        'layout': layout, 'automatic': True, 'discovered_from': origin})
                continue
            if depth >= 2: continue
            with os.scandir(path) as entries:
                for number, entry in enumerate(entries):
                    if stopped() or number >= 1024:
                        diagnostics['complete'] = False; break
                    if entry.name.casefold() not in SKIP and entry.is_dir(follow_symlinks=False):
                        queue.append((Path(entry.path), origin, depth+1))
        except (OSError, ValueError):
            diagnostics['complete'] = False
    if include_caches and not stopped():
        from .source_info import discover_caches
        for item in discover_caches():
            if os.path.normcase(item['path']) not in seen:
                item['automatic'] = False; result.append(item)
    diagnostics['elapsed_seconds'] = round(time.monotonic()-started, 3)
    return {'items': result, 'diagnostics': diagnostics,
            'message': '已自动找到 '+str(len(result))+' 个本机微信账号' if result else '请打开电脑版微信并登录，软件会自动继续查找账号。'}
