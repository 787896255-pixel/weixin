"""独立实现的聊天导出；不调用或分发第三方商业程序。"""
from __future__ import annotations
import csv
import html
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from .exporter import filename
from .query import normalize_filters, predicate
from .media import MediaLibrary

FORMATS=('html','txt','xlsx','csv','json','jsonl','md','pdf')
HEADERS=('时间','我的账号','会话','对方标识','发送者','方向','消息类型','内容','消息编号','来源表','附件路径与状态')
CSS='''body{font:14px "Microsoft YaHei","Segoe UI",sans-serif;background:#f3f5f8;color:#26334a;margin:0}header{padding:28px;background:white;border-bottom:1px solid #ddd}main{max-width:1000px;margin:auto;padding:24px}.msg{margin:16px 0;break-inside:avoid}.meta{color:#68758a;font-size:12px;margin-bottom:6px}.body{white-space:pre-wrap;overflow-wrap:anywhere;background:white;border:1px solid #e2e6ec;padding:14px;border-radius:8px}.self .body{background:#e6f2ed}.id{font-size:10px;color:#7c8798}button{padding:8px 18px;cursor:pointer}@page{size:A4;margin:15mm}@media print{body{background:white}main{padding:0}button{display:none}.body{box-shadow:none}header{padding:0 0 15px}}'''


def valid_options(values):
    if not isinstance(values,dict):raise ValueError('导出设置格式错误')
    fmt=str(values.get('format','html')).lower()
    if fmt not in FORMATS:raise ValueError('不支持的导出格式')
    try:split=int(values.get('split',5000))
    except (ValueError,TypeError):raise ValueError('每份消息数必须是整数')
    if not 1<=split<=100000:raise ValueError('每份消息数应在 1 至 100000 之间')
    return {'format':fmt,'split':split,'filters':normalize_filters(values.get('filters'))}


def cell(value):
    # Export all cells as strings, including formula-looking messages.
    return '' if value is None else str(value)


def xmltext(value):
    return html.escape(re.sub('[\x00-\x08\x0b\x0c\x0e-\x1f]','',cell(value)),quote=True)


def xlsx(path, rows):
    """写标准 OOXML，不执行公式；长文本分段保存，避免 Excel 单元格截断。"""
    with zipfile.ZipFile(path,'w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml','''<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>''')
        z.writestr('_rels/.rels','''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>''')
        z.writestr('xl/workbook.xml','''<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="记录" sheetId="1" r:id="rId1"/></sheets></workbook>''')
        z.writestr('xl/_rels/workbook.xml.rels','''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>''')
        with z.open('xl/worksheets/sheet1.xml','w') as f:
            f.write(b'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" state="frozen"/></sheetView></sheetViews><cols><col min="1" max="20" width="24" customWidth="1"/></cols><sheetData>')
            n=0
            for row in rows:
                chunks=[]
                for v in row:
                    text=cell(v);parts=[];part=[];units=0
                    # Excel counts UTF-16 code units. Keep astral characters intact.
                    for ch in text:
                        cost=2 if ord(ch)>65535 else 1
                        if units+cost>30000:parts.append(''.join(part));part=[];units=0
                        part.append(ch);units+=cost
                    parts.append(''.join(part));chunks.append(parts)
                for index in range(max(map(len,chunks),default=1)):
                    n+=1
                    if n>1048576:raise ValueError('Excel 超过单表行数，请减少每份消息数')
                    values=[parts[index] if index<len(parts) else '' for parts in chunks]
                    cells=''.join('<c t="inlineStr"><is><t xml:space="preserve">'+xmltext(v)+'</t></is></c>' for v in values)
                    f.write(('<row r="'+str(n)+'">'+cells+'</row>').encode('utf-8'))
            f.write(b'</sheetData></worksheet>')


def table_rows(person,messages):
    yield HEADERS
    for m in messages:
        yield (m['time'],person['account'],person['name'],person['peer_id'],m['sender'],
               '我发送' if m['is_self'] else '对方发送',m['message_type'],m['content'],m['id'],m['source_table'],
               '\n'.join(a.get('url') or a.get('error') or '附件尚未读取' for a in m.get('attachments',[])))


