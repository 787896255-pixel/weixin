"""Refresh an authenticated private snapshot from committed encrypted WAL pages.

The source base must retain its identity and header. An interrupted patch marks
the cache unhealthy before touching it; a later run rebuilds from the source.
No source database is opened through SQLite or modified.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import struct
import tempfile
from pathlib import Path

from Crypto.Cipher import AES
from .loading import fingerprint
from .snapshot import _inventory, _hash_copy, _compatible, SnapshotError
from .sqlcipher import identify_key, _valid_mac, SQLITE_HEADER
from .wal import index_wal, WALError


def _state_table(store):
    with store.connect() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS native_snapshot_state(
            source_id TEXT,origin TEXT,detail TEXT NOT NULL,healthy INTEGER NOT NULL,
            PRIMARY KEY(source_id,origin))''')


def remember(store,source_id,source,target,wal_report,expected_base=None):
    _state_table(store)
    source,target=Path(source),Path(target)
    if expected_base is not None and json.loads(fingerprint(source))[2][0]!=expected_base:return
    with source.open('rb') as stream:head=stream.read(4096)
    if head.startswith(SQLITE_HEADER):return
    log=Path(str(source)+'-wal')
    try:
        with log.open('rb') as stream:header=stream.read(32)
    except OSError:header=b''
    state={'base_signature':fingerprint(source),'header':hashlib.sha256(head).hexdigest(),
        'plain_signature':fingerprint(target),'plain_path':str(target),'base_pages':source.stat().st_size//4096,
        'wal_generation':header[16:24].hex(),'committed_frames':wal_report.get('last_commit_frame',0)}
    if expected_base is not None and json.loads(state['base_signature'])[2][0]!=expected_base:return
    with store.connect() as db:
        db.execute('INSERT INTO native_snapshot_state VALUES(?,?,?,1) ON CONFLICT(source_id,origin) DO UPDATE SET detail=excluded.detail,healthy=1',
            (source_id,str(source),json.dumps(state,separators=(',',':'))))


def refresh(store,source_id,source,target,materials,cancelled,progress):
    """Return None for a full rebuild, or a verified committed-WAL refresh report."""
    _state_table(store)
    source,target=Path(source),Path(target)
    with store.connect() as db:
        row=db.execute('SELECT detail,healthy FROM native_snapshot_state WHERE source_id=? AND origin=?',
            (source_id,str(source))).fetchone()
    if not row or not row['healthy'] or not target.is_file():return None
    state=json.loads(row['detail'])
    if state['base_signature']!=fingerprint(source) or state['plain_signature']!=fingerprint(target):return None
    with source.open('rb') as stream:head=stream.read(4096)
    if hashlib.sha256(head).hexdigest()!=state['header']:return None
    info=next((found for key in materials if (found:=identify_key(head,key)) is not None),None)
    if info is None:return None
    wal_source=Path(str(source)+'-wal')
    if not wal_source.is_file() or wal_source.stat().st_size<32:return None
    with tempfile.TemporaryDirectory(prefix='jingke-live-wal-',dir=target.parent) as directory:
        log=Path(directory)/'encrypted-wal'
        before=_inventory(source,('','-wal','-journal'))
        if before.get('-journal') and before['-journal'][2]:return None
        limit=before['-wal'][2]
        checksum=_hash_copy(wal_source,log,cancelled,limit=limit)
        if not _compatible(before,_inventory(source,('','-wal','-journal')),True):raise SnapshotError('微信写入期间日志发生变化，稍后继续同步')
        if checksum!=_hash_copy(wal_source,cancelled=cancelled,limit=limit):raise SnapshotError('微信日志仍在写入，稍后继续同步')
        if not _compatible(before,_inventory(source,('','-wal','-journal')),True):raise SnapshotError('微信记录仍在写入，稍后继续同步')
        with log.open('rb') as stream:header=stream.read(32)
        if header[16:24].hex()!=state['wal_generation']:return None
        index=index_wal(log,info.profile.page_size,state['base_pages'],cancelled)
        if index.report.get('last_commit_frame',0)<state.get('committed_frames',0):return None
        if not index.report['transactions']:return None
        p=info.profile
        # Validate every selected frame before modifying the private base.
        with log.open('rb') as stream:
            for number in sorted(index.pages):
                if cancelled():raise SnapshotError('已取消读取')
                page=index.read_page(stream,number)
                if not _valid_mac(page,number,info):raise WALError('实时日志页面认证失败，保留上一份已读记录')
                if number==1 and page[:16]!=info.salt:raise WALError('实时日志与当前账号数据库不匹配')
        journal=Path(directory)/'undo-pages';original_size=target.stat().st_size
        with store.connect() as db:db.execute('UPDATE native_snapshot_state SET healthy=0 WHERE source_id=? AND origin=?',(source_id,str(source)))
        try:
            with target.open('r+b') as output,journal.open('wb') as undo,log.open('rb') as stream:
                for number in sorted(index.pages):
                    if cancelled():raise SnapshotError('已取消读取')
                    offset=(number-1)*p.page_size;output.seek(offset);old=output.read(p.page_size)
                    undo.write(struct.pack('<QI',offset,len(old))+old)
                    page=index.read_page(stream,number);end=p.page_size-p.reserve
                    plain=AES.new(info.encryption_key,AES.MODE_CBC,page[end:end+16]).decrypt(page[16 if number==1 else 0:end])
                    if number==1:
                        plain=SQLITE_HEADER+plain
                        plain=plain[:18]+b'\x01\x01'+plain[20:28]+struct.pack('>I',index.database_pages)+plain[32:]
                    output.seek(offset);output.write(plain+page[end:])
                # Page 1 might not be among modified WAL pages; SQLite's header
                # still needs the committed page count when the DB grows.
                output.seek(0);old=output.read(p.page_size)
                undo.write(struct.pack('<QI',0,len(old))+old)
                output.seek(28);output.write(struct.pack('>I',index.database_pages))
                if index.database_pages*p.page_size<original_size:
                    # A truncating transaction needs a full rebuild so rollback
                    # never has to retain a potentially huge freed tail.
                    raise SnapshotError('数据库页数缩短，需要重新取得完整副本')
                output.flush();os.fsync(output.fileno())
            connection=sqlite3.connect(target.resolve().as_uri()+'?mode=ro&immutable=1',uri=True)
            try:
                connection.execute('PRAGMA schema_version').fetchone()
                connection.execute('SELECT name FROM sqlite_master LIMIT 1').fetchone()
            finally:connection.close()
        except Exception:
            if journal.exists():
                with target.open('r+b') as output,journal.open('rb') as undo:
                    # Restore in reverse order because page 1 is journaled twice.
                    records=[]
                    while True:
                        at=undo.tell();header=undo.read(12)
                        if not header:break
                        offset,length=struct.unpack('<QI',header);records.append((at,offset,length));undo.seek(length,1)
                    for at,offset,length in reversed(records):
                        undo.seek(at+12);output.seek(offset);output.write(undo.read(length))
                    output.truncate(original_size);output.flush();os.fsync(output.fileno())
            raise
        state.update(plain_signature=fingerprint(target),committed_frames=index.report.get('last_commit_frame',0))
        with store.connect() as db:
            db.execute('UPDATE native_snapshot_state SET detail=?,healthy=1 WHERE source_id=? AND origin=?',
                (json.dumps(state,separators=(',',':')),source_id,str(source)))
        progress('已合并微信实时日志：'+source.name+' · '+str(len(index.pages))+' 页')
        return {'status':'wal_refreshed','attempts':1,'bytes':log.stat().st_size,
                'verification':'stable-source-base-and-committed-wal-page-authentication','wal':index.report}
