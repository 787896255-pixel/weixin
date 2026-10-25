#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""chatlog_platform —— Windows 微信目录 / iPhone 聊天记录 适配层（纯标准库）

这个模块给 chatlog_extract 用，补上三类真实来源：

  1. Windows 微信目录
     * 3.x：``WeChat Files/<wxid>/Msg/Multi/MSG*.db``（SQLCipher，需密钥）
     * 4.x：``xwechat_files/<wxid>/db_storage/message/message_*.db``
     * 同目录的 ``contact`` / ``WCDB_Contact.sqlite`` 用来把 wxid 还原成昵称
  2. iPhone 备份（Finder / iTunes / iMazing / 爱思助手 生成的备份目录）
     * 未加密备份：直接按 ``Manifest.db`` 的映射把微信库取出来
     * 加密备份：用备份密码解 keybag → 解 Manifest.db → 解每个文件
       （AES-256-CBC + RFC3394 密钥解包，纯 Python 实现，不需要 pycryptodome）
  3. 微信专有表结构
     * ``MSG``：Windows 3.x，会话在 StrTalker，方向在 IsSender
     * ``Chat_<md5(wxid)>``：iOS 3.x~8.x 的 MM.sqlite，方向在 Des
     * ``Msg_<md5(wxid)>`` + ``Name2Id``：Windows 4.x / iOS 新版 message_*.db
     * 群聊里 ``wxid_xxx:\\n正文`` 前缀会被拆成发送者
     * 图片/语音/视频/表情等无正文消息按类型补 ``[图片]`` 占位，保证时间线完整

