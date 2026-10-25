import hashlib
import json
import plistlib
import sqlite3
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path

from app import Application,handler_for
from wechatbrain.store import Store
from wechatbrain.importer import Importer
from wechatbrain.classify import AIClassifier
from wechatbrain.exporter import export_markdown
from wechatbrain.access import matches
import chatlog_platform as platform

ROOT=Path(__file__).resolve().parents[1]


class AppTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.store=Store(self.root/'state')
    def tearDown(self):self.tmp.cleanup()
    def run_import(self,paths,**kw):return Importer(self.store,lambda x:None).run([str(p) for p in paths],**kw)
    def sample(self,name,records):
        path=self.root/name;path.mkdir()
        (path/'chat.json').write_text(json.dumps(records,ensure_ascii=False),encoding='utf-8')
        return path

    def test_windows_and_nested_iphone_contacts(self):
        import shutil
        mixed=self.root/'mixed';mixed.mkdir()
        shutil.copytree(ROOT/'fixtures/ios_backup',mixed/'old'/'phone')
        self.run_import([ROOT/'fixtures/win3',ROOT/'fixtures/win4',mixed],owner='本人')
        self.assertEqual(self.store.stats()['messages'],28)
        names={c['name'] for c in self.store.conversations()}
        self.assertTrue({'家人群','赵六-供应商','上线群','周八'}<=names)
        for c in self.store.conversations():self.assertNotIn('会话 ',c['name'])

    def test_reimport_idempotent_and_repeated_messages_preserved(self):
        records=[{'conversation':'张三','sender':'我','isSend':1,'time':'2024-01-01 10:00:00','content':'收到'}]*2
        path=self.sample('repeat',records)
        self.run_import([path],owner='本人')
        self.run_import([path],owner='本人')
        self.assertEqual(self.store.stats()['messages'],2)

    def test_different_contacts_and_long_contents_not_collapsed(self):
        prefix='同一段文字'*100
        records=[{'conversation':name,'sender':'我','isSend':1,'time':'2024-01-01 10:00:00','content':content}
                 for name,content in [('甲',prefix+'甲'),('甲',prefix+'乙'),('乙',prefix+'甲')]]
        path=self.sample('different',records);self.run_import([path],owner='本人')
        self.assertEqual(self.store.stats()['messages'],3)
        self.assertEqual(self.store.stats()['conversations'],2)

    def test_same_display_name_different_windows_accounts(self):
        self.run_import([ROOT/'fixtures/win3'])
        import shutil
        other=self.root/'other'
        shutil.copytree(ROOT/'fixtures/win3/WeChat Files/wxid_win3demo',other/'WeChat Files'/'wxid_second')
        self.run_import([other])
        self.assertEqual(self.store.stats()['messages'],20)
        self.assertEqual(self.store.stats()['conversations'],6)

    def test_source_files_unchanged(self):
        source=ROOT/'fixtures/ios_backup'
        before={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in source.rglob('*') if p.is_file()}
        self.run_import([source])
        after={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in source.rglob('*') if p.is_file()}
        self.assertEqual(before,after)

    def test_wal_snapshot_reads_committed_messages(self):
        source=self.root/'wal';source.mkdir();db=sqlite3.connect(source/'chat.db')
        try:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE messages(sender TEXT,content TEXT,conversation TEXT,time TEXT)')
            db.execute('INSERT INTO messages VALUES(?,?,?,?)',('张三','项目今天交付','张三','2024-01-01 10:00:00'));db.commit()
            self.run_import([source],owner='本人')
            self.assertEqual(self.store.stats()['messages'],1)
        finally:db.close()

    def test_iphone_des_direction(self):
        conn=sqlite3.connect(':memory:')
        conn.execute('CREATE TABLE Chat_abcdef(CreateTime INTEGER,Message TEXT,Des INTEGER,Type INTEGER)')
        conn.executemany('INSERT INTO Chat_abcdef VALUES(?,?,?,?)',[(1700000000,'我说的话',0,1),(1700000001,'对方的话',1,1)])
        recs=platform.parse_wechat_db(conn,self.root/'MM.sqlite')
        self.assertTrue(recs[0].is_self);self.assertFalse(recs[1].is_self)
        conn.close()

    def test_modern_encrypted_iphone_keybag_and_uid_metadata(self):
        try:from Crypto.Cipher import AES
        except ImportError:self.skipTest('可选加速依赖未安装')
        import os
        source=self.root/'modern-phone';source.mkdir()
        password='synthetic-backup-password';salt=os.urandom(16);dpsl=os.urandom(20)
        intermediate=hashlib.pbkdf2_hmac('sha256',password.encode(),dpsl,1000,32)
        password_key=hashlib.pbkdf2_hmac('sha1',intermediate,salt,1000,32)
        class_key=os.urandom(32);manifest_key=os.urandom(32)
        def tag(name,value):
            if isinstance(value,int):value=value.to_bytes(4,'big')
            return name.encode()+len(value).to_bytes(4,'big')+value
        bag=b''.join(tag(k,v) for k,v in [('VERS',5),('TYPE',1),('UUID',os.urandom(16)),
            ('SALT',salt),('ITER',1000),('DPSL',dpsl),('DPIC',1000),('UUID',os.urandom(16)),
            ('CLAS',4),('WRAP',2),('KTYP',0),('WPKY',platform.aes_key_wrap(password_key,class_key))])
        manifest=source/'manifest-plain.db';db=sqlite3.connect(manifest)
        db.execute('CREATE TABLE Files(fileID TEXT,domain TEXT,relativePath TEXT,flags INTEGER,file BLOB)')
        for original in (ROOT/'fixtures/ios/DB').glob('*.sqlite'):
            data=original.read_bytes();key=os.urandom(32)
            rel='Documents/account-hash/DB/'+original.name
            fid=hashlib.sha1(rel.encode()).hexdigest();folder=source/fid[:2];folder.mkdir(exist_ok=True)
            padded=data+bytes((-len(data))%16)
            (folder/fid).write_bytes(AES.new(key,AES.MODE_CBC,bytes(16)).encrypt(padded))
            ek=(4).to_bytes(4,'little')+platform.aes_key_wrap(class_key,key)
            meta={'$objects':['$null',{'EncryptionKey':plistlib.UID(2),'ProtectionClass':4,'Size':len(data)},{'NS.data':ek}],
                  '$top':{'root':plistlib.UID(1)}}
            db.execute('INSERT INTO Files VALUES(?,?,?,?,?)',(fid,'AppDomain-com.tencent.xinWeChat',rel,1,plistlib.dumps(meta,fmt=plistlib.FMT_BINARY)))
        db.commit();db.close()
        data=manifest.read_bytes();manifest.unlink()
        (source/'Manifest.db').write_bytes(AES.new(manifest_key,AES.MODE_CBC,bytes(16)).encrypt(data))
        (source/'Manifest.plist').write_bytes(plistlib.dumps({'IsEncrypted':True,'BackupKeyBag':bag,
            'ManifestKey':(4).to_bytes(4,'little')+platform.aes_key_wrap(class_key,manifest_key)}))
        result=self.run_import([source],backup_password=password,owner='本人')
        self.assertEqual(self.store.stats()['messages'],10,result['sources'])
        self.assertIn('家人群',{c['name'] for c in self.store.conversations()})
        self.assertFalse((source/'.chatlog_manifest_plain.db').exists())

    def test_export_identity_categories_and_protected_edits(self):
        self.run_import([ROOT/'fixtures/win4'],owner='本人')
        vault=self.root/'vault';vault.mkdir()
        result=export_markdown(self.store,vault)
        self.assertEqual(result['conversations'],2)
        profile=next(p for p in (vault/'微信第二大脑').rglob('联系人.md') if '赵六' in p.parent.name)
        text=profile.read_text();self.assertIn('peer_id: "wxid_zhaoliu"',text)
        self.assertIn('participants: ["我", "赵六-供应商"]',text)
        profile.write_text(text+'\n用户自己添加的笔记\n')
        result=export_markdown(self.store,vault)
        self.assertIn(str(profile.resolve()),result['conflicts'])
        self.assertTrue(profile.read_text().endswith('用户自己添加的笔记\n'))

    def test_manual_category_persists_reimport(self):
        self.run_import([ROOT/'fixtures/win4'],owner='本人')
        cid=self.store.conversations()[0]['id'];self.store.set_category(cid,'生活')
        self.run_import([ROOT/'fixtures/win4'],owner='本人')
        self.assertEqual(self.store.conversation(cid)['category'],'生活')

    def test_secret_settings_not_saved(self):
        self.store.save_settings({'api_key':'SYNTHETIC_SECRET','backup_password':'SYNTHETIC_SECRET','owner':'我'})
        self.assertEqual(self.store.settings(),{'owner':'我'})
        self.assertNotIn(b'SYNTHETIC_SECRET',self.store.path.read_bytes())

    def test_encrypted_database_automatic_local_material(self):
        try:from Crypto.Cipher import AES
        except ImportError:self.skipTest('可选加速依赖未安装')
        import shutil
        source=self.root/'encrypted_sample'
        shutil.copytree(ROOT/'fixtures/win3_enc',source)
        (source/'keys.json').write_text(json.dumps({'password':'wechatdemo'}))
        result=self.run_import([source],owner='本人')
        self.assertEqual(self.store.stats()['messages'],10)
        self.assertEqual(result['sources'][0]['errors'],[])

    def test_unknown_encrypted_input_not_reported_success(self):
        source=self.root/'encrypted';source.mkdir();(source/'MSG0.db').write_bytes(b'not-decryptable'*400)
        result=self.run_import([source])
        self.assertEqual(result['sources'][0]['status'],'partial')
        self.assertEqual(self.store.stats()['messages'],0)

    def test_http_auth_and_origin_protection(self):
        app=Application(self.root/'server');server=ThreadingHTTPServer(('127.0.0.1',0),handler_for(app))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base='http://127.0.0.1:'+str(server.server_port)
        try:
            with self.assertRaises(urllib.error.HTTPError) as cm:urllib.request.urlopen(base+'/api/state')
            self.assertEqual(cm.exception.code,403)
            req=urllib.request.Request(base+'/api/state',headers={'X-App-Token':app.token})
            with urllib.request.urlopen(req) as response:self.assertEqual(json.load(response)['stats']['messages'],0)
            req=urllib.request.Request(base+'/api/settings',data=b'{}',headers={'X-App-Token':app.token,'Origin':'https://example.com'})
            with self.assertRaises(urllib.error.HTTPError) as cm:urllib.request.urlopen(req)
            self.assertEqual(cm.exception.code,403)
        finally:server.shutdown();server.server_close()

    def test_ai_contract_uses_all_messages_without_truncation(self):
        captured=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                data=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                chunk=json.loads(data['messages'][1]['content'])['messages'];captured.extend(chunk)
                items=[{'id':m['id'],'category':'工作','reason':'测试服务'} for m in chunk]
                result={'choices':[{'message':{'content':json.dumps({'items':items})}}]}
                payload=json.dumps(result).encode();self.send_response(200);self.end_headers();self.wfile.write(payload)
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        try:
            client=AIClassifier('http://127.0.0.1:'+str(server.server_port)+'/v1','test-model')
            messages=[{'id':str(i),'time':'2024-01-01','sender':'我','is_self':1,'content':'工作内容'*4000 if i==0 else '项目内容'} for i in range(45)]
            result=client.classify({'name':'测试联系人','account':'本人'},messages,lambda x:None)
            self.assertEqual(len(result),45);self.assertEqual(captured[0]['content'],messages[0]['content'])
        finally:server.shutdown();server.server_close()

    def test_ai_incomplete_result_rejected(self):
        from unittest.mock import patch
        payload={'choices':[{'message':{'content':'{"items":[]}'}}]}
        class Response:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,*args):return json.dumps(payload).encode()
        class Opener:
            def open(self,*args,**kwargs):return Response()
        client=AIClassifier('http://127.0.0.1:12345/v1','synthetic-model')
        messages=[{'id':'known','time':'2024-01-01','sender':'我','is_self':1,'content':'项目完成了'}]
        with patch('urllib.request.build_opener',return_value=Opener()):
            with self.assertRaisesRegex(ValueError,'未返回全部消息'):
                client.classify({'name':'测试','account':'本人'},messages,lambda x:None)


if __name__=='__main__':unittest.main()
