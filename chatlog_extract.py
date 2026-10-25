#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""chatlog_extract —— 本地聊天记录提取器（零依赖，单文件）

扫描「指定的聊天文件夹」，自动识别聊天数据载体（SQLite 数据库 / 文本 / JSON /
HTML / CSV），把里面的消息统一解析成结构化记录，并导出
JSONL / CSV / Markdown / HTML / Excel 五种成品。

典型用法：
    python3 chatlog_extract.py --src "~/Downloads/聊天记录" --out out
    python3 chatlog_extract.py --src CHAT_DIR --schemas          # 只看库结构
    python3 chatlog_extract.py --src CHAT_DIR --scan-only        # 只看发现了什么
    python3 chatlog_extract.py --src CHAT_DIR --since 2024-01-01 --keyword 报价

支持的聊天数据来源（自动识别，无需指定）：
  * SQLite：WeChat / QQ / 钉钉 / 飞书 / Telegram / WhatsApp 桌面导出、
    各类自研 IM 的 *.db / *.sqlite
  * 纯文本：导出的 .txt / .log（[时间] 昵称: 内容 等常见排版）
  * JSON：导出的消息数组、嵌套结构的 messages/records/history
  * HTML：网页版聊天记录导出
  * CSV/TSV：表格导出的聊天记录

额外适配（见 chatlog_platform.py）：
  * Windows 微信：WeChat Files（3.x）/ xwechat_files（4.x）整目录，自动认账号、
    MSG / Msg_<md5> 表、群聊发送者、联系人昵称还原；加密库给 --enc-key
  * iPhone 聊天记录：Finder/iTunes 备份目录（Manifest.db 自动取库），
    加密备份给 --backup-password，内置 keybag 解密，无需 pycryptodome
  * 微信 iOS 库：MM.sqlite 的 Chat_<md5> 表、message_*.db 的 Msg_ 表
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import html as html_mod
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple
from urllib.parse import quote

try:  # 平台适配层：Windows 微信目录 / iPhone 备份 / 微信专有表
    from chatlog_platform import (IOSBackup, detect_layouts, parse_wechat_db,
                                  summarize_wechat_dir, ios_backup_summary,
                                  sqlcipher_decrypt)
    HAVE_PLATFORM = True
except ImportError:  # 单独拷走 chatlog_extract.py 时退化为通用解析
    HAVE_PLATFORM = False
    IOSBackup = None            # type: ignore
    detect_layouts = None       # type: ignore
    parse_wechat_db = None      # type: ignore
    summarize_wechat_dir = None  # type: ignore
    ios_backup_summary = None   # type: ignore
    sqlcipher_decrypt = None    # type: ignore

# --------------------------------------------------------------------------- #
# 0. 常量
# --------------------------------------------------------------------------- #

SQLITE_EXT = {".db", ".sqlite", ".sqlite3", ".db3", ".sqlitedb", ".sqlite2", ".bakdb"}
TEXT_EXT = {".txt", ".log", ".text", ".chat", ".msg"}
JSON_EXT = {".json", ".jsonl", ".ndjson"}
HTML_EXT = {".html", ".htm", ".xhtml"}
CSV_EXT = {".csv", ".tsv"}

SKIP_DIRS = {".git", ".svn", ".hg", "node_modules", "__pycache__", ".venv", "venv",
             ".idea", ".vscode", ".DS_Store", "Caches"}

# 微信账号目录里的纯媒体/缓存目录：里面有几十万张图，扫了也没正文
WECHAT_MEDIA_DIRS = {"FileStorage", "MsgAttach", "Video", "Image", "Audio", "emoji",
                     "Emoji", "Cache", "Temp", "attach", "media", "favorite"}

# 消息表名提示词（命中越多，越可能存消息）
TABLE_HINTS = ("message", "messages", "msg", "msgs", "chat", "chatlog", "chat_log",
               "chatrecord", "chat_record", "history", "record", "records", "dialog",
               "conversation", "im_msg", "t_msg")

# 列名别名（全部小写比较）
COL_CONTENT_CN = ("消息内容", "内容", "正文", "文本", "聊天内容", "消息")
COL_SENDER_CN = ("发送人", "发送者", "发言人", "昵称", "用户", "姓名", "发送方")
COL_TIME_CN = ("发送时间", "时间", "日期", "消息时间", "创建时间")
COL_CONV_CN = ("会话", "群聊", "群名", "聊天对象", "会话名称", "联系人")
COL_SELF_CN = ("是否本人", "是否自己", "发送方向", "本人")

COL_CONTENT = ("content", "strcontent", "str_content", "msgcontent", "msg_content",
               "messagecontent", "message_content", "msg", "message", "text", "body",
               "words", "richtext", "rich_text", "talkercontent", "data", "payload",
               "content_text", "msgtext")
COL_SENDER = ("sendername", "sender_name", "send_nickname", "sendnickname", "sender",
              "fromusername", "from_username", "fromuser", "from_user", "from",
              "nickname", "nick_name", "speaker", "author", "username", "user_name",
              "strtalker", "talker", "displayname", "display_name", "send_member_name",
              "membername", "wxid", "uin", "senderid", "sender_id")
COL_TIME = ("createtime", "create_time", "msgtime", "msg_time", "msgsvr_time",
            "msg_svr_time", "sendtime", "send_time", "timestamp", "ts", "datetime",
            "date_time", "time", "date", "strtime", "created_at", "updated_at",
            "sorttime", "sort_time")
COL_CONV = ("strchat", "chatname", "chat_name", "conversation", "conversationid",
            "conversation_id", "session", "sessionid", "session_id", "dialog", "dialogid",
            "room", "roomid", "room_id", "group", "groupid", "group_id", "chatuin",
            "frienduin", "peer", "peerid", "peer_id", "target", "strtalker", "talker",
            "chatroom", "chat_room")
COL_SELF = ("issend", "is_send", "isfromme", "is_from_me", "fromme", "from_me",
            "direction", "issender", "is_sender", "self")

TS_FORMATS = (
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
    "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d",
    "%Y.%m.%d %H:%M:%S", "%Y.%m.%d %H:%M", "%Y.%m.%d",
    "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d %H:%M:%S.%f",
    "%Y%m%d%H%M%S", "%Y%m%d",
    "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%d/%m/%Y %H:%M:%S",
    "%Y年%m月%d日 %H:%M:%S", "%Y年%m月%d日 %H:%M",
)

# 文本行排版：[2024-03-01 09:12:33] 小明: 你好
TS_STR = r"(\d{4}[-/.]\d{1,2}[-/.]\d{1,2}[ T]\d{1,2}:\d{2}(?::\d{2})?(?:\.\d{1,6})?)"
LINE_PATTERNS = [
    re.compile(r"^\[" + TS_STR + r"\]\s*(?P<sender>[^:：\]\n]{1,40})\s*[:：]\s?(?P<content>.*)$"),
    re.compile(r"^" + TS_STR + r"\s+(?P<sender>[^:：\n]{1,40})\s*[:：]\s?(?P<content>.*)$"),
    re.compile(r"^(?P<sender>[^:：\n]{1,40})\s*[:：]\s?(?P<content>.*)$"),
]
LINE_PATTERNS_TS_ONLY = [
    re.compile(r"^\[" + TS_STR + r"\]\s*(?P<sender>[^\n]{1,40})$"),
    re.compile(r"^" + TS_STR + r"\s+(?P<sender>[^\s:：]{1,40})$"),
]

JSON_CONTENT_KEYS = {"content", "strcontent", "msgcontent", "msg_content", "messagecontent",
                     "message_content", "msg", "message", "text", "body", "words",
                     "richtext", "content_text", "msgtext", "reply"}
JSON_SENDER_KEYS = {"sendername", "sender", "from", "fromuser", "fromusername", "nickname",
                    "nick_name", "username", "speaker", "author", "strtalker", "talker",
                    "displayname", "send_member_name", "user_name", "wxid"}
