"""Private source snapshots and completed-file checkpoints for fast reopening."""
from __future__ import annotations

import json
from pathlib import Path

PARSER_VERSION = 13


def fingerprint(path, owner='', database=False):
    path = Path(path)
    values = []
    for suffix in (('', '-wal', '-journal') if database else ('',)):
        item = Path(str(path)+suffix)
        try:
            info = item.stat()
            values.append((suffix, info.st_size, info.st_mtime_ns, info.st_ctime_ns,
                           info.st_dev, info.st_ino))
        except FileNotFoundError:
            values.append((suffix, None))
    return json.dumps([PARSER_VERSION, owner, values], separators=(',', ':'))


class InputCache:
    def __init__(self, store, source_id):
        self.store, self.source_id = store, source_id
        self.directory = store.directory/'source-cache'/source_id/'input'
        self.directory.mkdir(parents=True, exist_ok=True)

    def completed(self, origin, signature):
        with self.store.connect() as db:
            row = db.execute('SELECT signature,rows FROM import_file_state WHERE source_id=? AND origin=?',
                             (self.source_id, str(origin))).fetchone()
            return row['rows'] if row and row['signature'] == signature else None

    def finish(self, origin, signature, rows, metadata=None):
        with self.store.connect() as db:
            db.execute('''INSERT INTO import_file_state VALUES(?,?,?,?)
              ON CONFLICT(source_id,origin) DO UPDATE SET signature=excluded.signature,rows=excluded.rows''',
              (self.source_id, str(origin), signature, rows))
            if metadata is not None:
                db.execute('INSERT INTO import_file_metadata VALUES(?,?,?) ON CONFLICT(source_id,origin) DO UPDATE SET detail=excluded.detail',
                    (self.source_id,str(origin),json.dumps(metadata,ensure_ascii=False)))
            else:db.execute('DELETE FROM import_file_metadata WHERE source_id=? AND origin=?',(self.source_id,str(origin)))

    def metadata(self, origin):
        with self.store.connect() as db:
            row=db.execute('SELECT detail FROM import_file_metadata WHERE source_id=? AND origin=?',(self.source_id,str(origin))).fetchone()
        try:
            value=json.loads(row['detail']) if row else None
            return value if isinstance(value,dict) else None
        except (ValueError,TypeError):return None

    def reusable_snapshot(self, origin, signature, target, database):
        with self.store.connect() as db:
            row=db.execute('SELECT * FROM import_snapshots WHERE source_id=? AND origin=?',
                           (self.source_id,str(origin))).fetchone()
        return bool(row and row['signature']==signature and row['path']==str(target)
                    and target.is_file() and row['cache_signature']==fingerprint(target,database=database))

    def save_snapshot(self, origin, signature, target, database):
        with self.store.connect() as db:
            db.execute('''INSERT INTO import_snapshots VALUES(?,?,?,?,?) ON CONFLICT(source_id,origin)
              DO UPDATE SET signature=excluded.signature,path=excluded.path,cache_signature=excluded.cache_signature''',
              (self.source_id,str(origin),signature,str(target),fingerprint(target,database=database)))
