#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端自测：把 fixtures 里每种来源都跑一遍，核对条数/会话数/关键字段。

用法：python3 selftest.py
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

import chatlog_extract as ce

HERE = Path(__file__).parent
FIX = HERE / "fixtures"

CASES = [
    # 名称, argv, 期望消息数, 期望会话数
    ("通用混合样本(chat_src)", ["--src", "fixtures/chat_src"], 23, 9),
    ("Windows 3.x", ["--src", "fixtures/win3"], 10, 3),
    ("Windows 3.x 加密库", ["--src", "fixtures/win3_enc", "--enc-key", "wechatdemo"], 10, 3),
    ("Windows 4.x", ["--src", "fixtures/win4"], 8, 2),
    ("iOS 库文件夹", ["--src", "fixtures/ios"], 10, 3),
    ("iOS 未加密备份", ["--src", "fixtures/ios_backup"], 10, 3),
    ("iOS 加密备份", ["--src", "fixtures/ios_backup_enc", "--backup-password", "demo1234"], 10, 3),
    ("多来源合并", ["--src", "fixtures/win3", "--src", "fixtures/ios_backup_enc",
                    "--backup-password", "demo1234"], 20, 6),
]


def run_case(name, argv, want_msgs, want_convs):
    out = Path(tempfile.mkdtemp(prefix="chatlog_test_"))
    try:
        code = ce.main(argv + ["--out", str(out), "--formats", "jsonl", "--quiet"])
        path = out / "messages.jsonl"
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        convs = {r["conversation"] for r in rows}
        ok = len(rows) == want_msgs and len(convs) == want_convs and code in (0, 1)
        print("%-24s %s  条数=%d/%d 会话=%d/%d"
              % (name, "PASS" if ok else "FAIL", len(rows), want_msgs, len(convs), want_convs))
        return ok, rows
    finally:
        shutil.rmtree(out, ignore_errors=True)


def check_fields(rows, conv, sender, needle):
    for r in rows:
        if r["conversation"] == conv and r["sender"] == sender and needle in r["content"]:
            return True
    return False


def main():
    ok = True
    results = {}
    for name, argv, n, c in CASES:
        good, rows = run_case(name, argv, n, c)
        ok &= good
        results[name] = rows

    # 关键字段断言：群聊发送者还原、联系人昵称、群名前缀剥离、媒体占位
    win3 = results["Windows 3.x"]
    checks = [
        ("群聊发送者还原", check_fields(win3, "项目群", "李雷-产品", "这版什么时候发")),
        ("联系人群名还原", check_fields(win3, "项目群", "我", "周五发")),
        ("媒体占位", check_fields(win3, "项目群", "群成员", "[图片]")),
        ("iOS 群成员还原", check_fields(results["iOS 库文件夹"], "家人群", "吴九", "周末回家吃饭")),
        ("iOS 加密备份一致",
         [(r["content"], r["sender"]) for r in results["iOS 加密备份"]]
         == [(r["content"], r["sender"]) for r in results["iOS 库文件夹"]]),
    ]
    for label, good in checks:
        print("%-24s %s" % (label, "PASS" if good else "FAIL"))
        ok &= good

    # 错误路径：无密码 / 错误密码都必须给出可读提示且不崩
    for label, argv in (("加密备份无密码", ["--src", "fixtures/ios_backup_enc"]),
                        ("加密备份错误密码", ["--src", "fixtures/ios_backup_enc",
                                              "--backup-password", "nope"]),
                        ("Windows 库错误密钥", ["--src", "fixtures/win3_enc",
                                                "--enc-key", "nope"])):
        out = Path(tempfile.mkdtemp(prefix="chatlog_neg_"))
        try:
            code = ce.main(argv + ["--out", str(out), "--formats", "jsonl", "--quiet"])
            rows = (out / "messages.jsonl").read_text(encoding="utf-8").strip()
            good = code in (0, 1) and rows == ""
            print("%-24s %s" % (label, "PASS" if good else "FAIL"))
            ok &= good
        finally:
            shutil.rmtree(out, ignore_errors=True)

    print("\n结果：", "全部通过" if ok else "有失败项")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