JSON_TIME_KEYS = {"time", "timestamp", "createtime", "create_time", "msgtime", "msg_time",
                  "sendtime", "send_time", "datetime", "date", "msgsvr_time", "strtime",
                  "ts", "created_at"}
JSON_CONV_KEYS = {"conversation", "chatname", "chat_name", "session", "sessionid", "room",
                  "roomid", "group", "groupname", "group_name", "dialog", "chatuin",
                  "strchat", "peer"}
JSON_SELF_KEYS = {"issend", "is_send", "isfromme", "is_from_me", "fromme", "direction"}


# --------------------------------------------------------------------------- #
# 1. 数据模型
# --------------------------------------------------------------------------- #

@dataclass
class Message:
    source: str                 # 来源文件
    conversation: str           # 会话 / 群名
    sender: str                 # 发送者
    ts: Optional[datetime]      # 归一化后的时间
    ts_raw: str                 # 原始时间串
    content: str                # 正文
    is_self: bool = False       # 是否自己发的
    kind: str = ""              # sqlite / text / json / html / csv
    table: str = ""             # 来源表名（sqlite）
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "time": self.ts.strftime("%Y-%m-%d %H:%M:%S") if self.ts else "",
            "conversation": self.conversation,
            "sender": self.sender,
            "content": self.content,
            "is_self": self.is_self,
            "source": self.source,
            "kind": self.kind,
        }
        if self.table:
            d["table"] = self.table
        if self.ts_raw and self.ts_raw != (d["time"]):
            d["raw_time"] = self.ts_raw
        if self.extra:
            d["extra"] = self.extra
        return d


class Ctx:
    """收集日志与统计。"""

    def __init__(self) -> None:
        self.notes: List[str] = []
        self.errors: List[str] = []
        self.files: List[Tuple[str, str, int]] = []   # (path, kind, msg_count)
        self.temp_files: List[Path] = []              # 解码/解包产生的临时文件
        self.schemas: List[str] = []

    def note(self, msg: str) -> None:
        self.notes.append(msg)

    def error(self, msg: str) -> None:
        self.errors.append(msg)


# --------------------------------------------------------------------------- #
# 2. 通用工具：编码、时间、文本清洗
# --------------------------------------------------------------------------- #

def read_text_auto(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "gb18030", "big5", "utf-16", "utf-16-le", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def value_to_text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, (bytes, bytearray)):
        return blob_to_text(bytes(v))
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


PRINTABLE = set(chr(c) for c in range(32, 127))


def blob_to_text(blob: bytes) -> str:
    """BLOB 转文本：优先 utf-8，其次抽取可打印片段（含中文），最后 base64。"""
    if not blob:
        return ""
    try:
        s = blob.decode("utf-8")
        if sum(1 for ch in s if ch.isprintable() or ch in "\n\t") >= max(1, int(len(s) * 0.85)):
            return s
    except UnicodeDecodeError:
        pass
    try:
        s = blob.decode("utf-16-le")
        if sum(1 for ch in s if ch.isprintable()) >= max(1, int(len(s) * 0.85)):
            return s
    except UnicodeDecodeError:
        pass
    # 抽取可读片段（ASCII 可打印 + 中日韩 + 常见标点）
    chunks = re.findall(r"[0-9A-Za-z\u4e00-\u9fff\u3000-\u303f\uff00-\uffef"
                        r" \t\r\n.,:;!?'\"()\[\]{}<>/@#%&*+=_\-~`|\\^$]{3,}", blob.decode("latin-1"))
    text = " ".join(x.strip() for x in chunks if x.strip())
    if text and len(text) >= 3:
        return text
    return "base64:" + base64.b64encode(blob[:256]).decode("ascii")


def parse_ts(v: Any) -> Optional[datetime]:
    """把各种时间表示归一化成 datetime（本地时间）。"""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return _ts_from_number(float(v))
    s = str(v).strip()
    if not s:
        return None
    if re.fullmatch(r"-?\d{9,19}(\.\d+)?", s):
        return _ts_from_number(float(s))
    s2 = s.replace("Z", "").replace("T", " ").strip()
    s2 = re.sub(r"([+-]\d{2}:?\d{2})$", "", s2).strip()
    s2 = re.sub(r"\.\d+$", "", s2)
    for fmt in TS_FORMATS:
        for cand in (s, s2, s2.split(".")[0]):
            try:
                return datetime.strptime(cand, fmt)
            except (ValueError, TypeError):
                continue
    m = re.match(r"^(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})", s)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def _ts_from_number(x: float) -> Optional[datetime]:
    if x <= 0:
        return None
    try:
        if x > 1e17:            # 纳秒
            x /= 1e9
        elif x > 1e14:          # 微秒
            x /= 1e6
        elif x > 1e11:          # 毫秒
            x /= 1e3
        if 1e8 <= x < 1e11:     # 秒
            return datetime.fromtimestamp(x)
        if 1e7 <= x < 1e8:      # YYYYMMDD
            s = str(int(x))
            return datetime(int(s[0:4]), int(s[4:6]), int(s[6:8]))
        if 1e5 <= x < 1e7:      # 可能是 Excel 序列日期
            return datetime(1899, 12, 30) + timedelta(days=x)
    except (ValueError, OSError, OverflowError):
        return None
    return None


def clean_text(s: str, keep_markup: bool = False) -> str:
    if not s:
        return ""
    s = s.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    s = "".join(ch for ch in s if ch in "\n\t" or ord(ch) >= 32)
    if not keep_markup:
        s = re.sub(r"(?i)<br\s*/?>", "\n", s)
        s = re.sub(r"(?i)</(p|div|li|tr|h[1-6])>", "\n", s)
        s = re.sub(r"<[^<>]{1,4000}>", " ", s)
        try:
            s = html_mod.unescape(s)
        except Exception:
            pass
        s = re.sub(r"[ \t\u00a0]{2,}", " ", s)
        s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def normalize_text_for_match(s: str) -> str:
    return s.casefold()


# --------------------------------------------------------------------------- #
# 3. 发现：扫描目录里的聊天载体
# --------------------------------------------------------------------------- #

@dataclass
class Candidate:
    path: Path
    kind: str            # sqlite / text / json / html / csv
    size: int


def kind_of(path: Path, sniff: bool = True) -> Optional[str]:
    ext = path.suffix.lower()
    if ext in SQLITE_EXT:
        return "sqlite"
    if ext in TEXT_EXT:
        return "text"
    if ext in JSON_EXT:
        return "json"
    if ext in HTML_EXT:
        return "html"
    if ext in CSV_EXT:
        return "csv"
    if sniff:
        try:
            with path.open("rb") as fh:
                head = fh.read(2048)
        except OSError:
            return None
        if head.startswith(b"SQLite format 3\x00"):
            return "sqlite"
        low = head.lstrip()[:64].lower()
        if low.startswith(b"<!doctype html") or low.startswith(b"<html"):
            return "html"
        if low.startswith(b"{") or low.startswith(b"["):
            # 避免把 ini 的 [section] 误判成 JSON：紧跟括号的必须是 JSON 合法起始字符
            rest = low[1:].lstrip()[:1]
            if rest and rest in b'{}[]"0123456789-tfn':
                return "json"
        if head.count(b"\x00") > 32:
            return "sqlite?"     # 疑似加密库
        text = head.decode("utf-8", errors="ignore")
        if any(p.search(text) for p in LINE_PATTERNS[:2]):
            return "text"
    return None


def discover(root: Path, ctx: Ctx, max_mb: float = 512.0,
             extra_skip: Iterable[str] = ()) -> List[Candidate]:
    out: List[Candidate] = []
    seen = set()
    limit = max_mb * 1024 * 1024
    skip = set(SKIP_DIRS) | set(extra_skip)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in skip and not d.startswith(".Trash"))
        for fn in sorted(filenames):
            if fn.startswith("._") or fn in ("Icon\r",):
                continue
            if fn.endswith(("-wal", "-shm", "-journal")):
                continue      # SQLite 的 WAL/回滚文件，主库打开时会自动合并
            p = Path(dirpath) / fn
            if p.is_symlink() and not p.exists():
                continue
            try:
                size = p.stat().st_size
            except OSError:
                continue
            if size == 0 or size > limit:
                if size > limit:
                    ctx.note("跳过超大文件(%.0f MB): %s" % (size / 1048576, p))
                continue
            k = kind_of(p)
            if not k:
                continue
            key = str(p.resolve())
            if key in seen:
                continue
            seen.add(key)
            out.append(Candidate(p, k.rstrip("?"), size))
    return out