def html_document(person,messages):
    esc=lambda s:html.escape(str(s or ''),quote=True)
    yield '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; script-src \'none\'; img-src \'self\' data: file:; media-src \'self\' file:;"><title>'+esc(person['name'])+'</title><style>'+CSS+'img,video{max-width:100%;max-height:520px}audio{max-width:100%}</style><header><h1>'+esc(person['name'])+'</h1><p>京客导出助手 · 我的账号：'+esc(person['account'])+' · 对方标识：'+esc(person['peer_id'])+'</p><p>附件保存在旁边的 media 文件夹，请与此文件一起保留。</p></header><main>'
    for m in messages:
        yield '<article class="msg '+('self' if m['is_self'] else '')+'"><div class="meta">'+esc(m['time'])+' · '+esc(m['sender'])+' · '+esc(m['message_type'])+'</div><div class="body">'+esc(m['content'])+'</div><div class="id">'+esc(m['id'])+'</div>'
        for asset in m.get('attachments',[]):
            url=asset.get('url')
            if not url:yield '<p>'+esc(asset.get('error') or '附件未读出')+'</p>';continue
            url=esc(url)
            if asset['kind']=='image':yield '<p><img src="'+url+'" alt="'+esc(asset['name'])+'"></p>'
            elif asset['kind'] in ('audio','video'):
                tag=asset['kind'];yield '<p><'+tag+' controls preload="none" src="'+url+'"></'+tag+'></p>'
            yield '<p><a href="'+url+'" download>'+esc(asset['name'])+'</a></p>'
        yield '</article>'
    yield '</main></html>'


