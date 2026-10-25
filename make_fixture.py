#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成合成聊天记录测试目录（不含任何真实数据），用于验证 chatlog_extract.py。"""
import json
import os
import random
import sqlite3
from pathlib import Path

ROOT = Path(__file__).parent / "fixtures" / "chat_src"
random.seed(7)


def w(path: Path, text: str, encoding="utf-8"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding=encoding)


def make_wechat_db():
    p = ROOT / "WeChat Files" / "wxid_demo01" / "Msg"
    p.mkdir(parents=True, exist_ok=True)
    db = p / "MSG0.db"
    if db.exists():
        db.unlink()
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE message (
            localId  INTEGER PRIMARY KEY,
            StrTalker TEXT,     -- 会话/群
            IsSender  INTEGER,  -- 1=自己
            CreateTime INTEGER, -- 秒级时间戳
            StrContent TEXT,
            Type INTEGER,
            BytesExtra BLOB
        );
        CREATE TABLE ChatInfo (UserName TEXT, Remark TEXT, NickName TEXT);
        CREATE TABLE ContactHeadImgUrl (usrName TEXT, url TEXT);
        """
    )
    rows = [
        ("文件传输助手", 1, 1709251933, "把周报发我一下", 1),
        ("文件传输助手", 0, 1709251990, "好的，稍等", 1),
        ("产品讨论群", 0, 1709260000, "新版聊天记录导出功能今天上线了吗？", 1),
        ("产品讨论群", 1, 1709260120, "灰度中，明天全量。", 1),
        ("产品讨论群", 0, 1709260200, "<msg><appmsg><title>周报-第9周.pdf</title>"
                                     "<des>大小 1.2MB</des></appmsg></msg>", 49),
        ("张三", 0, 1709348400, "发票抬头开公司还是个人？", 1),
        ("张三", 1, 1709348520, "开公司，税号 91310000XXXXXXXXXX", 1),
        ("李四", 1, 1709434800, "明天下午三点评审，会议室 A", 1),
        ("李四", 0, 1709434860, "收到", 1),
    ]
    conn.executemany(
        "INSERT INTO message(StrTalker,IsSender,CreateTime,StrContent,Type,BytesExtra) VALUES(?,?,?,?,?,?)",
        [(a, b, c, d, e, bytes([1, 2, 3]) + d.encode("utf-8")[:8]) for a, b, c, d, e in rows],
    )
    conn.executemany("INSERT INTO ChatInfo VALUES(?,?,?)",
                     [("wxid_demo01", "自己", "我"), ("zhangsan_88", "", "张三")])
    conn.commit()
    conn.close()
    return db


def make_qq_txt():
    txt = "\n".join([
        "===== 消息记录（QQ 导出）=====",
        "[2024-03-01 09:12:33] 小明: 早上好，昨天的需求文档看了吗",
        "[2024-03-01 09:13:02] 我: 看了，第三节的字段定义要再对一下",
        "[2024-03-01 09:13:40] 小明: 好，我下午改完发你",
        "这条是没有前缀的续行，应当并入上一条消息",
        "[2024-03-01 09:15:10] 我: ok",
        "[2024-03-02 20:41:07] 小明: 改好了，见附件",
    ])
    w(ROOT / "QQ" / "10001" / "chat_2024-03.txt", txt)


def make_json_export():
    data = {
        "exportInfo": {"app": "DemoIM", "version": "2.3.0"},
        "sessions": [
            {
                "sessionName": "运营小组",
                "records": [
                    {"createTime": 1709352000000, "sender": "王五", "content": "双十一活动页文案定稿了吗", "isSend": 0},
                    {"createTime": 1709352060000, "sender": "我", "content": "定稿了，走审核流程", "isSend": 1},
                ],
            },
            {
                "sessionName": "客户-南方贸易",
                "records": [
                    {"createTime": 1709438400000, "sender": "陈经理",
                     "content": "合同第二版已盖章，扫描件发您邮箱", "isSend": 0},
                ],
            },
        ],
    }
    w(ROOT / "export" / "demo_im_export.json", json.dumps(data, ensure_ascii=False, indent=2))


def make_html_export():
    doc = """<!doctype html><html><head><meta charset="utf-8"><title>钉钉聊天记录</title></head><body>
