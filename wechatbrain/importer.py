"""桌面导入适配：直接调用 DeepSeek 编写的 chatlog_extract / chatlog_platform。"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path

import chatlog_extract as engine
import chatlog_platform as platform
from .access import KEY_NAMES, material_from_folder, account_material, acquire_windows_keys, matches
from .sqlcipher import decrypt_database
from .snapshot import copy_snapshot, SnapshotError
from .wal import WALError
from .media import MediaLibrary
from .classify import classify
from .store import digest
from .loading import InputCache, fingerprint
from .source_info import inspect_source
from .source_plan import native_plan

# Keep the desktop entry aligned with the original reader's supported formats.
# In particular .sqlite2 / .text / .chat / .msg / .xhtml must reach its parsers too.
SKIP=set(engine.SKIP_DIRS)|set(engine.WECHAT_MEDIA_DIRS)|{'.obsidian'}
EXT=set().union(engine.SQLITE_EXT,engine.TEXT_EXT,engine.JSON_EXT,
                engine.HTML_EXT,engine.CSV_EXT)
PRIVATE_NAMES=KEY_NAMES|{'image_key.json','image_keys.json'}


class Importer:
    def __init__(self, store, progress, cancelled=lambda:False, keys=None, *, incremental=False, force=False,
                 peers=None, account='', files_filter=None):
        self.store=store;self.progress=progress;self.cancelled=cancelled
        self.keys=keys if keys is not None else []
        self.incremental=incremental;self.force=force
        self.peers=set(peers or []);self.account=account
        self.files_filter=set(files_filter) if files_filter is not None else None

    def run(self, paths, backup_password='', owner=''):
        if not paths:raise ValueError('请先选择聊天记录文件夹')
        reports=[]
        for raw in dict.fromkeys(paths):
            if self.cancelled():break
            path=Path(os.path.expandvars(os.path.expanduser(raw))).resolve()
            if not path.exists():raise ValueError('文件夹不存在：'+str(path))
            reports.append(self.import_source(path,backup_password,owner))
        return {'sources':reports,'stats':self.store.stats(),'cancelled':self.cancelled()}

    def import_source(self, root, password, owner):
        sid=digest(str(root))
        with self.store.connect() as db:
            previous=db.execute('SELECT detail FROM sources WHERE id=?',(sid,)).fetchone()
        try:previous=json.loads(previous['detail']) if previous else None
        except (ValueError,TypeError):previous=None
        media=MediaLibrary(self.store,self.progress,self.cancelled)
        cache=InputCache(self.store,sid)
        report={'id':sid,'path':str(root),'imported':0,'duplicates':0,'read':0,
                'errors':[],'notes':[],'files':0,'kind':'聊天文件夹','status':'reading',
                'encrypted_databases':0,'decrypted_databases':0,'unread_files':[],
                'diagnostics':{'status':'not_needed'},'database_results':[],
                'snapshot_results':[], 'wal_databases':0, 'reused_files':0, 'cached_messages':0,
                'loading_mode':'cached-batches-with-deferred-media','cache_sources':[]}
        report['scope']='selected-conversations' if self.peers else 'changed-databases' if self.files_filter is not None else 'source'
        checked={};last_check=[0.0];connection_attempted=False
        def accepts(path,key):
            if self.cancelled():return False
            pair=(str(path),key)
            if pair not in checked:
                now=time.monotonic()
                if now-last_check[0]>.5:
                    self.progress('正在校验读取连接：'+path.name+f' · 已检查 {len(checked):,} 个组合')
                    last_check[0]=now
                checked[pair]=matches(path,key)
            return checked[pair]
        with self.store.connect() as db:
            db.execute('INSERT INTO sources(id,path,kind,status,updated,detail) VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status',
                (sid,str(root),report['kind'],'reading',datetime.now().isoformat(timespec='seconds'),json.dumps(report,ensure_ascii=False)))
        with tempfile.TemporaryDirectory(prefix='wechatbrain_') as work:
            stage=cache.directory
            origin={};states={};selected=[];backup_pending=[]
            files=[];backup_roots=[]
            plan=native_plan(root,self.cancelled)
            if plan['recognized']:
                files=plan['databases']
                if self.account:files=[p for p in files if self._account(p,root)==self.account]
                if self.files_filter is not None:files=[p for p in files if str(p) in self.files_filter]
                report['kind']='Windows 微信记录'
                report['errors']+=plan['errors']
                report['native_plan']={'mode':plan['mode'],'accounts':plan['accounts'],
                    'database_files':len(files),'documents_ignored':True,'media_deferred':True}
            elif root.is_file():files=[root]
            else:
                # Recognise backups at any depth; never parse their hashed files as random logs.
                for directory,dirs,names in os.walk(root,followlinks=False):
                    if self.cancelled():break
                    dirs[:]=[d for d in dirs if d not in SKIP and not (Path(directory)/d).is_symlink()
                             and (Path(directory)/d).resolve()!=self.store.directory.resolve()]
                    folder=Path(directory)
                    if (folder/'Manifest.db').is_file() and (folder/'Manifest.plist').is_file():
                        backup_roots.append(folder);dirs[:]=[];continue
                    files.extend(folder/n for n in names if Path(n).suffix.lower() in EXT
                                 and n.lower() not in PRIVATE_NAMES)
            report['source_info']=inspect_source(root,files,self.cancelled)
            if plan['recognized']:
                report['source_info']['layout']=('Windows 微信多账号目录' if len(plan['accounts'])>1
                    else plan['accounts'][0]['layout'] if plan['accounts'] else 'Windows 微信目录')
                report['source_info']['native_only']=True
                self.progress('仅连接微信账号数据库，跳过附件、文档和导出文件')
            # WeChat's FTS databases use its custom tokenizer. Original message
            # tables already contain the records; search indexes are not input.
            excluded=[p for p in files if p.name.lower() in ('contact_fts.db','message_fts.db')
                and 'db_storage' in p.parts]
            if excluded:
                report['excluded_databases']=[{'name':p.name,'reason':'微信搜索索引，不含独立聊天原文'} for p in excluded]
                files=[p for p in files if p not in excluded]
                report['notes'].append('直接读取聊天原始表；微信专用搜索索引无需导入。')
            cache_paths=set(report['source_info']['cache_paths'])
            self.progress('已识别：'+report['source_info']['layout']+f" · {report['source_info']['database_files']} 个数据库")
            if cache_paths:report['kind']='聊天缓存 / 本地记录'
            raw_locked=[]
            if report['source_info']['layout'].startswith('Windows 微信'):
                for path in files:
                    if self.cancelled():break
                    if path.suffix.lower() not in engine.SQLITE_EXT or path.is_symlink():continue
                    try:
                        signature=fingerprint(path,owner,True)
                        if not self.force and not self.peers and cache.completed(path,signature) is not None:continue
                        relative=path.relative_to(root) if root.is_dir() else Path(path.name)
                        target=stage/'folder'/root.name/relative
                        if cache.reusable_snapshot(path,signature,target,True):
                            with target.open('rb') as stream:
                                if stream.read(16)==b'SQLite format 3\x00':continue
                        with path.open('rb') as stream:
                            if stream.read(16)!=b'SQLite format 3\x00':raw_locked.append(path)
                    except OSError:continue
                if raw_locked:
                    self.keys[:]=dict.fromkeys(self.keys+material_from_folder(root)+account_material(
                        root,raw_locked,self.progress,self.cancelled,report['diagnostics']))
                    if not self.cancelled() and any(not any(accepts(p,k) for k in self.keys) for p in raw_locked):
                        connection_attempted=True
                        self.progress('先校验读取连接，通过后再复制数据库')
                        try:self.keys+=acquire_windows_keys(raw_locked,self.progress,self.cancelled,diagnostics=report['diagnostics'])
                        except (OSError,RuntimeError,ImportError,ValueError) as exc:
                            report['diagnostics'].update(status='reader_error',error_type=type(exc).__name__,message='连接组件未能完成检查，请查看读取诊断。')
                    else:report['diagnostics'].update(status='cached_material',message='正在复用本次运行中已校验的连接信息。')
                    report['preflight']={'locked_files':len(raw_locked),'copy_after_connection':True}
            for bindex,backup_root in enumerate(backup_roots):
                self.progress('正在检查 iPhone 备份缓存：'+backup_root.name)
                marker=str(backup_root)+'#iphone-manifest'
                signature=fingerprint(backup_root/'Manifest.db',owner,True)+fingerprint(backup_root/'Manifest.plist')
                report['kind']='iPhone 备份' if not files else '混合目录 / iPhone 备份'
                count=cache.completed(marker,signature)
                if count is not None:
                    report['reused_files']+=1;report['cached_messages']+=count;report['read']+=count
                    continue
                report['kind']='iPhone 备份' if not files else '混合目录 / iPhone 备份'
                backup=platform.IOSBackup(backup_root,password or None,report['notes'])
                try:
                    if not backup.unlock():
                        report['errors'].append('iPhone 备份需要正确的备份密码；在导入设置填写后重试')
                        continue
                    destination=stage/('iphone-'+digest(str(backup_root))[:12])
                    extracted=backup.extract_wechat(destination,include_media=False,cancelled=self.cancelled,progress=self.progress)
                    report['notes'].append('已提取聊天数据库；备份媒体留到单独读取时处理。')
                    selected.extend(extracted)
                    backup_pending.append((marker,signature,extracted))
                    for target in extracted:
                        original=str(backup_root)+' / '+str(target.relative_to(destination))
                        origin[str(target)]=original
                        states[str(target)]=(original,fingerprint(target,owner,True),None)
                    if not extracted:report['errors'].append('备份中没有可识别的微信数据库')
                finally:
                    if backup._tmp_manifest:backup._tmp_manifest.unlink(missing_ok=True)
            # Only changed files enter the verified source snapshot path.
            for path in files:
                if self.cancelled():break
                if path.is_symlink() or path==self.store.path or path.name.lower() in PRIVATE_NAMES:continue
                try:
                    is_database=path.suffix.lower() in engine.SQLITE_EXT or str(path) in cache_paths
                    signature=fingerprint(path,owner,is_database)+('|qx-cache-reader-2' if str(path) in cache_paths else '')
                    count=None if self.force or self.peers else cache.completed(path,signature)
                    if count is not None:
                        report['reused_files']+=1;report['cached_messages']+=count;report['read']+=count
                        metadata=cache.metadata(path)
                        if metadata:report['cache_sources'].append(metadata)
                        self.progress('复用已读取记录：'+path.name)
                        continue
                    relative=path.relative_to(root) if root.is_dir() else Path(path.name)
                    target=stage/'folder'/root.name/relative
                    snapshot_reusable=cache.reusable_snapshot(path,signature,target,is_database)
                    if path in raw_locked and not snapshot_reusable and not any(accepts(path,key) for key in self.keys):
                        report['encrypted_databases']+=1
                        report['unread_files'].append(str(path))
                        report['database_results'].append({'name':path.name,'status':'no_matching_material','copy_deferred':True})
                        continue
                    self.progress('正在复制并校验记录：'+path.name)
                    live_snapshot=None
                    if self.incremental and plan['recognized'] and not snapshot_reusable and self.keys:
                        from .live_snapshot import refresh
                        live_snapshot=refresh(self.store,sid,path,target,self.keys,self.cancelled,self.progress)
                    if live_snapshot:
                        snapshot=live_snapshot
                        report.setdefault('updated_databases',[]).append({'name':path.name,**snapshot})
                        cache.save_snapshot(path,signature,target,is_database)
                    elif snapshot_reusable:
                        snapshot={'status':'cached_snapshot','attempts':0}
                    else:
                        snapshot=copy_snapshot(path,target,is_database,self.cancelled,progress=lambda done,total,phase:
                            self.progress({'message':('正在复制：' if phase=='copy' else '正在校验副本：')+path.name+
                                f' · {done/1024**2:.1f}/{total/1024**2:.1f} MB',
                                'phase':phase,'bytes':done,'total_bytes':total}))
                        cache.save_snapshot(path,signature,target,is_database)
                    report['snapshot_results'].append({'name':path.name,**snapshot})
                    origin[str(target)]=str(path)
                    states[str(target)]=(str(path),signature,path)
                    selected.append(target)
                except (OSError,SnapshotError) as exc:
                    status=getattr(exc,'status','snapshot_unreadable')
                    message=str(exc) if isinstance(exc,SnapshotError) else '文件无法读取：'+str(path)
                    report['errors'].append(message)
                    report['unread_files'].append(str(path))
                    report['snapshot_results'].append({'name':path.name,'status':status})
                    report['database_results'].append({'name':path.name,'status':status,'message':message})
            candidates=[];encrypted=[]
            for p in selected:
                if self.cancelled():break
                is_cache=origin.get(str(p),'') in cache_paths
                if p.suffix.lower() not in EXT and not is_cache:continue
                if p.name.lower() in ('manifest.db','info.plist','manifest.plist'):continue
                kind='sqlite' if is_cache else engine.kind_of(p,False)
                if not kind:continue
                candidates.append((p,kind))
                if kind=='sqlite':
                    # Backup SHM indexes may be stale or belong to another machine.
                    Path(str(p)+'-shm').unlink(missing_ok=True)
                    journal=Path(str(p)+'-journal')
                    if journal.exists() and journal.stat().st_size:
                        message='存在回滚日志，请正常退出微信后重新读取：'+p.name
                        report['errors'].append(message)
                        report['database_results'].append({'name':p.name,'status':'rollback_journal','message':message})
                        report['unread_files'].append(origin.get(str(p),p.name))
                        candidates.pop()
                        continue
                    with p.open('rb') as f:header=f.read(16)
                    if header!=b'SQLite format 3\x00':encrypted.append(p)
            if encrypted:
                self.keys[:]=dict.fromkeys(self.keys+material_from_folder(root)+(
                    [] if raw_locked else account_material(root,encrypted,self.progress,self.cancelled,report['diagnostics'])))
                report['encrypted_databases']+=len(encrypted)
                connected=False
                self.progress('正在自动识别所选文件夹的访问方式')
                if not connection_attempted and not self.cancelled() and (not self.keys or any(not any(accepts(p,k) for k in self.keys) for p in encrypted)):
                    try:
                        self.keys+=acquire_windows_keys(encrypted,self.progress,self.cancelled,diagnostics=report['diagnostics'])
                    except (OSError, RuntimeError, ImportError, ValueError) as exc:
                        report['diagnostics'].update(status='reader_error',error_type=type(exc).__name__,
                            message='连接组件未能完成检查，需要查看读取诊断；不能据此判断是否在原电脑。')
                elif not connection_attempted:
                    report['diagnostics'].update(status='cached_material',message='使用本次运行已保留的连接信息。')
                for path in encrypted:
                    if self.cancelled():break
                    okay=False
                    database_result={'name':path.name,'status':'no_matching_material'}
                    for key in self.keys:
                        if not accepts(path,key):continue
                        connected=True
                        try:
                            self.progress('正在校验数据库与已提交日志：'+path.name)
                            plain=decrypt_database(path,key,Path(work)/'decoded',report['notes'],self.cancelled,database_result,
                                progress=lambda done,total:self.progress({'message':f'正在解密校验 · {path.name} · {done:,}/{total:,} 页',
                                    'phase':'decrypting','pages':done,'total_pages':total}))
                        except (ValueError,sqlite3.DatabaseError,OSError) as exc:
                            text=str(exc) if isinstance(exc,ValueError) else f'{path.name} 无法完成解密检查（{type(exc).__name__}）'
                            report['errors'].append(text)
                            database_result['status']=('wal_validation_failed' if isinstance(exc,WALError)
                                else 'page_authentication_failed' if '页校验失败' in text
                                else 'incomplete_file' if '页面不完整' in text else 'snapshot_validation_failed')
                            database_result['message']=text
                            break
                        if plain:
                            shutil.copyfile(plain,path)
                            for suffix in ('-wal','-shm'):Path(str(path)+suffix).unlink(missing_ok=True)
                            state=states.get(str(path))
                            if state:cache.save_snapshot(state[0],state[1],path,True)
                            if state and state[2] and json.loads(fingerprint(state[2],owner))[2][0]==json.loads(state[1])[2][0]:
                                from .live_snapshot import remember
                                remember(self.store,sid,state[2],path,database_result.get('wal',{}),json.loads(state[1])[2][0])
                            report['decrypted_databases']+=1
                            database_result['status']='decrypted'
                            if (database_result.get('wal') or {}).get('transactions'):
                                report['wal_databases']+=1
                            okay=True;break
                    report['database_results'].append(database_result)
                    if not okay:
                        report['unread_files'].append(origin.get(str(path),path.name))
            if report['unread_files'] and not self.cancelled():
                environment=('连接信息已在本次软件运行中保留，请按上方提示处理后重新读取。' if report['decrypted_databases']
                             else report['diagnostics'].get('message','尚未获得能通过数据库校验的连接信息，请查看读取诊断。'))
                failed=sum(d.get('status')!='decrypted' for d in report['database_results']
                           if d.get('status') not in ('snapshot_changed','snapshot_unreadable','rollback_journal'))
                if failed:report['errors'].append(f'{failed} 个加密数据库尚未读出。'+environment)
            from .identity import read_metadata
            report['identity']=read_metadata(self.store,candidates,sid,root,owner,self._account,
                origin,self.progress,self.cancelled)
            report['errors']+=report['identity']['errors']
            opts=engine.build_parser().parse_args(['--src',str(stage),'--quiet'])
            opts.max_rows=2**63-1;opts.max_file_mb=10**9
            opts.wechat_only=plan['recognized']
            opts.peer_filter=self.peers
            opts.cancelled=self.cancelled;opts.contact_cache={}
            opts.on_scan=lambda name:self.progress({'message':f"正在扫描聊天索引 · {name} · 已读 {report['read']:,} 条",
                'phase':'scanning','messages':report['read']})
            ctx=engine.Ctx()
            row_counts={};completed_paths=set()
            # Disk-backed occurrence counters avoid keeping a hash for every
            # message lacking a stable server ID in Python memory.
            with closing(sqlite3.connect(str(Path(work)/'occurrence.sqlite3'))) as occurrence:
                occurrence.execute('PRAGMA cache_size=-2048')
                occurrence.execute('CREATE TABLE counts(k TEXT PRIMARY KEY,n INTEGER) WITHOUT ROWID')
                for index,(path,kind) in enumerate(candidates):
                    if self.cancelled():break
                    occurrence.execute('DELETE FROM counts');occurrence.commit()
                    if path in encrypted:
                        with path.open('rb') as f:
                            if f.read(16)!=b'SQLite format 3\x00':continue
                    self.progress('正在读取聊天：'+path.name)
                    source_path=origin.get(str(path),str(root)+' / '+str(path.relative_to(stage)))
                    before_rows=report['read'];before_errors=len(ctx.errors);before_notes=len(ctx.notes)
                    opts.contact_scope=self._account(path,root)
                    opts.source_origin=source_path
                    with self.store.connect() as db:
                        opts.reader_state={r[0]:r[1] for r in db.execute(
                            'SELECT source_table,high_row FROM reader_watermarks WHERE source_id=? AND origin=?',(sid,source_path))} if self.incremental else {}
                    updated_state={}
                    opts.on_reader_state=lambda table,high:updated_state.__setitem__(table,high)
                    opts.cache_account=''
                    if len(cache_paths)>1 and opts.contact_scope.startswith('未确认账号-'):
                        opts.cache_account='未确认账号-'+digest(source_path)[:8]
                    def consume(batch):
                        imported=duplicates=0
                        with self.store.connect() as db:
                            for m in batch:
                                if self.cancelled():raise ValueError('已停止读取')
                                if not m.content.strip():continue
                                extra=m.extra or {}
                                account=owner.strip() or extra.get('account_id') or self._account(path,root)
                                peer=extra.get('conversation_id') or m.conversation
                                cid=digest(account,peer)
                                category,reason=classify(m.content,m.conversation)
                                when=m.ts.strftime('%Y-%m-%d %H:%M:%S') if m.ts else m.ts_raw or ''
                                source_id=str(extra.get('message_id') or '')
                                if source_id in ('0','None'):source_id=''
                                # Strong server IDs deduplicate across backups. Without them, preserve
                                # repeated identical messages by their occurrence within each source file.
                                base=(account,peer,when,bool(m.is_self),m.sender,m.content,extra.get('type',''))
                                if source_id:
                                    mid=digest(account,peer,'server',source_id)
                                else:
                                    occurrence_key=digest(*base)
                                    count=occurrence.execute('INSERT INTO counts VALUES(?,1) ON CONFLICT(k) DO UPDATE SET n=n+1 RETURNING n',(occurrence_key,)).fetchone()[0]
                                    mid=digest(*base,count)
                                db.execute('''INSERT INTO conversations(id,account,peer_id,name) VALUES(?,?,?,?)
                                  ON CONFLICT(id) DO UPDATE SET name=CASE WHEN excluded.name NOT LIKE '会话 %'
                                    AND excluded.name NOT LIKE 'wxid_%' THEN excluded.name ELSE conversations.name END''',
                                    (cid,account,peer,m.conversation))
                                db.execute('UPDATE conversations SET origin_kind=? WHERE id=?',
                                    ('wechat' if extra.get('wechat_native') else 'chat_export',cid))
                                changed=db.execute('''INSERT OR IGNORE INTO messages
                                  (id,conversation_id,time,sender,content,is_self,type,category,reason,source_message_id,source_table)
                                  VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                                  (mid,cid,when,'我' if m.is_self else m.sender,m.content,int(m.is_self),
                                   str(extra.get('type',kind)),category,reason,source_id,m.table)).rowcount
                                if changed:imported+=1
                                else:
                                    duplicates+=1
                                    if source_id and m.table.startswith('Msg_'):
                                        # Reparse stable server IDs when the reader fixes direction
                                        # or display decoding; keep user classification unchanged.
                                        db.execute('UPDATE messages SET sender=?,is_self=?,content=?,type=? WHERE id=?',
                                            ('我' if m.is_self else m.sender,int(m.is_self),m.content,str(extra.get('type',kind)),mid))
                                if extra.get('sender_id'):
                                    db.execute('UPDATE messages SET sender_id=? WHERE id=?',(extra['sender_id'],mid))
                                db.execute('INSERT OR IGNORE INTO provenance VALUES(?,?,?,?)',(mid,sid,source_path,m.table))
                                db.execute('INSERT OR IGNORE INTO conversation_sources VALUES(?,?,?,?)',(cid,sid,source_path,m.table))
                                if extra.get('media'):
                                    db.execute('INSERT INTO message_media_evidence VALUES(?,?) ON CONFLICT(message_id) DO UPDATE SET metadata=excluded.metadata',
                                               (mid,json.dumps(extra['media'],ensure_ascii=False)))
                        occurrence.commit()
                        report['imported']+=imported;report['duplicates']+=duplicates
                        report['read']+=len(batch)
                        self.progress({'message':f"已读取 {report['read']:,} 条聊天 · 文件 {index+1}/{len(candidates)} · {path.name}",
                            'phase':'reading','messages':report['read'],'imported':report['imported'],
                            'file':index+1,'files':len(candidates),'reused_files':report['reused_files']})
                    opts.on_batch=consume if kind=='sqlite' else None
                    try:
                        got=engine.EXTRACTORS[kind](engine.Candidate(path,kind,path.stat().st_size),opts,ctx)
                        if got:consume(got)
                    except Exception as exc:
                        if self.cancelled():break
                        report['errors'].append(f'{path.name} 解析失败（{type(exc).__name__}）');continue
                    report['files']+=1
                    if not self.cancelled() and len(ctx.errors)==before_errors:
                        with self.store.connect() as db:
                            for table,high in updated_state.items():
                                db.execute('INSERT INTO reader_watermarks VALUES(?,?,?,?) ON CONFLICT(source_id,origin,source_table) DO UPDATE SET high_row=excluded.high_row',
                                    (sid,source_path,table,high))
                    rows=report['read']-before_rows;row_counts[str(path)]=rows
                    state=states.get(str(path))
                    failed_notes=any(('失败' in n or '不可读' in n) for n in ctx.notes[before_notes:])
                    if failed_notes:
                        report['errors'].append(path.name+' 的部分聊天表不可读，已保留成功读取的记录')
                        report['unread_files'].append(source_path)
                    if state and not self.peers and not self.cancelled() and len(ctx.errors)==before_errors and not failed_notes:
                        original,signature,actual=state
                        expected=signature if actual is None else fingerprint(actual,owner,kind=='sqlite')+('|qx-cache-reader-2' if str(actual) in cache_paths else '')
                        if expected==signature:
                            metadata=next((dict(m) for m in reversed(getattr(ctx,'cache_sources',[])) if m['name']==path.name),None)
                            cache.finish(original,signature,rows,metadata)
                            completed_paths.add(str(path))
            if not self.cancelled() and not report['errors'] and not ctx.errors:
                for marker,signature,extracted in backup_pending:
                    if all(str(p) in completed_paths for p in extracted if p.suffix.lower() in EXT):
                        cache.finish(marker,signature,sum(row_counts.get(str(p),0) for p in extracted))
            with self.store.connect() as db:
                for path,kind in candidates:
                    if kind=='sqlite':
                        db.execute('INSERT INTO media_databases VALUES(?,?,?) ON CONFLICT(source_id,path) DO UPDATE SET original=excluded.original',
                            (sid,str(path),origin.get(str(path),str(path))))
            report['media']=dict(media.summary(sid),deferred=True)
            report['notes'].append('聊天读取不再预先转换全部媒体；图片、语音和视频在“媒体文件”中按需读取。')
            report['cache_sources']+=getattr(ctx,'cache_sources',[])
            incomplete_cache=any(not item.get('complete') for item in report['cache_sources'])
            if incomplete_cache:report['notes'].append('部分原导出器缓存没有全部同步完成；本次成功读取的记录可查看，不能视为全部历史聊天。')
            report['errors']+=ctx.errors
            report['notes']+=ctx.notes
            locked=any(d.get('status')=='no_matching_material' for d in report['database_results'])
            report['status']=('cancelled' if self.cancelled() else 'locked' if locked and not report['read']
                              else 'partial' if report['errors'] or incomplete_cache else 'ready' if report['read'] or report['identity']['sessions'] else 'empty')
            if not report['read'] and not report['errors']:
                report['notes'].append('没有找到可解析的聊天记录。若是微信自带的专用备份，请先在电脑版微信恢复或迁移，再选择实际账号记录目录。iPhone Finder/iTunes 备份可直接选择完整备份目录。')
        stored_report=report
        stored_status=report['status']
        if previous and (self.incremental or self.peers):
            stored_report=dict(previous,last_refresh=report)
            stored_status=('partial' if report['errors'] else previous.get('status','ready'))
            if stored_status in ('reading','empty') and previous.get('read'):stored_status='ready'
            stored_report['status']=stored_status
        with self.store.connect() as db:
            db.execute('''INSERT OR REPLACE INTO sources(id,path,kind,status,imported,duplicates,updated,detail)
              VALUES(?,?,?,?,?,?,?,?)''',(sid,str(root),stored_report['kind'],stored_status,stored_report['imported'],
                stored_report['duplicates'],datetime.now().isoformat(timespec='seconds'),json.dumps(stored_report,ensure_ascii=False)))
        return report

    @staticmethod
    def _account(path,root):
        for part in (*path.parts,*root.parts):
            if part.startswith('wxid_'):
                name=Path(part).stem if Path(part).suffix.lower() in engine.SQLITE_EXT else part
                return name
        # iPhone app's stable account directory is usually Documents/<32 hex>/DB.
        for i,part in enumerate(path.parts):
            if part=='Documents' and i+1<len(path.parts):return path.parts[i+1]
        return '未确认账号-'+digest(str(root))[:8]
