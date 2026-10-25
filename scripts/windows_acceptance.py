"""在 Windows 便携运行时中执行的合成数据验收，不读取用户微信。"""
import json
import platform
import sys
import tempfile
from pathlib import Path

root=Path(sys.argv[1])
sys.path.insert(0,str(root/'app'))
import webview
import Crypto
import psutil
import zstandard
import lz4
from wechatbrain.store import Store
from wechatbrain.importer import Importer
from wechatbrain.exporter import export_markdown

with tempfile.TemporaryDirectory(prefix='wechatbrain_acceptance_') as work:
    store=Store(Path(work)/'state')
    sources=[root/'app/fixtures'/name for name in ('win3','win4','ios_backup')]
    first=Importer(store,lambda x:None).run([str(p) for p in sources],owner='Windows 合成验收账号')
    stats=store.stats()
    assert stats['messages']==28,stats
    assert stats['conversations']==8,stats
    names={c['name'] for c in store.conversations()}
    assert {'赵六-供应商','家人群','上线群'}<=names,names
    second=Importer(store,lambda x:None).run([str(p) for p in sources],owner='Windows 合成验收账号')
    assert store.stats()['messages']==28
    assert sum(r['duplicates'] for r in second['sources'])==28
    vault=Path(work)/'vault';vault.mkdir()
    result=export_markdown(store,vault)
    assert result['written']==20,result
    assert Path(result['index']).is_file()
    print(json.dumps({'platform':platform.platform(),'python':sys.version,'dependencies':True,
        'messages':28,'conversations':8,'reimport_duplicates':28,'markdown_files':result['written'],
        'sources_unread':[r['errors'] for r in first['sources'] if r['errors']],
        'scope':'synthetic Windows 3.x/4.x and iPhone backup; no personal data'},ensure_ascii=False))
