"""仅在应用运行期间触发；与手动操作共用串行任务队列。"""
import json
import threading
import uuid
from datetime import datetime,timedelta
from pathlib import Path
from .archive import export_archive,valid_options


class Scheduler:
    def __init__(self,application):
        self.app=application;self.stop=threading.Event()

    def list(self):
        with self.app.store.connect() as db:
            return [dict(dict(r),config=json.loads(r['config'])) for r in db.execute('SELECT * FROM schedules ORDER BY next_run')]

    def save(self,data):
        if not isinstance(data,dict):raise ValueError('计划格式错误')
        name=str(data.get('name','定时导出')).strip()[:100] or '定时导出'
        repeat=str(data.get('repeat','once'))
        if repeat not in ('once','daily','weekly'):raise ValueError('重复方式不正确')
        try:
            when=datetime.fromisoformat(str(data['next_run']))
            if when.tzinfo:raise ValueError()
        except (ValueError,KeyError):raise ValueError('请选择本机日期和时间')
        ids=data.get('ids')
        if not isinstance(ids,list) or not ids or any(not isinstance(i,str) for i in ids):raise ValueError('计划至少选择一个会话')
        for cid in ids:self.app.store.conversation(cid)
        folder=str(data.get('folder','')).strip()
        if not folder or not Path(folder).is_dir():raise ValueError('计划需要存在的导出文件夹')
        options=valid_options(data.get('options',{}))
        if options['filters']['message_ids']:raise ValueError('定时导出请使用筛选条件，不使用临时勾选消息')
        config={'repeat':repeat,'ids':ids,'folder':str(Path(folder).resolve()),'options':options,
                'incremental':bool(data.get('incremental',True))}
        sid=str(data.get('id') or uuid.uuid4().hex)
        if len(sid)>80:raise ValueError('计划标识错误')
        with self.app.store.connect() as db:
            # Changing participants/filters must not skip previously excluded messages.
            watermark=0
            db.execute('INSERT OR REPLACE INTO schedules VALUES(?,?,?,?,?,?,?)',
                (sid,name,json.dumps(config,ensure_ascii=False),int(data.get('enabled',True)),when.isoformat(timespec='seconds'),watermark,''))
        return {'id':sid,'next_run':when.isoformat(timespec='seconds')}

    def enable(self,sid,enabled):
        with self.app.store.connect() as db:
            if not db.execute('UPDATE schedules SET enabled=? WHERE id=?',(int(enabled),sid)).rowcount:raise ValueError('计划不存在')

    def run(self,sid):
        with self.app.store.connect() as db:row=db.execute('SELECT * FROM schedules WHERE id=?',(sid,)).fetchone()
        if not row:raise ValueError('计划不存在')
        config=json.loads(row['config'])
        def execute():
            try:
                result=export_archive(self.app.store,config['folder'],config['ids'],config['options'],
                    self.app.progress,self.app.cancel.is_set,row['watermark'] if config['incremental'] else 0)
            except Exception:
                with self.app.store.connect() as db:
                    db.execute("UPDATE schedules SET enabled=0,last_result='执行失败，请检查导出目录和配置后重新启用' WHERE id=? AND config=?",(sid,row['config']))
                raise
            current=datetime.now();next_time=datetime.fromisoformat(row['next_run'])
            repeat=config['repeat'];enabled=row['enabled']
            if repeat=='once':enabled=0
            else:
                step=timedelta(days=1 if repeat=='daily' else 7)
                while next_time<=current:next_time+=step
            status=('完成：'+str(result['messages'])+' 条' if result['status']=='done' else '未完成：'+result['status'])
            with self.app.store.connect() as db:
                # A concurrent edit wins over completion of an older configuration.
                db.execute('UPDATE schedules SET next_run=?,watermark=?,last_result=?,enabled=CASE WHEN enabled=0 THEN 0 ELSE ? END WHERE id=? AND config=?',
                    (next_time.isoformat(timespec='seconds'),result['watermark'],status,enabled,sid,row['config']))
            result['schedule_name']=row['name'];return result
        return self.app.start_job('archive',execute)

    def loop(self):
        while not self.stop.wait(10):
            row=None
            try:
                with self.app.lock:
                    if self.app.job and self.app.job['status']=='running':continue
                now=datetime.now().isoformat(timespec='seconds')
                with self.app.store.connect() as db:
                    row=db.execute('SELECT id FROM schedules WHERE enabled=1 AND next_run<=? ORDER BY next_run LIMIT 1',(now,)).fetchone()
                if row:self.run(row['id'])
            except Exception:
                # An invalid folder/task must not be retried every ten seconds forever.
                if row:
                    with self.app.store.connect() as db:
                        db.execute("UPDATE schedules SET enabled=0,last_result='启动失败，请检查目录和设置后重新启用' WHERE id=?",(row['id'],))

    def start(self):threading.Thread(target=self.loop,daemon=True,name='jingke-schedule').start()
