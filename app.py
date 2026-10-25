#!/usr/bin/env python3
"""京客导出助手 Windows 桌面入口；--headless 提供本机浏览器界面。"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from wechatbrain import __version__
from wechatbrain.store import Store
from wechatbrain.importer import Importer
from wechatbrain.classify import AIClassifier
from wechatbrain.exporter import export_markdown
from wechatbrain.exporter import export_insights
from wechatbrain.analyze import MemoryAnalyzer,fingerprint
from wechatbrain.archive import export_archive,export_contacts
from wechatbrain.schedule import Scheduler
from wechatbrain.accounts import AccountController
from wechatbrain.media import MediaLibrary
from wechatbrain.live import LiveSync

ROOT=Path(getattr(sys,'_MEIPASS',Path(__file__).resolve().parent))


class Application:
    def __init__(self,directory,demo=False):
        self.store=Store(directory);self.token=secrets.token_urlsafe(32)
        self.job=None;self.lock=threading.Lock();self.cancel=threading.Event()
        self.api_key='';self.demo=demo;self.window=None
        self.reader_keys=[]  # In-memory only; reused for subsequent verified snapshots.
        self.account_flow=None
        self.accounts=AccountController(self)
        self.media=MediaLibrary(self.store,self.progress,self.cancel.is_set)
        self.scheduler=Scheduler(self)
        self.live=LiveSync(self)

    def progress(self,text):
        details=text if isinstance(text,dict) else None
        message=details.get('message','') if details else text
        with self.lock:
            if self.job:
                self.job['message']=message
                self.job['updated']=time.time()
                if details:self.job['progress']=details
            if self.account_flow and self.account_flow.get('active'):
                self.account_flow['message']=message
                if details:self.account_flow['progress']=details

    def connect_v4(self,paths,ready=None,status=None,owner=None,backup_password=''):
        from wechatbrain.windows_capture import capture_keys
        from wechatbrain.importer import SKIP
        if os.name!='nt':raise ValueError('当前软件运行在非 Windows 环境，请复制读取诊断确认运行位置。')
        databases=[]
        for raw in paths:
            root=Path(os.path.expandvars(os.path.expanduser(raw))).resolve()
            if not root.exists():raise ValueError('所选聊天目录不存在')
            if root.is_file():databases.append(root);continue
            for directory,dirs,names in os.walk(root,followlinks=False):
                if self.cancel.is_set():break
                dirs[:]=[d for d in dirs if d not in SKIP and not (Path(directory)/d).is_symlink()]
                databases.extend(Path(directory)/name for name in names
                                 if Path(name).suffix.lower() in ('.db','.sqlite','.sqlite3','.bakdb')
                                 and not (Path(directory)/name).is_symlink())
        connection=capture_keys(databases,self.reader_keys,self.progress,self.cancel.is_set,
                                ready=ready,status=status)
        result={'sources':[]}
        if connection['status']=='matched' and not self.cancel.is_set():
            if status:status(dict(connection,status='reading',message='正在读取联系人和聊天记录…'))
            result=Importer(self.store,self.progress,self.cancel.is_set,self.reader_keys).run(
                paths,backup_password,self.store.settings().get('owner','') if owner is None else owner)
        result['capture_diagnostics']=connection
        return result

    def read_diagnostics(self):
        """Public connection metadata only: never serialize settings, chats, or key cache."""
        with self.lock:job=dict(self.job) if self.job else {}
        result=job.get('result') or {}
        sources=result.get('sources')
        if not sources:
            sources=[]
            for row in self.store.stats()['sources'][:3]:
                try:sources.append(json.loads(row['detail']))
                except (ValueError,TypeError):continue
        keys=('status','message','platform','python_bits','source_layout','candidates_checked',
              'scanned_bytes','read_failures','query_failures','matched_targets','elapsed_seconds',
              'stopped_by','last_read_error','last_query_error','error_type','sha512_tables',
              'hooked_entries','attach_error','mode','target_count','layout_warning',
              'signature_matches','direct_key_entries','sha512_entries','capture_method',
              'attached_processes','queue_full')
        def public(values):
            out={k:values[k] for k in keys if k in values}
            process_keys=('pid','name','version','same_user','opened','open_error','module_error',
                          'module_search_error','pointer_bits','modules')
            out['processes']=[{k:p[k] for k in process_keys if k in p} for p in values.get('processes',[])]
            return out
        report={'app_version':__version__,'runtime_platform':sys.platform,
                'python_bits':__import__('struct').calcsize('P')*8,
                'account_discovery':{k:v for k,v in self.accounts.discovery_snapshot().items()
                    if k in ('status','revision','message','diagnostics')},
                'sources':[{'status':s.get('status'),'read':s.get('read',0),
                    'scope':s.get('scope','source'),
                    'native_plan':{k:s.get('native_plan',{}).get(k) for k in ('mode','database_files','documents_ignored','media_deferred')},
                    'last_refresh':{k:s.get('last_refresh',{}).get(k) for k in ('scope','status','read','imported','reused_files')},
                    'encrypted_databases':s.get('encrypted_databases',0),
                    'decrypted_databases':s.get('decrypted_databases',0),
                    'wal_databases':s.get('wal_databases',0),
                    'source_info':{k:v for k,v in s.get('source_info',{}).items() if k!='cache_paths'},
                    'preflight':s.get('preflight',{}),
                    'identity':{k:v for k,v in s.get('identity',{}).items() if k in ('contacts','sessions','accounts')},
                    'excluded_databases':s.get('excluded_databases',[]),
                    'local_connection':s.get('diagnostics',{}).get('local_account_material',{}),
                    'unread_database_names':sorted({Path(p).name for p in s.get('unread_files',[])})[:30],
                    'database_results':[{'name':d.get('name'),'status':d.get('status'),
                        'wal':d.get('wal')} for d in s.get('database_results',[])],
                    'snapshots':[{'name':d.get('name'),'status':d.get('status'),
                        'attempts':d.get('attempts'),'wal_bytes':d.get('wal_bytes')}
                        for d in s.get('snapshot_results',[])],
                    'connection':public(s.get('diagnostics',{}))} for s in sources]}
        if result.get('capture_diagnostics'):report['startup_connection']=public(result['capture_diagnostics'])
        try:(self.store.directory/'read-diagnostics.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        except OSError:report['diagnostic_file_saved']=False
        return report

    def start_job(self,kind,function):
        with self.lock:
            if self.job and self.job['status']=='running':raise ValueError('当前任务尚未完成，请等待或取消')
            self.cancel.clear()
            if kind=='account-read':self.account_flow=None
            self.job={'id':secrets.token_hex(8),'kind':kind,'status':'running',
                      'message':'正在准备…','result':None,'started':time.time()}
        def run():
            try:
                result=function()
                with self.lock:self.job.update(status='cancelled' if self.cancel.is_set() else 'done',result=result,message='已取消' if self.cancel.is_set() else '处理完成')
            except Exception as exc:
                with self.lock:self.job.update(status='error',message=str(exc) if isinstance(exc,ValueError) else '处理失败：'+type(exc).__name__)
        threading.Thread(target=run,daemon=True).start()
        return dict(self.job)

    def classify(self,ids):
        settings=self.store.settings()
        client=AIClassifier(settings.get('ai_url',''),settings.get('ai_model',''),self.api_key)
        conversations=self.store.conversations()
        if ids is not None:conversations=[c for c in conversations if c['id'] in ids]
        # Apply a conversation atomically only after all AI batches have validated.
        total=0;completed=0
        for c in conversations:
            if self.cancel.is_set():break
            updates=client.classify(c,self.store.all_messages(c['id']),self.progress,self.cancel.is_set)
            if self.cancel.is_set():break
            with self.store.connect() as db:
                db.executemany("UPDATE messages SET category=?,reason=?,classification='ai' WHERE id=?",updates)
                db.execute("UPDATE conversations SET classification='ai' WHERE id=?",(c['id'],))
            total+=len(updates);completed+=1
        return {'messages':total,'conversations':completed}

    def analyze(self,ids=None,save=True,force=False):
        settings=self.store.settings()
        if save and (not settings.get('vault') or not Path(settings['vault']).is_dir()):
            raise ValueError('先在设置里选择 Obsidian 库文件夹')
        if not settings.get('ai_url') or not settings.get('ai_model'):
            raise ValueError('先在设置里填写 AI 接口和模型')
        client=MemoryAnalyzer(settings['ai_url'],settings['ai_model'],self.api_key)
        people=self.store.conversations()
        if ids is not None:people=[p for p in people if p['id'] in ids]
        if not any(person['message_count'] for person in people):raise ValueError('所选会话暂无聊天消息，读取到消息后再提炼重要内容')
        completed=[];errors=[];reused=[];items=0
        for number,person in enumerate(people,1):
            if self.cancel.is_set():break
            if not person['message_count']:continue
            self.progress(f'正在提炼第 {number}/{len(people)} 位：{person["name"]}')
            messages=self.store.all_messages(person['id']);version=fingerprint(messages)
            old=self.store.insight(person['id'])
            if old and old['fingerprint']==version and old['model']==settings['ai_model'] and not force:
                reused.append(person['name']);completed.append(person['id']);continue
            try:
                profile=client.analyze(person,messages,self.progress,self.cancel.is_set)
                if self.cancel.is_set():break
                self.store.save_insight(person['id'],version,profile,settings['ai_model'])
                completed.append(person['id']);items+=len(profile['items'])
            except ValueError as exc:
                if self.cancel.is_set():break
                errors.append({'person':person['name'],'error':str(exc)})
        exported=export_insights(self.store,settings['vault'],completed,self.progress) if save and completed else None
        return {'people':len(completed),'important_items':items,'reused':reused,'errors':errors,'export':exported}

    def choose_folder(self):
        if self.window:
            import webview
            result=self.window.create_file_dialog(webview.FOLDER_DIALOG)
            return str(result[0]) if result else ''
        # Tk must own its interpreter's main thread; a short-lived subprocess handles the dialog.
        if getattr(sys,'frozen',False):
            cmd=[sys.executable,'--pick-folder']
        else:
            executable=Path(sys.executable)
            if os.name=='nt' and executable.name.lower()=='pythonw.exe':
                executable=executable.with_name('python.exe')
            cmd=[str(executable),str(ROOT/'app.py'),'--pick-folder']
        result=subprocess.run(cmd,capture_output=True,timeout=180,encoding='utf-8',
                              creationflags=0x08000000 if os.name=='nt' else 0)
        if result.returncode:raise ValueError('无法打开文件夹选择器，请直接粘贴文件夹路径')
        return result.stdout.strip()


def handler_for(app):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def respond(self,status,data,ctype='application/json; charset=utf-8'):
            if isinstance(data,(dict,list)):
                def avatars(value):
                    if isinstance(value,list):return [avatars(item) for item in value]
                    if isinstance(value,dict):return {key:avatars(item) for key,item in value.items()}
                    if isinstance(value,str) and value.startswith('/api/avatar?id=') and '&session=' not in value:
                        return value+'&session='+app.token
                    return value
                data=json.dumps(avatars(data),ensure_ascii=False).encode()
            if isinstance(data,str):data=data.encode()
            self.send_response(status)
            self.send_header('Content-Type',ctype);self.send_header('Content-Length',str(len(data)))
            self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self'; connect-src 'self'; frame-ancestors 'none'")
            self.send_header('Referrer-Policy','no-referrer')
            self.end_headers();self.wfile.write(data)

        def authorised(self):
            expected=f'127.0.0.1:{self.server.server_port}'
            if self.headers.get('Host')!=expected:return False
            origin=self.headers.get('Origin')
            if origin and origin!='http://'+expected:return False
            return secrets.compare_digest(self.headers.get('X-App-Token',''),app.token)

        def do_GET(self):
            parsed=urlparse(self.path);path=parsed.path
            if path=='/api/avatar':
                query={k:v[0] for k,v in parse_qs(parsed.query).items()}
                expected=f'127.0.0.1:{self.server.server_port}'
                if (self.headers.get('Host')!=expected or self.headers.get('Sec-Fetch-Site','same-origin') not in ('same-origin','none')
                    or not secrets.compare_digest(query.get('session',''),app.token)):
                    return self.respond(403,{'error':'头像会话已失效'})
                with app.store.connect() as db:row=db.execute('SELECT mime,data FROM avatars WHERE id=?',(query.get('id',''),)).fetchone()
                return self.respond(200,row['data'],row['mime']) if row else self.respond(404,{'error':'头像尚未读取'})
            if path=='/api/media-file':
                headers_sent=False
                query={k:v[0] for k,v in parse_qs(parsed.query).items()}
                expected=f'127.0.0.1:{self.server.server_port}'
                origin=self.headers.get('Origin')
                if (self.headers.get('Host')!=expected or (origin and origin!='http://'+expected)
                    or self.headers.get('Sec-Fetch-Site','same-origin') not in ('same-origin','none')
                    or not secrets.compare_digest(query.get('session',''),app.token)):
                    return self.respond(403,{'error':'媒体会话已失效'})
                try:
                    asset=app.media.get(query.get('id',''));local=app.media.asset_path(asset);size=local.stat().st_size
                    start,end=0,size-1
                    header=self.headers.get('Range','')
                    if header:
                        import re
                        match=re.fullmatch(r'bytes=(\d*)-(\d*)',header)
                        if not match or not any(match.groups()):raise ValueError('媒体范围无效')
                        left,right=match.groups()
                        if left:start=int(left);end=min(size-1,int(right)) if right else size-1
                        else:start=max(0,size-int(right))
                        if start>end or start>=size:raise ValueError('媒体范围超出文件')
                    self.send_response(206 if header else 200)
                    self.send_header('Content-Type',asset['mime']);self.send_header('Accept-Ranges','bytes')
                    self.send_header('Content-Length',str(end-start+1));self.send_header('Cache-Control','no-store')
                    self.send_header('X-Content-Type-Options','nosniff');self.send_header('Referrer-Policy','no-referrer')
                    disposition='attachment' if asset['kind']=='file' else 'inline'
                    self.send_header('Content-Disposition',disposition+'; filename="'+local.name+'"')
                    if header:self.send_header('Content-Range',f'bytes {start}-{end}/{size}')
                    self.end_headers();headers_sent=True
                    with local.open('rb') as stream:
                        stream.seek(start);remaining=end-start+1
                        while remaining:
                            chunk=stream.read(min(1024*1024,remaining))
                            if not chunk:break
                            self.wfile.write(chunk);remaining-=len(chunk)
                    return
                except (ValueError,OSError,KeyError):
                    if not headers_sent:return self.respond(404,{'error':'媒体文件不可用，请重新读取'})
                    self.close_connection=True;return
            if path in ('/','/index.html','/app.js','/accounts.js','/media.js','/style.css') or path.startswith(('/assets/','/icons/')):
                if self.headers.get('Host')!=f'127.0.0.1:{self.server.server_port}':
                    return self.respond(403,{'error':'本机访问地址无效'})
                name='index.html' if path in ('/','/index.html') else path[1:]
                asset=(ROOT/'web'/name).resolve()
                if not asset.is_relative_to((ROOT/'web').resolve()) or not asset.is_file():
                    return self.respond(404,{'error':'资源不存在'})
                data=asset.read_bytes()
                if name=='index.html':data=data.replace(b'__SESSION_TOKEN__',app.token.encode())
                ctype={'index.html':'text/html; charset=utf-8','app.js':'text/javascript; charset=utf-8','accounts.js':'text/javascript; charset=utf-8','media.js':'text/javascript; charset=utf-8','style.css':'text/css; charset=utf-8'}.get(name,{'svg':'image/svg+xml','png':'image/png','webp':'image/webp'}.get(asset.suffix[1:].lower(),'application/octet-stream'))
                return self.respond(200,data,ctype)
            if not self.authorised():return self.respond(403,{'error':'本机会话已失效，请重新打开软件'})
            query={k:v[0] for k,v in parse_qs(parsed.query).items()}
            try:
                if path=='/api/state':
                    with app.lock:
                        job=dict(app.job) if app.job else None
                        flow=dict(app.account_flow) if app.account_flow else None
                    return self.respond(200,{'version':__version__,'settings':app.store.settings(),
                        'stats':app.store.stats(),'job':job,'demo':app.demo,'has_api_key':bool(app.api_key),
                        'runtime_platform':sys.platform,'account_flow':flow})
                if path=='/api/job-state':
                    with app.lock:
                        result={'job':dict(app.job) if app.job else None,
                            'account_flow':dict(app.account_flow) if app.account_flow else None}
                    return self.respond(200,result)
                if path=='/api/accounts':return self.respond(200,app.accounts.catalog(refresh=query.get('refresh')=='1'))
                if path=='/api/live-state':return self.respond(200,app.live.state(query.get('source_id','')))
                if path=='/api/read-diagnostics':return self.respond(200,app.read_diagnostics())
                if path=='/api/media-list':return self.respond(200,app.media.listing(query.get('source',''),max(0,int(query.get('offset',0)))))
                if path=='/api/media-status':
                    identities=query.get('ids','').split(',')
                    if len(identities)>12:raise ValueError('一次最多查看 12 个媒体状态')
                    return self.respond(200,{'items':app.media.statuses(identities)})
                if path=='/api/conversations':return self.respond(200,app.store.conversations(query.get('q',''),query.get('category','')))
                if path=='/api/messages':return self.respond(200,app.store.filtered_messages(query['id'],query,int(query.get('offset',0)),100))
                if path=='/api/detect-sources':
                    from wechatbrain.discovery import detect_sources
                    return self.respond(200,detect_sources())
                if path=='/api/schedules':return self.respond(200,app.scheduler.list())
                if path=='/api/archive-history':
                    with app.store.connect() as db:
                        rows=[dict(r) for r in db.execute('SELECT * FROM export_runs ORDER BY created DESC LIMIT 100')]
                    for row in rows:row['detail']=json.loads(row['detail'])
                    return self.respond(200,rows)
                if path=='/api/insight':return self.respond(200,{'conversation':app.store.conversation(query['id']),'insight':app.store.insight(query['id'])})
                return self.respond(404,{'error':'接口不存在'})
            except (ValueError,KeyError) as exc:return self.respond(400,{'error':str(exc)})

        def do_POST(self):
            if not self.authorised():return self.respond(403,{'error':'本机会话已失效，请重新打开软件'})
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0<=length<=1024*1024:raise ValueError('请求太大')
                data=json.loads(self.rfile.read(length) or b'{}')
                if not isinstance(data,dict):raise ValueError('请求格式错误')
                path=urlparse(self.path).path
                if path=='/api/settings':
                    if set(data)-{'vault','owner','ai_url','ai_model','source_paths','export_dir','api_key'}:
                        raise ValueError('设置字段无效')
                    for k,v in data.items():
                        if k!='source_paths' and not isinstance(v,str):raise ValueError('设置格式错误')
                        if k=='source_paths' and (not isinstance(v,list) or any(not isinstance(p,str) for p in v)):
                            raise ValueError('记录目录格式错误')
                    if 'api_key' in data:app.api_key=data.pop('api_key')
                    app.store.save_settings(data);return self.respond(200,{'ok':True})
                if path=='/api/pick-folder':return self.respond(200,{'path':app.choose_folder()})
                if path=='/api/chat-refresh':
                    return self.respond(200,app.live.refresh(str(data.get('source_id','')),
                        str(data.get('conversation_id','')),str(data.get('mode','incremental'))))
                if path=='/api/live-config':
                    return self.respond(200,app.live.configure(str(data.get('source_id','')),data.get('enabled') is True))
                if path=='/api/media/prepare':
                    identities=data.get('ids',[])
                    if not isinstance(identities,list) or len(identities)>12 or any(not isinstance(v,str) for v in identities):
                        raise ValueError('一次最多预览 12 个媒体文件')
                    return self.respond(200,app.media.request_prepare(identities))
                if path=='/api/media/read':
                    folder=Path(str(data.get('path',''))).expanduser().resolve()
                    if not folder.is_dir():raise ValueError('请先选择聊天记录文件夹')
                    def read_media():
                        from wechatbrain.store import digest
                        if (folder/'Manifest.db').is_file():
                            result=app.media.index_backup(folder,digest(str(folder)),str(data.get('backup_password','')))
                        else:result=app.media.index_folder(folder,digest(str(folder)))
                        sid=digest(str(folder))
                        app.media.index_source_databases(sid)
                        result=app.media.summary(sid)
                        result['linked_messages']=app.media.link(sid)
                        return result
                    return self.respond(200,app.start_job('media',read_media))
                if path=='/api/media/export':
                    folder=Path(str(data.get('folder',''))).expanduser().resolve()
                    if not folder.is_dir():raise ValueError('请先选择媒体导出目录')
                    return self.respond(200,app.start_job('media-export',lambda:app.media.export_folder(
                        folder,str(data.get('source','')))))
                if path=='/api/accounts/launch':
                    from wechatbrain.accounts import launch_client
                    return self.respond(200,launch_client())
                if path=='/api/accounts/open':
                    return self.respond(200,app.accounts.open(str(data.get('id','')),str(data.get('folder','')).strip()))
                if path=='/api/accounts/auto-read':
                    selected=app.accounts.automatic(str(data.get('id','')))
                    mode=str(data.get('mode','read'))
                    if mode not in ('read','capture','restart'):raise ValueError('读取方式无效')
                    confirmed=data.get('restart_confirmed') is True
                    if mode=='restart' and not confirmed:raise ValueError('请先确认重启电脑版微信')
                    return self.respond(200,app.start_job('account-read',lambda:app.accounts.read(
                        selected['path'],mode,str(data.get('backup_password','')),str(data.get('owner','')).strip(),confirmed,True)))
                if path=='/api/accounts/read':
                    selected=str(data.get('path','')).strip()
                    if not selected:raise ValueError('请先选择微信记录或本地备份目录')
                    mode=str(data.get('mode','read'))
                    if mode not in ('read','capture','restart'):raise ValueError('读取方式无效')
                    confirmed=data.get('restart_confirmed') is True
                    if mode=='restart' and not confirmed:raise ValueError('请先确认重启电脑版微信')
                    return self.respond(200,app.start_job('account-read',lambda:app.accounts.read(
                        selected,mode,str(data.get('backup_password','')),str(data.get('owner','')).strip(),confirmed)))
                if path=='/api/import':
                    paths=data.get('paths',[])
                    if not isinstance(paths,list) or not paths or any(not isinstance(p,str) for p in paths):raise ValueError('请选择至少一个文件夹')
                    owner=str(data.get('owner','')).strip()
                    app.store.save_settings({'source_paths':paths,'owner':owner})
                    password=str(data.get('backup_password',''))
                    return self.respond(200,app.start_job('import',lambda:Importer(app.store,app.progress,app.cancel.is_set,app.reader_keys).run(paths,password,owner)))
                if path=='/api/connect-v4':
                    paths=data.get('paths') or app.store.settings().get('source_paths',[])
                    if not isinstance(paths,list) or not paths or any(not isinstance(p,str) for p in paths):raise ValueError('先选择聊天记录目录')
                    app.store.save_settings({'source_paths':paths})
                    return self.respond(200,app.start_job('connect-v4',lambda:app.connect_v4(paths)))
                if path=='/api/category':
                    app.store.set_category(data['id'],data['category']);return self.respond(200,{'ok':True})
                if path=='/api/contact':
                    app.store.update_contact(data['id'],str(data.get('alias','')),str(data.get('note','')))
                    return self.respond(200,{'ok':True})
                if path in ('/api/archive','/api/contacts-export'):
                    folder=str(data.get('folder','')).strip() or app.store.settings().get('export_dir','')
                    if not folder or not Path(folder).is_dir():raise ValueError('请先选择导出文件夹')
                    app.store.save_settings({'export_dir':folder})
                    if path=='/api/contacts-export':
                        function=lambda:export_contacts(app.store,folder,data.get('ids'),str(data.get('format','xlsx')))
                    else:
                        function=lambda:export_archive(app.store,folder,data.get('ids'),data.get('options',{}),app.progress,app.cancel.is_set)
                    return self.respond(200,app.start_job('archive',function))
                if path=='/api/schedule-save':return self.respond(200,app.scheduler.save(data))
                if path=='/api/schedule-enabled':
                    app.scheduler.enable(str(data['id']),bool(data['enabled']));return self.respond(200,{'ok':True})
                if path=='/api/schedule-run':return self.respond(200,app.scheduler.run(str(data['id'])))
                if path=='/api/open-archive':
                    folder=app.store.settings().get('export_dir','')
                    if not folder or not Path(folder).is_dir():raise ValueError('导出文件夹不存在')
                    if os.name=='nt':os.startfile(folder)
                    elif sys.platform=='darwin':subprocess.Popen(['open',folder])
                    else:subprocess.Popen(['xdg-open',folder])
                    return self.respond(200,{'ok':True})
                if path=='/api/classify':
                    return self.respond(200,app.start_job('classify',lambda:app.classify(data.get('ids'))))
                if path=='/api/analyze':
                    return self.respond(200,app.start_job('analyze',lambda:app.analyze(data.get('ids'),data.get('save',True),data.get('force',False))))
                if path=='/api/export':
                    vault=str(data.get('vault','')) or app.store.settings().get('vault','')
                    app.store.save_settings({'vault':vault})
                    return self.respond(200,app.start_job('export',lambda:export_insights(app.store,vault,data.get('ids'),app.progress)))
                if path=='/api/cancel':
                    app.cancel.set()
                    if app.job and app.job.get('kind')=='live-sync':
                        try:app.live.configure(app.store.settings().get('active_source',''),False)
                        except ValueError:pass
                    return self.respond(200,{'ok':True})
                if path=='/api/open-export':
                    vault=app.store.settings().get('vault','')
                    folder=Path(vault)/'微信第二大脑'
                    if not vault or not folder.is_dir():raise ValueError('请先导出到 Obsidian')
                    if os.name=='nt':os.startfile(str(folder))
                    elif sys.platform=='darwin':subprocess.Popen(['open',str(folder)])
                    else:subprocess.Popen(['xdg-open',str(folder)])
                    return self.respond(200,{'ok':True})
                return self.respond(404,{'error':'接口不存在'})
            except (ValueError,KeyError,OSError) as exc:
                return self.respond(400,{'error':str(exc)})
            except Exception as exc:return self.respond(500,{'error':'操作失败：'+type(exc).__name__})
    return Handler


def main():
    parser=argparse.ArgumentParser(description='京客导出助手 Windows 桌面软件')
    parser.add_argument('--data-dir');parser.add_argument('--port',type=int,default=0)
    parser.add_argument('--headless',action='store_true');parser.add_argument('--browser',action='store_true')
    parser.add_argument('--demo',action='store_true');parser.add_argument('--pick-folder',action='store_true')
    args=parser.parse_args()
    if args.pick_folder:
        if os.name=='nt':
            import ctypes
            from ctypes import wintypes as w
            class BrowseInfo(ctypes.Structure):
                _fields_=[('hwndOwner',w.HWND),('pidlRoot',ctypes.c_void_p),
                          ('pszDisplayName',w.LPWSTR),('lpszTitle',w.LPCWSTR),
                          ('ulFlags',w.UINT),('lpfn',ctypes.c_void_p),
                          ('lParam',ctypes.c_ssize_t),('iImage',ctypes.c_int)]
            shell=ctypes.windll.shell32;ole=ctypes.windll.ole32
            shell.SHBrowseForFolderW.argtypes=[ctypes.POINTER(BrowseInfo)]
            shell.SHBrowseForFolderW.restype=ctypes.c_void_p
            shell.SHGetPathFromIDListW.argtypes=[ctypes.c_void_p,w.LPWSTR]
            shell.SHGetPathFromIDListW.restype=w.BOOL
            ole.CoTaskMemFree.argtypes=[ctypes.c_void_p]
            ole.CoInitialize(None)
            display=ctypes.create_unicode_buffer(32768)
            info=BrowseInfo(None,None,ctypes.cast(display,w.LPWSTR),'选择聊天记录或 Obsidian 文件夹',0x0041,None,0,0)
            pidl=shell.SHBrowseForFolderW(ctypes.byref(info))
            try:
                if pidl:
                    path=ctypes.create_unicode_buffer(32768)
                    if shell.SHGetPathFromIDListW(pidl,path):print(path.value)
            finally:
                if pidl:ole.CoTaskMemFree(pidl)
                ole.CoUninitialize()
            return
        import tkinter as tk
        from tkinter import filedialog
        root=tk.Tk();root.withdraw();root.attributes('-topmost',True)
        selected=filedialog.askdirectory(title='选择文件夹',mustexist=True)
        root.destroy()
        if selected:print(selected)
        return
    directory=args.data_dir or (Path(os.environ.get('LOCALAPPDATA',str(Path.home()/'.local/share')))/'WeChatBrain')
    if args.demo and not args.data_dir:directory=tempfile.mkdtemp(prefix='wechatbrain_demo_')
    app=Application(directory,args.demo)
    if args.demo:
        paths=[str(ROOT/'fixtures/win3'),str(ROOT/'fixtures/win4'),str(ROOT/'fixtures/ios_backup')]
        Importer(app.store,lambda text:None).run(paths,owner='演示账号（合成数据）')
        app.store.save_settings({'source_paths':paths,'owner':'演示账号（合成数据）'})
    server=ThreadingHTTPServer(('127.0.0.1',args.port),handler_for(app))
    server.daemon_threads=True
    app.scheduler.start()
    app.live.start()
    if not args.demo:app.accounts.start()
    url=f'http://127.0.0.1:{server.server_port}'
    if args.headless:
        print(url,flush=True)
        try:server.serve_forever()
        except KeyboardInterrupt:pass
        finally:app.media.close();app.scheduler.stop.set();app.live.stop.set();app.accounts.stop.set();server.server_close()
        return
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    if not args.browser:
        try:
            import webview
            app.window=webview.create_window('京客导出助手',url,width=1180,height=800,x=80 if os.name=='nt' else None,y=55 if os.name=='nt' else None,
                                            min_size=(1100,740),maximized=False,background_color='#f6f8fd')
            webview.start()
            app.cancel.set();app.media.close();app.scheduler.stop.set();app.live.stop.set();app.accounts.stop.set();server.shutdown();return
        except Exception:
            # Edge WebView2 may not be available on a clean Windows installation.
            # The same local UI remains usable in the user's default browser.
            pass
    webbrowser.open(url)
    try:
        while True:time.sleep(.5)
    except KeyboardInterrupt:pass
    finally:app.cancel.set();app.media.close();app.scheduler.stop.set();app.live.stop.set();app.accounts.stop.set();server.shutdown();server.server_close()


if __name__=='__main__':main()
