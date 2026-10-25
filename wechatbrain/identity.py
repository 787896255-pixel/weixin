"""Read contacts and sessions from authenticated, private SQLite snapshots.

Windows 4.1 SessionTable/contact column names were observed in the live account.
Session summaries remain metadata, never synthesized into chat messages.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from chatlog_platform import _to_dt, verified_self_id
from .store import digest


def _quote(value):
    return '"'+value.replace('"','""')+'"'


def _fields(columns,aliases):
    return {name:next((columns[a] for a in variants if a in columns),None) for name,variants in aliases.items()}


def _avatars(store,source,tables,account,cancelled):
    count=0
    for key in ('head_image','contactheadimg1','contactheadimg'):
        if key not in tables:continue
        table=tables[key]
        cols={r[1].casefold():r[1] for r in source.execute('PRAGMA table_info('+_quote(table)+')')}
        uid=next((cols[c] for c in ('username','usrname','user_name') if c in cols),None)
        blob=next((cols[c] for c in ('image_buffer','smallheadbuf','headimgbuf') if c in cols),None)
        if not uid or not blob:continue
        cursor=source.execute('SELECT '+_quote(uid)+','+_quote(blob)+' FROM '+_quote(table)+
            ' WHERE length('+_quote(blob)+') BETWEEN 16 AND 4194304')
        while not cancelled():
            rows=cursor.fetchmany(100)
            if not rows:break
            with store.connect() as db:
                for peer,data in rows:
                    if not peer or not isinstance(data,bytes):continue
                    mime=('image/jpeg' if data.startswith(b'\xff\xd8\xff') else 'image/png' if data.startswith(b'\x89PNG\r\n\x1a\n')
                          else 'image/webp' if data.startswith(b'RIFF') and data[8:12]==b'WEBP' else 'image/gif' if data[:6] in (b'GIF87a',b'GIF89a') else '')
                    if not mime:continue
                    db.execute('INSERT INTO avatars VALUES(?,?,?,?,?) ON CONFLICT(account,peer_id) DO UPDATE SET mime=excluded.mime,data=excluded.data',
                        (digest(account,str(peer)),account,str(peer),mime,data));count+=1
    return count


def read_metadata(store, candidates, source_id, root, owner, account_for, origins,
                  progress, cancelled):
    result={'contacts':0,'sessions':0,'accounts':0,'avatars':0,'errors':[]}
    snapshots=[]
    for path,kind in candidates:
        if kind!='sqlite' or path.name.lower() not in ('contact.db','session.db','micromsg.db','head_image.db','headimage.db','misc.db'):continue
        try:
            with path.open('rb') as stream:
                if stream.read(16)!=b'SQLite format 3\x00':continue
            snapshots.append((path,owner.strip() or account_for(path,root)))
        except OSError:continue
    # Contacts first, irrespective of directory enumeration order.
    snapshots.sort(key=lambda item:item[0].name.lower() not in ('contact.db','micromsg.db'))
    for path,account in snapshots:
        if cancelled():break
        progress('正在读取'+('联系人资料：' if path.name.lower()=='contact.db' else '会话列表：')+path.name)
        source=None
        try:
            source=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)
            source.row_factory=sqlite3.Row
            source.set_progress_handler(lambda:1 if cancelled() else 0,1000)
            tables={row[0].casefold():row[0] for row in source.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            result['avatars']+=_avatars(store,source,tables,account,cancelled)
            if path.name.lower() in ('contact.db','micromsg.db') and 'contact' in tables:
                table=tables['contact']
                columns={r[1].casefold():r[1] for r in source.execute('PRAGMA table_info('+_quote(table)+')')}
                mapped=_fields(columns,{'username':('username','user_name'),'nick_name':('nick_name','nickname'),
                    'remark':('remark','conremark'),'alias':('alias',),'local_type':('local_type','type')})
                if not mapped['username'] or not mapped['nick_name']:continue
                fields=[f for f,value in mapped.items() if value]
                cursor=source.execute('SELECT '+','.join(_quote(mapped[f]) for f in fields)+' FROM '+_quote(table))
                known={}
                while not cancelled():
                    rows=cursor.fetchmany(500)
                    if not rows:break
                    with store.connect() as db:
                        for row in rows:
                            values=dict(zip(fields,row));uid=str(values.get('username') or '').strip()
                            if not uid:continue
                            nickname=str(values.get('nick_name') or '').strip()
                            remark=str(values.get('remark') or '').strip()
                            alias=str(values.get('alias') or '').strip()
                            known[uid]=(remark or nickname or uid,alias)
                            db.execute('''INSERT INTO contacts(account,peer_id,name,nickname,remark,alias,local_type,source_id)
                                VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(account,peer_id) DO UPDATE SET
                                name=excluded.name,nickname=excluded.nickname,remark=excluded.remark,
                                alias=excluded.alias,local_type=excluded.local_type,source_id=excluded.source_id''',
                                (account,uid,remark or nickname or uid,nickname,remark,alias,values.get('local_type',0),source_id))
                            # Refresh names of existing conversations without creating empty ones
                            # for every service account or friend.
                            db.execute('UPDATE conversations SET name=? WHERE account=? AND peer_id=?',
                                       (remark or nickname or uid,account,uid))
                            result['contacts']+=1
                folder=account_for(path,root)
                self_id=verified_self_id(folder,known)
                if self_id in known:
                    with store.connect() as db:
                        db.execute('''INSERT INTO account_profiles VALUES(?,?,?,?,?) ON CONFLICT(account) DO UPDATE SET
                            username=excluded.username,name=excluded.name,alias=excluded.alias,source_id=excluded.source_id''',
                            (account,self_id,*known[self_id],source_id))
                    result['accounts']+=1
            if path.name.lower() in ('session.db','micromsg.db') and ('sessiontable' in tables or 'session' in tables):
                table=tables.get('sessiontable') or tables['session']
                columns={r[1].casefold():r[1] for r in source.execute('PRAGMA table_info('+_quote(table)+')')}
                mapped=_fields(columns,{'username':('username','strusrname'),'last_timestamp':('last_timestamp','ntime'),
                    'summary':('summary','strcontent'),'unread_count':('unread_count','nunreadcount'),'is_hidden':('is_hidden',)})
                if not mapped['username']:continue
                fields=[f for f,value in mapped.items() if value]
                cursor=source.execute('SELECT '+','.join(_quote(mapped[f]) for f in fields)+' FROM '+_quote(table))
                while not cancelled():
                    rows=cursor.fetchmany(500)
                    if not rows:break
                    with store.connect() as db:
                        for row in rows:
                            values=dict(zip(fields,row));uid=str(values.get('username') or '').strip()
                            if not uid:continue
                            cid=digest(account,uid)
                            contact=db.execute('SELECT name FROM contacts WHERE account=? AND peer_id=?',(account,uid)).fetchone()
                            when=_to_dt(values.get('last_timestamp'))
                            db.execute('''INSERT INTO conversations(id,account,peer_id,name) VALUES(?,?,?,?)
                                ON CONFLICT(id) DO UPDATE SET name=CASE WHEN ? THEN excluded.name ELSE conversations.name END''',
                                (cid,account,uid,contact[0] if contact else uid,bool(contact)))
                            db.execute("UPDATE conversations SET origin_kind='wechat' WHERE id=?",(cid,))
                            db.execute('''INSERT INTO session_metadata VALUES(?,?,?,?,?) ON CONFLICT(conversation_id) DO UPDATE SET
                                last_time=excluded.last_time,summary=excluded.summary,unread=excluded.unread,hidden=excluded.hidden''',
                                (cid,when.strftime('%Y-%m-%d %H:%M:%S') if when else '',
                                 str(values.get('summary') or ''),int(values.get('unread_count') or 0),int(values.get('is_hidden') or 0)))
                            db.execute('INSERT OR IGNORE INTO conversation_sources VALUES(?,?,?,?)',
                                (cid,source_id,origins.get(str(path),str(path)),table))
                            result['sessions']+=1
        except (OSError,sqlite3.DatabaseError,ValueError,TypeError):
            if not cancelled():result['errors'].append(path.name+' 的联系人或会话资料未完整读出')
        finally:
            if source:source.close()
    return result
