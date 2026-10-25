"""Bounded read-only source discovery; metadata is not proof of decryption."""
from __future__ import annotations

import os
import sqlite3
import time
import tempfile
from pathlib import Path

SQLITE_EXT={'.db','.sqlite','.sqlite3','.sqlite2','.bakdb','.db3','.sqlitedb'}
CACHE_COLUMNS={'username','seq','local_id','create_time','kind','search_text','data'}
SKIP={'.git','.obsidian','node_modules','runtime','FileStorage','Image','Video','attach','media',
      'EBWebView','Code Cache','GPUCache','Crashpad','source-cache','.build-cache'}


def cache_info(path, seconds=.15):
    """Probe a checkpointed schema without creating a WAL/SHM in the source.

    immutable deliberately ignores a live WAL. A negative result is unknown,
    and the actual import still takes a checked snapshot of the DB and WAL.
    """
    path=Path(path)
    try:
        if path.is_symlink() or not path.is_file():return None
        with path.open('rb') as stream:
            if stream.read(16)!=b'SQLite format 3\x00':return None
        deadline=time.monotonic()+seconds
        conn=sqlite3.connect(path.resolve().as_uri()+'?mode=ro&immutable=1',uri=True,timeout=.1)
        try:
            conn.set_progress_handler(lambda:1 if time.monotonic()>deadline else 0,500)
            table=conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='msg_rows'").fetchone()
            if not table:
                # A live exporter may have created its schema entirely in WAL.
                # Probe only small, verified private copies; never open its source
                # through SQLite's WAL reader (which could create source SHM).
                wal=Path(str(path)+'-wal')
                if wal.is_file() and path.stat().st_size+wal.stat().st_size<=8*1024*1024:
                    from .snapshot import copy_snapshot, SnapshotError
                    try:
                        with tempfile.TemporaryDirectory(prefix='jingke-cache-probe-') as work:
                            target=Path(work)/path.name
                            copy_snapshot(path,target,True,lambda:time.monotonic()>deadline)
                            probe=sqlite3.connect(target.resolve().as_uri()+'?mode=ro',uri=True,timeout=.1)
                            try:
                                probe.set_progress_handler(lambda:1 if time.monotonic()>deadline else 0,500)
                                fields={r[1] for r in probe.execute('PRAGMA table_info("msg_rows")')}
                                if CACHE_COLUMNS.issubset(fields):
                                    return {'path':str(path.resolve()),'kind':'qx-cache','layout':'原导出器聊天缓存','schema_only':True,'schema_in_wal':True}
                            finally:probe.close()
                    except (OSError,ValueError,sqlite3.DatabaseError,SnapshotError):pass
                return None
            columns={row[1] for row in conn.execute('PRAGMA table_info("msg_rows")')}
            if not CACHE_COLUMNS.issubset(columns):return None
            return {'path':str(path.resolve()),'kind':'qx-cache','layout':'原导出器聊天缓存','schema_only':True}
        finally:conn.close()
    except (OSError,sqlite3.DatabaseError):return None


def bounded_files(root, seconds=2, limit=4096, depth=6, cancelled=lambda:False):
    root=Path(root);deadline=time.monotonic()+seconds;seen=0;complete=True;files=[]
    if root.is_file():return [root],True
    for directory,dirs,names in os.walk(root,followlinks=False):
        if cancelled() or time.monotonic()>deadline or seen>=limit:complete=False;break
        base=Path(directory)
        if (base/'Manifest.db').is_file() and (base/'Manifest.plist').is_file():
            files.append(base/'Manifest.db');dirs[:]=[];continue
        dirs[:]=[d for d in dirs if d not in SKIP and not (base/d).is_symlink()
                 and len((base/d).relative_to(root).parts)<=depth]
        for name in names:
            if cancelled() or seen>=limit or time.monotonic()>deadline:complete=False;break
            seen+=1;p=base/name
            if p.suffix.lower() in SQLITE_EXT and not p.is_symlink():files.append(p)
    return files,complete


def inspect_source(root, files=None, cancelled=lambda:False):
    root=Path(root)
    if files is None:files,complete=bounded_files(root,cancelled=cancelled)
    else:complete=True
    layout=('Windows 微信 4.x 目录布局' if (root/'db_storage').is_dir() else
            'Windows 微信 3.x 目录布局' if (root/'Msg').is_dir() else
            'iPhone 本地备份' if (root/'Manifest.db').is_file() else '聊天文件或文件夹')
    result={'layout':layout,'scan_complete':complete,'database_files':0,'plaintext_databases':0,
            'encrypted_or_unknown_databases':0,'cache_paths':[],'core_database_names':[]}
    for path in files:
        if cancelled():result['scan_complete']=False;break
        path=Path(path)
        if path.suffix.lower() not in SQLITE_EXT and path!=root:continue
        if path.is_symlink():continue
        try:
            with path.open('rb') as stream:head=stream.read(16)
        except OSError:continue
        result['database_files']+=1
        if head==b'SQLite format 3\x00':
            result['plaintext_databases']+=1
            if cache_info(path):result['cache_paths'].append(str(path.resolve()))
        else:result['encrypted_or_unknown_databases']+=1
        if path.name.lower() in {'micromsg.db','contact.db','wcdb_contact.sqlite','msg0.db','message_0.db'}:
            result['core_database_names'].append(path.name)
    if result['cache_paths']:result['layout']='原导出器聊天缓存' if len(result['cache_paths'])==result['database_files'] else layout+' / 原导出器缓存'
    return result


def discover_caches():
    roots=[]
    for variable in ('LOCALAPPDATA','APPDATA'):
        if os.environ.get(variable):
            for name in ('wechat-exporter','wechatexport'):
                roots.append(Path(os.environ[variable])/name)
    found=[];seen=set();deadline=time.monotonic()+2
    for root in roots:
        if time.monotonic()>deadline:break
        if not root.is_dir() or root.is_symlink():continue
        files,_=bounded_files(root,seconds=min(.5,max(.01,deadline-time.monotonic())),limit=512,depth=3)
        for path in files:
            if time.monotonic()>deadline:break
            item=cache_info(path)
            if item and item['path'] not in seen:
                seen.add(item['path']);item['account']='聊天缓存 · '+path.stem;found.append(item)
    return found
