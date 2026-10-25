"""Read user-selected QX msg_rows caches with an independently written adapter.

The observed cachePage path deserializes JSON in msg_rows.data. This adapter
reads the same stored data; it does not execute or distribute the vendor app.
Schema and payload evidence: native/loading-20261023 in the private REA notes.
"""
from __future__ import annotations

import json


def extract_cache(conn, path, opts, ctx):
    from chatlog_extract import Message, clean_text, parse_ts
    from chatlog_platform import RecordBuffer, _media_meta
    columns={r[1] for r in conn.execute('PRAGMA table_info("msg_rows")')}
    needed={'username','seq','local_id','create_time','kind','search_text','data'}
    if not needed.issubset(columns): return None
    out=RecordBuffer(getattr(opts,'on_batch',None),getattr(opts,'cancelled',lambda:False))
    invalid=0;source_rows=0
    tables={row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    names={}
    if 'contact_overrides' in tables:
        fields={row[1] for row in conn.execute('PRAGMA table_info("contact_overrides")')}
        if {'username','nickname'}.issubset(fields):
            for peer,nickname in conn.execute('SELECT username,nickname FROM contact_overrides LIMIT 100001'):
                if len(names)>=100000:
                    ctx.note('联系人名称过多，仅载入前 100000 个名称');break
                if peer and nickname:names[str(peer)]=str(nickname)
    sync={'known':False,'complete':False,'sessions':0,'synced_sessions':0,'expected_messages':None}
    if 'msg_sync' in tables:
        fields={row[1] for row in conn.execute('PRAGMA table_info("msg_sync")')}
        if {'username','synced','total'}.issubset(fields):
            row=conn.execute('SELECT count(*),sum(CASE WHEN synced=1 THEN 1 ELSE 0 END),sum(total) FROM msg_sync').fetchone()
            sync.update(known=bool(row[0]),sessions=row[0],synced_sessions=row[1] or 0,expected_messages=row[2])
            sync['complete']=bool(row[0] and row[0]==sync['synced_sessions'])
    if not hasattr(ctx,'cache_sources'):ctx.cache_sources=[]
    metadata={'name':path.name,**sync,'contact_names':len(names),'read_messages':0}
    ctx.cache_sources.append(metadata)
    for peer,seq,local_id,created,kind,search_text,data in conn.execute(
            'SELECT username,seq,local_id,create_time,kind,search_text,data FROM msg_rows ORDER BY username,seq,local_id'):
        if getattr(opts,'cancelled',lambda:False)(): raise ValueError('已停止读取')
        source_rows+=1
        if out.total>=opts.max_rows:
            ctx.note('原导出器缓存达到读取条数上限');break
        try:
            if not isinstance(data,(str,bytes)) or len(data)>16*1024*1024: raise ValueError()
            item=json.loads(data)
            if not isinstance(item,dict): raise ValueError()
        except (ValueError,UnicodeError):
            invalid+=1;continue
        peer=str(peer or '').strip()
        if not peer:
            invalid+=1;continue
        kind=str(item.get('kind') or kind or 'text')
        value=item.get('text') or search_text or ''
        content=clean_text(str(value),opts.keep_markup)
        if not content and kind!='text':content='['+{'image':'图片','voice':'语音','video':'视频','file':'文件','emoji':'表情','quote':'引用'}.get(kind,kind)+']'
        if len(content)<opts.min_len:continue
        self_value=item.get('isSend')
        is_self=self_value is True or self_value==1 or str(self_value).lower()=='true'
        sender_uid=str(item.get('senderUsername') or '')
        sender='我' if is_self else str(names.get(sender_uid) or item.get('senderDisplay') or sender_uid or names.get(peer) or peer)
        ts_raw=str(item.get('createTime') or created or '')
        mtype=item.get('localType')
        if mtype is None:mtype={'text':1,'image':3,'voice':34,'video':43,'emoji':47,'file':49,'quote':49}.get(kind,kind)
        extra={'conversation_id':peer,'type':mtype,'cache_sequence':seq,'local_id':local_id,'wechat_native':True}
        if getattr(opts,'cache_account',''):extra['account_id']=opts.cache_account
        # localId is not a globally unique server ID. Keep it only as provenance.
        meta=_media_meta(str(value),mtype,'',local_id,item.get('createTime') or created)
        if meta:extra['media']=meta
        out.append(Message(source=str(path),conversation=names.get(peer,peer),sender=sender,ts=parse_ts(ts_raw),
                           ts_raw=ts_raw,content=content,is_self=is_self,kind='sqlite',table='msg_rows',extra=extra))
    out.flush()
    metadata['read_messages']=out.total
    metadata['source_rows']=source_rows
    if invalid or (sync['expected_messages'] is not None and sync['expected_messages']>source_rows):metadata['complete']=False
    ctx.note(f'读取原导出器 msg_rows 本地缓存：{out.total:,} 条；恢复 {len(names):,} 个已保存的联系人名称，其余保留会话 ID')
    if not metadata['complete']:ctx.note('原导出器缓存尚未确认全部同步完成，本次只能读取缓存中实际包含的记录')
    if invalid:ctx.error(f'原导出器缓存有 {invalid} 条记录不可读，未标记为完整缓存')
    return out
