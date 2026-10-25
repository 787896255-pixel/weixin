"""Watch selected Windows account DB/WAL metadata and append committed messages."""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime
from pathlib import Path

from .importer import Importer
from .loading import fingerprint
from .source_plan import native_plan


class LiveSync:
    def __init__(self,app):
        self.app=app;self.stop=threading.Event();self.lock=threading.RLock()
        self.states={};self.stamps={};self.retry_after={};self.interval=3

    def _source(self,sid=''):
        sid=sid or self.app.store.settings().get('active_source','')
        with self.app.store.connect() as db:
            row=db.execute('SELECT id,path,status FROM sources WHERE id=?',(sid,)).fetchone()
        if not row:raise ValueError('先选择并读取一个本地微信账号')
        return dict(row)

    def _inventory(self,source):
        plan=native_plan(source['path'],self.stop.is_set)
        if not plan['recognized']:raise ValueError('实时同步需要选择实际的微信账号目录或微信总目录')
        if plan['errors']:raise ValueError(plan['errors'][0])
        return {str(path):fingerprint(path,database=True) for path in plan['databases']}

    def state(self,sid=''):
        try:source=self._source(sid)
        except ValueError:return {'enabled':False,'running':False,'source_id':sid,'revision':0,'interval_seconds':self.interval}
        sid=source['id'];configured=self.app.store.settings().get('live_sources') or {}
        enabled=bool(configured.get(sid,os.name=='nt' and source['status'] in ('ready','partial','reading')))
        with self.lock:
            value=self.states.setdefault(sid,{'source_id':sid,'running':False,'revision':0,
                'last_sync':'','new_messages':0,'changed':False,'error':''})
            return dict(value,enabled=enabled,interval_seconds=self.interval)

    def configure(self,sid,enabled):
        source=self._source(sid)
        if enabled:self._inventory(source)
        values=self.app.store.settings().get('live_sources') or {};values[source['id']]=bool(enabled)
        self.app.store.save_settings({'live_sources':values})
        if enabled:
            with self.lock:
                self.stamps.pop(source['id'],None);self.retry_after.pop(source['id'],None)
        else:
            with self.app.lock:
                if self.app.job and self.app.job['status']=='running' and self.app.job['kind']=='live-sync':
                    self.app.cancel.set()
        return self.state(source['id'])

    def refresh(self,sid='',conversation_id='',mode='incremental',background=False,changed_files=None,observed=None):
        if mode not in ('full','incremental'):raise ValueError('刷新方式不正确')
        source=self._source(sid);sid=source['id']
        inventory=observed if observed is not None else self._inventory(source)
        peer=None
        if conversation_id:
            peer=self.app.store.conversation(conversation_id)
            with self.app.store.connect() as db:
                belongs=db.execute('''SELECT 1 FROM conversation_sources WHERE conversation_id=? AND source_id=?
                    UNION SELECT 1 FROM messages m JOIN provenance p ON p.message_id=m.id
                    WHERE m.conversation_id=? AND p.source_id=? LIMIT 1''',
                    (conversation_id,sid,conversation_id,sid)).fetchone()
            if not belongs:raise ValueError('当前会话不属于所选微信数据源')
        def execute():
            with self.lock:
                self.states.setdefault(sid,self.state(sid)).update(running=True,error='')
            try:
                settings=self.app.store.settings()
                result=Importer(self.app.store,self.app.progress,self.app.cancel.is_set,self.app.reader_keys,
                    incremental=mode=='incremental',force=mode=='full',
                    peers=[peer['peer_id']] if peer else None,
                    files_filter=changed_files if background else None).run([source['path']],owner=settings.get('owner',''))
                reports=result['sources'];new=sum(r['imported'] for r in reports)
                errors=[message for r in reports for message in r['errors']]
                with self.lock:
                    value=self.states[sid];value.update(running=False,new_messages=new,changed=bool(new),
                        last_sync=datetime.now().isoformat(timespec='seconds'),error=errors[0] if errors else '')
                    if new or mode=='full' or any(r['files'] for r in reports):value['revision']+=1
                    # Keep the boundary observed BEFORE reading. Frames arriving
                    # during this pass must trigger the next pass, not be skipped.
                    if not self.app.cancel.is_set() and not peer and not errors:
                        self.stamps[sid]=inventory;self.retry_after.pop(sid,None)
                    elif errors:self.retry_after[sid]=time.monotonic()+30
                result['refresh_mode']=mode;result['new_messages']=new
                return result
            except Exception as exc:
                with self.lock:
                    self.states[sid].update(running=False,error=str(exc) if isinstance(exc,(ValueError,OSError)) else '实时读取未完成')
                    self.retry_after[sid]=time.monotonic()+30
                raise
        return self.app.start_job('live-sync' if background else 'chat-refresh',execute)

    def loop(self):
        while not self.stop.wait(self.interval):
            settings=self.app.store.settings();sid=settings.get('active_source','')
            if not sid or not self.state(sid)['enabled']:continue
            with self.lock:
                if time.monotonic()<self.retry_after.get(sid,0):continue
            with self.app.lock:
                if self.app.job and self.app.job['status']=='running':continue
            try:
                source=self._source(sid);inventory=self._inventory(source)
                with self.lock:previous=self.stamps.get(sid)
                changed=[name for name,stamp in inventory.items() if previous is None or previous.get(name)!=stamp]
                if changed:self.refresh(sid,mode='incremental',background=True,changed_files=changed,observed=inventory)
            except ValueError as exc:
                with self.lock:
                    self.states.setdefault(sid,{'source_id':sid,'revision':0}).update(error=str(exc),running=False)
                    self.retry_after[sid]=time.monotonic()+30
            except OSError:
                with self.lock:
                    self.states.setdefault(sid,{'source_id':sid,'revision':0}).update(error='微信数据目录暂不可访问',running=False)
                    self.retry_after[sid]=time.monotonic()+30
            except Exception:
                with self.lock:
                    self.states.setdefault(sid,{'source_id':sid,'revision':0}).update(error='实时同步未完成，保留已读记录，请查看读取报告',running=False)
                    self.retry_after[sid]=time.monotonic()+30

    def start(self):threading.Thread(target=self.loop,daemon=True,name='jingke-live-sync').start()
