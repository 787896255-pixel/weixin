from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


def digest(*parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "library.sqlite3"
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript("""
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sources(
              id TEXT PRIMARY KEY,path TEXT UNIQUE NOT NULL,kind TEXT,status TEXT,
              imported INTEGER DEFAULT 0,duplicates INTEGER DEFAULT 0,updated TEXT,detail TEXT);
            CREATE TABLE IF NOT EXISTS conversations(
              id TEXT PRIMARY KEY,account TEXT,peer_id TEXT,name TEXT NOT NULL,
              category TEXT DEFAULT '',summary TEXT DEFAULT '',classification TEXT DEFAULT 'rules',
              origin_kind TEXT DEFAULT 'legacy_document');
            CREATE TABLE IF NOT EXISTS messages(
              id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL,time TEXT,sender TEXT,
              content TEXT,is_self INTEGER,type TEXT,category TEXT,reason TEXT,
              classification TEXT DEFAULT 'rules',source_message_id TEXT,source_table TEXT,
              FOREIGN KEY(conversation_id) REFERENCES conversations(id));
            CREATE INDEX IF NOT EXISTS message_timeline ON messages(conversation_id,time,id);
            CREATE INDEX IF NOT EXISTS message_category ON messages(category);
            CREATE TABLE IF NOT EXISTS provenance(
              message_id TEXT,source_id TEXT,path TEXT,source_table TEXT,
              PRIMARY KEY(message_id,source_id,path,source_table));
            CREATE INDEX IF NOT EXISTS provenance_source ON provenance(source_id,message_id);
            CREATE TABLE IF NOT EXISTS import_file_state(
              source_id TEXT,origin TEXT,signature TEXT NOT NULL,rows INTEGER NOT NULL,
              PRIMARY KEY(source_id,origin));
            CREATE TABLE IF NOT EXISTS import_file_metadata(
              source_id TEXT,origin TEXT,detail TEXT NOT NULL,PRIMARY KEY(source_id,origin));
            CREATE TABLE IF NOT EXISTS import_snapshots(
              source_id TEXT,origin TEXT,signature TEXT NOT NULL,path TEXT NOT NULL,cache_signature TEXT NOT NULL,
              PRIMARY KEY(source_id,origin));
            CREATE TABLE IF NOT EXISTS export_files(
              path TEXT PRIMARY KEY,conversation_id TEXT,content_hash TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS insights(
              conversation_id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,
              profile TEXT NOT NULL,model TEXT NOT NULL,updated TEXT NOT NULL,
              FOREIGN KEY(conversation_id) REFERENCES conversations(id));
            CREATE TABLE IF NOT EXISTS message_sequence(
              seq INTEGER PRIMARY KEY AUTOINCREMENT,message_id TEXT NOT NULL UNIQUE);
            CREATE TRIGGER IF NOT EXISTS sequence_new_message AFTER INSERT ON messages
              BEGIN INSERT OR IGNORE INTO message_sequence(message_id) VALUES(new.id); END;
            CREATE TABLE IF NOT EXISTS export_runs(
              id TEXT PRIMARY KEY,created TEXT,status TEXT,detail TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS schedules(
              id TEXT PRIMARY KEY,name TEXT NOT NULL,config TEXT NOT NULL,enabled INTEGER NOT NULL,
              next_run TEXT NOT NULL,watermark INTEGER NOT NULL DEFAULT 0,last_result TEXT DEFAULT '');
            CREATE TABLE IF NOT EXISTS contact_notes(
              conversation_id TEXT PRIMARY KEY,alias TEXT DEFAULT '',note TEXT DEFAULT '');
            CREATE TABLE IF NOT EXISTS contacts(
              account TEXT,peer_id TEXT,name TEXT,nickname TEXT,remark TEXT,alias TEXT,
              local_type INTEGER,source_id TEXT,PRIMARY KEY(account,peer_id));
            CREATE TABLE IF NOT EXISTS account_profiles(
              account TEXT PRIMARY KEY,username TEXT,name TEXT,alias TEXT,source_id TEXT);
            CREATE TABLE IF NOT EXISTS session_metadata(
              conversation_id TEXT PRIMARY KEY,last_time TEXT,summary TEXT,unread INTEGER,hidden INTEGER);
            CREATE TABLE IF NOT EXISTS conversation_sources(
              conversation_id TEXT,source_id TEXT,path TEXT,source_table TEXT,
              PRIMARY KEY(conversation_id,source_id,path,source_table));
            CREATE INDEX IF NOT EXISTS conversation_source ON conversation_sources(source_id,conversation_id);
            CREATE TABLE IF NOT EXISTS reader_watermarks(
              source_id TEXT,origin TEXT,source_table TEXT,high_row INTEGER,
              PRIMARY KEY(source_id,origin,source_table));
            CREATE TABLE IF NOT EXISTS avatars(
              id TEXT PRIMARY KEY,account TEXT,peer_id TEXT,mime TEXT,data BLOB,
              UNIQUE(account,peer_id));
            CREATE TABLE IF NOT EXISTS media_assets(
              id TEXT PRIMARY KEY,source_id TEXT NOT NULL,original_path TEXT,name TEXT,
              signature TEXT,kind TEXT,cache_path TEXT,mime TEXT,status TEXT,error TEXT);
            CREATE INDEX IF NOT EXISTS media_source ON media_assets(source_id,status);
            CREATE INDEX IF NOT EXISTS media_name ON media_assets(source_id,name);
            CREATE TABLE IF NOT EXISTS media_databases(
              source_id TEXT,path TEXT,original TEXT,PRIMARY KEY(source_id,path));
            CREATE TABLE IF NOT EXISTS media_origins(
              asset_id TEXT PRIMARY KEY,locator TEXT NOT NULL,
              FOREIGN KEY(asset_id) REFERENCES media_assets(id));
            CREATE TABLE IF NOT EXISTS media_asset_aliases(
              asset_id TEXT,alias TEXT,PRIMARY KEY(asset_id,alias),
              FOREIGN KEY(asset_id) REFERENCES media_assets(id));
            CREATE TABLE IF NOT EXISTS message_media_evidence(
              message_id TEXT PRIMARY KEY,metadata TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS message_media(
              message_id TEXT,asset_id TEXT,match_basis TEXT,
              PRIMARY KEY(message_id,asset_id),
              FOREIGN KEY(message_id) REFERENCES messages(id),
              FOREIGN KEY(asset_id) REFERENCES media_assets(id));
            CREATE INDEX IF NOT EXISTS message_media_asset ON message_media(asset_id);
            """)
            columns={r[1] for r in db.execute('PRAGMA table_info(conversations)')}
            if 'origin_kind' not in columns:
                db.execute("ALTER TABLE conversations ADD COLUMN origin_kind TEXT DEFAULT 'legacy_document'")
            if 'sender_id' not in {r[1] for r in db.execute('PRAGMA table_info(messages)')}:
                db.execute("ALTER TABLE messages ADD COLUMN sender_id TEXT DEFAULT ''")
            if not db.execute("SELECT 1 FROM settings WHERE key='_native_origin_migration'").fetchone():
                db.execute("""UPDATE conversations SET origin_kind='wechat' WHERE id IN (
                    SELECT DISTINCT conversation_id FROM messages WHERE source_table='MSG'
                        OR source_table GLOB 'Msg_[0-9a-fA-F]*' OR source_table GLOB 'Chat_[0-9a-fA-F]*'
                        OR source_table='msg_rows' UNION SELECT conversation_id FROM session_metadata)""")
                db.execute("INSERT INTO settings VALUES('_native_origin_migration','1')")
            db.execute('CREATE INDEX IF NOT EXISTS conversation_origin ON conversations(origin_kind)')
            if db.execute('SELECT 1 FROM message_sequence LIMIT 1').fetchone() is None:
                db.execute('INSERT OR IGNORE INTO message_sequence(message_id) SELECT id FROM messages ORDER BY rowid')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        from .query import message_type
        db.create_function('jk_type',2,message_type,deterministic=True)
        db.execute("PRAGMA foreign_keys=ON")
        db.execute('PRAGMA synchronous=NORMAL')
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def settings(self):
        with self.connect() as db:
            return {r['key']: json.loads(r['value']) for r in db.execute('SELECT * FROM settings')}

    def save_settings(self, values):
        # Passwords / API credentials deliberately live in memory only.
        allowed = {'vault', 'owner', 'ai_url', 'ai_model', 'source_paths', 'export_dir',
                   'active_source', 'account_export_dirs','live_sources'}
        with self.connect() as db:
            for key, value in values.items():
                if key in allowed:
                    db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',
                               (key, json.dumps(value, ensure_ascii=False)))

    def conversations(self, query='', category='', include_documents=False):
        pattern = '%' + query.replace('\\','\\\\').replace('%','\\%').replace('_','\\_') + '%'
        with self.connect() as db:
            rows = db.execute("""SELECT c.*,COUNT(m.id) AS message_count,
              COALESCE(MAX(m.time),s.last_time,'') AS last_time,
              MIN(m.time) AS first_time,
              COALESCE((SELECT content FROM messages x WHERE x.conversation_id=c.id
               ORDER BY time DESC,id DESC LIMIT 1),s.summary,'') AS preview,
              COALESCE(s.unread,0) AS unread_count
              FROM conversations c LEFT JOIN messages m ON c.id=m.conversation_id
              LEFT JOIN session_metadata s ON s.conversation_id=c.id
              WHERE (? OR c.origin_kind IN ('wechat','chat_export'))
              AND (c.name LIKE ? ESCAPE '\\' OR c.peer_id LIKE ? ESCAPE '\\'
                 OR EXISTS(SELECT 1 FROM messages s WHERE s.conversation_id=c.id
                   AND s.content LIKE ? ESCAPE '\\')
                 OR EXISTS(SELECT 1 FROM contact_notes n WHERE n.conversation_id=c.id
                   AND n.alias LIKE ? ESCAPE '\\'))
              AND (?='' OR c.category=? OR (c.category='' AND EXISTS(
                 SELECT 1 FROM messages k WHERE k.conversation_id=c.id AND k.category=?)))
              GROUP BY c.id ORDER BY last_time DESC,c.name""",
              (int(include_documents),)+(pattern,)*4
              + (category, category, category)).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                note=db.execute('SELECT alias,note FROM contact_notes WHERE conversation_id=?',(row['id'],)).fetchone()
                item['original_name']=item['name'];item['note']=note['note'] if note else ''
                if note and note['alias']:item['name']=note['alias']
                item['is_group']=str(item['peer_id']).endswith('@chatroom')
                avatar=db.execute('SELECT id FROM avatars WHERE account=? AND peer_id=?',
                    (item['account'],item['peer_id'])).fetchone()
                item['avatar_url']='/api/avatar?id='+avatar['id'] if avatar else ''
                item['type']='group' if item['is_group'] else 'person'
                item['categories'] = {r[0]:r[1] for r in db.execute(
                    'SELECT category,count(*) FROM messages WHERE conversation_id=? GROUP BY category',
                    (row['id'],))}
                profile=db.execute('SELECT fingerprint,profile,updated FROM insights WHERE conversation_id=?',(row['id'],)).fetchone()
                item['analyzed']=bool(profile)
                if profile:
                    data=json.loads(profile['profile'])
                    item['insight_count']=len(data.get('items',[]))
                    item['insight_summary']=data.get('summary','')
                    item['analysis_count']=data.get('message_count',0)
                    item['analysis_time']=profile['updated']
                result.append(item)
            return result

    def insight(self,cid):
        self.conversation(cid)
        with self.connect() as db:
            row=db.execute('SELECT * FROM insights WHERE conversation_id=?',(cid,)).fetchone()
            if row is None:return None
            result=dict(row);result['profile']=json.loads(result['profile'])
            return result

    def contacts(self,ids=None):
        """Local contact records, including friends without a message history."""
        conversations=self.conversations()
        by_peer={(p['account'],p['peer_id']):p for p in conversations}
        wanted=set(ids) if ids is not None else None
        selected={(p['account'],p['peer_id']) for p in conversations if wanted is not None and p['id'] in wanted}
        active=self.settings().get('active_source','')
        with self.connect() as db:
            rows=[dict(r) for r in db.execute('SELECT * FROM contacts ORDER BY account,name,peer_id')]
        if ids is not None:rows=[p for p in rows if (p['account'],p['peer_id']) in selected]
        elif active:rows=[p for p in rows if p['source_id']==active]
        # Existing 3.x/iPhone/imported files may only provide conversation names.
        keys={(p['account'],p['peer_id']) for p in rows}
        for p in conversations:
            pair=(p['account'],p['peer_id'])
            if pair in keys or (ids is not None and pair not in selected):continue
            if ids is None and active:
                with self.connect() as db:
                    if not db.execute('SELECT 1 FROM conversation_sources WHERE source_id=? AND conversation_id=?',
                                      (active,p['id'])).fetchone():continue
            rows.append({'account':p['account'],'peer_id':p['peer_id'],'name':p['original_name'],
                         'nickname':p['original_name'],'remark':'','alias':'','source_id':''})
        for p in rows:
            conversation=by_peer.get((p['account'],p['peer_id']),{})
            p.update(id=conversation.get('id',digest(p['account'],p['peer_id'])),
                     original_name=p['name'],is_group=p['peer_id'].endswith('@chatroom'),
                     note=conversation.get('note',''),message_count=conversation.get('message_count',0),
                     first_time=conversation.get('first_time') or '',last_time=conversation.get('last_time') or '')
            p['name']=conversation.get('name') or p['name']
        return rows

    def save_insight(self,cid,fingerprint,profile,model):
        from datetime import datetime
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO insights VALUES(?,?,?,?,?)',
                       (cid,fingerprint,json.dumps(profile,ensure_ascii=False),model,datetime.now().isoformat(timespec='seconds')))

    def conversation(self, cid):
        with self.connect() as db:
            r = db.execute('SELECT * FROM conversations WHERE id=?', (cid,)).fetchone()
            if r is None:
                raise ValueError('会话不存在')
            result=dict(r)
            note=db.execute('SELECT alias,note FROM contact_notes WHERE conversation_id=?',(cid,)).fetchone()
            result['original_name']=result['name'];result['note']=note['note'] if note else ''
            if note and note['alias']:result['name']=note['alias']
            result['is_group']=str(result['peer_id']).endswith('@chatroom')
            return result

    def update_contact(self,cid,alias='',note=''):
        self.conversation(cid)
        if len(alias)>100 or len(note)>5000:raise ValueError('备注名称或说明过长')
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO contact_notes VALUES(?,?,?)',(cid,alias.strip(),note.strip()))

    def max_sequence(self):
        with self.connect() as db:return db.execute('SELECT COALESCE(MAX(seq),0) FROM message_sequence').fetchone()[0]

    def filtered_messages(self,cid,filters=None,offset=0,limit=100):
        from .query import predicate
        self.conversation(cid);where,args=predicate(cid,filters)
        with self.connect() as db:
            total=db.execute('SELECT count(*) FROM messages m JOIN message_sequence s ON s.message_id=m.id WHERE '+where,args).fetchone()[0]
            rows=db.execute('SELECT m.*,s.seq,jk_type(m.type,m.content) AS message_type FROM messages m '
                'JOIN message_sequence s ON s.message_id=m.id WHERE '+where+' ORDER BY m.time,m.id LIMIT ? OFFSET ?',
                args+[max(1,min(int(limit),500)),max(0,int(offset))]).fetchall()
            from .media import MediaLibrary
            person=db.execute('SELECT account,peer_id FROM conversations WHERE id=?',(cid,)).fetchone()
            profile=db.execute('SELECT username FROM account_profiles WHERE account=?',(person['account'],)).fetchone()
            avatars={r['peer_id']:'/api/avatar?id='+r['id'] for r in db.execute('SELECT id,peer_id FROM avatars WHERE account=?',(person['account'],))}
            items=[]
            for r in rows:
                item=dict(r);sender=(profile['username'] if profile else '') if item['is_self'] else item.get('sender_id') or ('' if person['peer_id'].endswith('@chatroom') else person['peer_id'])
                item['avatar_url']=avatars.get(sender,'');item['self_avatar_url']=avatars.get(profile['username'],'') if profile else ''
                items.append(item)
            return {'items':MediaLibrary(self).for_messages(items),'total':total,'offset':int(offset),
                    'has_more':int(offset)+len(rows)<total}

    def messages(self, cid, offset=0, limit=100, query=''):
        with self.connect() as db:
            pattern = '%' + query.replace('\\','\\\\').replace('%','\\%').replace('_','\\_') + '%'
            total = db.execute("SELECT count(*) FROM messages WHERE conversation_id=? AND content LIKE ? ESCAPE '\\'",
                               (cid, pattern)).fetchone()[0]
            rows = db.execute("""SELECT * FROM messages WHERE conversation_id=?
                AND content LIKE ? ESCAPE '\\' ORDER BY time,id LIMIT ? OFFSET ?""",
                (cid, pattern, min(int(limit),500),max(0,int(offset)))).fetchall()
            return {'items':[dict(r) for r in rows], 'total':total,'offset':offset,
                    'has_more':offset+len(rows)<total}

    def all_messages(self, cid):
        with self.connect() as db:
            return [dict(r) for r in db.execute(
                'SELECT * FROM messages WHERE conversation_id=? ORDER BY time,id',(cid,))]

    def stats(self):
        with self.connect() as db:
            return {'messages': db.execute("SELECT count(*) FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE c.origin_kind IN ('wechat','chat_export')").fetchone()[0],
                    'conversations':db.execute("SELECT count(*) FROM conversations WHERE origin_kind IN ('wechat','chat_export')").fetchone()[0],
                    'quarantined_conversations':db.execute("SELECT count(*) FROM conversations WHERE origin_kind='legacy_document'").fetchone()[0],
                    'sources':[dict(r) for r in db.execute('SELECT * FROM sources ORDER BY updated DESC')]}

    def provenance(self, cid):
        with self.connect() as db:
            return [dict(r) for r in db.execute('''SELECT DISTINCT p.source_id,p.path,p.source_table
                FROM provenance p JOIN messages m ON m.id=p.message_id WHERE m.conversation_id=?
                UNION SELECT source_id,path,source_table FROM conversation_sources WHERE conversation_id=?''',(cid,cid))]

    def set_category(self, cid, category):
        if category not in ('','生活','工作','其他'):
            raise ValueError('分类不正确')
        self.conversation(cid)
        with self.connect() as db:
            db.execute('UPDATE conversations SET category=? WHERE id=?',(category,cid))
