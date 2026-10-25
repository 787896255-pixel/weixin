"""自动复用所选目录中的访问材料；Windows 上有界读取本人微信进程。

仅返回逐库校验成功的材料，不输出密钥，不写入源目录，不修改微信进程。
不同微信版本的密钥布局不同，无法取得时明确报告未读取。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .sqlcipher import match_database

KEY_NAMES={'keys.json','key.json','db_keys.json','all_keys.json','decrypt_keys.json',
           'password.txt','key.txt','key_windows.txt','config.ini','chatlog.json'}


def material_from_folder(root):
    result=[]
    def add(value):
        if isinstance(value,str) and value.strip() and len(value)<1024:
            value=value.strip()
            if re.fullmatch('[a-fA-F0-9]{64}(?:[a-fA-F0-9]{32})?',value):value='0x'+value
            if value not in result:result.append(value)
        elif isinstance(value,dict):
            for k,v in value.items():
                if isinstance(v,(dict,list)) or str(k).lower() in ('key','keys','password','enc_key','db_key','raw_key','data_key','datakey') or str(k).endswith(('.db','.sqlite')):
                    add(v)
        elif isinstance(value,list):
            for v in value[:512]:add(v)
    root=Path(root)
    if root.is_file():root=root.parent
    for directory,dirs,files in os.walk(root):
        depth=len(Path(directory).relative_to(root).parts)
        dirs[:]=[d for d in dirs if depth<5 and d not in ('FileStorage','attach','media','Image','Video','.git','node_modules')]
        for name in files:
            if name.lower() not in KEY_NAMES:continue
            path=Path(directory)/name
            if path.is_symlink() or path.stat().st_size>1024*1024:continue
            try:
                text=path.read_text(encoding='utf-8-sig')
                if name.endswith('.json'):add(json.loads(text))
                elif name.lower()=='config.ini':
                    for line in text.splitlines():
                        if '=' in line and line.split('=',1)[0].strip().lower() in ('key','enc_key','password','db_key'):
                            add(line.split('=',1)[1])
                else:add(text)
            except (OSError,ValueError,UnicodeError):pass
        if len(result)>512:break
    return result[:512]


def account_material(root, databases, progress=lambda message: None, cancelled=lambda: False, diagnostics=None):
    """Reuse an exact account's locally saved candidates, after DB authentication.

    REA loadKeys/keyStorePath and the observed local JSON establish this format.
    This reads only the selected account's file. Candidates never leave memory;
    the report contains counts and the storage type, never key values.
    """
    if os.name != 'nt' or not os.environ.get('LOCALAPPDATA'):
        return []
    accounts=set()
    for path in [Path(root), *(Path(p) for p in databases)]:
        for part in path.parts:
            if re.fullmatch(r'wxid_[A-Za-z0-9_-]{1,120}', part):
                accounts.add(part)
    if not accounts:
        return []
    directory=Path(os.environ['LOCALAPPDATA'])/'wechat-exporter'/'keys'
    result=[];files=0;rejected=0
    for account in sorted(accounts):
        path=directory/(account+'.json')
        try:
            if not path.is_file() or path.is_symlink() or path.stat().st_size>1024*1024:
                continue
            data=json.loads(path.read_text(encoding='utf-8-sig'))
            values=data.get('keys',[]) if isinstance(data,dict) else []
            if not isinstance(values,list):continue
            files+=1
            progress('正在校验当前账号已保存的读取连接')
            for value in values[:512]:
                if cancelled():break
                if not isinstance(value,str):continue
                value=value.strip().removeprefix('0x').removeprefix('0X')
                if not re.fullmatch(r'[a-fA-F0-9]{64}(?:[a-fA-F0-9]{32})?',value):continue
                candidate='0x'+value
                if candidate in result:continue
                valid=False
                for database in databases:
                    if cancelled():break
                    # A selected parent can contain more than one account.
                    if account not in Path(database).parts:continue
                    if matches(database,candidate):valid=True;break
                if valid:result.append(candidate)
                else:rejected+=1
        except (OSError,ValueError,UnicodeError):
            continue
    if diagnostics is not None and files:
        diagnostics['local_account_material']={'files':files,'verified_candidates':len(result),
            'rejected_candidates':rejected,'storage':'account-bound-local-json'}
    return result


def matches(path, material):
    """Authenticate the chosen database; accepts process secrets and derived keys."""
    return match_database(path, material)


def acquire_windows_keys(databases, progress, cancelled=lambda: False, seconds=45, diagnostics=None):
    from .windows_reader import acquire_keys
    return acquire_keys(databases, progress, cancelled, seconds, diagnostics)