<div class="msg-item"><div class="time">2024-03-05 10:00:12</div><div class="name">赵六</div>
<div class="text">服务器扩容申请批了吗？</div></div>
<div class="msg-item"><div class="time">2024-03-05 10:02:44</div><div class="name">我</div>
<div class="text">批了，今晚 22:00 开始迁移</div></div>
<div class="msg-item"><div class="time">2024-03-05 10:03:01</div><div class="name">赵六</div>
<div class="text">好，我盯着监控</div></div>
</body></html>"""
    w(ROOT / "export" / "dingtalk_chat.html", doc)


def make_csv_export():
    w(ROOT / "export" / "feishu_chat.csv",
      "发送时间,发送人,消息内容,是否本人\n"
      "2024-03-06 14:20:00,孙七,周会纪要我整理好了,0\n"
      "2024-03-06 14:21:30,我,辛苦，发群里吧,1\n"
      "2024-03-06 14:22:05,孙七,已发,0\n")


def make_encrypted_like():
    p = ROOT / "WeChat Files" / "wxid_demo01" / "Msg" / "MSG1.db"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(bytes(random.getrandbits(8) for _ in range(4096)))
    return p


def make_decoy():
    w(ROOT / "WeChat Files" / "wxid_demo01" / "Msg" / "config.ini",
      "[general]\nlast_session=产品讨论群\n")




# =========================================================================== #
# 以下为 Windows 微信 / iPhone 备份 合成样本（全部虚构，不含真实聊天数据）
# =========================================================================== #

import hashlib
import plistlib
import struct
import sys

FIX = Path(__file__).parent / "fixtures"
sys.path.insert(0, str(Path(__file__).parent))
import chatlog_platform as cp  # noqa: E402


def _db(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    return sqlite3.connect(path)


def make_win3():
    """Windows 微信 3.x：WeChat Files/<wxid>/Msg/Multi/MSG0.db"""
    acct = FIX / "win3" / "WeChat Files" / "wxid_win3demo"
    msg_dir = acct / "Msg" / "Multi"
    conn = _db(msg_dir / "MSG0.db")
    conn.execute("""CREATE TABLE MSG (
        localId INTEGER PRIMARY KEY, TalkerId INTEGER, MsgSvrID INTEGER, Type INTEGER,
        SubType INTEGER, IsSender INTEGER, CreateTime INTEGER, Sequence INTEGER,
        StatusEx INTEGER, FlagEx INTEGER, Status INTEGER, MsgServerSeq INTEGER,
        MsgSequence INTEGER, StrTalker TEXT, StrContent TEXT, DisplayContent TEXT,
        Reserved0 INTEGER, CompressContent TEXT, BytesExtra BLOB, BytesTrans BLOB)""")
    g = "12345678@chatroom"
    rows = [
        (1, g, 0, 1711000000, "wxid_lilei:\n这版什么时候发？", 1),
        (1, g, 1, 1711000060, "周五发，今天在灰度。", 1),
        (1, g, 0, 1711000120, "", 3),
        (1, g, 0, 1711000180, "<msg><appmsg><title>第9周周报.pdf</title><des>1.2MB</des></appmsg></msg>", 49),
        (0, "张三", 0, 1711001000, "发票抬头开公司还是个人？", 1),
        (1, "张三", 1, 1711001060, "开公司，税号 91310000XXXXXXXXXX", 1),
        (0, "张三", 0, 1711001120, "", 34),
        (1, g, 0, 1711002000, "wxid_wangwu:\n我这边测完了，没发现阻塞。", 1),
        (1, g, 1, 1711002060, "收到，我合并一下。", 1),
        (0, "李四", 0, 1711003000, "明天下午三点评审，会议室 A", 1),
    ]
    conn.executemany(
        "INSERT INTO MSG(TalkerId,StrTalker,IsSender,CreateTime,StrContent,Type) "
        "VALUES(?,?,?,?,?,?)",
        rows)
    conn.commit()
    conn.close()

    c = _db(acct / "Msg" / "Contact" / "contact.db")
    c.execute("CREATE TABLE Contact (userName TEXT, nickName TEXT, remark TEXT)")
    c.execute("CREATE TABLE ChatRoom (ChatRoomName TEXT, NickName TEXT, Remark TEXT)")
    c.executemany("INSERT INTO Contact VALUES(?,?,?)", [
        ("wxid_lilei", "李雷", "李雷-产品"),
        ("wxid_wangwu", "王五", ""),
        ("zhangsan_88", "张三", "张三-财务"),
        ("lisi_99", "李四", "李四-研发"),
    ])
    c.execute("INSERT INTO ChatRoom VALUES(?,?,?)", ("12345678@chatroom", "项目群", "项目群"))
    c.commit()
    c.close()
    return acct


def make_win4():
    """Windows 微信 4.x：xwechat_files/<wxid>/db_storage/message/message_0.db"""
    acct = FIX / "win4" / "xwechat_files" / "wxid_win4demo"
    conn = _db(acct / "db_storage" / "message" / "message_0.db")
    conn.execute("CREATE TABLE Name2Id (user_name TEXT)")
    conn.executemany("INSERT INTO Name2Id(user_name) VALUES(?)",
                     [("wxid_win4demo",), ("wxid_zhaoliu",), ("wxid_sunqi",),
                      ("wxid_self",), ("98765432@chatroom",)])
    conv = "wxid_zhaoliu"
    table = "Msg_" + hashlib.md5(conv.encode()).hexdigest()
    conn.execute(f'''CREATE TABLE "{table}" (
        local_id INTEGER PRIMARY KEY, server_id INTEGER, local_type INTEGER,
        sort_seq INTEGER, real_sender_id INTEGER, create_time INTEGER,
        message_content TEXT, compress_content TEXT, status INTEGER)''')
    rows = [
        (1, 1711100000, 1, "报价单第三版看了吗？", 2),      # 对方
        (2, 1711100060, 1, "看了，毛利那栏要再核一下。", 1),  # 自己
        (3, 1711100120, 3, "", 2),                          # 图片
        (4, 1711100180, 49, "<msg><appmsg><title>报价单v3.xlsx</title></appmsg></msg>", 2),
        (5, 1711101000, 1, "核完了，发你。", 1),
    ]
    conn.executemany(f'INSERT INTO "{table}"(local_id,create_time,local_type,message_content,real_sender_id) '
                     "VALUES(?,?,?,?,?)", rows)
    group = "98765432@chatroom"
    gtable = "Msg_" + hashlib.md5(group.encode()).hexdigest()
    conn.execute(f'''CREATE TABLE "{gtable}" (
        local_id INTEGER PRIMARY KEY, server_id INTEGER, local_type INTEGER,
        sort_seq INTEGER, real_sender_id INTEGER, create_time INTEGER,
        message_content TEXT, compress_content TEXT, status INTEGER)''')
    conn.executemany(f'INSERT INTO "{gtable}"(local_id,create_time,local_type,message_content,real_sender_id) '
                     "VALUES(?,?,?,?,?)", [
                         (1, 1711102000, 1, "周五上线，注意回归。", 3),
                         (2, 1711102060, 1, "收到。", 1),
                         (3, 1711102120, 34, "", 3),
                     ])
    conn.commit()
    conn.close()

    c = _db(acct / "db_storage" / "contact" / "contact.db")
    c.execute("CREATE TABLE contact (username TEXT, nick_name TEXT, remark TEXT)")
    c.executemany("INSERT INTO contact VALUES(?,?,?)", [
        ("wxid_zhaoliu", "赵六", "赵六-供应商"),
        ("wxid_sunqi", "孙七", ""),
        ("98765432@chatroom", "上线群", "上线群"),
    ])
    c.commit()
    c.close()
    return acct


def _make_ios_dbs(base: Path):
    """生成 iOS 微信库：MM.sqlite（Chat_<md5> 表）+ WCDB_Contact.sqlite。"""
    base.mkdir(parents=True, exist_ok=True)
    mm = base / "MM.sqlite"
    if mm.exists():
        mm.unlink()
    conn = sqlite3.connect(mm)
    contacts = {"wxid_ios_a": "周八", "wxid_ios_b": "吴九", "11223344@chatroom": "家人群"}
    a = "Chat_" + hashlib.md5(b"wxid_ios_a").hexdigest()
    b = "Chat_" + hashlib.md5(b"wxid_ios_b").hexdigest()
    g = "Chat_" + hashlib.md5(b"11223344@chatroom").hexdigest()
    for t in (a, b, g):
        conn.execute(f'''CREATE TABLE "{t}" (MesLocalID INTEGER PRIMARY KEY, MesSvrID INTEGER,
            CreateTime INTEGER, Message BLOB, Status INTEGER, Type INTEGER, Des INTEGER)''')
    conn.executemany(f'INSERT INTO "{a}"(CreateTime,Message,Type,Des) VALUES(?,?,?,?)', [
        (1711200000, "到家了吗".encode(), 1, 0),
        (1711200060, "到了，刚进门".encode(), 1, 1),
        (1711200120, "明天记得带伞".encode(), 1, 0),
        (1711200180, b"", 3, 1),
    ])
    conn.executemany(f'INSERT INTO "{b}"(CreateTime,Message,Type,Des) VALUES(?,?,?,?)', [
        (1711201000, "项目款到账了".encode(), 1, 0),
        (1711201060, "收到，我核对下".encode(), 1, 1),
    ])
    conn.executemany(f'INSERT INTO "{g}"(CreateTime,Message,Type,Des) VALUES(?,?,?,?)', [
        (1711202000, "wxid_ios_b:\n周末回家吃饭吗".encode(), 1, 0),
        (1711202060, "回，我带菜".encode(), 1, 1),
        (1711202120, b"", 34, 0),
        (1711202180, "wxid_ios_a:\n好，六点开饭".encode(), 1, 0),
    ])
    conn.commit()
    conn.close()

    c = base / "WCDB_Contact.sqlite"
    if c.exists():
        c.unlink()
    cc = sqlite3.connect(c)
    cc.execute("CREATE TABLE Friend (userName TEXT, nickName TEXT, remark TEXT)")
    cc.executemany("INSERT INTO Friend VALUES(?,?,?)", [
        ("wxid_ios_a", "周八", "周八"),
        ("wxid_ios_b", "吴九", ""),
        ("11223344@chatroom", "家人群", "家人群"),
    ])
    cc.commit()
    cc.close()
    return mm, c


def make_ios_files():
    """直接给一个 iOS 微信库文件夹（越狱/第三方工具导出后的样子）。"""
    mm, c = _make_ios_dbs(FIX / "ios" / "DB")
    return mm, c


def _manifest_db(path: Path, entries, encrypted: bool, conn_files=None):
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE Files (fileID TEXT PRIMARY KEY, domain TEXT, "
                 "relativePath TEXT, flags INTEGER, file BLOB)")
    for item in entries:
        conn.execute("INSERT INTO Files VALUES(?,?,?,?,?)", item)
    conn.commit()
    conn.close()


def _hash_file(root: Path, data: bytes, fid: str):
    d = root / fid[:2]
    d.mkdir(parents=True, exist_ok=True)
    (d / fid).write_bytes(data)
    return fid


def make_ios_backup(encrypted: bool):
    """构造 Finder/iTunes 备份目录；encrypted=True 时用密码 demo1234 加密。"""
    root = FIX / ("ios_backup_enc" if encrypted else "ios_backup")
    if root.exists():
        import shutil as _sh
        _sh.rmtree(root)
    root.mkdir(parents=True)
    tmp = FIX / (".ios_src_enc" if encrypted else ".ios_src_plain")
    if tmp.exists():
        import shutil as _sh
        _sh.rmtree(tmp)
    mm, contact = _make_ios_dbs(tmp)

    info = {
        "Device Name": "iPhone (fixture)",
        "Product Name": "iPhone 15",
        "Product Version": "17.4.1",
        "Serial Number": "FIXTURE0001",
        "Last Backup Date": "2026-10-20T10:00:00Z",
    }
    (root / "Info.plist").write_bytes(plistlib.dumps(info))

    rel_mm = "Documents/0123456789abcdef0123456789abcdef/DB/MM.sqlite"
    rel_contact = "Documents/0123456789abcdef0123456789abcdef/DB/WCDB_Contact.sqlite"
    fid_mm = hashlib.sha1(("AppDomain-com.tencent.xinWeChat" + rel_mm).encode()).hexdigest()
    fid_contact = hashlib.sha1(("AppDomain-com.tencent.xinWeChat" + rel_contact).encode()).hexdigest()

    domain = "AppDomain-com.tencent.xinWeChat"
    if not encrypted:
        _hash_file(root, mm.read_bytes(), fid_mm)
        _hash_file(root, contact.read_bytes(), fid_contact)
        _manifest_db(root / "Manifest.db", [
            (fid_mm, domain, rel_mm, 1, None),
            (fid_contact, domain, rel_contact, 1, None),
        ], False)
        (root / "Manifest.plist").write_bytes(plistlib.dumps({
            "IsEncrypted": False, "Version": "10.0", "Lockdown": {},
            "SystemDomainsVersion": "22.0",
        }))
        return root

    password = "demo1234"
    salt = b"0123456789abcdef"
    iters = 10000
    pwd_key = hashlib.pbkdf2_hmac("sha1", password.encode(), salt, iters, 32)
    class_key = bytes(range(32, 64))          # 固定的“保护类 11”密钥
    wrapped_class = cp.aes_key_wrap(pwd_key, class_key)

    keybag = bytearray()
    keybag += b"LVVK" + struct.pack(">II", 4, 0) + bytes(16) + struct.pack(">I", 1)
    keybag += bytes(16) + struct.pack(">IIII", 11, 1, 0, len(wrapped_class)) + wrapped_class
    keybag = bytes(keybag)

    manifest_key = bytes(range(64, 96))
    manifest_wrapped = struct.pack(">I", 11) + cp.aes_key_wrap(class_key, manifest_key)

    def enc_blob(plain: bytes, key: bytes) -> bytes:
        iv = bytes(random.getrandbits(8) for _ in range(16))
        return iv + cp.aes_cbc_encrypt(key, plain, iv)

    def wrapped_for(key: bytes) -> bytes:
        return struct.pack(">I", 11) + cp.aes_key_wrap(class_key, key)

    fkey_mm = bytes(range(96, 128))
    fkey_contact = bytes(range(128, 160))
    _hash_file(root, enc_blob(mm.read_bytes(), fkey_mm), fid_mm)
    _hash_file(root, enc_blob(contact.read_bytes(), fkey_contact), fid_contact)

    def meta(fkey_wrapped: bytes, size: int) -> bytes:
        return plistlib.dumps({
            "$archiver": "NSKeyedArchiver",
            "$version": 100000,
            "$top": {"root": plistlib.UID(1)},
            "$objects": ["$null", {
                "EncryptionKey": {"NS.data": fkey_wrapped},
                "ProtectionClass": 11,
                "Size": size,
            }],
        }, fmt=plistlib.FMT_BINARY)

    _manifest_db(root / "Manifest.db", [
        (fid_mm, domain, rel_mm, 1, meta(wrapped_for(fkey_mm), mm.stat().st_size)),
        (fid_contact, domain, rel_contact, 1, meta(wrapped_for(fkey_contact), contact.stat().st_size)),
    ], True)

    # Manifest.db 本身也要加密
    manifest_plain = (root / "Manifest.db").read_bytes()
    (root / "Manifest.db").write_bytes(enc_blob(manifest_plain, manifest_key))

    (root / "Manifest.plist").write_bytes(plistlib.dumps({
        "IsEncrypted": True,
        "Version": "10.0",
        "Lockdown": {},
        "BackupKeyBag": keybag,
        "ManifestKey": manifest_wrapped,
        "Salt": salt,
        "Iterations": iters,
    }))
    (root / "password.txt").write_text("demo1234\n", encoding="utf-8")
    return root


def _vi(n: int) -> bytes:
    """SQLite varint（大端，除末字节外最高位为 1）。"""
    if n < 0:
        n += 1 << 64
    parts = [n & 0x7F]
    n >>= 7
    while n:
        parts.append(n & 0x7F)
        n >>= 7
    parts.reverse()
    return bytes([b | 0x80 for b in parts[:-1]] + [parts[-1]])


def _record(values) -> bytes:
    types, body = [], bytearray()
    for v in values:
        if isinstance(v, int):
            if 0 <= v <= 127:
                types.append(1); body += bytes([v])
            elif -32768 <= v <= 32767:
                types.append(2); body += struct.pack(">h", v)
            else:
                types.append(4); body += struct.pack(">i", v)
        else:
            b = str(v).encode("utf-8")
            types.append(13 + 2 * len(b)); body += b
    types_b = b"".join(_vi(t) for t in types)
    hl = len(types_b) + 1
    while len(_vi(hl)) + len(types_b) != hl:
        hl = len(types_b) + len(_vi(hl))
    return _vi(hl) + types_b + bytes(body)


def _cell(payload: bytes, rowid: int) -> bytes:
    return _vi(len(payload)) + _vi(rowid) + payload


def _btree_page(cells, page1: bool, page: int, reserve: int) -> bytes:
    usable = page - reserve
    ptrs, end = [], usable
    for c in cells:
        end -= len(c)
        ptrs.append(end)
    hdr_off = 100 if page1 else 0
    start = hdr_off + 8 + 2 * len(cells)
    if end < start:
        raise ValueError("页面装不下：end=%d start=%d" % (end, start))
    out = bytearray(usable)
    out[hdr_off] = 0x0D
    out[hdr_off + 3:hdr_off + 5] = struct.pack(">H", len(cells))
    out[hdr_off + 5:hdr_off + 7] = struct.pack(">H", end)
    pos = hdr_off + 8
    for ptr in ptrs:
        out[pos:pos + 2] = struct.pack(">H", ptr)
        pos += 2
    for c, ptr in zip(cells, ptrs):
        out[ptr:ptr + len(c)] = c
    return bytes(out)


def _db_header(page: int, reserve: int, page_count: int, schema_cookie: int = 1) -> bytes:
    h = bytearray(100)
    h[0:16] = b"SQLite format 3\x00"
    h[16:18] = struct.pack(">H", page)
    h[18] = 1
    h[19] = 1
    h[20] = reserve
    h[21], h[22], h[23] = 64, 32, 32
    h[24:28] = struct.pack(">I", 1)
    h[28:32] = struct.pack(">I", page_count)
    h[40:44] = struct.pack(">I", schema_cookie)
    h[44:48] = struct.pack(">I", 4)
    h[56:60] = struct.pack(">I", 1)
    h[92:96] = struct.pack(">I", 1)
    h[96:100] = struct.pack(">I", 3045000)
    return bytes(h)


def _build_reserved_db(path: Path, schema: str, columns, rows,
                       page: int = 4096, reserve: int = 80) -> None:
    """手工拼一个带 reserved 页尾的 SQLite 库（SQLCipher 的真实页布局）。"""
    master = _record(["table", "MSG", "MSG", 2, schema])
    page1 = (_db_header(page, reserve, 2) +
             _btree_page([_cell(master, 1)], True, page, reserve)[100:]).ljust(page, b"\x00")
    cells = [_cell(_record([i + 1] + list(r)), i + 1) for i, r in enumerate(rows)]
    page2 = _btree_page(cells, False, page, reserve).ljust(page, b"\x00")
    path.write_bytes(page1 + page2)


def make_win3_encrypted(password="wechatdemo"):
    """加密版 3.x 样本：SQLCipher 4 页格式（4096 / reserve 80 / HMAC-SHA512 / 256000 轮）。"""
    acct = FIX / "win3_enc" / "WeChat Files" / "wxid_encdemo"
    dst = acct / "Msg" / "Multi" / "MSG0.db"
    dst.parent.mkdir(parents=True, exist_ok=True)

    schema = ("CREATE TABLE MSG (localId INTEGER PRIMARY KEY, StrTalker TEXT, "
              "StrContent TEXT, CreateTime INTEGER, IsSender INTEGER, Type INTEGER)")
    g = "12345678@chatroom"
    rows = [
        (g, "wxid_lilei:\n这版什么时候发？", 1711000000, 0, 1),
        (g, "周五发，今天在灰度。", 1711000060, 1, 1),
        (g, "", 1711000120, 0, 3),
        (g, "<msg><appmsg><title>第9周周报.pdf</title></appmsg></msg>", 1711000180, 0, 49),
        ("张三", "发票抬头开公司还是个人？", 1711001000, 0, 1),
        ("张三", "开公司，税号 91310000XXXXXXXXXX", 1711001060, 1, 1),
        ("张三", "", 1711001120, 0, 34),
        (g, "wxid_wangwu:\n我这边测完了，没发现阻塞。", 1711002000, 0, 1),
        (g, "收到，我合并一下。", 1711002060, 1, 1),
        ("李四", "明天下午三点评审，会议室 A", 1711003000, 0, 1),
    ]
    tmp = FIX / ".win3_plain.db"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    _build_reserved_db(tmp, schema, None, rows)
    plain = tmp.read_bytes()
    tmp.unlink()

    page = 4096
    reserve = 80
    salt = bytes(random.getrandbits(8) for _ in range(16))
    key = hashlib.pbkdf2_hmac("sha512", password.encode(), salt, 256000, 32)
    import hmac
    mac_key = hashlib.pbkdf2_hmac("sha512", key, bytes(b ^ 0x3a for b in salt), 2, 32)
    out = bytearray(salt)
    for i in range(0, len(plain), page):
        chunk = plain[i:i + page]
        iv = bytes(random.getrandbits(8) for _ in range(16))
        body = chunk[16:page - reserve] if i == 0 else chunk[:page - reserve]
        ct = cp.aes_cbc_encrypt(key, body, iv, pad=False)
        mac = hmac.new(mac_key, ct + iv + struct.pack('<I', i // page + 1), 'sha512').digest()
        out += ct + iv + mac
    dst.write_bytes(bytes(out))

    c = _db(acct / "Msg" / "Contact" / "contact.db")
    c.execute("CREATE TABLE Contact (userName TEXT, nickName TEXT, remark TEXT)")
    c.execute("CREATE TABLE ChatRoom (ChatRoomName TEXT, NickName TEXT, Remark TEXT)")
    c.executemany("INSERT INTO Contact VALUES(?,?,?)", [
        ("wxid_lilei", "李雷", "李雷-产品"),
        ("wxid_wangwu", "王五", ""),
        ("zhangsan_88", "张三", "张三-财务"),
        ("lisi_99", "李四", "李四-研发"),
    ])
    c.execute("INSERT INTO ChatRoom VALUES(?,?,?)", ("12345678@chatroom", "项目群", "项目群"))
    c.commit()
    c.close()
    (FIX / "passwords.json").write_text(json.dumps({"win3_enc": password}, ensure_ascii=False, indent=2) + "\n",
                                       encoding="utf-8")
    return dst


if __name__ == "__main__":
    make_wechat_db()
    make_qq_txt()
    make_json_export()
    make_html_export()
    make_csv_export()
    make_encrypted_like()
    make_decoy()
    make_win3()
    make_win4()
    make_ios_files()
    make_ios_backup(encrypted=False)
    make_ios_backup(encrypted=True)
    make_win3_encrypted()
    import shutil as _sh
    for junk in (FIX / ".ios_src_plain", FIX / ".ios_src_enc"):
        _sh.rmtree(junk, ignore_errors=True)
    total = sum(f.stat().st_size for f in FIX.rglob("*") if f.is_file())
    print("fixtures ready:", FIX, "files:", len([f for f in FIX.rglob("*") if f.is_file()]),
          "bytes:", total)
