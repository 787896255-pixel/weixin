"""Index selected local media, decode into private cache, and link by evidence."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict
from pathlib import Path

from .media_codec import decode_image, convert_audio, convert_video, voice_payload, MediaError
from .snapshot import copy_snapshot
from .store import digest

IMAGE_EXT = {'.dat', '.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.tif', '.tiff', '.pic', '.pic_hd', '.pic_thum', '.wxgf'}
AUDIO_EXT = {'.silk', '.aud', '.amr', '.wav', '.mp3', '.aac', '.m4a', '.ogg'}
VIDEO_EXT = {'.mp4', '.mov', '.m4v', '.video', '.hevc', '.h265'}
FILE_EXT = {'.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.txt', '.csv', '.zip', '.7z'}
SKIP = {'.git', '.obsidian', 'node_modules', '__pycache__', '.wechatbrain', '.build-cache', 'runtime'}


class MediaLibrary:
    def __init__(self, store, progress=lambda s: None, cancelled=lambda: False):
        self.store, self.progress, self.cancelled = store, progress, cancelled
        self.folder = store.directory / 'media'
        self.folder.mkdir(exist_ok=True)
        self.image_keys=[]
        self.xor_keys=[]
        self.image_diagnostics={}
        self.backup_passwords={}
        self._executor=None
        self._preparing=set()
        self._queue_lock=threading.Lock()
        self._queue_slots=threading.BoundedSemaphore(32)
        self._stop=threading.Event()
        self._image_roots=set()

    def asset_path(self, asset):
        path = (self.store.directory / asset['cache_path']).resolve()
        if not path.is_relative_to(self.folder.resolve()) or not path.is_file():
            raise MediaError('媒体缓存不存在，请重新读取原目录')
        return path

    def _save(self, identity, source_id, path, signature, kind, cached='', mime='', error=''):
        with self.store.connect() as db:
            db.execute('''INSERT INTO media_assets(id,source_id,original_path,name,signature,kind,cache_path,mime,status,error)
              VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
              signature=excluded.signature,kind=excluded.kind,cache_path=excluded.cache_path,
              mime=excluded.mime,status=excluded.status,error=excluded.error''',
              (identity, source_id, str(path), path.name, signature, kind, cached, mime,
               'ready' if cached else 'unread', error))

    def ingest_file(self, path, source_id, identity_path=None, signature_override=None):
        path = Path(path)
        ext = path.suffix.lower()
        kind = ('image' if ext in IMAGE_EXT else 'audio' if ext in AUDIO_EXT
                else 'video' if ext in VIDEO_EXT else 'file' if ext in FILE_EXT else None)
        if kind is None or path.is_symlink(): return None
        identity = digest(source_id, str(identity_path or path))
        try:
            stat = path.stat()
            signature = signature_override or f'{stat.st_size}:{stat.st_mtime_ns}:{stat.st_ctime_ns}'
            with self.store.connect() as db:
                old = db.execute('SELECT * FROM media_assets WHERE id=?', (identity,)).fetchone()
            if old and old['signature'] == signature and old['status'] == 'ready':
                try: self.asset_path(old); return identity
                except MediaError: pass
            limit = 64*1024*1024 if kind == 'image' else 32*1024*1024 if kind == 'audio' else 2*1024**3
            if stat.st_size > limit: raise MediaError('文件超过本次媒体处理上限')
            self.progress('正在处理本地媒体：' + path.name)
            with tempfile.TemporaryDirectory(prefix='.work-', dir=self.folder) as work:
                work = Path(work)
                raw = work / ('source' + ext)
                copy_snapshot(path, raw, cancelled=self.cancelled)
                if kind == 'image':
                    raw_bytes=raw.read_bytes()
                    content, suffix, mime = decode_image(raw_bytes,self.image_keys,self.xor_keys,self.cancelled)
                    if raw_bytes and content and not raw_bytes.startswith((b'\x07\x08V1',b'\x07\x08V2')):
                        xor=raw_bytes[0]^content[0]
                        if xor not in self.xor_keys:self.xor_keys.append(xor)
                    ready = work / ('decoded.' + suffix); ready.write_bytes(content)
                elif kind == 'audio':
                    ready = work / 'decoded.wav'
                    suffix, mime = convert_audio(raw, ready, self.cancelled)
                elif kind == 'video':
                    ready = work / 'decoded.mp4'
                    suffix, mime = convert_video(raw, ready, self.cancelled)
                else:
                    ready, suffix, mime = raw, ext[1:], 'application/octet-stream'
                checksum = hashlib.sha256()
                with ready.open('rb') as stream:
                    while True:
                        if self.cancelled(): raise MediaError('已停止媒体处理')
                        chunk = stream.read(1024*1024)
                        if not chunk: break
                        checksum.update(chunk)
                destination = self.folder / (checksum.hexdigest() + '.' + suffix)
                if not destination.exists(): os.replace(ready, destination)
                self._save(identity, source_id, Path(identity_path or path), signature, kind,
                           str(destination.relative_to(self.store.directory)), mime)
            return identity
        except (OSError, ValueError) as exc:
            self._save(identity, source_id, Path(identity_path or path), '', kind,
                       error=str(exc) if isinstance(exc, ValueError) else '媒体文件无法读取')
            return identity

    def _load_image_keys(self, root):
        root=Path(root)
        # Accept only explicitly named local image-connection metadata. Secrets
        # stay in this reader object and never enter SQLite, reports or exports.
        for name in ('image_key.json','image_keys.json'):
            path=root/name
            if path.is_file() and not path.is_symlink() and path.stat().st_size<65536:
                try:
                    values=json.loads(path.read_text(encoding='utf-8-sig'))
                    if isinstance(values,dict):values=[v for k,v in values.items() if k.lower() in ('aes_key','image_key','image_aes_key')]
                    if isinstance(values,str):values=[values]
                    if isinstance(values,list):
                        for value in values:
                            if not isinstance(value,str):continue
                            key=bytes.fromhex(value) if re.fullmatch(r'[a-fA-F0-9]{32}',value) else value.encode()
                            if len(key)==16 and key not in self.image_keys:self.image_keys.append(key)
                except (ValueError,OSError):pass
        from .image_material import discover_image_keys
        keys,self.image_diagnostics=discover_image_keys(root,self.progress,self.cancelled)
        for key in keys:
            if key not in self.image_keys:self.image_keys.append(key)

    def _pending(self, db, path, source_id, locator, signature, kind):
        path=Path(path);identity=digest(source_id,str(path))
        db.execute("""INSERT INTO media_assets(id,source_id,original_path,name,signature,kind,cache_path,mime,status,error)
          VALUES(?,?,?,?,?,?,?,'','queued','') ON CONFLICT(id) DO UPDATE SET
          cache_path=CASE WHEN media_assets.signature=excluded.signature THEN media_assets.cache_path ELSE '' END,
          mime=CASE WHEN media_assets.signature=excluded.signature THEN media_assets.mime ELSE '' END,
          status=CASE WHEN media_assets.signature=excluded.signature THEN media_assets.status ELSE 'queued' END,
          error=CASE WHEN media_assets.signature=excluded.signature THEN media_assets.error ELSE '' END,
          signature=excluded.signature""",(identity,source_id,str(path),path.name,signature,kind,''))
        db.execute('INSERT INTO media_origins VALUES(?,?) ON CONFLICT(asset_id) DO UPDATE SET locator=excluded.locator',
                   (identity,json.dumps(locator,ensure_ascii=False)))
        return identity

    def index_folder(self, root, source_id, identity_root=None):
        """Register paths and metadata only; decode on preview or explicit export."""
        root=Path(root)
        if (root/'Manifest.db').is_file() and (root/'Manifest.plist').is_file():
            return self.index_backup(root,source_id)
        batch=[];total=0
        def flush():
            nonlocal total
            with self.store.connect() as db:
                for path,identity,kind,signature in batch:
                    self._pending(db,identity,source_id,{'kind':'file','path':str(path),'root':str(root)},signature,kind)
            total+=len(batch);batch.clear()
            self.progress(f'已登记 {total:,} 个媒体文件，预览时再解码')
        for directory,dirs,names in os.walk(root,followlinks=False):
            if self.cancelled():break
            dirs[:]=[d for d in dirs if d not in SKIP and not (Path(directory)/d).is_symlink()
                     and (Path(directory)/d).resolve()!=self.store.directory.resolve()]
            for name in names:
                if self.cancelled():break
                path=Path(directory)/name;ext=path.suffix.lower()
                kind=('image' if ext in IMAGE_EXT else 'audio' if ext in AUDIO_EXT else
                      'video' if ext in VIDEO_EXT else 'file' if ext in FILE_EXT else None)
                if not kind or path.is_symlink():continue
                if kind=='file' and not any(p.lower() in ('file','filestorage','attach','attachment') for p in path.relative_to(root).parts[:-1]):continue
                try:stat=path.stat()
                except OSError:continue
                identity=Path(identity_root)/path.relative_to(root) if identity_root else path
                batch.append((path,identity,kind,f'{stat.st_size}:{stat.st_mtime_ns}:{stat.st_ctime_ns}'))
                if len(batch)>=500:flush()
        if batch:flush()
        return self.summary(source_id)

    def index_backup(self, root, source_id, password=''):
        from chatlog_platform import IOSBackup
        root=Path(root)
        if password:self.backup_passwords[str(root)]=password
        backup=IOSBackup(root,self.backup_passwords.get(str(root)),[])
        try:
            if not backup.unlock():raise MediaError('这个 iPhone 备份需要原备份密码，请填写后再读取媒体')
            entries=backup.wechat_entries(include_media=True)
            with self.store.connect() as db:
                for fid,rel,domain in entries:
                    if self.cancelled():break
                    ext=Path(rel).suffix.lower()
                    kind=('image' if ext in IMAGE_EXT else 'audio' if ext in AUDIO_EXT else 'video' if ext in VIDEO_EXT else None)
                    if not kind:continue
                    path=root/fid[:2]/fid
                    if not path.exists():path=root/fid
                    if path.is_symlink():continue
                    try:info=path.stat()
                    except OSError:continue
                    identity=str(root)+' / '+domain+'/'+rel
                    self._pending(db,identity,source_id,{'kind':'iphone','root':str(root),'file_id':fid,'relative':rel},
                                  f'{info.st_size}:{info.st_mtime_ns}:{info.st_ctime_ns}',kind)
            return self.summary(source_id)
        finally:
            if backup._tmp_manifest:backup._tmp_manifest.unlink(missing_ok=True)

    def statuses(self, identities):
        if not identities:return []
        with self.store.connect() as db:
            return [dict(r) for r in db.execute('SELECT id,name,kind,mime,status,error FROM media_assets WHERE id IN ('+
                ','.join('?' for _ in identities)+')',identities)]

    def request_prepare(self, identities):
        for identity in identities:
            with self._queue_lock:
                if identity in self._preparing:continue
                if not self._queue_slots.acquire(blocking=False):break
                self._preparing.add(identity)
                if self._executor is None:self._executor=ThreadPoolExecutor(max_workers=2,thread_name_prefix='jingke-media')
            with self.store.connect() as db:
                db.execute("UPDATE media_assets SET status='queued',error='' WHERE id=? AND status<>'ready'",(identity,))
            def run(item=identity):
                try:self.prepare(item,preview=True)
                finally:
                    with self._queue_lock:self._preparing.discard(item)
                    self._queue_slots.release()
            self._executor.submit(run)
        return {'items':self.statuses(identities)}

    def prepare(self, identity, preview=False):
        with self.store.connect() as db:
            asset=db.execute('SELECT a.*,o.locator FROM media_assets a LEFT JOIN media_origins o ON o.asset_id=a.id WHERE a.id=?',(identity,)).fetchone()
        if not asset:return
        if asset['status']=='ready':
            try:self.asset_path(asset);return
            except MediaError:pass
        if not asset['locator']:return
        locator=json.loads(asset['locator']);stop=self._stop.is_set if preview else self.cancelled
        reader=MediaLibrary(self.store,(lambda text:None) if preview else self.progress,stop)
        reader.image_keys=self.image_keys;reader.xor_keys=self.xor_keys
        def decode(path):
            with Path(path).open('rb') as f:head=f.read(6)
            if head==b'\x07\x08V2\x08\x07':
                root=locator['root']
                with self._queue_lock:
                    if root not in self._image_roots:
                        reader._load_image_keys(root);self._image_roots.add(root)
            reader.ingest_file(path,asset['source_id'],asset['original_path'],asset['signature'])
        try:
            if locator['kind']=='file':
                path=Path(locator['path']);info=path.stat()
                if path.is_symlink() or f'{info.st_size}:{info.st_mtime_ns}:{info.st_ctime_ns}'!=asset['signature']:
                    raise MediaError('源媒体已变化，请重新读取媒体目录')
                decode(path)
            elif locator['kind']=='sqlite-voice':
                from .loading import fingerprint
                path=Path(locator['path'])
                if path.is_symlink() or fingerprint(path,database=True)!=locator['snapshot_signature']:
                    raise MediaError('语音数据库缓存已变化，请重新读取媒体位置')
                quote=lambda text:'"'+text.replace('"','""')+'"'
                connection=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)
                try:
                    row=connection.execute('SELECT '+quote(locator['blob'])+' FROM '+quote(locator['table'])+
                        ' WHERE rowid=? AND length('+quote(locator['blob'])+') BETWEEN 10 AND 33554432',(locator['rowid'],)).fetchone()
                finally:connection.close()
                if not row or not isinstance(row[0],bytes):raise MediaError('没有可读取的语音数据')
                data,ext=voice_payload(row[0])
                if not ext:raise MediaError('语音数据格式尚未识别')
                with tempfile.TemporaryDirectory(prefix='.voice-',dir=self.folder) as work:
                    raw=Path(work)/('voice'+ext);raw.write_bytes(data)
                    reader.ingest_file(raw,asset['source_id'],asset['original_path'],asset['signature'])
            elif locator['kind']=='iphone':
                from chatlog_platform import IOSBackup
                root=locator['root'];backup=IOSBackup(Path(root),self.backup_passwords.get(root),[])
                try:
                    if not backup.unlock():raise MediaError('重开软件后，请填写 iPhone 备份密码并重新读取媒体目录')
                    db=backup.manifest_conn()
                    try:row=db.execute('SELECT file FROM Files WHERE fileID=?',(locator['file_id'],)).fetchone()
                    finally:db.close()
                    physical=Path(root)/locator['file_id'][:2]/locator['file_id']
                    if not physical.exists():physical=Path(root)/locator['file_id']
                    if physical.is_symlink() or physical.stat().st_size>256*1024*1024:
                        raise MediaError('备份媒体超过单文件读取上限或不是普通文件')
                    info=physical.stat()
                    if f'{info.st_size}:{info.st_mtime_ns}:{info.st_ctime_ns}'!=asset['signature']:
                        raise MediaError('备份媒体已变化，请重新读取媒体目录')
                    data=backup.read_file(locator['file_id'],row[0] if row else None,locator['relative'])
                    if len(data)>256*1024*1024:raise MediaError('备份媒体超过单文件读取上限')
                    with tempfile.TemporaryDirectory(prefix='.iphone-',dir=self.folder) as work:
                        path=Path(work)/Path(asset['name']).name;path.write_bytes(data);decode(path)
                finally:
                    if backup._tmp_manifest:backup._tmp_manifest.unlink(missing_ok=True)
        except Exception as exc:
            with self.store.connect() as db:db.execute("UPDATE media_assets SET status='unread',error=? WHERE id=?",
                (str(exc) if isinstance(exc,ValueError) else '媒体读取失败，请重新选择原目录',identity))

    def close(self):
        self._stop.set()
        if self._executor:self._executor.shutdown(wait=False,cancel_futures=True)

    def index_source_databases(self, source_id):
        with self.store.connect() as db:
            sources=db.execute('SELECT path,original FROM media_databases WHERE source_id=?',(source_id,)).fetchall()
        for item in sources:
            if self.cancelled(): break
            self.index_database(Path(item['path']),source_id,item['original'])

    def index_database(self, path, source_id, original):
        """Register voice row locators without reading or decoding BLOB payloads."""
        from .loading import fingerprint
        from chatlog_platform import _to_dt
        path=Path(path)
        try:
            signature=fingerprint(path,database=True)
            with path.open('rb') as stream:
                if stream.read(16)!=b'SQLite format 3\x00':return
            connection=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)
            connection.set_progress_handler(lambda:1 if self.cancelled() else 0,10000)
            quote=lambda text:'"'+text.replace('"','""')+'"'
            try:
                tables=[r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
                for table in tables:
                    if table.lower() not in ('media','voiceinfo','voice_info','voice'):continue
                    columns={r[1].lower():r[1] for r in connection.execute('PRAGMA table_info('+quote(table)+')')}
                    blob=next((columns[k] for k in ('buf','buffer','voice_data','data') if k in columns),None)
                    sid=next((columns[k] for k in ('msgsvrid','msg_svr_id','svr_id','server_id','reserved0') if k in columns),None)
                    local=next((columns[k] for k in ('local_id','localid','msglocalid') if k in columns),None)
                    created=next((columns[k] for k in ('create_time','createtime') if k in columns),None)
                    if not blob or not (sid or (local and created)):continue
                    query='SELECT rowid,'+','.join(quote(c) if c else 'NULL' for c in (sid,local,created))+',length('+quote(blob)+') FROM '+quote(table)+' WHERE length('+quote(blob)+') BETWEEN 10 AND 33554432'
                    if 'data_index' in columns:query+=' ORDER BY '+quote(columns['data_index'])
                    seen=set();batch=[]
                    def flush():
                        with self.store.connect() as db:
                            for rowid,name,alias,length in batch:
                                virtual=str(original)+'#'+table+'/'+name+'.aud'
                                locator={'kind':'sqlite-voice','path':str(path),'table':table,'blob':blob,
                                         'rowid':rowid,'snapshot_signature':signature}
                                identity=self._pending(db,virtual,source_id,locator,digest(signature,rowid,length),'audio')
                                if alias:db.execute('INSERT OR IGNORE INTO media_asset_aliases VALUES(?,?)',(identity,alias))
                        batch.clear()
                    for rowid,identity,local_id,timestamp,length in connection.execute(query):
                        if self.cancelled():break
                        alias=''
                        if str(local_id or '').isdigit() and int(local_id)>0:
                            when=_to_dt(timestamp)
                            if when:alias='voice-local-'+str(int(local_id))+'-'+str(int(when.timestamp()))
                        name=str(identity) if re.fullmatch(r'\d{7,20}',str(identity)) else alias
                        if not name or name in seen:continue
                        seen.add(name);batch.append((rowid,name,alias,length))
                        if len(batch)>=500:
                            flush();self.progress(f'正在登记语音位置 · {path.name} · {len(seen):,} 条')
                    if batch:flush()
            finally:connection.close()
        except (sqlite3.DatabaseError,OSError):
            self.progress('该文件尚不能登记语音位置：'+path.name)

    def link(self, source_id):
        with self.store.connect() as db:
            assets = db.execute('SELECT * FROM media_assets WHERE source_id=?', (source_id,)).fetchall()
            evidence = db.execute('''SELECT e.* FROM message_media_evidence e JOIN provenance p ON p.message_id=e.message_id
              WHERE p.source_id=? GROUP BY e.message_id''', (source_id,)).fetchall()
            by_hash, by_path = defaultdict(list), defaultdict(list)
            by_id = {a['id']:a for a in assets}
            for asset in assets:
                for md5 in set(re.findall(r'(?i)[0-9a-f]{32}', asset['name'])):
                    by_hash[md5.lower()].append(asset)
                by_path[asset['name'].lower()].append(asset)
            for row in db.execute('''SELECT x.asset_id,x.alias FROM media_asset_aliases x
              JOIN media_assets a ON a.id=x.asset_id WHERE a.source_id=?''',(source_id,)):
                if row['asset_id'] in by_id: by_path[row['alias'].lower()].append(by_id[row['asset_id']])
            linked = 0
            for item in evidence:
                meta = json.loads(item['metadata'])
                candidates = {}
                for md5 in meta.get('md5s', []):
                    for a in by_hash.get(md5.lower(), []): candidates[a['id']] = a
                for hint in meta.get('files', []):
                    for a in by_path.get(Path(hint.replace('\\','/')).name.lower(), []): candidates[a['id']] = a
                expected = meta.get('kind')
                candidates = [a for a in candidates.values() if a['kind'] == expected or expected == 'file']
                if not candidates: continue
                if not meta.get('md5s') and len({a['cache_path'] or a['original_path'] for a in candidates}) > 1:
                    # Repeated generic filenames do not establish message identity.
                    continue
                # Prefer ready originals. A thumbnail is used only if no original was found.
                candidates.sort(key=lambda a: (a['status'] != 'ready', bool(re.search(r'(?i)(thumb|_t[._]|\.pic_thum)', a['name'])), a['id']))
                chosen = candidates[0]
                db.execute('INSERT OR IGNORE INTO message_media VALUES(?,?,?)', (item['message_id'], chosen['id'], 'metadata-md5-or-filename'))
                linked += 1
        return linked

    def summary(self, source_id):
        with self.store.connect() as db:
            rows = db.execute('SELECT status,count(*) FROM media_assets WHERE source_id=? GROUP BY status', (source_id,)).fetchall()
            counts = dict(rows)
            return {'files': sum(counts.values()), 'ready': counts.get('ready',0), 'unread': counts.get('unread',0), 'queued':counts.get('queued',0),
                    'image_connection':dict(self.image_diagnostics)}

    def listing(self, source_id='', offset=0):
        with self.store.connect() as db:
            where = 'WHERE source_id=?' if source_id else ''
            args = [source_id] if source_id else []
            total = db.execute('SELECT count(*) FROM media_assets '+where, args).fetchone()[0]
            rows = db.execute('SELECT id,name,kind,mime,status,error,(SELECT count(*) FROM message_media l WHERE l.asset_id=media_assets.id) AS linked_messages FROM media_assets '+where+
                              ' ORDER BY name,id LIMIT 60 OFFSET ?', args+[offset]).fetchall()
            return {'items':[dict(r) for r in rows], 'total':total, 'offset':offset, 'has_more':offset+len(rows)<total}

    def get(self, identity):
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM media_assets WHERE id=?', (identity,)).fetchone()
            if not row or row['status'] != 'ready': raise MediaError('媒体尚未成功解码')
            return dict(row)

    def for_messages(self, messages):
        if not messages: return messages
        ids = [m['id'] for m in messages]
        mapping = defaultdict(list)
        with self.store.connect() as db:
            # Resolve only the visible messages against indexed local names.
            # Never materialize the entire media catalogue to display a page.
            pending=[]
            for start in range(0,len(ids),400):
                chunk=ids[start:start+400]
                pending+=db.execute('''SELECT e.message_id,e.metadata,p.source_id,p.path,c.peer_id,m.time FROM message_media_evidence e
                    JOIN provenance p ON p.message_id=e.message_id JOIN messages m ON m.id=e.message_id
                    JOIN conversations c ON c.id=m.conversation_id WHERE e.message_id IN ('''+','.join('?' for _ in chunk)+''')
                    AND NOT EXISTS(SELECT 1 FROM message_media l WHERE l.message_id=e.message_id)
                    GROUP BY e.message_id,p.source_id''',chunk).fetchall()
            for item in pending:
                meta=json.loads(item['metadata']);candidates={}
                for md5 in meta.get('md5s',[]):
                    if not re.fullmatch('[0-9a-fA-F]{32}',md5):continue
                    for asset in db.execute('SELECT * FROM media_assets WHERE source_id=? AND name GLOB ? LIMIT 16',
                        (item['source_id'],md5.lower()+'*')):candidates[asset['id']]=dict(asset)
                for hint in meta.get('files',[]):
                    name=Path(str(hint).replace('\\','/')).name
                    if not name:continue
                    for asset in db.execute('SELECT * FROM media_assets WHERE source_id=? AND name=? LIMIT 16',
                        (item['source_id'],name)):candidates[asset['id']]=dict(asset)
                candidates=[a for a in candidates.values() if a['kind']==meta.get('kind') or meta.get('kind')=='file']
                if not candidates:
                    # Known account/month locations can be addressed directly by
                    # the message MD5. No recursive attachment scan is needed.
                    source=Path(item['path']);account=None
                    for parent in list(source.parents)[:6]:
                        if (parent/'db_storage').is_dir() or (parent/'Msg').is_dir():account=parent;break
                    if account:
                        month=str(item['time'] or '')[:7];peer_hash=hashlib.md5(item['peer_id'].encode()).hexdigest()
                        if re.fullmatch(r'\d{4}-\d{2}',month):
                            folders=[account/'msg/attach'/peer_hash/month/sub for sub in ('Img','Video','File')]
                            folders+=[account/'FileStorage/MsgAttach'/peer_hash/sub/month for sub in ('Image','Video','File')]
                            folders+=[account/'FileStorage'/sub/month for sub in ('Image','Video','File')]
                            extensions=('.dat','_t.dat','_h.dat','.pic','.pic_hd','.pic_thum','.jpg','.png','.gif','.webp','.mp4','.silk','.amr')
                            names={name+ext for name in meta.get('md5s',[]) if re.fullmatch('[0-9a-fA-F]{32}',name) for ext in extensions}
                            names.update(Path(str(v).replace('\\','/')).name for v in meta.get('files',[]) if str(v) and not str(v).startswith(('http:','https:')))
                            for folder in folders:
                                if not folder.is_dir() or folder.is_symlink():continue
                                for name in sorted(names)[:64]:
                                    path=folder/name
                                    if not path.is_file() or path.is_symlink():continue
                                    stat=path.stat();ext=path.suffix.lower()
                                    kind='image' if ext in IMAGE_EXT else 'audio' if ext in AUDIO_EXT else 'video' if ext in VIDEO_EXT else 'file'
                                    if kind!=meta.get('kind') and meta.get('kind')!='file':continue
                                    identity=self._pending(db,path,item['source_id'],{'kind':'file','path':str(path),'root':str(account)},
                                        f'{stat.st_size}:{stat.st_mtime_ns}:{stat.st_ctime_ns}',kind)
                                    candidates.append(dict(db.execute('SELECT * FROM media_assets WHERE id=?',(identity,)).fetchone()))
                if not candidates:continue
                if not meta.get('md5s') and len({a['original_path'] for a in candidates})>1:continue
                candidates.sort(key=lambda a:(a['status']!='ready',bool(re.search(r'(?i)(thumb|_t[._]|\.pic_thum)',a['name'])),a['id']))
                db.execute('INSERT OR IGNORE INTO message_media VALUES(?,?,?)',
                    (item['message_id'],candidates[0]['id'],'visible-message-metadata'))
            for start in range(0,len(ids),400):
                batch = ids[start:start+400]
                rows = db.execute('''SELECT l.message_id,a.id,a.name,a.kind,a.mime,a.status,a.error FROM message_media l
                  JOIN media_assets a ON a.id=l.asset_id WHERE l.message_id IN ('''+','.join('?' for _ in batch)+')', batch)
                for row in rows:
                    asset = dict(row); mid = asset.pop('message_id'); mapping[mid].append(asset)
        for m in messages: m['attachments'] = mapping.get(m['id'], [])
        return messages

    def export_messages(self, messages, folder):
        self.for_messages(messages)
        target = Path(folder)/'media'; target.mkdir(exist_ok=True)
        for message in messages:
            for item in message['attachments']:
                if self.cancelled():return messages
                if item['status']!='ready':
                    self.prepare(item['id'])
                    item.update(next(iter(self.statuses([item['id']])),{}))
                if item['status']!='ready': continue
                try:
                    asset=self.get(item['id']); source=self.asset_path(asset)
                    destination=target/source.name
                    if not destination.exists(): shutil.copyfile(source,destination)
                    item['url']='media/'+destination.name
                except (OSError,ValueError):
                    item['status']='unread';item['error']='附件缓存不可用，请重新读取源目录'
        return messages

    def export_folder(self, destination, source_id=''):
        import uuid
        from datetime import datetime
        root=Path(destination)/('京客媒体-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:6])
        root.mkdir()
        with self.store.connect() as db:
            rows=db.execute('SELECT * FROM media_assets'+
                            (' WHERE source_id=?' if source_id else ''),[source_id] if source_id else []).fetchall()
        manifest=[];errors=[]
        for number,row in enumerate(rows,1):
            if self.cancelled():break
            self.progress(f'正在导出媒体 {number}/{len(rows)} · '+row['name'])
            try:
                if row['status']!='ready':self.prepare(row['id'])
                row=self.get(row['id'])
                path=self.asset_path(row);folder=root/row['kind'];folder.mkdir(exist_ok=True)
                target=folder/path.name
                if not target.exists():copy_snapshot(path,target,cancelled=self.cancelled)
                manifest.append({'id':row['id'],'name':row['name'],'kind':row['kind'],
                                 'file':str(target.relative_to(root))})
            except (ValueError,OSError) as exc:errors.append({'name':row['name'],'error':str(exc)})
        result={'folder':str(root),'files':len(manifest),'errors':errors,'cancelled':bool(self.cancelled())}
        (root/'媒体清单.json').write_text(json.dumps(dict(result,items=manifest),ensure_ascii=False,indent=2),encoding='utf-8')
        return result
