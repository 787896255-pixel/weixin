"""Select native account databases without walking attachment/document trees."""
from __future__ import annotations

import re
from pathlib import Path

CONTAINERS={'wechat files','xwechat_files'}
SKIP={'filestorage','attach','msgattach','image','video','file','cache','backup','all users','all_users',
      'source-cache','.build-cache','node_modules','.git'}


def account_layout(path):
    if (path/'db_storage').is_dir():return 'Windows 微信 4.x'
    if (path/'Msg').is_dir():return 'Windows 微信 3.x'
    return ''


def native_plan(root, cancelled=lambda:False):
    root=Path(root)
    result={'recognized':False,'accounts':[],'databases':[],'errors':[],
            'mode':'documents','deferred_media':True}
    if root.is_file():return result
    queue=[(root,0)]
    while queue and not cancelled():
        path,depth=queue.pop(0)
        layout=account_layout(path)
        if layout:
            result['accounts'].append({'path':str(path),'account':path.name,'layout':layout})
            continue
        if depth>=2 or path.name.casefold() in SKIP:continue
        try:
            children=[]
            for index,child in enumerate(path.iterdir()):
                if index>=4096:
                    result['errors'].append('账号目录项目过多，部分目录未检查：'+str(path));break
                if child.is_dir() and not child.is_symlink() and child.name.casefold() not in SKIP:
                    children.append((child,depth+1))
            queue.extend(children)
        except OSError:result['errors'].append('账号目录无法访问：'+str(path))
    result['recognized']=bool(result['accounts'] or root.name.casefold() in CONTAINERS)
    if not result['recognized']:return result
    result['mode']='wechat-native'
    for account in result['accounts']:
        path=Path(account['path'])
        if '4.x' in account['layout']:
            base=path/'db_storage'
            for relative in ('contact/contact.db','session/session.db','head_image/head_image.db'):
                candidate=base/relative
                if candidate.is_file() and not candidate.is_symlink():result['databases'].append(candidate)
            folder=base/'message'
            if folder.is_dir():
                try:
                    result['databases'].extend(p for p in folder.iterdir() if p.is_file() and not p.is_symlink()
                        and re.fullmatch(r'(?:biz_)?message_\d+\.db',p.name,re.I))
                except OSError:result['errors'].append('消息分库目录无法读取：'+str(folder))
        else:
            base=path/'Msg'
            for folder in (base,base/'Multi',base/'Contact'):
                if not folder.is_dir():continue
                try:
                    result['databases'].extend(p for p in folder.iterdir() if p.is_file() and not p.is_symlink()
                        and (re.fullmatch(r'MSG\d*\.db',p.name,re.I)
                             or p.name.casefold() in ('micromsg.db','contact.db','headimage.db','head_image.db','misc.db')))
                except OSError:result['errors'].append('微信数据库目录无法读取：'+str(folder))
    result['databases']=sorted(set(result['databases']),key=lambda p:(
        0 if p.name.casefold() in ('micromsg.db','contact.db') else 1 if p.name.casefold()=='session.db' else 2,str(p)))
    if not result['databases']:
        result['errors'].append('没有找到实际的微信账号数据库；请选择含 Msg 或 db_storage 的账号目录。')
    return result
