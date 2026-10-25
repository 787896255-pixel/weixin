"""共享的聊天筛选；浏览、手动导出和计划导出使用相同条件。"""
from datetime import datetime, timedelta

TYPES = ('文本','图片','语音','视频','表情','链接/文件','转账/红包','位置','名片','通话','系统','其他')


def message_type(value, content=''):
    text=str(value or '')
    codes={'1':'文本','3':'图片','34':'语音','42':'名片','43':'视频','47':'表情',
           '48':'位置','49':'链接/文件','50':'通话','62':'视频','10000':'系统','10002':'系统'}
    result=codes.get(text,text)
    if result=='小视频':result='视频'
    if result=='链接/文件' and any(s in content for s in ('<wcpayinfo','<type>2000</type>','<type>2001</type>')):
        return '转账/红包'
    if result in TYPES:return result
    for label in TYPES[:-1]:
        if str(content).startswith('['+label+']'):return label
    return '文本' if result in ('','text','txt','sqlite','json','jsonl','csv','html') else '其他'


def normalize_filters(values=None):
    values=values or {}
    if not isinstance(values,dict):raise ValueError('筛选条件格式错误')
    result={'q':str(values.get('q','')).strip()[:1000]}
    for key in ('since','until'):
        text=str(values.get(key,'')).strip().replace('T',' ')
        if text:
            try:
                parsed=datetime.fromisoformat(text)
                if parsed.tzinfo:raise ValueError()
                if key=='until' and len(text)==10:parsed+=timedelta(days=1,microseconds=-1)
                text=parsed.isoformat(sep=' ',timespec='microseconds')
            except ValueError:raise ValueError('时间格式应为 YYYY-MM-DD 或本地日期时间')
        result[key]=text
    if result['since'] and result['until'] and result['since']>result['until']:
        raise ValueError('开始日期不能晚于结束日期')
    raw=values.get('types',[])
    if isinstance(raw,str):raw=raw.split(',') if raw else []
    if not isinstance(raw,list) or any(x not in TYPES for x in raw):raise ValueError('消息类型不正确')
    result['types']=list(dict.fromkeys(raw))
    direction=str(values.get('direction',''))
    if direction not in ('','self','other'):raise ValueError('发送方向不正确')
    result['direction']=direction
    ids=values.get('message_ids',[])
    if not isinstance(ids,list) or len(ids)>10000 or any(not isinstance(x,str) for x in ids):
        raise ValueError('最多选择 10000 条具体消息；更多记录请按筛选条件导出')
    result['message_ids']=ids
    return result


def predicate(cid, filters=None, after_seq=0, through_seq=None):
    f=normalize_filters(filters);parts=['m.conversation_id=?'];args=[cid]
    if f['q']:
        pattern='%'+f['q'].replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
        parts.append("(m.content LIKE ? ESCAPE '\\' OR m.sender LIKE ? ESCAPE '\\')");args.extend([pattern,pattern])
    if f['since']:parts.append('m.time>=?');args.append(f['since'].split('.')[0])
    if f['until']:parts.append('m.time<=?');args.append(f['until'])
    if f['types']:
        parts.append('jk_type(m.type,m.content) IN ('+','.join('?' for _ in f['types'])+')');args.extend(f['types'])
    if f['direction']:parts.append('m.is_self=?');args.append(int(f['direction']=='self'))
    if f['message_ids']:
        # json_each avoids SQLite's platform-dependent bind-parameter limit.
        import json
        parts.append('m.id IN (SELECT value FROM json_each(?))');args.append(json.dumps(f['message_ids']))
    if after_seq:parts.append('s.seq>?');args.append(int(after_seq))
    if through_seq is not None:parts.append('s.seq<=?');args.append(int(through_seq))
    return ' AND '.join(parts),args