对外只暴露纯数据（``Rec``），不 import chatlog_extract，避免循环依赖。
"""

from __future__ import annotations

import hashlib
import os
import plistlib
import re
import shutil
import sqlite3
import struct
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote

try:
    from Crypto.Cipher import AES as _FastAES
except ImportError:
    _FastAES = None

# =========================================================================== #
# 0. 纯 Python AES（128/192/256 + CBC）与 RFC3394 密钥解包
# =========================================================================== #

def _gmul(a: int, b: int) -> int:
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        hi = a & 0x80
        a = (a << 1) & 0xFF
        if hi:
            a ^= 0x1B
        b >>= 1
    return p & 0xFF


def _build_sbox() -> Tuple[List[int], List[int]]:
    sbox = [0] * 256
    p = q = 1
    while True:
        p = (p ^ ((p << 1) & 0xFF) ^ (0x1B if p & 0x80 else 0)) & 0xFF
        q ^= (q << 1) & 0xFF
        q ^= (q << 2) & 0xFF
        q ^= (q << 4) & 0xFF
        q &= 0xFF
        if q & 0x80:
            q ^= 0x09
        x = q
        for shift in (1, 2, 3, 4):
            x ^= ((q << shift) | (q >> (8 - shift))) & 0xFF
        sbox[p] = (x ^ 0x63) & 0xFF
        if p == 1:
            break
    sbox[0] = 0x63
    inv = [0] * 256
    for i, v in enumerate(sbox):
        inv[v] = i
    return sbox, inv


SBOX, INV_SBOX = _build_sbox()


def _xtime(a: int) -> int:
    a <<= 1
    if a & 0x100:
        a = (a ^ 0x1B) & 0xFF
    return a & 0xFF


def _mul(a: int, b: int) -> int:
    r = 0
    while b:
        if b & 1:
            r ^= a
        a = _xtime(a)
        b >>= 1
    return r & 0xFF


class AES:
    """最小可用 AES 分组密码（ECB 单块），用于 CBC / 密钥解包。"""

    def __init__(self, key: bytes):
        if len(key) not in (16, 24, 32):
            raise ValueError("AES key 长度必须是 16/24/32 字节")
        self.nk = len(key) // 4
        self.nr = self.nk + 6
        self.w = self._expand(key)

    def _expand(self, key: bytes) -> List[List[int]]:
        nk, nr = self.nk, self.nr
        w = [list(key[4 * i:4 * i + 4]) for i in range(nk)]
        rcon = 1
        for i in range(nk, 4 * (nr + 1)):
            temp = list(w[i - 1])
            if i % nk == 0:
                temp = temp[1:] + temp[:1]
                temp = [SBOX[b] for b in temp]
                temp[0] ^= rcon
                rcon = _xtime(rcon)
            elif nk > 6 and i % nk == 4:
                temp = [SBOX[b] for b in temp]
            w.append([w[i - nk][j] ^ temp[j] for j in range(4)])
        return w

    def _add_round_key(self, s: List[int], rnd: int) -> None:
        for c in range(4):
            word = self.w[rnd * 4 + c]
            for r in range(4):
                s[r + 4 * c] ^= word[r]

    def _shift_rows(self, s: List[int]) -> None:
        for r in range(1, 4):
            row = [s[r + 4 * c] for c in range(4)]
            row = row[r:] + row[:r]
            for c in range(4):
                s[r + 4 * c] = row[c]

    def _inv_shift_rows(self, s: List[int]) -> None:
        for r in range(1, 4):
            row = [s[r + 4 * c] for c in range(4)]
            row = row[-r:] + row[:-r]
            for c in range(4):
                s[r + 4 * c] = row[c]

    def _mix_columns(self, s: List[int]) -> None:
        for c in range(4):
            a0, a1, a2, a3 = s[4 * c:4 * c + 4]
            s[4 * c + 0] = _mul(a0, 2) ^ _mul(a1, 3) ^ a2 ^ a3
            s[4 * c + 1] = a0 ^ _mul(a1, 2) ^ _mul(a2, 3) ^ a3
            s[4 * c + 2] = a0 ^ a1 ^ _mul(a2, 2) ^ _mul(a3, 3)
            s[4 * c + 3] = _mul(a0, 3) ^ a1 ^ a2 ^ _mul(a3, 2)

    def _inv_mix_columns(self, s: List[int]) -> None:
        for c in range(4):
            a0, a1, a2, a3 = s[4 * c:4 * c + 4]
            s[4 * c + 0] = _mul(a0, 14) ^ _mul(a1, 11) ^ _mul(a2, 13) ^ _mul(a3, 9)
            s[4 * c + 1] = _mul(a0, 9) ^ _mul(a1, 14) ^ _mul(a2, 11) ^ _mul(a3, 13)
            s[4 * c + 2] = _mul(a0, 13) ^ _mul(a1, 9) ^ _mul(a2, 14) ^ _mul(a3, 11)
            s[4 * c + 3] = _mul(a0, 11) ^ _mul(a1, 13) ^ _mul(a2, 9) ^ _mul(a3, 14)

    def encrypt_block(self, block: bytes) -> bytes:
        if len(block) != 16:
            raise ValueError("block 必须 16 字节")
        s = list(block)
        self._add_round_key(s, 0)
        for rnd in range(1, self.nr):
            s = [SBOX[b] for b in s]
            self._shift_rows(s)
            self._mix_columns(s)
            self._add_round_key(s, rnd)
        s = [SBOX[b] for b in s]
        self._shift_rows(s)
        self._add_round_key(s, self.nr)
        return bytes(s)

    def decrypt_block(self, block: bytes) -> bytes:
        if len(block) != 16:
            raise ValueError("block 必须 16 字节")
        s = list(block)
        self._add_round_key(s, self.nr)
        for rnd in range(self.nr - 1, 0, -1):
            self._inv_shift_rows(s)
            s = [INV_SBOX[b] for b in s]
            self._add_round_key(s, rnd)
            self._inv_mix_columns(s)
        self._inv_shift_rows(s)
        s = [INV_SBOX[b] for b in s]
        self._add_round_key(s, 0)
        return bytes(s)


def _native_cbc(key: bytes, data: bytes, iv: bytes, decrypt: bool) -> Optional[bytes]:
    """macOS 上用 CommonCrypto 做 AES-CBC（比纯 Python 快两个数量级）。"""
    try:
        import ctypes
        import ctypes.util
        lib = ctypes.CDLL(ctypes.util.find_library("System") or "/usr/lib/libSystem.B.dylib")
        fn = lib.CCCrypt
        fn.restype = ctypes.c_int32
        fn.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
                       ctypes.c_char_p, ctypes.c_size_t, ctypes.c_char_p,
                       ctypes.c_char_p, ctypes.c_size_t,
                       ctypes.c_char_p, ctypes.c_size_t,
                       ctypes.POINTER(ctypes.c_size_t)]
        out = ctypes.create_string_buffer(len(data) + 16)
        moved = ctypes.c_size_t(0)
        rc = fn(1 if decrypt else 0, 0, 0, key, len(key), iv, data, len(data),
                out, len(out), ctypes.byref(moved))
        if rc != 0:
            return None
        return out.raw[:moved.value]
    except Exception:
        return None


def aes_cbc_encrypt(key: bytes, data: bytes, iv: bytes, pad: bool = True) -> bytes:
    if len(iv) != 16:
        raise ValueError("iv 必须 16 字节")
    if pad:
        n = 16 - (len(data) % 16)
        data = data + bytes([n]) * n
    elif len(data) % 16:
        raise ValueError("pad=False 时数据长度必须是 16 的倍数")
    native = _native_cbc(key, data, iv, decrypt=False)
    if native is not None:
        return native
    aes = AES(key)
    out = bytearray()
    prev = iv
    for i in range(0, len(data), 16):
        block = bytes(a ^ b for a, b in zip(data[i:i + 16], prev))
        prev = aes.encrypt_block(block)
        out += prev
    return bytes(out)


def aes_cbc_decrypt(key: bytes, data: bytes, iv: bytes, unpad: bool = True) -> bytes:
    if _FastAES is not None:
        if len(data) % 16:
            raise ValueError("AES 数据长度不完整")
        out = _FastAES.new(key, _FastAES.MODE_CBC, iv).decrypt(data)
        if unpad and out:
            pad = out[-1]
            if 1 <= pad <= 16 and out[-pad:] == bytes([pad]) * pad:
                out = out[:-pad]
        return out
    if len(data) % 16:
        data = data[:len(data) - (len(data) % 16)]
    if data:
        native = _native_cbc(key, bytes(data), iv, decrypt=True)
        if native is not None:
            out = bytearray(native)
            if unpad and out:
                pad = out[-1]
                if 1 <= pad <= 16 and out[-pad:] == bytes([pad]) * pad:
                    out = out[:-pad]
            return bytes(out)
    aes = AES(key)
    out = bytearray()
    prev = iv
    for i in range(0, len(data), 16):
        block = data[i:i + 16]
        plain = aes.decrypt_block(block)
        out += bytes(a ^ b for a, b in zip(plain, prev))
        prev = block
    if unpad and out:
        pad = out[-1]
        if 1 <= pad <= 16 and out[-pad:] == bytes([pad]) * pad:
            out = out[:-pad]
    return bytes(out)


def aes_key_unwrap(kek: bytes, wrapped: bytes) -> bytes:
    """RFC 3394 AES Key Wrap 解包。"""
    if len(wrapped) < 24 or len(wrapped) % 8:
        raise ValueError("wrapped key 长度不合法")
    n = len(wrapped) // 8 - 1
    aes = AES(kek)
    a = int.from_bytes(wrapped[:8], "big")
    r = [int.from_bytes(wrapped[8 + 8 * i:16 + 8 * i], "big") for i in range(n)]
    for j in range(5, -1, -1):
        for i in range(n, 0, -1):
            t = n * j + i
            a ^= t
            block = a.to_bytes(8, "big") + r[i - 1].to_bytes(8, "big")
            plain = aes.decrypt_block(block)
            a = int.from_bytes(plain[:8], "big")
            r[i - 1] = int.from_bytes(plain[8:], "big")
    if a != 0xA6A6A6A6A6A6A6A6:
        raise ValueError("密钥解包校验失败（密码不对或数据损坏）")
    return b"".join(x.to_bytes(8, "big") for x in r)


def aes_key_wrap(kek: bytes, key: bytes) -> bytes:
    """RFC 3394 AES Key Wrap（生成测试样本用）。"""
    if len(key) % 8:
        raise ValueError("被包装密钥长度必须是 8 的倍数")
    n = len(key) // 8
    aes = AES(kek)
    a = 0xA6A6A6A6A6A6A6A6
    r = [int.from_bytes(key[8 * i:8 * i + 8], "big") for i in range(n)]
    for j in range(6):
        for i in range(1, n + 1):
            t = n * j + i
            block = a.to_bytes(8, "big") + r[i - 1].to_bytes(8, "big")
            enc = aes.encrypt_block(block)
            a = int.from_bytes(enc[:8], "big") ^ t
            r[i - 1] = int.from_bytes(enc[8:], "big")
    return a.to_bytes(8, "big") + b"".join(x.to_bytes(8, "big") for x in r)


def _self_test_crypto() -> None:
    aes128 = AES(bytes.fromhex("000102030405060708090a0b0c0d0e0f"))
    ct = aes128.encrypt_block(bytes.fromhex("00112233445566778899aabbccddeeff"))
    assert ct.hex() == "69c4e0d86a7b0430d8cdb78070b4c55a", ct.hex()
    assert aes128.decrypt_block(ct).hex() == "00112233445566778899aabbccddeeff"
    aes256 = AES(bytes.fromhex("000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"))
    ct = aes256.encrypt_block(bytes.fromhex("00112233445566778899aabbccddeeff"))
    assert ct.hex() == "8ea2b7ca516745bfeafc49904b496089", ct.hex()
    kek = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
    key = bytes.fromhex("00112233445566778899aabbccddeeff")
    wrapped = aes_key_wrap(kek, key)
    assert wrapped.hex() == "1fa68b0a8112b447aef34bd8fb5a7b829d3e862371d2cfe5", wrapped.hex()
    assert aes_key_unwrap(kek, wrapped) == key


# =========================================================================== #
# 1. iOS 备份：keybag / Manifest.db / 文件解密
# =========================================================================== #

IOS_WECHAT_DOMAIN = "AppDomain-com.tencent.xinWeChat"
IOS_WECHAT_REL = re.compile(
    r"(?i)(?:^|/)(?:MM\.sqlite|message_\d+\.db|WCDB_Contact\.sqlite|contact\.db|"
    r"msg_\d+\.db|mm\.sqlite|ChatLog[^/]*\.db)$"
)


@dataclass
class KeybagItem:
    uuid: str
    clas: int
    wrap: int
    ktype: int
    key: bytes


class Keybag:
    """iOS 备份 keybag（Manifest.plist → BackupKeyBag）。"""

    def __init__(self, data: bytes):
        self.raw = data
        self.attrs = {}
        self.modern = data[:4] in (b"VERS", b"TYPE")
        if self.modern:
            self.items = []
            current = {}
            off = 0
            while off + 8 <= len(data):
                tag = data[off:off+4].decode('ascii', 'replace')
                length = int.from_bytes(data[off+4:off+8], 'big')
                off += 8
                if length > len(data)-off:
                    raise ValueError('keybag 条目长度错误')
                value = data[off:off+length]
                off += length
                decoded = int.from_bytes(value,'big') if tag in (
                    'VERS','TYPE','ITER','DPIC','CLAS','WRAP','KTYP') else value
                if tag == 'UUID':
                    if 'CLAS' in current:
                        self.items.append(KeybagItem(str(current.get('UUID','')),current['CLAS'],
                            current.get('WRAP',0),current.get('KTYP',0),current.get('WPKY',b'')))
                    current = {'UUID': value.hex()}
                elif tag in ('CLAS','WRAP','KTYP','WPKY'):
                    current[tag] = decoded
                else:
                    self.attrs[tag] = decoded
            if 'CLAS' in current:
                self.items.append(KeybagItem(str(current.get('UUID','')),current['CLAS'],
                    current.get('WRAP',0),current.get('KTYP',0),current.get('WPKY',b'')))
            return
        if len(data) < 32:
            raise ValueError("keybag 数据过短")
        self.signature = data[0:4]
        self.version, self.type = struct.unpack(">II", data[4:12])
        self.uuid = data[12:28].hex()
        self.count = struct.unpack(">I", data[28:32])[0]
        self.items: List[KeybagItem] = []
        off = 32
        for _ in range(self.count):
            if off + 32 > len(data):
                break
            uuid = data[off:off + 16].hex()
            clas, wrap, ktype, klen = struct.unpack(">IIII", data[off + 16:off + 32])
            key = data[off + 32:off + 32 + klen]
            self.items.append(KeybagItem(uuid, clas, wrap, ktype, key))
            off += 32 + klen

    def class_keys(self, password_key: bytes) -> Dict[int, bytes]:
        out: Dict[int, bytes] = {}
        for it in self.items:
            if not it.key:
                continue
            if it.wrap & 2 or (not self.modern and it.wrap):
                try:
                    out[it.clas] = aes_key_unwrap(password_key, it.key)
                except ValueError:
                    continue
            elif not it.wrap:
                out[it.clas] = it.key
        return out


def _find_bytes(obj: Any, min_len: int = 16) -> List[bytes]:
    """在 NSKeyedArchiver plist 结构里递归收集 bytes。"""
    found: List[bytes] = []
    stack = [obj]
    seen = 0
    while stack and seen < 10000:
        cur = stack.pop()
        seen += 1
        if isinstance(cur, bytes):
            if len(cur) >= min_len:
                found.append(cur)
        elif isinstance(cur, dict):
            stack.extend(cur.values())
        elif isinstance(cur, (list, tuple)):
            stack.extend(cur)
    return found


def parse_manifest_file_meta(blob: Optional[bytes]) -> Dict[str, Any]:
    """解析 Files.file 列（NSKeyedArchiver），取出保护类与包装后的文件密钥。"""
    meta: Dict[str, Any] = {}
    if not blob:
        return meta
    obj = None
    try:
        obj = plistlib.loads(blob)
    except Exception:
        return meta
    # 标准结构：{'$objects': [...], '$top': {...}}
    objs = obj.get('$objects',[]) if isinstance(obj,dict) else []
    if isinstance(obj, dict) and isinstance(obj.get("$objects"), list):
        for o in obj["$objects"]:
            if isinstance(o, dict) and any(k in o for k in ("EncryptionKey", "ProtectionClass", "Size")):
                for k in ("EncryptionKey", "ProtectionClass", "Size", "VolumeOffset"):
                    if k in o:
                        meta[k] = o[k]
    if "EncryptionKey" not in meta:
        # 宽松回退：取最像「4 字节类 + 8 的倍数」的二进制块
        cands = _find_bytes(obj, 24)
        for b in cands:
            if len(b) >= 24 and (len(b) - 4) % 8 == 0 and 0 < int.from_bytes(b[:4], "big") <= 12:
                meta.setdefault("EncryptionKey", b)
                break
    ek = meta.get("EncryptionKey")
    if isinstance(ek, plistlib.UID) and objs and ek.data < len(objs):
        ek = objs[ek.data]
    if isinstance(ek, dict):
        ek = ek.get("NS.data") or ek.get("NS.bytes")
    if isinstance(ek, (bytes, bytearray)) and len(ek) > 4:
        meta["wrapped_key"] = bytes(ek)
        cls = int.from_bytes(bytes(ek[:4]), "big")
        meta["class"] = cls if cls <= 64 else int.from_bytes(bytes(ek[:4]), "little")
        meta["wrapped"] = bytes(ek[4:])
    elif isinstance(ek, (bytes, bytearray)) and len(ek) == 4:
        meta["class"] = int.from_bytes(bytes(ek), "big")
    return meta


class IOSBackup:
    """读取一个 iOS 备份目录；加密备份用 --backup-password 解锁。"""

    def __init__(self, root: Path, password: Optional[str] = None,
                 notes: Optional[List[str]] = None):
        self.root = Path(root)
        self.password = password
        self.notes = notes if notes is not None else []
        self.manifest_plist: Dict[str, Any] = {}
        self.encrypted = False
        self.unlocked = False
        self.file_keys: Dict[str, bytes] = {}
        self._manifest_path: Optional[Path] = None
        self._tmp_manifest: Optional[Path] = None
        self._load_plist()

    # -- 基础信息 ---------------------------------------------------------- #
    def _load_plist(self) -> None:
        p = self.root / "Manifest.plist"
        if p.exists():
            try:
                self.manifest_plist = plistlib.loads(p.read_bytes())
            except Exception as exc:  # noqa: BLE001
                self.notes.append("Manifest.plist 解析失败：%s" % exc)
        self.encrypted = bool(self.manifest_plist.get("IsEncrypted"))
        self.device = {}
        ip = self.root / "Info.plist"
        if ip.exists():
            try:
                info = plistlib.loads(ip.read_bytes())
                self.device = {
                    "name": info.get("Device Name", ""),
                    "product": "%s %s" % (info.get("Product Name", ""), info.get("Product Version", "")),
                    "serial": info.get("Serial Number", ""),
                    "last_backup": str(info.get("Last Backup Date", "")),
                }
            except Exception:
                pass

    # -- 解密 -------------------------------------------------------------- #
    def unlock(self) -> bool:
        if not self.encrypted:
            self.unlocked = True
            return True
        if self.unlocked:
            return True
        if not self.password:
            self.notes.append(
                "这是加密备份（Manifest.plist IsEncrypted=true）。请加 --backup-password 传入备份密码；"
                "密码只在本地参与 keybag 推导，不联网、不落盘。")
            return False
        bag_blob = self.manifest_plist.get("BackupKeyBag")
        mk = self.manifest_plist.get("ManifestKey")
        salt = self.manifest_plist.get("Salt")
        iters = int(self.manifest_plist.get("Iterations") or 10000)
        if not isinstance(bag_blob, (bytes, bytearray)) or not isinstance(mk, (bytes, bytearray)):
            self.notes.append("加密备份缺少 BackupKeyBag / ManifestKey，无法解锁。")
            return False
        try:
            bag = Keybag(bytes(bag_blob))
            self._modern_keybag = bag.modern
            pwd = self.password.encode('utf-8')
            if bag.modern:
                if bag.attrs.get('DPSL'):
                    pwd = hashlib.pbkdf2_hmac('sha256',pwd,bag.attrs['DPSL'],bag.attrs['DPIC'],32)
                salt = bag.attrs.get('SALT',salt)
                iters = bag.attrs.get('ITER',iters)
            pwd_key = hashlib.pbkdf2_hmac("sha1", pwd, bytes(salt or b""), iters, 32)
            class_keys = bag.class_keys(pwd_key)
            clas = int.from_bytes(bytes(mk[:4]), "big")
            if clas > 64:
                clas = int.from_bytes(bytes(mk[:4]), "little")
            if clas not in class_keys:
                raise ValueError("keybag 中没有保护类 %d 的密钥" % clas)
            self.manifest_key = aes_key_unwrap(class_keys[clas], bytes(mk[4:]))
            self.class_keys = class_keys
            self.unlocked = True
            return True
        except Exception as exc:  # noqa: BLE001
            self.notes.append("备份解锁失败（多为密码不对）：%s" % exc)
            return False

    def _manifest_plain(self) -> Optional[Path]:
        src = self.root / "Manifest.db"
        if not src.exists():
            return None
        if not self.encrypted:
            return src
        if not self.unlocked:
            return None
        if self._tmp_manifest and self._tmp_manifest.exists():
            return self._tmp_manifest
        blob = src.read_bytes()
        try:
            if len(blob) < 32 or blob[:16] == b"SQLite format 3\x00":
                plain = blob
            else:
                if getattr(self,'_modern_keybag',False):
                    plain = aes_cbc_decrypt(self.manifest_key, blob, bytes(16), unpad=False)
                else:
                    plain = aes_cbc_decrypt(self.manifest_key, blob[16:], blob[:16])
            if not plain.startswith(b"SQLite format 3\x00"):
                raise ValueError("Manifest.db 解密结果不是 SQLite")
            fd, tmp = tempfile.mkstemp(prefix='chatlog_manifest_',suffix='.db')
            os.close(fd)
            out = Path(tmp)
            out.write_bytes(plain)
            self._tmp_manifest = out
            return out
        except Exception as exc:  # noqa: BLE001
            self.notes.append("Manifest.db 解密失败：%s" % exc)
            return None

    def manifest_conn(self) -> Optional[sqlite3.Connection]:
        p = self._manifest_plain()
        if not p:
            return None
        try:
            return sqlite3.connect(p.resolve().as_uri() + "?mode=ro", uri=True)
        except sqlite3.DatabaseError as exc:
            self.notes.append("Manifest.db 打开失败：%s" % exc)
            return None

    # -- 单个文件 ---------------------------------------------------------- #
    def _decode_file(self, blob: bytes, meta: Dict[str, Any]) -> bytes:
        wrapped = meta.get("wrapped")
        if not wrapped:
            return blob
        clas = meta.get("class")
        if clas not in getattr(self, "class_keys", {}):
            raise ValueError("缺少保护类 %s 的密钥" % clas)
        file_key = aes_key_unwrap(self.class_keys[clas], wrapped)
        if getattr(self,'_modern_keybag',False):
            data = aes_cbc_decrypt(file_key,blob,bytes(16),unpad=False)
            size = meta.get('Size')
            return data[:size] if isinstance(size,int) else data
        iv, ct = blob[:16], blob[16:]
        return aes_cbc_decrypt(file_key, ct, iv)

    def read_file(self, file_id: str, blob: Optional[bytes], rel: str = "") -> bytes:
        if not self.encrypted:
            p = self.root / file_id[:2] / file_id
            if not p.exists(): p = self.root / file_id
            return p.read_bytes()
        meta = parse_manifest_file_meta(blob)
        if not meta.get("wrapped"):
            # 未加密条目（部分系统文件）仍然按 fileID 存放
            p = self.root / file_id[:2] / file_id
            if p.exists():
                return p.read_bytes()
            raise ValueError("条目既无加密密钥也无实体文件：%s" % (rel or file_id))
        raw = (self.root / file_id[:2] / file_id).read_bytes()
        return self._decode_file(raw, meta)

    # -- 微信库提取 -------------------------------------------------------- #
    def wechat_entries(self, include_media: bool = False) -> List[Tuple[str, str, str]]:
        """返回 [(fileID, relativePath, domain)]，只保留微信的库文件。"""
        conn = self.manifest_conn()
        if conn is None:
            return []
        out: List[Tuple[str, str, str]] = []
        try:
            # Filter out unrelated backup payloads in SQL before creating Python rows.
            query = ("SELECT fileID, domain, relativePath FROM Files WHERE "
                     "(domain LIKE '%com.tencent.xinWeChat%' OR domain LIKE '%com.tencent.xin%')")
            if not include_media:
                query += (" AND (lower(relativePath) LIKE '%.db' OR lower(relativePath) LIKE '%.sqlite'"
                          " OR lower(relativePath) LIKE '%.db-wal' OR lower(relativePath) LIKE '%.sqlite-wal'"
                          " OR lower(relativePath) LIKE '%.db-journal' OR lower(relativePath) LIKE '%.sqlite-journal'"
                          " OR lower(relativePath) LIKE '%.db-shm' OR lower(relativePath) LIKE '%.sqlite-shm')")
            for fid, domain, rel in conn.execute(query):
                rel = rel or ""
                base_rel = re.sub(r'-(wal|shm|journal)$','',rel)
                media = include_media and re.search(r'(?i)\.(?:dat|jpe?g|png|gif|webp|pic|pic_hd|pic_thum|mp4|mov|video|silk|amr|aud|wav)$',rel)
                if media or IOS_WECHAT_REL.search(base_rel) or ("/DB/" in base_rel and base_rel.lower().endswith((".db", ".sqlite"))):
                    out.append((fid, rel, domain or ""))
        except sqlite3.DatabaseError as exc:
            self.notes.append("Manifest.db 查询失败：%s" % exc)
        finally:
            conn.close()
        return out

    def extract_wechat(self, dest: Path, include_media: bool = False,
                       cancelled=lambda: False, progress=lambda text: None) -> List[Path]:
        dest = Path(dest)
        entries = self.wechat_entries(include_media)
        got: List[Path] = []
        if not entries:
            return got
        conn = self.manifest_conn()
        blobs: Dict[str, Any] = {}
        if conn is not None:
            try:
                for fid, rel, _d in entries:
                    if cancelled(): break
                    row = conn.execute("SELECT file FROM Files WHERE fileID=?", (fid,)).fetchone()
                    blobs[fid] = row[0] if row else None
            except sqlite3.DatabaseError:
                blobs = {}
            finally:
                conn.close()
        for fid, rel, domain in entries:
            if cancelled(): break
            progress('正在提取备份文件：'+Path(rel).name)
            if include_media:
                physical=self.root/fid[:2]/fid
                if physical.is_symlink() or (physical.is_file() and physical.stat().st_size>256*1024*1024):
                    self.notes.append('备份媒体超过单文件提取上限或不是普通文件：'+rel)
                    continue
            safe = re.sub(r"[^\w\u4e00-\u9fff./-]+", "_", rel).strip("/")
            safe = safe.replace("..", "_")
            target = dest / re.sub(r'[^\w.-]','_',domain) / safe
            try:
                data = self.read_file(fid, blobs.get(fid), rel)
            except Exception as exc:  # noqa: BLE001
                self.notes.append("提取失败 %s：%s" % (rel, exc))
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            got.append(target)
        if self._tmp_manifest and self._tmp_manifest.exists():
            try:
                self._tmp_manifest.unlink()
            except OSError:
                pass
        return got

# =========================================================================== #
# 2. 微信专有表解析（Windows 3.x / Windows&iOS 4.x / iOS Chat_<md5>）
# =========================================================================== #

TYPE_LABEL = {
    1: "文本", 3: "图片", 34: "语音", 42: "名片", 43: "视频", 47: "表情",
    48: "位置", 49: "链接/文件", 50: "通话", 62: "小视频", 10000: "系统",
    10002: "系统", 100000: "系统",
}
TYPE_PLACEHOLDER = {
    3: "[图片]", 34: "[语音]", 42: "[名片]", 43: "[视频]", 47: "[表情]",
    48: "[位置]", 49: "[链接/文件]", 50: "[通话]", 62: "[小视频]",
}
GROUP_PREFIX = re.compile(r"^((?:wxid_|wm_|gh_)[A-Za-z0-9_-]{2,}):\r?\n")
WXID_RE = re.compile(r"(wxid_[A-Za-z0-9_-]+)")


@dataclass
class Rec:
    conversation: str
    sender: str
    ts: Optional[datetime]
    ts_raw: str
    content: str
    is_self: bool
    table: str
    type_label: str = ""
    conversation_id: str = ""
    account_id: str = ""
    sender_id: str = ""
    message_id: str = ""
    media: Dict[str, Any] = field(default_factory=dict)


class RecordBuffer(list):
    """Keep CLI list behavior, or deliver bounded batches to the desktop store."""
    def __init__(self, on_batch=None, cancelled=lambda: False, size=500):
        super().__init__()
        self.on_batch, self.cancelled, self.size = on_batch, cancelled, size
        self.total = 0
        self.buffer_bytes = 0

    def append(self, record):
        if self.cancelled(): raise ValueError('已停止读取')
        super().append(record)
        self.total += 1
        if self.on_batch:
            self.buffer_bytes += len(getattr(record, 'content', '')) * 4
            if len(self) >= self.size or self.buffer_bytes >= 4*1024*1024: self.flush()

    def flush(self):
        if self.on_batch and self:
            self.on_batch(self)
            self.clear()
            self.buffer_bytes = 0


def _media_meta(value: Any, mtype: Any, server_id: Any = '', local_id: Any = '', created: Any = '') -> Dict[str, Any]:
    """Preserve only local attachment identifiers before XML display stripping."""
    try: code = int(mtype) & 0xffffffff
    except (ValueError, TypeError): return {}
    kind = {3:'image',34:'audio',43:'video',62:'video',47:'image',49:'file'}.get(code)
    if not kind: return {}
    raw = _text(value, True)[:1024*1024]
    md5s, files = set(), set()
    for name, _, text in re.findall(r'([\w:.-]+)\s*=\s*([\"\x27])(.*?)\2', raw, re.S):
        if 'md5' in name.lower() and re.fullmatch('[0-9a-fA-F]{32}', text): md5s.add(text.lower())
        if name.lower() in ('filename','filepath','path') and not re.match(r'(?i)^\w+://',text): files.add(text[:1000])
    if kind == 'file':
        title = re.search(r'<title>(.*?)</title>', raw, re.S)
        if title: files.add(re.sub(r'<!\[CDATA\[(.*?)\]\]>',r'\1',title[1],flags=re.S)[:1000])
    server = str(server_id or '')
    if kind == 'audio' and re.fullmatch(r'\d{7,20}', server):
        files.update(server + ext for ext in ('.silk','.amr','.aud','.wav','.mp3'))
    if kind == 'audio' and str(local_id or '').isdigit() and int(local_id) > 0:
        when = _to_dt(created)
        if when: files.add('voice-local-' + str(int(local_id)) + '-' + str(int(when.timestamp())))
    return {'kind':kind,'md5s':sorted(md5s),'files':sorted(files), 'server_id':server}


def _to_dt(v: Any) -> Optional[datetime]:
    if v is None:
        return None
    if isinstance(v, (bytes, bytearray)):
        try:
            v = bytes(v).decode("utf-8", "ignore")
        except Exception:
            return None
    if isinstance(v, (int, float)):
        x = float(v)
        if x <= 0:
            return None
        if x > 1e17:
            x /= 1e9
        elif x > 1e14:
            x /= 1e6
        elif x > 1e11:
            x /= 1e3
        try:
            return datetime.fromtimestamp(x)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(v, str):
        s = v.strip()
        if re.fullmatch(r"\d{9,19}", s):
            return _to_dt(int(s))
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S",
                    "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                continue
    return None


def _text(v: Any, keep_markup: bool = False) -> str:
    if v is None:
        return ""
    if isinstance(v, (bytes, bytearray)):
        b = bytes(v)
        if b.startswith(b'\x28\xb5\x2f\xfd'):
            try:
                import zstandard
                b = zstandard.ZstdDecompressor().decompress(b,max_output_size=32*1024*1024)
            except ImportError:
                return '[压缩消息：需要安装 zstandard 组件]'
            except Exception:
                return '[压缩消息解析失败]'
        for enc in ("utf-8", "utf-16-le", "utf-16"):
            try:
                s = b.decode(enc)
                if s and ("\ufffd" not in s[:50]):
                    break
            except UnicodeDecodeError:
                s = ""
        else:
            s = b.decode("utf-8", "ignore")
    else:
        s = str(v)
    if not keep_markup:
        s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", s)
        s = re.sub(r"(?s)<[^>]{1,200}>", " ", s)
        s = s.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
        s = s.replace("&quot;", '"').replace("&apos;", "'").replace("&#39;", "'")
        s = re.sub(r"[ \t\u00a0]{2,}", " ", s)
        s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _rows(conn: sqlite3.Connection, sql: str) -> Tuple[List[str], List[Tuple[Any, ...]]]:
    cur = conn.execute(sql)
    names = [d[0] for d in cur.description]
    return names, cur.fetchall()


def _col(cols_lower: List[str], *aliases: str) -> Optional[int]:
    for a in aliases:
        if a in cols_lower:
            return cols_lower.index(a)
    return None


def load_contacts(path: Path) -> Dict[str, str]:
    """收集 wxid → 昵称/备注。向上找 3 层，覆盖 WeChat 的 Contact / db_storage/contact 布局。"""
    out: Dict[str, str] = {}
    bases: List[Path] = []
    cur = path.parent
    for _ in range(4):
        bases.append(cur)
        if cur.name.lower().startswith(("wxid_", "wechat files", "xwechat_files")):
            break
        if cur.parent == cur:
            break
        cur = cur.parent
    cands: List[Path] = []
    skip_names = ("msg0.db", "msg1.db", "media.db", "message_0.db", "message_1.db")
    for base in bases:
        for pat in ("WCDB_Contact.sqlite", "MicroMsg.db", "contact.db", "Contact.db", "contact.sqlite",
                    "*WCDB_Contact.sqlite", "*contact*.db", "*Contact*.db"):
            for p0 in sorted(base.glob(pat)):
                if p0.is_file() and p0 != path:
                    cands.append(p0)
        for sub in ("Contact", "contact", "Msg/Contact", "db_storage/contact", "DB"):
            d = base / sub
            if d.is_dir():
                for p0 in sorted(d.glob("*.db")):
                    if p0 != path and p0.name.lower() not in skip_names:
                        cands.append(p0)
    # 本库自己（Friend 表内嵌）放最后
    cands.append(path)
    seen: set = set()
    for db in cands[:6]:
        key = str(db.resolve())
        if key in seen:
            continue
        seen.add(key)
        try:
            with db.open("rb") as fh:
                if fh.read(16) != b"SQLite format 3\x00":
                    continue          # 加密库/非库文件，读了只会报错
        except OSError:
            continue
        try:
            conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        except sqlite3.DatabaseError:
            continue
        try:
            for t in tables:
                try:
                    cols = [r[1] for r in conn.execute('PRAGMA table_info("%s")' % t.replace('"', '""'))]
                except sqlite3.DatabaseError:
                    continue
                low = [c.lower() for c in cols]
                i_u = _col(low, "username", "user_name", "wxid", "userid", "user_id",
                           "strwxid", "chatroomname", "chatroom_name")
                if i_u is None:
                    continue
                i_n = _col(low, "nickname", "nick_name", "alias", "displayname", "display_name")
                i_r = _col(low, "remark", "conremark", "remarkname")
                try:
                    for row in conn.execute('SELECT * FROM "%s"' % t.replace('"', '""')):
                        uid = str(row[i_u] or "").strip()
                        if not uid:
                            continue
                        name = ""
                        if i_r is not None and str(row[i_r] or "").strip():
                            name = str(row[i_r]).strip()
                        elif i_n is not None and str(row[i_n] or "").strip():
                            name = str(row[i_n]).strip()
                        if uid not in out or out[uid]==uid or (name and i_r is not None and row[i_r]):
                            out[uid] = name or out.get(uid) or uid
                except sqlite3.DatabaseError:
                    continue
        finally:
            conn.close()
    return out


def _md5_index(contacts: Dict[str, str]) -> Dict[str, str]:
    idx: Dict[str, str] = {}
    for uid, name in contacts.items():
        idx[hashlib.md5(uid.encode("utf-8")).hexdigest()] = name
    return idx


def _self_wxid(path: Path) -> str:
    for part in path.parts:
        m = WXID_RE.search(part)
        if m:
            return m.group(1)
    return ""


def verified_self_id(folder_id: str, known_ids) -> str:
    """A 4.x account folder may append _<four hex>; require a known username.

    Keep an exact username first. Never identify the owner by nickname or by a
    row number in Name2Id, which is local to each database.
    """
    known=set(known_ids)
    if folder_id in known:return folder_id
    match=re.fullmatch(r'(wxid_[A-Za-z0-9_-]+)_[0-9a-fA-F]{4}',folder_id)
    return match.group(1) if match and match.group(1) in known else folder_id


def parse_wechat_db(conn: sqlite3.Connection, path: Path, *, min_len: int = 0,
                    max_rows: int = 500000, keep_markup: bool = False,
                    notes: Optional[List[str]] = None, on_batch=None,
                    cancelled=lambda: False, contact_cache=None, contact_scope='',
                    reader_state=None,on_reader_state=None,peer_filter=None) -> Optional[List[Rec]]:
    """识别并解析微信库；不是微信结构时返回 None。"""
    notes = notes if notes is not None else []
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    except sqlite3.DatabaseError:
        return None
    lower = {t.lower(): t for t in tables}
    if not ('msg' in lower or 'name2id' in lower or any(t.startswith(('Chat_', 'Msg_')) for t in tables)):
        return None
    if contact_cache is not None and contact_scope in contact_cache:
        contacts = contact_cache[contact_scope]
    else:
        contacts = load_contacts(path)
        if contact_cache is not None: contact_cache[contact_scope] = contacts
    self_id = _self_wxid(path)

    if "msg" in lower:
        cols = [r[1] for r in conn.execute('PRAGMA table_info("%s")' % lower["msg"])]
        low = [c.lower() for c in cols]
        if _col(low, "strtalker", "talker", "str_talker") is not None and \
                _col(low, "strcontent", "content", "message") is not None:
            return _parse_msg_table(conn, lower["msg"], cols, path, contacts, self_id,
                                    min_len, max_rows, keep_markup, notes, on_batch, cancelled,
                                    reader_state,on_reader_state,peer_filter)

    chat_tables = [t for t in tables if t.startswith("Chat_") and len(t) > 5]
    if chat_tables:
        return _parse_ios_chats(conn, chat_tables, path, contacts, self_id,
                                min_len, max_rows, keep_markup, notes, on_batch, cancelled)

    if "name2id" in lower or any(t.startswith("Msg_") for t in tables):
        return _parse_v4(conn, tables, lower, path, contacts, self_id,
                         min_len, max_rows, keep_markup, notes, on_batch, cancelled,
                         reader_state,on_reader_state,peer_filter)
    return None


def _conv_from_hash(h: str, idx: Dict[str, str]) -> str:
    return idx.get(h.lower(), "")


def _parse_msg_table(conn: sqlite3.Connection, table: str, cols: List[str], path: Path,
                     contacts: Dict[str, str], self_id: str, min_len: int, max_rows: int,
                     keep_markup: bool, notes: List[str], on_batch=None, cancelled=lambda: False,
                     reader_state=None,on_reader_state=None,peer_filter=None) -> List[Rec]:
    low = [c.lower() for c in cols]
    i_talk = _col(low, "strtalker", "talker")
    i_content = _col(low, "strcontent", "content", "message")
    i_time = _col(low, "createtime", "create_time", "createtime_ms")
    i_self = _col(low, "issender", "is_sender", "issend")
    i_type = _col(low, "type", "msgtype", "msg_type")
    i_disp = _col(low, "displaycontent", "display_content")
    i_compress = _col(low, "compresscontent", "compress_content")
    i_id = _col(low,'msgsvrid','msg_svr_id','server_id')
    i_local = _col(low,'localid','local_id','msglocalid')
    out = RecordBuffer(on_batch, cancelled)
    field=cols[i_local] if i_local is not None else 'rowid'
    scope=table+(':'+hashlib.sha256('\n'.join(sorted(peer_filter)).encode()).hexdigest()[:16] if peer_filter else '')
    high=int((reader_state or {}).get(scope,(reader_state or {}).get(table,0)))
    maximum=conn.execute('SELECT COALESCE(MAX("'+field+'"),0) FROM "'+table.replace('"','""')+'"').fetchone()[0]
    if maximum is not None and high>int(maximum):high=0
    where=['"'+field+'">?'];params=[high]
    if peer_filter and i_talk is not None:
        where.append('"'+cols[i_talk]+'" IN ('+','.join('?' for _ in peer_filter)+')');params+=sorted(peer_filter)
    order = ' ORDER BY "'+field+'"'
    try:
        cur = conn.execute('SELECT rowid,* FROM "'+table.replace('"','""')+'" WHERE '+' AND '.join(where)+order,params)
    except sqlite3.DatabaseError as exc:
        notes.append("表 %s 读取失败：%s" % (table, exc))
        return out
    names = [d[0] for d in cur.description]
    n = 0
    while True:
        rows = cur.fetchmany(2000)
        if not rows:
            break
        for row in rows:
            rowid,row=row[0],row[1:]
            high=max(high,int(row[i_local] if i_local is not None else rowid))
            n += 1
            if n > max_rows:
                notes.append("表 %s 达到 --max-rows=%d，截断" % (table, max_rows))
                break
            raw_talker = str(row[i_talk] or "").strip() if i_talk is not None else ""
            conv = contacts.get(raw_talker, raw_talker) or path.stem
            content = _text(row[i_content], keep_markup)
            if not content and i_compress is not None:
                content = _text(row[i_compress], keep_markup)
            mtype = None
            if i_type is not None:
                try:
                    mtype = int(row[i_type])
                except (TypeError, ValueError):
                    mtype = None
            if not content and mtype in TYPE_PLACEHOLDER:
                content = TYPE_PLACEHOLDER[mtype]
            sender = ""
            m = GROUP_PREFIX.match(content)
            if m:
                sender = contacts.get(m.group(1), m.group(1))
                content = content[m.end():]
            if len(content) < min_len:
                continue
            is_self = False
            if i_self is not None:
                try:
                    is_self = int(row[i_self]) == 1
                except (TypeError, ValueError):
                    is_self = str(row[i_self]).strip().lower() in ("1", "true", "yes")
            if not sender:
                if is_self:
                    sender = "我"
                elif raw_talker.endswith("@chatroom"):
                    sender = "群成员"
                else:
                    sender = conv
            ts_raw = str(row[i_time]) if i_time is not None else ""
            out.append(Rec(conv or path.stem, sender, _to_dt(row[i_time]) if i_time is not None else None,
                           ts_raw, content, is_self, table,
                           TYPE_LABEL.get(mtype, str(mtype) if mtype else ""),
                           raw_talker,self_id,(m.group(1) if m else self_id if is_self else raw_talker),
                           str(row[i_id] or '') if i_id is not None else '',
                           _media_meta(row[i_content] or (row[i_compress] if i_compress is not None else ''),
                               mtype,row[i_id] if i_id is not None else '',
                               row[i_local] if i_local is not None else '',row[i_time] if i_time is not None else '')))
        if n > max_rows:
            break
    out.flush()
    if on_reader_state and not cancelled():on_reader_state(scope,high)
    return out


def _parse_ios_chats(conn: sqlite3.Connection, tables: List[str], path: Path,
                     contacts: Dict[str, str], self_id: str, min_len: int, max_rows: int,
                     keep_markup: bool, notes: List[str], on_batch=None, cancelled=lambda: False) -> List[Rec]:
    idx = _md5_index(contacts)
    uid_index = {hashlib.md5(uid.encode()).hexdigest():uid for uid in contacts}
    out = RecordBuffer(on_batch, cancelled)
    total = 0
    for table in sorted(tables):
        if total > max_rows:
            break
        suffix = table[5:]
        conv = _conv_from_hash(suffix, idx) or contacts.get(suffix, "") or ("会话 " + suffix[:8])
        try:
            cols = [r[1] for r in conn.execute('PRAGMA table_info("%s")' % table.replace('"', '""'))]
        except sqlite3.DatabaseError:
            notes.append("表 %s 读取失败" % table)
            continue
        low = [c.lower() for c in cols]
        i_msg = _col(low, "message", "content", "msg")
        i_time = _col(low, "createtime", "create_time")
        i_des = _col(low, "des", "is_send", "issender")
        i_type = _col(low, "type", "msgtype")
        i_id = _col(low,'messvrid','server_id','msgsvrid')
        i_local = _col(low,'meslocalid','local_id','localid')
        if i_msg is None or i_time is None:
            notes.append("表 %s 字段不可读，未标记完成" % table)
            continue
        try:
            cur = conn.execute('SELECT * FROM "%s" ORDER BY "%s"' % (table.replace('"', '""'), cols[i_time]))
        except sqlite3.DatabaseError:
            notes.append("表 %s 读取失败" % table)
            continue
        n = 0
        while True:
            rows = cur.fetchmany(1000)
            if not rows:
                break
            for row in rows:
                n += 1
                total += 1
                if total > max_rows:
                    break
                content = _text(row[i_msg], keep_markup)
                mtype = None
                if i_type is not None:
                    try:
                        mtype = int(row[i_type])
                    except (TypeError, ValueError):
                        mtype = None
                if not content and mtype in TYPE_PLACEHOLDER:
                    content = TYPE_PLACEHOLDER[mtype]
                is_self = False
                if i_des is not None:
                    try:
                        is_self = int(row[i_des]) == (0 if low[i_des]=='des' else 1)
                    except (TypeError, ValueError):
                        is_self = False
                sender = ""
                m = GROUP_PREFIX.match(content)
                if m:
                    sender = contacts.get(m.group(1), m.group(1))
                    content = content[m.end():]
                if len(content) < min_len:
                    continue
                if not sender:
                    sender = "我" if is_self else conv
                out.append(Rec(conv, sender, _to_dt(row[i_time]), str(row[i_time]),
                               content, is_self, table,
                               TYPE_LABEL.get(mtype, str(mtype) if mtype else ""),
                               uid_index.get(suffix,suffix), self_id,
                               (m.group(1) if m else self_id if is_self else uid_index.get(suffix,suffix)),
                               str(row[i_id] or '') if i_id is not None else '',
                               _media_meta(row[i_msg],mtype,row[i_id] if i_id is not None else '',
                                   row[i_local] if i_local is not None else '',row[i_time])))
            if total > max_rows:
                break
    out.flush()
    return out


def _parse_v4(conn: sqlite3.Connection, tables: List[str], lower: Dict[str, str], path: Path,
              contacts: Dict[str, str], self_id: str, min_len: int, max_rows: int,
              keep_markup: bool, notes: List[str], on_batch=None, cancelled=lambda: False,
              reader_state=None,on_reader_state=None,peer_filter=None) -> List[Rec]:
    account_id = self_id
    idx = _md5_index(contacts)
    id2name: Dict[int, str] = {}
    if "name2id" in lower:
        try:
            for rid, uname in conn.execute('SELECT rowid, user_name FROM "%s"' % lower["name2id"]):
                id2name[int(rid)] = str(uname or "")
                if uname and str(uname) not in contacts and str(uname).startswith(("wxid_", "wm_")):
                    contacts.setdefault(str(uname), str(uname))
        except sqlite3.DatabaseError:
            pass
    idx = _md5_index(contacts)
    uid_index = {hashlib.md5(uid.encode()).hexdigest():uid for uid in contacts}
    for uid in id2name.values():
        h = hashlib.md5(uid.encode()).hexdigest()
        uid_index[h] = uid
        idx.setdefault(h,contacts.get(uid,uid))
    self_id = verified_self_id(self_id, set(contacts) | set(id2name.values()))
    out = RecordBuffer(on_batch, cancelled)
    total = 0
    msg_tables = [t for t in tables if t.startswith("Msg_")]
    self_row = next((rid for rid, name in id2name.items() if name and name == self_id), None) if self_id else None
    for table in sorted(msg_tables):
        if total > max_rows:
            break
        suffix = table[4:]
        if peer_filter and suffix not in {hashlib.md5(peer.encode()).hexdigest() for peer in peer_filter}:continue
        conv = _conv_from_hash(suffix, idx) or ("会话 " + suffix[:8])
        try:
            cols = [r[1] for r in conn.execute('PRAGMA table_info("%s")' % table.replace('"', '""'))]
        except sqlite3.DatabaseError:
            notes.append("表 %s 读取失败" % table)
            continue
        low = [c.lower() for c in cols]
        i_time = _col(low, "create_time", "createtime")
        i_cont = _col(low, "message_content", "content", "compress_content")
        i_comp = _col(low, "compress_content")
        i_send = _col(low, "real_sender_id", "sender_id", "sender")
        i_type = _col(low, "local_type", "type", "msg_type")
        i_sort = _col(low, "sort_seq", "local_id", "server_id")
        i_id = _col(low,'server_id','msgsvrid')
        i_local = _col(low,'local_id','localid')
        if i_time is None or i_cont is None:
            notes.append("表 %s 字段不可读，未标记完成" % table)
            continue
        field=cols[i_local] if i_local is not None else 'rowid'
        high=int((reader_state or {}).get(table,0))
        maximum=conn.execute('SELECT COALESCE(MAX("'+field+'"),0) FROM "'+table.replace('"','""')+'"').fetchone()[0]
        if maximum is not None and high>int(maximum):high=0
        order = ' ORDER BY "'+field+'"'
        try:
            cur = conn.execute('SELECT rowid,* FROM "'+table.replace('"','""')+'" WHERE "'+field+'">?'+order,(high,))
        except sqlite3.DatabaseError:
            notes.append("表 %s 读取失败" % table)
            continue
        n = 0
        while True:
            rows = cur.fetchmany(1000)
            if not rows:
                break
            for row in rows:
                rowid,row=row[0],row[1:]
                high=max(high,int(row[i_local] if i_local is not None else rowid))
                n += 1
                total += 1
                if total > max_rows:
                    break
                content = _text(row[i_cont], keep_markup)
                if not content and i_comp is not None and i_comp != i_cont:
                    content = _text(row[i_comp], keep_markup)
                mtype = None
                if i_type is not None:
                    try:
                        mtype = int(row[i_type])
                    except (TypeError, ValueError):
                        mtype = None
                if not content and mtype in TYPE_PLACEHOLDER:
                    content = TYPE_PLACEHOLDER[mtype]
                sender_uid = ""
                if i_send is not None:
                    try:
                        sender_uid = id2name.get(int(row[i_send]), "")
                    except (TypeError, ValueError):
                        sender_uid = ""
                sender = contacts.get(sender_uid, sender_uid) if sender_uid else ""
                if not sender:
                    sender = conv
                is_self = bool(self_row is not None and i_send is not None and
                               _safe_int(row[i_send]) == self_row)
                if is_self:
                    sender = "我"
                if len(content) < min_len:
                    continue
                out.append(Rec(conv, sender, _to_dt(row[i_time]), str(row[i_time]),
                               content, is_self, table,
                               TYPE_LABEL.get(mtype, str(mtype) if mtype else ""),
                               uid_index.get(suffix,suffix),account_id,sender_uid,
                               str(row[i_id] or '') if i_id is not None else '',
                               _media_meta(row[i_cont] or (row[i_comp] if i_comp is not None else ''),
                                   mtype,row[i_id] if i_id is not None else '',
                                   row[i_local] if i_local is not None else '',row[i_time])))
            if total > max_rows:
                break
        if on_reader_state and not cancelled():on_reader_state(table,high)
    out.flush()
    return out


def _safe_int(v: Any) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# =========================================================================== #
# 3. 目录识别：WeChat Files / xwechat_files / iOS Backup / macOS
# =========================================================================== #

@dataclass
class Layout:
    kind: str          # win3 / win4 / ios-backup / mac / generic
    root: Path
    account: str = ""
    detail: str = ""


def _has_ios_backup(p: Path) -> bool:
    return (p / "Manifest.db").exists() or (p / "Manifest.plist").exists()


def _wechat_account(p: Path) -> str:
    m = WXID_RE.search(p.name)
    return m.group(1) if m else ""


def _account_dirs(base: Path) -> List[Path]:
    try:
        return [d for d in sorted(base.iterdir()) if d.is_dir()]
    except OSError:
        return []


def detect_layouts(root: Path, max_depth: int = 3) -> List[Layout]:
    root = Path(root)
    found: List[Layout] = []
    if root.is_file():
        return found

    if _has_ios_backup(root):
        return [Layout("ios-backup", root, detail="Finder/iTunes 备份")]

    # 直接指向某个账号目录：wxid_xxx/Msg 或 wxid_xxx/db_storage
    if (root / "Msg").is_dir() and (root / "Msg" / "Multi").is_dir():
        return [Layout("win3", root, _wechat_account(root) or root.name, "微信 3.x 账号目录")]
    if (root / "db_storage").is_dir():
        return [Layout("win4", root, _wechat_account(root) or root.name, "微信 4.x 账号目录")]
    backup_dir = root / "Backup"
    if backup_dir.is_dir():
        for udid in _account_dirs(backup_dir):
            if _has_ios_backup(udid):
                found.append(Layout("ios-backup", udid, udid.name, "Finder/iTunes 备份"))
        if found:
            return found

    if "com.tencent.xinWeChat" in str(root):
        found.append(Layout("mac", root, detail="macOS 微信容器"))
        return found

    queue: List[Tuple[Path, int]] = [(root, 0)]
    seen = set()
    while queue:
        cur, depth = queue.pop(0)
        if depth > max_depth or cur in seen:
            continue
        seen.add(cur)
        name = cur.name
        try:
            children = [c for c in cur.iterdir() if c.is_dir()]
        except OSError:
            continue
        if name == "WeChat Files":
            for acct in _account_dirs(cur):
                if (acct / "Msg").is_dir() or any(acct.glob("Msg/*.db")):
                    found.append(Layout("win3", acct, _wechat_account(acct) or acct.name,
                                        "WeChat Files（微信 3.x）"))
            continue
        if name == "xwechat_files":
            for acct in _account_dirs(cur):
                if (acct / "db_storage").is_dir():
                    found.append(Layout("win4", acct, _wechat_account(acct) or acct.name,
                                        "xwechat_files（微信 4.x）"))
            continue
        for child in children:
            if child.name in ("WeChat Files", "xwechat_files", "Backup"):
                queue.insert(0, (child, depth + 1))
            elif depth < max_depth:
                queue.append((child, depth + 1))
    return found


def looks_like_wechat_win(path: Path) -> bool:
    return any(p in str(path) for p in ("WeChat Files", "xwechat_files"))


def summarize_wechat_dir(root: Path) -> Dict[str, Any]:
    """目录体检：账号、消息库、是否加密、联系人库。root 可以是账号目录或父目录。"""
    root = Path(root)
    info: Dict[str, Any] = {"root": str(root), "accounts": [], "databases": [],
                            "encrypted": 0, "contact_dbs": []}
    accounts = [root] if ((root / "Msg").is_dir() or (root / "db_storage").is_dir()) else _account_dirs(root)
    for acct in accounts:
        if not acct.is_dir():
            continue
        info["accounts"].append(acct.name)
        for pat in ("Msg/Multi/MSG*.db", "Msg/MSG*.db", "db_storage/message/message_*.db",
                    "db_storage/message/message_*.db-wal"):
            for db in sorted(acct.glob(pat)):
                if db.name.endswith("-wal"):
                    continue
                enc = False
                try:
                    with db.open("rb") as fh:
                        enc = fh.read(16) != b"SQLite format 3\x00"
                except OSError:
                    continue
                info["databases"].append({"path": str(db), "encrypted": enc,
                                          "mb": round(db.stat().st_size / 1048576, 1)})
                info["encrypted"] += 1 if enc else 0
        for pat in ("Msg/Contact/*.db", "Msg/*.db", "db_storage/contact/*.db",
                    "db_storage/*/*.db", "Msg/Multi/*.db"):
            for db in sorted(acct.glob(pat)):
                low = db.name.lower()
                if ("contact" in low or "wcdb" in low or low.startswith("contact")) and \
                        str(db) not in info["contact_dbs"]:
                    info["contact_dbs"].append(str(db))
    return info


def ios_backup_summary(root: Path, password: Optional[str] = None,
                       notes: Optional[List[str]] = None) -> Dict[str, Any]:
    """iOS 备份体检：设备信息、是否加密、微信文件数（不落盘）。"""
    backup = IOSBackup(root, password, notes)
    info: Dict[str, Any] = {"root": str(root), "encrypted": backup.encrypted,
                            "device": getattr(backup, "device", {}), "wechat_files": 0,
                            "unlocked": backup.unlocked}
    if backup.encrypted:
        if backup.unlock():
            info["unlocked"] = True
            info["wechat_files"] = len(backup.wechat_entries())
    else:
        info["wechat_files"] = len(backup.wechat_entries())
    return info


# =========================================================================== #
# 4. SQLCipher 纯 Python 解码（没有 sqlcipher3 时的兜底）
# =========================================================================== #

# 微信不同版本的库参数：page_size, reserve, KDF 摘要, 迭代次数
SQLCIPHER_PROFILES: List[Tuple[int, int, str, int]] = [
    (4096, 80, "sha512", 256000),   # WeChat 4.x / SQLCipher 4 默认
    (4096, 48, "sha512", 256000),
    (4096, 80, "sha512", 64000),
    (4096, 80, "sha1", 64000),
    (4096, 48, "sha1", 64000),
    (1024, 48, "sha1", 64000),      # WeChat 3.x / SQLCipher 3 默认
    (4096, 80, "sha256", 256000),
]


def _decrypt_sqlcipher_pages(blob: bytes, key: bytes, page_size: int,
                             reserve: int) -> Optional[bytes]:
    if len(blob) < page_size * 2 or len(blob) % page_size:
        return None
    body = page_size - reserve
    if body <= 16:
        return None
    n = len(blob) // page_size
    out = bytearray()
    for i in range(n):
        page = blob[i * page_size:(i + 1) * page_size]
        if i == 0:
            ct = page[16:body]
            iv = page[body:body + 16]
            plain = aes_cbc_decrypt(key, ct, iv, unpad=False)
            page_plain = b"SQLite format 3\x00" + plain
        else:
            ct = page[:body]
            iv = page[body:body + 16]
            page_plain = aes_cbc_decrypt(key, ct, iv, unpad=False)
        if len(page_plain) > page_size:
            page_plain = page_plain[:page_size]
        out += page_plain.ljust(page_size, b"\x00")
    return bytes(out)


def _page1_ok(plain: Optional[bytes], page: int, reserve: int) -> bool:
    if not plain or len(plain) < 100:
        return False
    if not plain.startswith(b"SQLite format 3\x00"):
        return False
    if int.from_bytes(plain[16:18], "big") != page:
        return False
    if plain[18] not in (1, 2) or plain[19] not in (1, 2):
        return False
    if plain[20] not in (0, reserve):
        return False
    return plain[21:24] == b"\x40\x20\x20"


def _sqlite_ok(path: Path) -> bool:
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            row = conn.execute("PRAGMA quick_check").fetchone()
            if not row or str(row[0]).lower() != "ok":
                return False
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
            return True
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return False


def sqlcipher_decrypt(path: Path, enc_key: str, out_dir: Optional[Path] = None,
                      notes: Optional[List[str]] = None,
                      max_mb: float = 512.0) -> Optional[Path]:
    """尝试用纯 Python 把 SQLCipher 库解成明文临时文件；成功返回路径。

    enc_key 支持原始密码，或 ``0x`` 开头的 64 位十六进制 raw key。
    """
    notes = notes if notes is not None else []
    path = Path(path)
    try:
        size = path.stat().st_size
        if size > max_mb * 1048576:
            notes.append("库过大，跳过纯 Python 解码：%s（%.0f MB）" % (path.name, size / 1048576))
            return None
        blob = path.read_bytes()
    except OSError as exc:
        notes.append("读取失败：%s（%s）" % (path, exc))
        return None

    raw = None
    if re.fullmatch(r"[0-9a-fA-F]{64}", enc_key.strip()):
        enc_key = "0x" + enc_key.strip()      # 直接贴 64 位密钥也认
    if enc_key.lower().startswith("0x"):
        try:
            raw = bytes.fromhex(enc_key[2:])
        except ValueError:
            notes.append("--enc-key 的十六进制格式不对：需要 0x + 64 位十六进制")
            return None
    salt = blob[:16]
    tried = set()
    base_dir = Path(out_dir) if out_dir else Path(tempfile.mkdtemp(prefix="chatlog_sqlcipher_"))
    base_dir.mkdir(parents=True, exist_ok=True)

    for page_size, reserve, digest, iters in SQLCIPHER_PROFILES:
        if len(blob) % page_size or len(blob) < page_size * 2:
            continue
        keys: List[Tuple[str, bytes]] = []
        if raw is not None:
            keys.append(("raw", raw[:32]))
            if len(raw) >= 48:
                keys.append(("raw48", raw[:32]))
        else:
            try:
                keys.append(("%s/%d" % (digest, iters),
                             hashlib.pbkdf2_hmac(digest, enc_key.encode("utf-8"),
                                                 salt, iters, 32)))
            except ValueError:
                continue
        for tag, key in keys:
            sig = (page_size, reserve, tag, key)
            if sig in tried:
                continue
            tried.add(sig)
            # 先用第一页验证密钥/参数，命中才解全库（50MB 的库也不至于白跑几分钟）
            head = _decrypt_sqlcipher_pages(blob[:page_size * 2], key, page_size, reserve)
            if not _page1_ok(head[:page_size] if head else None, page_size, reserve):
                continue
            plain = _decrypt_sqlcipher_pages(blob, key, page_size, reserve)
            if plain is None:
                continue
            tmp = base_dir / ("%s.plain-%d-%d-%s.db" % (path.stem, page_size, reserve,
                                                        re.sub(r"[^A-Za-z0-9]+", "", tag)))
            try:
                tmp.write_bytes(plain)
            except OSError:
                continue
            if _sqlite_ok(tmp):
                notes.append("SQLCipher 解码成功：%s（page=%d reserve=%d kdf=%s iters=%d）"
                             % (path.name, page_size, reserve, digest if raw is None else "raw", iters))
                return tmp
            try:
                tmp.unlink()
            except OSError:
                pass
    notes.append("SQLCipher 纯 Python 解码未通过校验：%s（参数不在内置组合里，"
                 "或需要 sqlcipher3）" % path.name)
    return None
