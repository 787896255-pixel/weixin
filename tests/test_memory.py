import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path

from app import Application
from wechatbrain.importer import Importer
from wechatbrain.analyze import MemoryAnalyzer
from wechatbrain.exporter import export_insights

ROOT=Path(__file__).resolve().parents[1]


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.app=Application(self.root/'state')
        self.vault=self.root/'vault';self.vault.mkdir()
        self.requests=[];self.invalid=False
        owner=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                request=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                payload=json.loads(request['messages'][1]['content']);owner.requests.append(payload)
                if payload.get('findings'):
                    items=payload['findings']
                else:
                    m=payload['messages'][0]
                    items=[{'kind':'约定','category':'工作','title':'聊天中约定的事项','detail':m['content'],
                            'subject':'我' if m['is_self'] else '对方','date':m['time'][:10],
                            'status':'待核对','source_ids':['nonexistent'] if owner.invalid else [m['id']]}]
                profile={'summary':'根据该联系人聊天原文提炼的重要事项。','items':items}
                body=json.dumps({'choices':[{'message':{'content':json.dumps(profile,ensure_ascii=False)}}]}).encode()
                self.send_response(200);self.end_headers();self.wfile.write(body)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.url='http://127.0.0.1:'+str(self.server.server_port)+'/v1'
        self.app.store.save_settings({'vault':str(self.vault),'ai_url':self.url,'ai_model':'local-test-model'})
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.tmp.cleanup()

    def test_import_each_person_analyze_and_export_only_important_notes(self):
        Importer(self.app.store,lambda x:None).run([str(ROOT/'fixtures/win3'),str(ROOT/'fixtures/win4'),str(ROOT/'fixtures/ios_backup')],owner='本人')
        result=self.app.analyze()
        self.assertEqual(result['people'],8);self.assertEqual(result['important_items'],8)
        self.assertEqual(result['errors'],[]);self.assertEqual(result['export']['people'],8)
        files=list((self.vault/'微信第二大脑').rglob('*.md'))
        self.assertEqual(len(files),12)
        self.assertFalse(any(p.name=='2024-03.md' for p in files))
        notes=list((self.vault/'微信第二大脑/人物').glob('*.md'))
        self.assertEqual(len(notes),8)
        text=notes[0].read_text()
        self.assertIn('type: person-memory',text);self.assertIn('原文依据',text)
        self.assertIn('my_account: "本人"',text)
        count=len(self.requests)
        second=self.app.analyze()
        self.assertEqual(len(self.requests),count)
        self.assertEqual(len(second['reused']),8)

    def test_fabricated_evidence_rejected_and_existing_memory_preserved(self):
        Importer(self.app.store,lambda x:None).run([str(ROOT/'fixtures/win4')],owner='本人')
        first=self.app.analyze();cid=self.app.store.conversations()[0]['id']
        before=self.app.store.insight(cid)['profile']
        self.invalid=True
        failed=self.app.analyze(force=True)
        self.assertEqual(len(failed['errors']),2)
        self.assertEqual(self.app.store.insight(cid)['profile'],before)
        self.assertEqual(failed['people'],0)

    def test_full_history_chunking_and_consolidation(self):
        messages=[{'id':str(i),'time':'2024-01-01 10:00:00','sender':'我','is_self':1,
                   'content':'项目事项 '+str(i)} for i in range(125)]
        client=MemoryAnalyzer(self.url,'local-test-model')
        profile=client.analyze({'name':'测试人','peer_id':'wxid_test','account':'本人'},messages,lambda x:None)
        inputs=[p['messages'] for p in self.requests if 'messages' in p]
        self.assertEqual(sum(map(len,inputs)),125)
        self.assertTrue(any('findings' in p for p in self.requests))
        self.assertEqual(profile['message_count'],125)

    def test_edited_important_note_not_overwritten(self):
        Importer(self.app.store,lambda x:None).run([str(ROOT/'fixtures/win4')],owner='本人')
        self.app.analyze()
        note=next((self.vault/'微信第二大脑/人物').glob('*.md'))
        note.write_text(note.read_text()+'\n用户补充的事实\n')
        result=export_insights(self.app.store,self.vault)
        self.assertIn(str(note.resolve()),result['conflicts'])
        self.assertTrue(note.read_text().endswith('用户补充的事实\n'))

    def test_missing_model_does_not_generate_fake_insights(self):
        self.app.store.save_settings({'ai_url':'','ai_model':''})
        with self.assertRaisesRegex(ValueError,'AI 接口'):
            self.app.analyze()
        self.assertEqual(list(self.vault.rglob('*.md')),[])


if __name__=='__main__':unittest.main()