# --------------------------------------------------------------------------- #
# 4. SQLite 解析
# --------------------------------------------------------------------------- #

def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def open_sqlite(path: Path, enc_key: Optional[str], ctx: Ctx,
                max_mb: float = 512.0) -> Optional[sqlite3.Connection]:
    uri = path.resolve().as_uri() + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        conn.execute("SELECT count(*) FROM sqlite_master LIMIT 1").fetchone()
        return conn
    except sqlite3.DatabaseError as exc:
        try:
            conn.close()
        except Exception:
            pass
        enc = _looks_encrypted(path)
        if enc and enc_key:
            try:
                from sqlcipher3 import dbapi2 as sqlcipher  # type: ignore
            except ImportError:
                plain = None
                if HAVE_PLATFORM:
                    plain = sqlcipher_decrypt(path, enc_key, notes=ctx.notes,
                                              max_mb=max_mb)
                if plain is not None:
                    try:
                        conn = sqlite3.connect(plain.resolve().as_uri() + "?mode=ro", uri=True)
                        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
                        ctx.temp_files.append(plain)
                        return conn
                    except sqlite3.DatabaseError as exc:
                        ctx.error("纯 Python 解码结果打不开 %s：%s" % (path.name, exc))
                ctx.error("加密库 %s 打不开：密钥不对、或页面参数不在内置组合里。"
                          "可 pip install sqlcipher3-binary 再试，或先用你的解密工具导出明文库"
                          % path.name)
                return None
            try:
                c2 = sqlcipher.connect(uri, uri=True)
                key = enc_key
                if key.lower().startswith("0x"):
                    c2.execute('PRAGMA key = "x\'%s\'"' % key[2:])
                else:
                    c2.execute("PRAGMA key = '%s'" % key.replace("'", "''"))
                c2.execute("SELECT count(*) FROM sqlite_master").fetchone()
                ctx.note("已用 --enc-key 解密：%s" % path.name)
                return c2
            except Exception as exc2:  # noqa: BLE001
                ctx.error("解密失败 %s：%s" % (path.name, exc2))
                return None
        if enc:
            ctx.error("加密数据库（SQLCipher），跳过：%s（可用 --enc-key 或目录里的 key.txt 提供密钥）" % path)
        else:
            ctx.error("无法作为 SQLite 打开：%s（%s）" % (path.name, exc))
        return None


def _looks_encrypted(path: Path) -> bool:
    try:
        head = path.open("rb").read(16)
    except OSError:
        return False
    if head.startswith(b"SQLite format 3\x00"):
        return False
    return True