def find_pdf_browser():
    # A standard installed Edge/Chrome is sufficient; no commercial bundled renderer.
    choices=[]
    if os.name=='nt':
        for env in ('PROGRAMFILES(X86)','PROGRAMFILES','LOCALAPPDATA'):
            root=os.environ.get(env,'')
            if root:choices.extend([Path(root)/'Microsoft/Edge/Application/msedge.exe',Path(root)/'Google/Chrome/Application/chrome.exe'])
    elif __import__('sys').platform=='darwin':
        choices=[Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')]
    else:choices=[Path(p) for name in ('chromium','chromium-browser','google-chrome') if (p:=shutil.which(name))]
    return next((p for p in choices if p.is_file()),None)


def render_pdf(html_path,pdf_path):
    executable=find_pdf_browser()
    if not executable:raise ValueError('生成 PDF 需要已安装的 Microsoft Edge 或 Chrome；也可导出 HTML 后用浏览器打印为 PDF')
    with tempfile.TemporaryDirectory(prefix='jingke-pdf-') as profile:
        result=subprocess.run([str(executable),'--headless','--disable-gpu','--no-first-run',
            '--disable-extensions','--disable-background-networking','--user-data-dir='+profile,
            '--no-pdf-header-footer','--print-to-pdf='+str(pdf_path),html_path.as_uri()],
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=180,
            creationflags=0x08000000 if os.name=='nt' else 0)
    if result.returncode or not pdf_path.is_file() or pdf_path.stat().st_size<100:
        raise ValueError('PDF 渲染失败，已保留 HTML 源文件，可用浏览器打印')


def write_part(path,person,messages,fmt):
    if fmt=='xlsx':xlsx(path,table_rows(person,messages));return
    if fmt=='csv':
        with path.open('w',encoding='utf-8-sig',newline='') as f:
            writer=csv.writer(f)
            for row in table_rows(person,messages):
                writer.writerow([("'"+cell(v)) if cell(v).lstrip().startswith(('=','+','-','@')) else cell(v) for v in row])
        return
    if fmt=='pdf':
        source=path.with_suffix('.html');write_part(source,person,messages,'html');render_pdf(source,path);return
    with path.open('w',encoding='utf-8-sig' if fmt=='txt' else 'utf-8') as f:
        if fmt=='html':f.writelines(html_document(person,messages))
        elif fmt=='json':json.dump({'schema':'jingke-chat-export/v1','conversation':person,'messages':messages},f,ensure_ascii=False,indent=2)
        elif fmt=='jsonl':
            for m in messages:f.write(json.dumps(dict(m,account=person['account'],peer_id=person['peer_id'],conversation=person['name']),ensure_ascii=False)+'\n')
        else:
            f.write(('# ' if fmt=='md' else '')+person['name']+' · 聊天记录\n\n我的账号：'+person['account']+'\n对方标识：'+person['peer_id']+'\n\n')
            for m in messages:
                f.write(('### ' if fmt=='md' else '')+m['time']+' · '+m['sender']+' · '+m['message_type']+'\n\n'+m['content']+'\n\n消息编号：'+m['id']+'\n\n')
                for asset in m.get('attachments',[]):
                    url=asset.get('url')
                    if url and fmt=='md':f.write(('!' if asset['kind']=='image' else '')+'[附件]('+url+')\n\n')
                    else:f.write('附件：'+(url or asset.get('error') or '未读出')+'\n\n')


def export_archive(store,folder,ids=None,options=None,progress=lambda s:None,cancelled=lambda:False,after_seq=0):
    opts=valid_options(options or {})
    if not str(folder).strip():raise ValueError('先选择聊天导出文件夹')
    root=Path(folder).expanduser().resolve()
    if not root.is_dir():raise ValueError('导出文件夹不存在')
    people=store.conversations()
    if ids is not None:
        if not isinstance(ids,list) or any(not isinstance(x,str) for x in ids):raise ValueError('会话选择格式错误')
        wanted=set(ids);people=[p for p in people if p['id'] in wanted]
    if not people:raise ValueError('请选择至少一个已读取的会话')
    run_id=uuid.uuid4().hex;stamp=datetime.now().strftime('%Y%m%d-%H%M%S')
    base=root/('京客导出-'+stamp+'-'+run_id[:6]);base.mkdir()
    high=store.max_sequence();result={'id':run_id,'folder':str(base),'format':opts['format'],'people':0,
        'messages':0,'files':[],'errors':[],'cancelled':False,'watermark':int(after_seq),'options':opts}
    for i,p in enumerate(people,1):
        if cancelled():break
        progress(f'正在导出 {i}/{len(people)}：{p["name"]}')
        person=store.conversation(p['id']);where,args=predicate(p['id'],opts['filters'],after_seq,high)
        part=0;count=0
        try:
            with store.connect() as db:
                cursor=db.execute('SELECT m.*,s.seq,jk_type(m.type,m.content) AS message_type FROM messages m JOIN message_sequence s ON s.message_id=m.id WHERE '+where+' ORDER BY m.time,m.id',args)
                while not cancelled():
                    batch=cursor.fetchmany(opts['split'])
                    if not batch:break
                    part+=1;messages=MediaLibrary(store).export_messages([dict(m) for m in batch],base)
                    path=base/(filename(person['name'])+'-'+p['id'][:10]+f'-{part:04d}.'+opts['format'])
                    # Temporary paths prevent incomplete files being mistaken for successful exports.
                    tmp=path.with_name('.pending-'+path.name)
                    try:
                        write_part(tmp,person,messages,opts['format']);os.replace(tmp,path)
                        if opts['format']=='pdf':
                            source=tmp.with_suffix('.html')
                            if source.exists():os.replace(source,path.with_suffix('.html'))
                    except Exception:
                        tmp.unlink(missing_ok=True);raise
                    result['files'].append(str(path));result['messages']+=len(messages);count+=len(messages)
            if count:result['people']+=1
        except Exception as exc:
            result['errors'].append({'person':person['name'],'error':str(exc) if isinstance(exc,(ValueError,OSError)) else type(exc).__name__})
    result['cancelled']=bool(cancelled())
    if not result['errors'] and not result['cancelled']:result['watermark']=high
    result['status']='cancelled' if result['cancelled'] else 'partial' if result['errors'] else 'done'
    manifest=dict(result,created=datetime.now().isoformat(timespec='seconds'),
                  note='仅包含已成功读取的消息；缺失或未解码媒体保留占位；增量依据本地导入顺序，包含迟到的旧日期消息。')
    (base/'导出清单.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    with store.connect() as db:db.execute('INSERT INTO export_runs VALUES(?,?,?,?)',(run_id,manifest['created'],result['status'],json.dumps(result,ensure_ascii=False)))
    return result


def export_contacts(store,folder,ids=None,fmt='xlsx'):
    if fmt not in ('xlsx','csv','json'):raise ValueError('通讯录支持 Excel、CSV、JSON')
    root=Path(folder).expanduser().resolve()
    if not str(folder).strip() or not root.is_dir():raise ValueError('请选择存在的导出文件夹')
    people=store.contacts(ids)
    if not people:raise ValueError('当前没有可导出的联系人资料')
    rows=[('我的账号','名称','昵称','微信备注','微信号','微信标识','类型','本地备注','消息数','最早消息','最新消息')]
    rows.extend((p['account'],p['name'],p.get('nickname',''),p.get('remark',''),p.get('alias',''),p['peer_id'],'群聊' if p['is_group'] else '联系人',p['note'],p['message_count'],p['first_time'],p['last_time']) for p in people)
    path=root/('京客-通讯录-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:6]+'.'+fmt)
    if fmt=='xlsx':xlsx(path,rows)
    elif fmt=='json':path.write_text(json.dumps(people,ensure_ascii=False,indent=2),encoding='utf-8')
    else:
        with path.open('w',encoding='utf-8-sig',newline='') as f:
            writer=csv.writer(f);writer.writerows([["'"+cell(v) if cell(v).lstrip().startswith(('=','+','-','@')) else cell(v) for v in row] for row in rows])
    return {'folder':str(root),'files':[str(path)],'people':len(people),'messages':0,'scope':'所选本地记录中的联系人资料，含无消息的联系人；不代表云端全部好友或完整群成员','errors':[]}
