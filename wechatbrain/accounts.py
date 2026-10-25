"""Local account onboarding, reconstructed independently from observed UI flows.

An exporter service account and a local WeChat account are distinct. This module
opens the installed official client and imports selected local records; it never
fabricates QR/SMS authentication or calls the reference vendor's service.
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import threading
import time
from pathlib import Path

from .discovery import detect_sources
from .source_info import cache_info
from .store import digest

CLIENT_NAMES = {'weixin.exe', 'wechat.exe'}


def file_version(path):
    if os.name != 'nt':
        return ''
    from ctypes import wintypes as w
    try:
        api = ctypes.WinDLL('version', use_last_error=True)
        api.GetFileVersionInfoSizeW.argtypes = [w.LPCWSTR, ctypes.POINTER(w.DWORD)]
        api.GetFileVersionInfoSizeW.restype = w.DWORD
        api.GetFileVersionInfoW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p]
        api.GetFileVersionInfoW.restype = w.BOOL
        api.VerQueryValueW.argtypes = [ctypes.c_void_p, w.LPCWSTR, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(w.UINT)]
        api.VerQueryValueW.restype = w.BOOL
        ignored = w.DWORD()
        size = api.GetFileVersionInfoSizeW(str(path), ctypes.byref(ignored))
        if not size or size > 1024 * 1024:
            return ''
        data = ctypes.create_string_buffer(size)
        if not api.GetFileVersionInfoW(str(path), 0, size, data):
            return ''
        pointer, length = ctypes.c_void_p(), w.UINT()
        if not api.VerQueryValueW(data, '\\', ctypes.byref(pointer), ctypes.byref(length)) or length.value < 16:
            return ''
        values = ctypes.cast(pointer, ctypes.POINTER(w.DWORD))
        if values[0] != 0xFEEF04BD:
            return ''
        return '.'.join(str(n) for n in (values[2] >> 16, values[2] & 65535, values[3] >> 16, values[3] & 65535))
    except (OSError, ValueError):
        return ''


def client_processes():
    if os.name != 'nt':
        return []
    import psutil
    result = []
    owner = psutil.Process().username().casefold()
    for process in psutil.process_iter(['pid', 'name', 'username']):
        if str(process.info.get('name', '')).casefold() not in CLIENT_NAMES:
            continue
        if str(process.info.get('username', '')).casefold() != owner:
            continue
        try:
            path = Path(process.exe())
            result.append({'pid': process.pid, 'name': path.name, 'path': str(path),
                           'created': process.create_time()})
        except psutil.Error:
            continue
    return result


def installed_client():
    """Find an actual executable from running clients, registry or known paths."""
    if os.name != 'nt':
        return None
    candidates = [Path(p['path']) for p in client_processes()]
    import winreg
    for access in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for key_name in (r'Software\Tencent\Weixin', r'Software\Tencent\WeChat'):
                try:
                    with winreg.OpenKey(hive, key_name, 0, winreg.KEY_READ | access) as key:
                        for value_name in ('InstallPath', 'InstallLocation'):
                            try:
                                value = winreg.QueryValueEx(key, value_name)[0]
                                if isinstance(value, str) and value:
                                    directory = Path(os.path.expandvars(value.strip('"')))
                                    candidates.extend([directory, directory/'Weixin.exe', directory/'WeChat.exe'])
                            except OSError:
                                pass
                except OSError:
                    pass
    for variable in ('ProgramFiles', 'ProgramFiles(x86)', 'LOCALAPPDATA'):
        if os.environ.get(variable):
            root = Path(os.environ[variable])
            candidates.extend(root/'Tencent'/folder/name for folder, name in
                              [('Weixin', 'Weixin.exe'), ('WeChat', 'WeChat.exe')])
    for path in candidates:
        if path.name.casefold() not in CLIENT_NAMES or not path.is_file():
            continue
        try:
            with path.open('rb') as source:
                if source.read(2) != b'MZ':
                    continue
            return path.resolve()
        except OSError:
            continue
    return None


def environment():
    path = installed_client()
    processes = client_processes()
    return {'windows': os.name == 'nt', 'installed': path is not None,
            'client_path': str(path) if path else '', 'version': file_version(path) if path else '',
            'running': bool(processes), 'process_count': len(processes),
            # Process existence alone does not establish login or DB readability.
            'login_state': 'unknown', 'capture_supported': bool(path and path.name.casefold() == 'weixin.exe'),
            'message': '请在电脑版微信中登录，软件会自动识别本机账号。' if path else
                       '尚未找到已安装的电脑版微信，正在查找本机已保存的账号。'}


def launch_client(restart=False, confirmed=False):
    if os.name != 'nt':
        raise ValueError('打开和重启电脑版微信需要在 Windows 上运行京客导出助手。')
    path = installed_client()
    if not path:
        raise ValueError('未找到已安装的电脑版微信，请先安装微信，或直接选择已有记录文件夹。')
    if restart:
        if not confirmed:
            raise ValueError('请先确认重启微信；重启会中断当前收发消息和正在编辑的内容。')
        if path.name.casefold() != 'weixin.exe':
            raise ValueError('此启动读取方式目前仅用于微信 4.x；旧版微信请使用直接读取。')
        import psutil
        stopped = []
        # Re-resolve exact same-user/same-executable identities before terminating.
        # This action runs only after the user presses the explicit restart button.
        for item in client_processes():
            if os.path.normcase(item['path']) != os.path.normcase(str(path)):
                continue
            try:
                process = psutil.Process(item['pid'])
                if process.create_time() != item['created']:
                    continue
                if process.username().casefold() != psutil.Process().username().casefold():
                    continue
                process.terminate()
                stopped.append(process)
            except psutil.NoSuchProcess:
                pass
            except psutil.AccessDenied:
                raise ValueError('Windows 不允许重启该微信进程，请从微信菜单退出后使用“等待手动登录”。') from None
        _, alive = psutil.wait_procs(stopped, timeout=8)
        if alive:
            raise ValueError('微信尚未退出，请完成同步并从微信菜单退出，再重试读取。')
    subprocess.Popen([str(path)], cwd=str(path.parent), close_fds=True)
    return {'launched': True, 'restarted': restart, 'version': file_version(path)}


class AccountController:
    def __init__(self, app):
        self.app = app
        self.discovery_lock = threading.RLock()
        self.stop = threading.Event()
        self.discovery = {'status': 'idle', 'items': [], 'environment': {}, 'revision': 0,
                          'message': '正在自动查找本机微信账号…', 'diagnostics': {}}
        self.last_scan = 0.0

    def discover(self, refresh=False):
        """Schedule discovery; HTTP callers never wait for filesystem scanning."""
        with self.discovery_lock:
            interval = 30 if self.discovery['items'] else 5
            if self.discovery['status'] == 'scanning' or (not refresh and self.discovery['status'] == 'done'
                    and time.monotonic()-self.last_scan < interval):
                return self.discovery_snapshot()
            self.discovery.update(status='scanning', message='正在自动查找本机微信账号…')
        def execute():
            try:
                with self.app.store.connect() as db:
                    known = [row[0] for row in db.execute('SELECT path FROM sources')]
                known += self.app.store.settings().get('source_paths') or []
                value = detect_sources(known_paths=known, cancelled=self.stop.is_set)
                try: env = environment()
                except (OSError, RuntimeError): env = self.discovery_snapshot()['environment']
                with self.discovery_lock:
                    self.discovery.update(status='done', items=value['items'], environment=env,
                        message=value['message'], diagnostics=value['diagnostics'],
                        revision=self.discovery['revision']+1)
            except Exception:
                with self.discovery_lock:
                    self.discovery.update(status='done', message='自动检测暂未完成，软件会继续重试；也可点击重新检测。',
                        diagnostics={'error': 'discovery_unavailable'}, revision=self.discovery['revision']+1)
            finally:
                with self.discovery_lock: self.last_scan = time.monotonic()
        threading.Thread(target=execute, daemon=True, name='jingke-account-discovery').start()
        return self.discovery_snapshot()

    def discovery_snapshot(self):
        with self.discovery_lock:
            return dict(self.discovery, items=[dict(item) for item in self.discovery['items']],
                        environment=dict(self.discovery['environment']), diagnostics=dict(self.discovery['diagnostics']))

    def start(self):
        self.discover()
        def loop():
            while not self.stop.wait(5): self.discover()
        threading.Thread(target=loop, daemon=True, name='jingke-account-watch').start()

    def phase(self, phase, message, **values):
        with self.app.lock:
            flow = dict(self.app.account_flow or {})
            flow.update(phase=phase, message=message, **values)
            self.app.account_flow = flow
        self.app.progress(message)

    def catalog(self, with_environment=True, refresh=False):
        settings = self.app.store.settings()
        rows = self.app.store.stats()['sources']
        paths = {row['path']: row for row in rows}
        discovery = self.discover(refresh=refresh) if refresh or self.discovery_snapshot()['status']=='idle' else self.discovery_snapshot()
        detected = discovery['items']
        detected_by_path = {os.path.normcase(item['path']): item for item in detected}
        layouts = {item['path']: item['layout'] for item in detected}
        for raw in [*(settings.get('source_paths') or []), *(item['path'] for item in detected)]:
            normalized = str(Path(os.path.expandvars(os.path.expanduser(raw))).resolve())
            paths.setdefault(normalized, {'path': normalized, 'status': 'not_read'})
        items = []
        with self.app.store.connect() as db:
            for path, row in paths.items():
                sid = digest(path)
                counts = db.execute('''SELECT COUNT(DISTINCT m.id),COUNT(DISTINCT c.id)
                    FROM provenance p JOIN messages m ON m.id=p.message_id
                    JOIN conversations c ON c.id=m.conversation_id WHERE p.source_id=?
                    AND c.origin_kind IN ('wechat','chat_export')''', (sid,)).fetchone()
                conversation_count=db.execute('''SELECT count(*) FROM conversations c WHERE
                    c.origin_kind IN ('wechat','chat_export') AND (
                    EXISTS(SELECT 1 FROM conversation_sources s WHERE s.conversation_id=c.id AND s.source_id=?)
                    OR EXISTS(SELECT 1 FROM messages m JOIN provenance p ON p.message_id=m.id
                        WHERE m.conversation_id=c.id AND p.source_id=?))''',(sid,sid)).fetchone()[0]
                accounts = [r[0] for r in db.execute('''SELECT DISTINCT c.account FROM conversations c WHERE
                    c.origin_kind IN ('wechat','chat_export') AND (
                    EXISTS(SELECT 1 FROM conversation_sources s WHERE s.conversation_id=c.id AND s.source_id=?)
                    OR EXISTS(SELECT 1 FROM messages m JOIN provenance p ON p.message_id=m.id
                        WHERE m.conversation_id=c.id AND p.source_id=?)) ORDER BY c.account''',(sid,sid))]
                profile=db.execute('SELECT username,name,alias FROM account_profiles WHERE account=?',
                    (accounts[0],)).fetchone() if len(accounts)==1 else None
                avatar=db.execute('SELECT id FROM avatars WHERE account=? AND peer_id=?',
                    (accounts[0],profile['username'])).fetchone() if profile else None
                name = profile['name'] if profile else accounts[0] if len(accounts) == 1 else f'{len(accounts)} 个账号' if accounts else Path(path).name
                found = detected_by_path.get(os.path.normcase(path), {})
                automatic = bool(found.get('automatic') or
                    (accounts and row.get('kind') == 'Windows 微信记录'))
                items.append({'id': sid, 'path': path, 'name': name, 'account_ids': accounts,
                              'automatic': automatic, 'discovered_from': found.get('discovered_from','已有账号'),
                              'detected_account': found.get('account',''),
                              'messages': counts[0], 'conversations': conversation_count, 'ready': conversation_count > 0,
                              'alias':profile['alias'] if profile else '',
                              'avatar_url':'/api/avatar?id='+avatar['id'] if avatar else '',
                              'status': row['status'], 'exists': Path(path).exists(),
                              'layout': layouts.get(path, row.get('kind') or self.layout(path)),
                              'export_dir': (settings.get('account_export_dirs') or {}).get(sid, '') or
                                            settings.get('export_dir', '') or str(self.default_export(path)),
                              'updated': row.get('updated', '')})
        # A single-account parent imported by 0.13 already covers its discovered
        # child. Keep that cached identity, never merge unrelated account copies.
        covered = set()
        for item in items:
            if item['ready']: continue
            for parent in items:
                if parent['id']==item['id'] or not parent['ready'] or len(parent['account_ids'])!=1: continue
                if (item.get('detected_account') == parent['account_ids'][0]
                    and Path(item['path']).is_relative_to(Path(parent['path']))):
                    covered.add(item['id']); parent['automatic']=True; break
        items = [item for item in items if item['id'] not in covered]
        items.sort(key=lambda item:(not item['ready'], not item['automatic'], item['name']))
        return {'items': items, 'active_id': settings.get('active_source', ''),
                'discovery': {key: value for key, value in discovery.items() if key not in ('items','environment')},
                'environment': discovery['environment'] if with_environment else None}

    def automatic(self, identity=''):
        items = self.catalog(False)['items']
        available = [item for item in items if item['automatic'] and item['exists']]
        if identity:
            item = next((item for item in available if item['id']==identity), None)
            if item: return item
            raise ValueError('该微信账号暂不可用，正在重新检测；已有缓存仍可直接查看。')
        if len(available)==1: return available[0]
        if len(available)>1: raise ValueError('检测到多个微信账号，请选择要读取的账号。')
        raise ValueError('正在查找本机微信账号，请打开电脑版微信并登录，软件会自动继续检测。')

    @staticmethod
    def layout(path):
        root = Path(path)
        if root.is_file() and cache_info(root):return '原导出器聊天缓存'
        if (root/'db_storage').is_dir():
            return 'Windows 微信 4.x'
        if (root/'Msg').is_dir():
            return 'Windows 微信 3.x'
        if (root/'Manifest.db').is_file():
            return 'iPhone 本地备份'
        return '聊天记录文件夹'

    @staticmethod
    def default_export(path):
        import re
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', Path(path).name).strip('. ')[:80] or '微信账号'
        return Path.home()/'Documents'/'京客聊天导出'/name

    def open(self, identity, folder=''):
        item = next((a for a in self.catalog(False)['items'] if a['id'] == identity), None)
        if not item or not item['ready']:
            raise ValueError('这个账号尚未读到聊天记录，请先完成读取。')
        destination = Path(folder or item['export_dir']).expanduser().resolve()
        root = Path(item['path']).resolve()
        if destination == root or root in destination.parents:
            raise ValueError('请选择聊天记录源文件夹以外的导出目录。')
        destination.mkdir(parents=True, exist_ok=True)
        directories = self.app.store.settings().get('account_export_dirs') or {}
        directories[item['id']] = str(destination)
        self.app.store.save_settings({'active_source': item['id'], 'export_dir': str(destination),
                                      'account_export_dirs': directories})
        item['export_dir'] = str(destination)
        return item

    def read(self, path, mode='read', password='', owner='', restart_confirmed=False, automatic=False):
        from .importer import Importer
        root = Path(os.path.expandvars(os.path.expanduser(path))).resolve()
        self.phase('checking', '正在连接自动识别的微信账号…' if automatic else '正在检查账号目录与电脑微信…',
                   active=True, path=str(root), account=None, automatic=automatic)
        try:
            if not root.exists():
                raise ValueError('记录目录不存在；如果已有导入缓存，可返回账号列表直接查看。')
            self.app.store.save_settings({'source_paths': [str(root)], 'owner': owner})
            if mode in ('capture', 'restart'):
                action = (lambda: launch_client(True, restart_confirmed)) if mode == 'restart' else None
                self.phase('waiting_login', '请重新打开电脑版微信并完成登录，软件会继续读取。')
                result = self.app.connect_v4([str(root)], ready=action, status=self.capture_status,
                                             owner=owner, backup_password=password)
            else:
                self.phase('reading', '正在读取联系人和聊天记录…')
                result = Importer(self.app.store, self.app.progress, self.app.cancel.is_set,
                                  self.app.reader_keys).run([str(root)], password, owner)
            if self.app.cancel.is_set():
                self.phase('cancelled', '读取已停止，已导入的记录仍可查看。', active=False)
                return result
            sources = result.get('sources') or []
            account = next((a for a in self.catalog(False)['items'] if a['id'] == digest(str(root))), None)
            read = sum(s.get('read', 0) for s in sources)
            pending = sum(len(s.get('unread_files') or []) for s in sources)
            database_results = [d for s in sources for d in s.get('database_results', [])]
            journal = next((d for d in database_results if d.get('status') == 'rollback_journal'), None)
            retry = next((d for d in database_results if d.get('status') in (
                'snapshot_changed', 'snapshot_unreadable', 'wal_validation_failed',
                'page_authentication_failed', 'incomplete_file', 'snapshot_validation_failed')), None)
            if account and account['ready'] and (read or any(s.get('identity',{}).get('sessions') for s in sources)):
                self.phase('confirm', f'已读取 {account["conversations"]} 个会话、{account["messages"]:,} 条消息。',
                           active=False, account=account, partial=bool(pending or any(s.get('errors') or s.get('status')=='partial' for s in sources)))
            elif journal:
                self.phase('close_wechat', journal.get('message') or '存在回滚日志，请正常退出微信后继续读取。',
                           active=False, account=account)
            elif retry:
                self.phase('retry_read', retry.get('message') or '本次记录副本未通过校验，请重新读取；若仍失败，请正常退出微信后重试。',
                           active=False, account=account)
            elif pending or result.get('capture_diagnostics'):
                diagnostic = result.get('capture_diagnostics') or next((s.get('diagnostics', {}) for s in sources), {})
                phase='needs_connection' if diagnostic.get('status') in ('no_process','no_same_user_process','unsupported_host') else 'needs_restart'
                self.phase(phase, diagnostic.get('message') or '尚未获得所选数据库的有效连接，请查看读取诊断。',
                           active=False, account=account)
            else:
                errors = [e for s in sources for e in s.get('errors', [])]
                self.phase('error', errors[0] if errors else '目录内尚未读到可用的聊天记录，请检查记录格式或选择其他文件夹。',
                           active=False, account=account)
            return result
        except Exception as exc:
            self.phase('error', str(exc) if isinstance(exc, (ValueError, OSError)) else '账号读取未完成，请查看处理结果。', active=False)
            raise

    def capture_status(self, diagnostic):
        phase = ('reading' if diagnostic.get('status') == 'reading' else 'connecting'
                 if diagnostic.get('status') == 'matched' or diagnostic.get('matched_targets', 0) else 'waiting_login')
        self.phase(phase, diagnostic.get('message', '正在等待微信登录和数据库连接…'),
                   matched=diagnostic.get('matched_targets', 0), total=diagnostic.get('target_count', 0))