def list_tables(conn: sqlite3.Connection) -> List[Tuple[str, str]]:
    rows = conn.execute(
        "SELECT name, COALESCE(sql,'') FROM sqlite_master "
        "WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def table_columns(conn: sqlite3.Connection, table: str) -> List[str]:
    try:
        return [r[1] for r in conn.execute('PRAGMA table_info(%s)' % _q(table)).fetchall()]
    except sqlite3.DatabaseError:
        return []


def pick(cols_lower: List[str], aliases: Iterable[str]) -> Optional[int]:
    for a in aliases:
        if a in cols_lower:
            return cols_lower.index(a)
    # 再尝试包含匹配（如 create_time_ms / msgContentStr）
    for i, c in enumerate(cols_lower):
        for a in aliases:
            if a in c:
                return i
    return None


def score_table(name: str, cols: List[str]) -> int:
    n = name.lower()
    score = 0
    if any(h in n for h in TABLE_HINTS):
        score += 3
    low = [c.lower() for c in cols]
    if pick(low, COL_CONTENT + COL_CONTENT_CN) is not None:
        score += 3
    if pick(low, COL_TIME + COL_TIME_CN) is not None:
        score += 2
    if pick(low, COL_SENDER + COL_SENDER_CN) is not None:
        score += 1
    if pick(low, COL_CONV + COL_CONV_CN) is not None:
        score += 1
    if len(cols) < 2:
        score -= 2
    return score


def _rec_to_message(rec: Any, path: Path) -> Message:
    extra: Dict[str, Any] = {"type": rec.type_label} if getattr(rec, "type_label", "") else {}
    for key in ('conversation_id','account_id','sender_id','message_id'):
        if getattr(rec,key,''):
            extra[key]=getattr(rec,key)
    if getattr(rec,'media',None):extra['media']=rec.media
    extra['wechat_native']=True
    return Message(
        source=str(path), conversation=rec.conversation, sender=rec.sender,
        ts=rec.ts, ts_raw=rec.ts_raw, content=rec.content, is_self=rec.is_self,
        kind="sqlite", table=rec.table, extra=extra,
    )


def extract_sqlite(cand: Candidate, opts: argparse.Namespace, ctx: Ctx) -> List[Message]:
    path = cand.path
    conn = open_sqlite(path, opts.enc_key, ctx, opts.max_file_mb)
    if conn is None:
        return []
    msgs: List[Message] = []
    cancelled = getattr(opts, 'cancelled', lambda: False)
    on_batch = getattr(opts, 'on_batch', None)
    import time
    last_pulse = [0.0]
    def pulse():
        if cancelled(): return 1
        now = time.monotonic()
        if now-last_pulse[0] > 1:
            callback = getattr(opts, 'on_scan', None)
            if callback: callback(path.name)
            last_pulse[0] = now
        return 0
    conn.set_progress_handler(pulse, 10000)
    try:
        tables = list_tables(conn)
        if opts.schemas:
            lines = ["\n== %s (%d 张表) ==" % (path, len(tables))]
            for name, _sql in tables:
                cols = table_columns(conn, name)
                try:
                    cnt = conn.execute("SELECT count(*) FROM %s" % _q(name)).fetchone()[0]
                except sqlite3.DatabaseError:
                    cnt = -1
                lines.append("  %-28s rows=%-8s score=%-3s cols=%s"
                             % (name, cnt, score_table(name, cols), ", ".join(cols[:14])))
            ctx.schemas.append("\n".join(lines))
            if getattr(opts, "schema_only", False):
                return []

        # A user-selected original exporter cache contains already parsed JSON rows.
        if any(name == 'msg_rows' for name, _ in tables):
            from wechatbrain.qx_cache import extract_cache
            cached = extract_cache(conn, path, opts, ctx)
            if cached is not None: return cached

        # 微信专有结构优先：MSG / Chat_<md5> / Msg_<md5> + Name2Id
        if HAVE_PLATFORM and not opts.table:
            recs = parse_wechat_db(conn, path, min_len=opts.min_len, max_rows=opts.max_rows,
                                   keep_markup=opts.keep_markup, notes=ctx.notes,
                                   on_batch=(lambda rows: on_batch([_rec_to_message(r, path) for r in rows])) if on_batch else None,
                                   cancelled=cancelled, contact_cache=getattr(opts, "contact_cache", None),
                                   contact_scope=getattr(opts, "contact_scope", ""),
                                   reader_state=getattr(opts,'reader_state',None),
                                   on_reader_state=getattr(opts,'on_reader_state',None),
                                   peer_filter=getattr(opts,'peer_filter',None))
            if recs is not None:
                ctx.note("微信结构：%s（%d 条）" % (path.name, getattr(recs, "total", len(recs))))
                return [_rec_to_message(r, path) for r in recs]

        if getattr(opts,'wechat_only',False):
            return []

        for name, _sql in tables:
            if opts.table and name != opts.table:
                continue
            cols = table_columns(conn, name)
            if not cols:
                continue
            sc = score_table(name, cols)
            if not opts.table and not opts.all_tables and sc < 5:
                continue
            msgs.extend(_rows_to_messages(conn, name, cols, path, opts, ctx))
    except sqlite3.DatabaseError as exc:
        ctx.error("读取 %s 出错：%s" % (path.name, exc))
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return msgs


def _rows_to_messages(conn: sqlite3.Connection, table: str, cols: List[str],
                      path: Path, opts: argparse.Namespace, ctx: Ctx) -> List[Message]:
    low = [c.lower() for c in cols]
    i_content = pick(low, COL_CONTENT + COL_CONTENT_CN)
    i_sender = pick(low, COL_SENDER + COL_SENDER_CN)
    i_time = pick(low, COL_TIME + COL_TIME_CN)
    i_conv = pick(low, COL_CONV + COL_CONV_CN)
    i_self = pick(low, COL_SELF + COL_SELF_CN)
    # WeChat/QQ 常见：只有 StrTalker（会话），没有独立发送者列 → 交给 is_self 判定
    if i_sender is not None and i_conv is not None and i_sender == i_conv:
        i_sender = None
    if i_content is None:
        return []

    on_batch = getattr(opts, 'on_batch', None)
    if on_batch:
        from chatlog_platform import RecordBuffer
        out = RecordBuffer(on_batch, getattr(opts, 'cancelled', lambda: False))
    else:
        out = []
    try:
        cur = conn.execute("SELECT * FROM %s" % _q(table))
    except sqlite3.DatabaseError as exc:
        ctx.error("表 %s 不可读：%s" % (table, exc))
        return []
    names = [d[0] for d in cur.description]
    n = 0
    while True:
        rows = cur.fetchmany(2000)
        if not rows:
            break
        for row in rows:
            n += 1
            if n > opts.max_rows:
                ctx.note("表 %s 达到 --max-rows=%d 上限，截断" % (table, opts.max_rows))
                break
            content = clean_text(value_to_text(row[i_content]), opts.keep_markup)
            if len(content) < opts.min_len:
                continue
            ts_raw = value_to_text(row[i_time]) if i_time is not None else ""
            ts = parse_ts(ts_raw) if i_time is not None else None
            sender = clean_text(value_to_text(row[i_sender]), True) if i_sender is not None else ""
            conv = clean_text(value_to_text(row[i_conv]), True) if i_conv is not None else ""
            is_self = False
            if i_self is not None:
                raw_self = value_to_text(row[i_self]).strip().lower()
                is_self = raw_self in ("1", "true", "yes", "self", "out", "send")
            if not sender:
                sender = "我" if is_self else (conv or "对方")
            elif is_self:
                sender = "我"
            extra: Dict[str, Any] = {}
            for idx, cname in enumerate(names):
                cl = cname.lower()
                if cl in ("msgtype", "type", "msg_type", "subtype", "status"):
                    extra.setdefault("type", value_to_text(row[idx]))
            out.append(Message(
                source=str(path), conversation=conv or path.stem, sender=sender,
                ts=ts, ts_raw=ts_raw, content=content, is_self=is_self,
                kind="sqlite", table=table, extra=extra,
            ))
        if n > opts.max_rows:
            break
    if on_batch: out.flush()
    return out


# --------------------------------------------------------------------------- #
# 5. 文本 / 日志解析
# --------------------------------------------------------------------------- #

TS_FIND = re.compile(r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}[ T]\d{1,2}:\d{2}(?::\d{2})?(?:\.\d{1,6})?")
BANNER_RE = re.compile(r"^[\s=\-*#~_·—–]{3,}$|消息记录|聊天记录|聊天内容|导出时间|导出日期|会话记录")
SENT_PUNCT = set("，。！？；、,.!?;:\"“”‘’（）()")


def _is_banner(line: str) -> bool:
    """分隔线 / 导出说明行，不是消息。"""
    t = line.strip()
    if not t or TS_FIND.search(t):
        return False
    return bool(BANNER_RE.search(t))


def _senderish(s: str) -> bool:
    """判断一行像不像「昵称」。"""
    s = s.strip()
    if not s or len(s) > 24:
        return False
    if any(ch in SENT_PUNCT for ch in s):
        return False
    if s.count(" ") > 2:
        return False
    return True


def loose_records(text: str) -> List[Dict[str, str]]:
    """时间戳切块法：适配「时间 / 昵称 / 正文」各占一行的 HTML、网页导出。"""
    matches = list(TS_FIND.finditer(text))
    out: List[Dict[str, str]] = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[m.end():end]
        lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
        lines = [ln for ln in lines if not _is_banner(ln)]
        sender, content = "", ""
        if lines:
            if len(lines) >= 2 and _senderish(lines[0]):
                sender, content = lines[0], "\n".join(lines[1:])
            else:
                content = "\n".join(lines)
        if not content.strip():
            continue
        out.append({"ts": m.group(0), "sender": sender, "content": content})
    return out


def _match_line(line: str) -> Optional[Dict[str, str]]:
    for pat in LINE_PATTERNS[:2]:
        m = pat.match(line)
        if m:
            gd = m.groupdict()
            return {"ts": m.group(1), "sender": gd.get("sender") or "", "content": gd.get("content") or ""}
    for pat in LINE_PATTERNS_TS_ONLY:
        m = pat.match(line)
        if m:
            return {"ts": m.group(1), "sender": m.groupdict().get("sender") or "", "content": ""}
    m = LINE_PATTERNS[2].match(line)
    if m:
        sender, content = (m.group("sender") or "").strip(), (m.group("content") or "").strip()
        if content and _senderish(sender):
            return {"ts": "", "sender": sender, "content": content}
    return None


def extract_text(cand: Candidate, opts: argparse.Namespace, ctx: Ctx) -> List[Message]:
    path = cand.path
    try:
        text = read_text_auto(path)
    except OSError as exc:
        ctx.error("读取失败 %s：%s" % (path, exc))
        return []
    lines = text.splitlines()
    out: List[Message] = []
    cur: Optional[Dict[str, Any]] = None
    conv = path.parent.name if path.parent.name else path.stem

    def flush() -> None:
        if not cur:
            return
        content = clean_text(cur["content"], opts.keep_markup)
        if len(content) >= opts.min_len:
            ts = parse_ts(cur["ts"]) if cur["ts"] else None
            out.append(Message(
                source=str(path), conversation=conv, sender=cur["sender"] or "未知",
                ts=ts, ts_raw=cur["ts"], content=content, kind="text",
            ))

    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            if cur and cur["content"]:
                cur["content"] += "\n"
            continue
        if _is_banner(line):
            continue
        m = _match_line(line)
        if m:
            flush()
            cur = {"ts": m["ts"], "sender": m["sender"].strip(), "content": m["content"]}
        elif cur is not None:
            cur["content"] += ("\n" if cur["content"] else "") + line
        else:
            cur = {"ts": "", "sender": "", "content": line}
    flush()
    if not out:
        for rec in loose_records(text):
            content = clean_text(rec["content"], opts.keep_markup)
            if len(content) < opts.min_len:
                continue
            out.append(Message(
                source=str(path), conversation=conv, sender=rec["sender"] or "未知",
                ts=parse_ts(rec["ts"]), ts_raw=rec["ts"], content=content, kind="text",
            ))
    return out


# --------------------------------------------------------------------------- #
# 6. JSON 解析
# --------------------------------------------------------------------------- #

def extract_json(cand: Candidate, opts: argparse.Namespace, ctx: Ctx) -> List[Message]:
    path = cand.path
    out: List[Message] = []
    try:
        text = read_text_auto(path)
        if path.suffix.lower() in (".jsonl", ".ndjson"):
            data = []
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    data.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        else:
            data = json.loads(text)
    except (OSError, json.JSONDecodeError) as exc:
        ctx.error("JSON 解析失败 %s：%s" % (path.name, exc))
        return []

    collected: List[Dict[str, Any]] = []
    _walk_json(data, collected, depth=0)
    conv = path.stem
    for d in collected:
        content = clean_text(value_to_text(d.get("_content")), opts.keep_markup)
        if len(content) < opts.min_len:
            continue
        ts_raw = value_to_text(d.get("_ts"))
        is_self = str(d.get("_self", "")).strip().lower() in ("1", "true", "yes", "self", "out")
        out.append(Message(
            source=str(path),
            conversation=clean_text(value_to_text(d.get("_conv")), True) or conv,
            sender=clean_text(value_to_text(d.get("_sender")), True) or ("我" if is_self else "未知"),
            ts=parse_ts(ts_raw), ts_raw=ts_raw, content=content, is_self=is_self, kind="json",
        ))
    return out


CONV_HINT_KEYS = ("sessionname", "chatname", "chat_name", "conversation", "convname",
                  "roomname", "room_name", "groupname", "group_name", "title", "name",
                  "nickname", "displayname", "remark")


def _walk_json(node: Any, acc: List[Dict[str, Any]], conv: str = "", depth: int = 0) -> None:
    if depth > 24:
        return
    if isinstance(node, dict):
        low = {str(k).lower(): k for k in node.keys()}
        hit = set(low) & JSON_CONTENT_KEYS
        if hit:
            def _get(keys: Iterable[str]) -> Any:
                for k in keys:
                    if k in low:
                        v = node[low[k]]
                        if isinstance(v, (dict, list)):
                            return json.dumps(v, ensure_ascii=False)[:4000]
                        return v
                return None
            acc.append({
                "_content": _get(JSON_CONTENT_KEYS),
                "_sender": _get(JSON_SENDER_KEYS),
                "_ts": _get(JSON_TIME_KEYS),
                "_conv": _get(JSON_CONV_KEYS) or conv,
                "_self": _get(JSON_SELF_KEYS),
            })
            return
        hint = conv
        for k in CONV_HINT_KEYS:
            v = node.get(low[k]) if k in low else None
            if isinstance(v, str) and v.strip():
                hint = v.strip()
                break
        for v in node.values():
            _walk_json(v, acc, hint, depth + 1)
    elif isinstance(node, list):
        for v in node:
            _walk_json(v, acc, conv, depth + 1)


# --------------------------------------------------------------------------- #
# 7. HTML / CSV 解析
# --------------------------------------------------------------------------- #

BLOCK_TAGS = {"p", "div", "li", "tr", "br", "h1", "h2", "h3", "h4", "h5", "h6",
              "section", "article", "header", "footer", "blockquote", "pre"}


class _TextCollector(HTMLParser):
    def __init__(self) -> None:
        HTMLParser.__init__(self, convert_charrefs=True)
        self.parts: List[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)

    def text(self) -> str:
        s = "".join(self.parts)
        s = re.sub(r"[ \t\u00a0]+", " ", s)
        s = re.sub(r"\n\s*\n+", "\n", s)
        return s


def extract_html(cand: Candidate, opts: argparse.Namespace, ctx: Ctx) -> List[Message]:
    path = cand.path
    try:
        text = read_text_auto(path)
    except OSError as exc:
        ctx.error("读取失败 %s：%s" % (path, exc))
        return []
    p = _TextCollector()
    try:
        p.feed(text)
        p.close()
    except Exception:  # noqa: BLE001
        pass
    plain = p.text()
    title = _html_title(text) or path.stem
    out: List[Message] = []
    for rec in loose_records(plain):
        content = clean_text(rec["content"], opts.keep_markup)
        if len(content) < opts.min_len:
            continue
        out.append(Message(
            source=str(path), conversation=title, sender=rec["sender"] or "未知",
            ts=parse_ts(rec["ts"]), ts_raw=rec["ts"], content=content, kind="html",
        ))
    if out:
        return out
    tmp = Candidate(path=path, kind="text", size=cand.size)
    msgs = extract_text(tmp, opts, ctx)
    for m in msgs:
        m.kind = "html"
        m.conversation = title
    return msgs


def _html_title(text: str) -> str:
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", text)
    if not m:
        return ""
    return clean_text(re.sub(r"\s+", " ", m.group(1)), True)[:80]


def extract_csv(cand: Candidate, opts: argparse.Namespace, ctx: Ctx) -> List[Message]:
    path = cand.path
    try:
        text = read_text_auto(path)
    except OSError as exc:
        ctx.error("读取失败 %s：%s" % (path, exc))
        return []
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
    except csv.Error:
        dialect = csv.excel_tab if path.suffix.lower() == ".tsv" else csv.excel
    out: List[Message] = []
    reader = csv.DictReader(text.splitlines(), dialect=dialect)
    if not reader.fieldnames:
        return out
    low_map = {str(f).lower().strip(): f for f in reader.fieldnames}

    def col(aliases: Iterable[str]) -> Optional[str]:
        for a in aliases:
            for lk, orig in low_map.items():
                if lk == a or a in lk:
                    return orig
        return None

    c_content = col(COL_CONTENT + COL_CONTENT_CN)
    c_sender = col(COL_SENDER + COL_SENDER_CN)
    c_time = col(COL_TIME + COL_TIME_CN)
    c_conv = col(COL_CONV + COL_CONV_CN)
    c_self = col(COL_SELF + COL_SELF_CN)
    if not c_content:
        ctx.note("CSV 未找到内容列，跳过：%s（列：%s）" % (path.name, reader.fieldnames))
        return out
    for row in reader:
        content = clean_text(value_to_text(row.get(c_content)), opts.keep_markup)
        if len(content) < opts.min_len:
            continue
        ts_raw = value_to_text(row.get(c_time)) if c_time else ""
        is_self = value_to_text(row.get(c_self)).strip().lower() in ("1", "true", "yes", "self") if c_self else False
        out.append(Message(
            source=str(path),
            conversation=clean_text(value_to_text(row.get(c_conv)), True) if c_conv else path.stem,
            sender=clean_text(value_to_text(row.get(c_sender)), True) if c_sender else ("我" if is_self else "对方"),
            ts=parse_ts(ts_raw), ts_raw=ts_raw, content=content, is_self=is_self, kind="csv",
        ))
    return out


EXTRACTORS = {
    "sqlite": extract_sqlite,
    "text": extract_text,
    "json": extract_json,
    "html": extract_html,
    "csv": extract_csv,
}


# --------------------------------------------------------------------------- #
# 8. 过滤 / 去重 / 排序
# --------------------------------------------------------------------------- #

def filter_messages(msgs: List[Message], opts: argparse.Namespace) -> List[Message]:
    since = parse_ts(opts.since) if opts.since else None
    until = parse_ts(opts.until) if opts.until else None
    if since and until and until.hour == 0 and until.minute == 0 and until.second == 0:
        until = until.replace(hour=23, minute=59, second=59)
    kws = [normalize_text_for_match(k) for k in (opts.keyword or [])]
    convs = [normalize_text_for_match(c) for c in (opts.conversation or [])]
    senders = [normalize_text_for_match(s) for s in (opts.sender or [])]
    out: List[Message] = []
    for m in msgs:
        if since and m.ts and m.ts < since:
            continue
        if until and m.ts and m.ts > until:
            continue
        body = normalize_text_for_match(m.content)
        if kws and not any(k in body for k in kws):
            continue
        if convs and not any(c in normalize_text_for_match(m.conversation) for c in convs):
            continue
        if senders and not any(s in normalize_text_for_match(m.sender) for s in senders):
            continue
        out.append(m)
    return out


def dedup_messages(msgs: List[Message]) -> Tuple[List[Message], int]:
    seen = set()
    out: List[Message] = []
    dropped = 0
    for m in msgs:
        # 跨文件去重：同一时刻、同一发送者、同一内容视为重复（含备份目录副本）
        key = hashlib.sha1(("\x1f".join([
            m.sender,
            m.ts.strftime("%Y-%m-%d %H:%M:%S") if m.ts else "",
            m.content[:400],
        ])).encode("utf-8", "replace")).hexdigest()
        if key in seen:
            dropped += 1
            continue
        seen.add(key)
        out.append(m)
    return out, dropped


def sort_messages(msgs: List[Message], order: str = "time") -> List[Message]:
    if order == "source":
        return sorted(msgs, key=lambda m: (m.source, m.ts or datetime.max))
    return sorted(msgs, key=lambda m: (m.ts or datetime.max, m.source, m.table))


# --------------------------------------------------------------------------- #
# 9. 导出
# --------------------------------------------------------------------------- #

def export_jsonl(msgs: List[Message], path: Path) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for m in msgs:
            fh.write(json.dumps(m.to_dict(), ensure_ascii=False) + "\n")


def export_csv(msgs: List[Message], path: Path) -> None:
    cols = ["时间", "会话", "发送者", "内容", "是否本人", "来源文件", "来源类型", "来源表", "原始时间"]
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for m in msgs:
            w.writerow([
                m.ts.strftime("%Y-%m-%d %H:%M:%S") if m.ts else "",
                m.conversation, m.sender, m.content,
                "是" if m.is_self else "否",
                m.source, m.kind, m.table, m.ts_raw,
            ])


def export_xlsx(msgs: List[Message], path: Path) -> bool:
    """导出 Excel（需要 openpyxl；缺失时返回 False）。"""
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter
    except ImportError:
        return False
    wb = Workbook()
    ws = wb.active
    ws.title = "聊天记录"
    cols = ["时间", "会话", "发送者", "内容", "是否本人", "来源文件", "来源类型"]
    ws.append(cols)
    head_fill = PatternFill("solid", fgColor="1F4E79")
    for c in range(1, len(cols) + 1):
        cell = ws.cell(row=1, column=c)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = head_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")
    for m in msgs:
        ws.append([
            m.ts.strftime("%Y-%m-%d %H:%M:%S") if m.ts else (m.ts_raw or ""),
            m.conversation, m.sender, m.content,
            "是" if m.is_self else "否", os.path.basename(m.source), m.kind,
        ])
    widths = [19, 18, 14, 70, 8, 24, 10]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    for row in ws.iter_rows(min_row=2, max_col=len(cols)):
        row[3].alignment = Alignment(wrap_text=True, vertical="top")
        for cell in row:
            cell.alignment = Alignment(vertical="top",
                                       wrap_text=(cell.column == 4),
                                       horizontal="center" if cell.column in (1, 5, 7) else "left")
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = "A1:%s%d" % (get_column_letter(len(cols)), ws.max_row)
    wb.save(str(path))
    return True


def export_markdown(msgs: List[Message], path: Path, title: str) -> None:
    by_conv: Dict[str, List[Message]] = {}
    for m in msgs:
        by_conv.setdefault(m.conversation or "未命名会话", []).append(m)
    lines = ["# %s" % title, "", "- 导出时间：%s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             "- 消息总数：%d" % len(msgs), "- 会话数：%d" % len(by_conv), ""]
    for conv, items in by_conv.items():
        lines.append("## %s（%d 条）" % (conv, len(items)))
        lines.append("")
        for m in items:
            t = m.ts.strftime("%Y-%m-%d %H:%M:%S") if m.ts else (m.ts_raw or "时间未知")
            body = m.content.replace("\n", "  \n")
            lines.append("- **[%s] %s：** %s" % (t, m.sender or "未知", body))
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


HTML_HEAD = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--tx:#1d2129;--sub:#86909c;--line:#e5e6eb;--me:#d9f0ff;--them:#fff;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--tx);font:14px/1.65 -apple-system,"PingFang SC","Microsoft YaHei",Segoe UI,sans-serif}
header{position:sticky;top:0;z-index:5;background:#fff;border-bottom:1px solid var(--line);padding:12px 16px}
h1{font-size:16px;margin:0 0 8px}
.bar{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
input,select{padding:6px 10px;border:1px solid var(--line);border-radius:6px;font-size:13px;background:#fff}
input[type=search]{flex:1;min-width:180px}
.stat{color:var(--sub);font-size:12px}
main{max-width:900px;margin:0 auto;padding:16px}
.conv{margin:0 0 10px;font-size:13px;color:var(--sub);font-weight:600}
.msg{display:flex;margin:0 0 10px}
.msg .b{max-width:78%;background:var(--them);border:1px solid var(--line);border-radius:10px;padding:8px 12px;white-space:pre-wrap;word-break:break-word}
.msg.me{justify-content:flex-end}
.msg.me .b{background:var(--me);border-color:#b8e0f5}
.meta{font-size:11px;color:var(--sub);margin:0 0 3px;display:flex;gap:8px}
.msg.me .meta{justify-content:flex-end}
mark{background:#ffe58f;padding:0 1px}
.empty{color:var(--sub);text-align:center;padding:40px 0}
</style></head><body>
<header>
  <h1>__TITLE__</h1>
  <div class="bar">
    <input type="search" id="q" placeholder="搜索内容 / 发送者…">
    <select id="conv"><option value="">全部会话</option>__CONVOPT__</select>
    <span class="stat" id="stat">__STAT__</span>
  </div>
</header>
<main id="list">
"""

HTML_TAIL = """
</main>
<script>
const q=document.getElementById('q'),conv=document.getElementById('conv'),list=document.getElementById('list');
const items=[...list.querySelectorAll('.msg')];
function apply(){
  const kw=q.value.trim().toLowerCase(),c=conv.value;let n=0;
  items.forEach(el=>{
    const okConv=!c||el.dataset.conv===c;
    const okKw=!kw||el.dataset.search.includes(kw);
    const show=okConv&&okKw;el.style.display=show?'':'none';if(show)n++;
  });
  document.getElementById('stat').textContent='显示 '+n+' / '+items.length+' 条';
}
q.addEventListener('input',apply);conv.addEventListener('change',apply);apply();
</script>
</body></html>
"""


def export_html(msgs: List[Message], path: Path, title: str, group: bool = True) -> None:
    if group:
        # 按会话聚合，会话内保持时间顺序，避免同一会话的表头反复出现
        msgs = sorted(msgs, key=lambda m: (m.conversation or "", m.ts or datetime.max, m.source))
    convs: List[str] = []
    for m in msgs:
        c = m.conversation or "未命名会话"
        if c not in convs:
            convs.append(c)
    opts_html = "".join('<option value="%s">%s</option>' % (html_mod.escape(c, quote=True), html_mod.escape(c))
                        for c in convs)
    parts: List[str] = []
    last_conv = None
    for m in msgs:
        conv = m.conversation or "未命名会话"
        if conv != last_conv:
            parts.append('<div class="conv">%s</div>' % html_mod.escape(conv))
            last_conv = conv
        t = m.ts.strftime("%Y-%m-%d %H:%M:%S") if m.ts else (m.ts_raw or "")
        cls = "msg me" if m.is_self else "msg"
        search = (m.sender + " " + m.content).casefold()
        parts.append(
            '<div class="%s" data-conv="%s" data-search="%s">'
            '<div class="b"><div class="meta"><span>%s</span><span>%s</span></div>%s</div></div>'
            % (cls, html_mod.escape(conv, quote=True), html_mod.escape(search, quote=True),
               html_mod.escape(m.sender or "未知"), html_mod.escape(t),
               html_mod.escape(m.content))
        )
    if not parts:
        parts.append('<div class="empty">没有匹配的消息</div>')
    stat = "共 %d 条 / %d 个会话" % (len(msgs), len(convs))
    doc = (HTML_HEAD.replace("__TITLE__", html_mod.escape(title))
           .replace("__CONVOPT__", opts_html)
           .replace("__STAT__", html_mod.escape(stat))) + "\n".join(parts) + HTML_TAIL
    path.write_text(doc, encoding="utf-8")


# --------------------------------------------------------------------------- #
# 10. 主流程
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="chatlog_extract",
        description="从指定聊天文件夹提取聊天记录（SQLite/文本/JSON/HTML/CSV 自动识别）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n"
               "  python3 chatlog_extract.py --src ./WeChat_Files --out out\n"
               "  python3 chatlog_extract.py --src ./Msg --schemas\n"
               "  python3 chatlog_extract.py --src ./Msg --since 2024-01-01 --keyword 发票\n",
    )
    p.add_argument("--src", nargs="+", action="append", required=True,
                   help="聊天文件夹（可多个：--src A B 或重复 --src）")
    p.add_argument("--out", default="chatlog_out", help="输出目录（默认 chatlog_out）")
    p.add_argument("--since", help="起始时间，如 2024-01-01 或 '2024-01-01 08:00'")
    p.add_argument("--until", help="结束时间")
    p.add_argument("--keyword", action="append", help="关键字过滤（可重复，任一命中）")
    p.add_argument("--conversation", action="append", help="会话名过滤（包含匹配）")
    p.add_argument("--sender", action="append", help="发送者过滤（包含匹配）")
    p.add_argument("--min-len", type=int, default=0, help="正文最短长度，过滤空消息")
    p.add_argument("--max-rows", type=int, default=500000, help="每张表最多读取行数")
    p.add_argument("--max-file-mb", type=float, default=512, help="单文件大小上限(MB)")
    p.add_argument("--all-tables", action="store_true", help="不按评分过滤，扫描全部表")
    p.add_argument("--table", help="只解析指定表")
    p.add_argument("--enc-key", help="SQLCipher 密钥（原始密码或 0x 开头的十六进制密钥）")
    p.add_argument("--platform", choices=["auto", "win", "ios", "mac"], default="auto",
                   help="来源平台提示：auto/win/ios/mac（默认 auto 自动识别）")
    p.add_argument("--backup-password", help="iPhone 加密备份的备份密码（只本地参与 keybag 推导）")
    p.add_argument("--workdir", help="iPhone 备份解包目录（默认系统临时目录，跑完自动清理）")
    p.add_argument("--keep-work", action="store_true", help="保留解包出来的临时文件")
    p.add_argument("--probe", action="store_true", help="只体检来源目录（账号/库/是否加密），不解析")
    p.add_argument("--keep-markup", action="store_true", help="保留 XML/HTML 标记原文")
    p.add_argument("--no-dedup", action="store_true", help="关闭去重")
    p.add_argument("--order", choices=["time", "source"], default="time", help="排序方式")
    p.add_argument("--group-by", choices=["conversation", "time"], default="conversation",
                   help="HTML 报告的排列方式（默认按会话聚合）")
    p.add_argument("--formats", default="jsonl,csv,md,html", help="输出格式，逗号分隔：jsonl,csv,md,html,xlsx")
    p.add_argument("--scan-only", action="store_true", help="只列出发现的数据文件")
    p.add_argument("--schemas", action="store_true", help="打印每个库的表结构与命中评分")
    p.add_argument("--quiet", action="store_true", help="减少终端输出")
    return p


def human(n: int) -> str:
    return "%.1f KB" % (n / 1024) if n < 1048576 else "%.1f MB" % (n / 1048576)


def _prepare_ios_backup(layout: Any, opts: argparse.Namespace, ctx: Ctx,
                        temp_dirs: List[Path]) -> Optional[Path]:
    """把备份里的微信库解出来放到工作目录，返回可扫描目录。"""
    if getattr(opts, "workdir", None):
        base = Path(os.path.expanduser(opts.workdir)).resolve()
        base.mkdir(parents=True, exist_ok=True)
    else:
        base = Path(tempfile.mkdtemp(prefix="chatlog_ios_"))
        temp_dirs.append(base)
    backup = IOSBackup(layout.root, getattr(opts, "backup_password", None), ctx.notes)
    if backup.encrypted and not backup.password:
        ctx.error("iPhone 备份已加密：%s，需要 --backup-password" % layout.root)
        return None
    if not backup.unlock():
        reason = next((n for n in reversed(ctx.notes) if "解锁失败" in n), "")
        ctx.error("iPhone 备份解锁失败：%s%s（密码错或备份版本不支持）"
                  % (layout.root, ("；" + reason) if reason else ""))
        return None
    dest = base / "ios_wechat"
    files = backup.extract_wechat(dest)
    dev = getattr(backup, "device", {}) or {}
    if not files:
        ctx.error("备份里没找到微信库：%s（可能是别的 App 的备份，或微信从未登录）" % layout.root)
        return None
    ctx.note("iPhone 备份解包：%s %s → %d 个库文件（%s）"
             % (dev.get("name", ""), dev.get("product", ""), len(files), dest))
    return dest


def _print_probe(layouts: List[Any], opts: argparse.Namespace, ctx: Ctx) -> None:
    if not HAVE_PLATFORM:
        print("缺少 chatlog_platform.py，无法体检来源目录。")
        return
    if not layouts:
        print("没有识别到微信目录 / iPhone 备份，会按普通聊天文件夹扫描。")
        return
    for L in layouts:
        print("== %s ==" % L.root)
        print("  类型：%s   账号：%s" % (L.detail or L.kind, L.account or "-"))
        if L.kind.startswith("win"):
            info = summarize_wechat_dir(L.root)
            dbs = info.get("databases") or []
            print("  消息库：%d 个（其中加密 %d 个）" % (len(dbs), info.get("encrypted", 0)))
            for db in dbs:
                print("    %-4s %6.1fMB  %s" % ("加密" if db["encrypted"] else "明文",
                                               db.get("mb", 0), db["path"]))
            for c in info.get("contact_dbs") or []:
                print("    联系人库：%s" % c)
            if info.get("encrypted"):
                print("  下一步：加密库需要 --enc-key；没有密钥时先用你惯用的工具导出明文库再指向导出目录。")
        elif L.kind == "ios-backup":
            info = ios_backup_summary(L.root, getattr(opts, "backup_password", None), ctx.notes)
            dev = info.get("device") or {}
            print("  设备：%s %s" % (dev.get("name", "-"), dev.get("product", "-")))
            print("  加密：%s   微信库文件：%d 个"
                  % ("是" if info.get("encrypted") else "否", info.get("wechat_files", 0)))
            if info.get("encrypted") and not info.get("unlocked"):
                print("  下一步：加 --backup-password 再跑，密码只用于本地解锁。")
        elif L.kind == "mac":
            print("  macOS 微信容器：库通常在 Message/ 下，加密时同样需要 --enc-key。")


KEY_FILE_NAMES = ("key.txt", "keys.json", "enc_key.txt", "db_key.txt", "dbkey.txt")


def _as_raw_key(token: str) -> str:
    """64 位十六进制 = 32 字节 raw key，统一加 0x 前缀。"""
    token = token.strip()
    return token if token.lower().startswith("0x") else "0x" + token


def _discover_key(roots: List[Path]) -> Tuple[str, str]:
    """在聊天目录里找 key.txt / keys.json / config.ini(key=...)：返回 (路径, 密钥)。"""
    hex64 = re.compile(r"(?:0x)?[0-9a-fA-F]{64}")
    for r in roots:
        base = r if r.is_dir() else r.parent
        for name in KEY_FILE_NAMES + ("config.ini",):
            f = base / name
            if not f.is_file() or f.stat().st_size > 262144:
                continue
            try:
                text = f.read_text(encoding="utf-8-sig", errors="ignore")
            except OSError:
                continue
            if name.lower() == "keys.json":
                try:
                    data = json.loads(text)
                except ValueError:
                    continue
                stack = [data]
                while stack:
                    cur = stack.pop()
                    if isinstance(cur, str):
                        m = hex64.search(cur)
                        if m:
                            return str(f), _as_raw_key(m.group(0))
                    elif isinstance(cur, dict):
                        stack.extend(cur.values())
                    elif isinstance(cur, list):
                        stack.extend(cur)
                continue
            if name.lower() == "config.ini":
                for line in text.splitlines():
                    k, _, v = line.partition("=")
                    if k.strip().lower() in ("key", "enc_key", "db_key"):
                        m = hex64.search(v)
                        if m:
                            return str(f), _as_raw_key(m.group(0))
                continue
            m = hex64.search(text)
            if m:
                return str(f), _as_raw_key(m.group(0))
    return "", ""


def main(argv: Optional[List[str]] = None) -> int:
    opts = build_parser().parse_args(argv)
    ctx = Ctx()
    roots: List[Path] = []
    srcs: List[str] = [x for group in opts.src for x in group]
    for s in srcs:
        r = Path(os.path.expanduser(s)).resolve()
        if not r.exists():
            ctx.error("路径不存在：%s" % r)
            continue
        if r.is_file():
            roots.append(r)
        else:
            roots.append(r)
    if not roots:
        print("没有可扫描的路径。", file=sys.stderr)
        return 2

    if not opts.enc_key:
        key_file, key_val = _discover_key(roots)
        if key_file:
            opts.enc_key = key_val
            ctx.note("已从 %s 读取密钥（%s…）" % (key_file, key_val[:8]))

    # ---- 来源识别：Windows 微信目录 / iPhone 备份 ----
    layouts: List[Any] = []
    temp_dirs: List[Path] = []
    scan_roots: List[Any] = [(r, ()) for r in roots]
    if HAVE_PLATFORM:
        for r in roots:
            if r.is_file():
                continue
            found = detect_layouts(r)
            if opts.platform == "win":
                found = [L for L in found if L.kind.startswith("win")]
            elif opts.platform == "ios":
                found = [L for L in found if L.kind == "ios-backup"]
            elif opts.platform == "mac":
                found = [L for L in found if L.kind == "mac"]
            layouts.extend(found)
        if opts.probe:
            _print_probe(layouts, opts, ctx)
            _print_notes(ctx)
            return 0
        if layouts:
            scan_roots = []
            for r in roots:
                # 只有当 root 本身是微信容器/备份目录时才收窄扫描范围；
                # 混放目录（比如 fixtures 里同时有 WeChat Files 和 QQ 导出）保持原样全扫。
                container = (r.name in ("WeChat Files", "xwechat_files", "Backup")
                             or (r / "Backup").is_dir() or str(r).endswith("MobileSync"))
                handled = [L for L in layouts
                           if L.root == r or (container and str(L.root).startswith(str(r)))]
                if not handled:
                    scan_roots.append((r, ()))
                    continue
                for L in handled:
                    if L.kind == "ios-backup":
                        dest = _prepare_ios_backup(L, opts, ctx, temp_dirs)
                        if dest is not None:
                            scan_roots.append((dest, ()))
                    else:
                        if not opts.quiet:
                            print("识别到 %s：%s（账号 %s）"
                                  % (L.detail or L.kind, L.root, L.account or "-"))
                        ctx.note("来源识别：%s，账号 %s（%s）"
                                 % (L.detail or L.kind, L.account or "-", L.root))
                        skip = WECHAT_MEDIA_DIRS if L.kind.startswith("win") else ()
                        scan_roots.append((L.root, skip))
    elif opts.probe:
        _print_probe([], opts, ctx)
        return 0

    cands: List[Candidate] = []
    seen_paths = set()
    for r, extra_skip in scan_roots:
        if r.is_file():
            k = kind_of(r)
            if k and str(r) not in seen_paths:
                seen_paths.add(str(r))
                cands.append(Candidate(r, k.rstrip("?"), r.stat().st_size))
        else:
            for c in discover(r, ctx, opts.max_file_mb, extra_skip):
                if str(c.path) not in seen_paths:
                    seen_paths.add(str(c.path))
                    cands.append(c)

    if not opts.quiet:
        print("发现 %d 个候选数据文件：" % len(cands))
        for c in cands:
            print("  [%-6s] %8s  %s" % (c.kind, human(c.size), c.path))
        print()

    if opts.scan_only:
        if opts.schemas:
            opts.schema_only = True
            for c in cands:
                if c.kind == "sqlite":
                    extract_sqlite(c, opts, ctx)
            if ctx.schemas:
                print("\n".join(ctx.schemas))
        _print_notes(ctx)
        return 0

    all_msgs: List[Message] = []
    for c in cands:
        fn = EXTRACTORS.get(c.kind)
        if not fn:
            continue
        try:
            got = fn(c, opts, ctx)
        except Exception as exc:  # noqa: BLE001
            ctx.error("解析异常 %s：%s" % (c.path, exc))
            if os.environ.get("CHATLOG_DEBUG"):
                traceback.print_exc()
            continue
        ctx.files.append((str(c.path), c.kind, len(got)))
        all_msgs.extend(got)
        if not opts.quiet:
            print("  %-8s %6d 条  %s" % (c.kind, len(got), c.path))

    if opts.schemas and ctx.schemas:
        print("\n".join(ctx.schemas))

    before = len(all_msgs)
    msgs = filter_messages(all_msgs, opts)
    if not opts.no_dedup:
        msgs, dropped = dedup_messages(msgs)
    else:
        dropped = 0
    msgs = sort_messages(msgs, opts.order)

    out_dir = Path(os.path.expanduser(opts.out)).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    title = "聊天记录导出 · %s" % (roots[0].name if roots else "chatlog")
    formats = [f.strip().lower() for f in opts.formats.split(",") if f.strip()]
    written: List[Path] = []
    if "jsonl" in formats or "json" in formats:
        p = out_dir / "messages.jsonl"
        export_jsonl(msgs, p)
        written.append(p)
    if "csv" in formats:
        p = out_dir / "messages.csv"
        export_csv(msgs, p)
        written.append(p)
    if "md" in formats or "markdown" in formats:
        p = out_dir / "messages.md"
        export_markdown(msgs, p, title)
        written.append(p)
    if "html" in formats:
        p = out_dir / "messages.html"
        export_html(msgs, p, title, group=(opts.group_by == "conversation"))
        written.append(p)
    if "xlsx" in formats or "excel" in formats:
        p = out_dir / "messages.xlsx"
        if export_xlsx(msgs, p):
            written.append(p)
        else:
            ctx.note("未安装 openpyxl，跳过 xlsx 导出（pip install openpyxl 后重试）")

    convs = {}
    for m in msgs:
        convs[m.conversation] = convs.get(m.conversation, 0) + 1
    summary = {
        "scanned_roots": [str(r) for r in roots],
        "sources": [{"kind": L.kind, "root": str(L.root), "account": L.account,
                     "detail": L.detail} for L in layouts],
        "candidate_files": len(cands),
        "parsed_files": [{"path": p, "kind": k, "messages": n} for p, k, n in ctx.files],
        "messages_raw": before,
        "messages_exported": len(msgs),
        "filtered_out": before - len(msgs) - dropped,
        "duplicates_dropped": dropped,
        "conversations": dict(sorted(convs.items(), key=lambda kv: -kv[1])),
        "time_range": {
            "from": min((m.ts for m in msgs if m.ts), default=None).strftime("%Y-%m-%d %H:%M:%S")
            if any(m.ts for m in msgs) else "",
            "to": max((m.ts for m in msgs if m.ts), default=None).strftime("%Y-%m-%d %H:%M:%S")
            if any(m.ts for m in msgs) else "",
        },
        "outputs": [str(p) for p in written],
        "notes": ctx.notes,
        "errors": ctx.errors,
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    written.append(out_dir / "summary.json")

    if not opts.quiet:
        _print_notes(ctx)
        print("\n解析 %d 个文件，原始消息 %d 条 → 导出 %d 条（去重 %d，过滤 %d）"
              % (len(ctx.files), before, len(msgs), dropped, before - len(msgs) - dropped))
        for p in written:
            print("  写出：%s" % p)
    if getattr(ctx, "temp_files", None) and not opts.keep_work:
        for f in ctx.temp_files:
            try:
                f.unlink()
            except OSError:
                pass
    if temp_dirs and not opts.keep_work:
        for d in temp_dirs:
            shutil.rmtree(d, ignore_errors=True)
    if msgs:
        return 0
    return 1 if ctx.errors else 0


def _print_notes(ctx: Ctx) -> None:
    if ctx.notes:
        print("\n提示：")
        for n in ctx.notes:
            print("  - %s" % n)
    if ctx.errors:
        print("\n问题：")
        for e in ctx.errors:
            print("  - %s" % e)


if __name__ == "__main__":
    sys.exit(main())
