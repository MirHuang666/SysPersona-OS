#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""persona_tray_version.py — 托盘程序的版本号（全局可读）

人 2026-11-15：「今天要给托盘程序加版本控制，每次修改托盘程序都要添加全局可读的版本号，
今天这个机器托盘还是旧的。」

所以定两条：

1. **版本号存在库里**（`配置\\客户端\\tray-version.json`），任何一台机器、任何脚本、
   任何一次同步之后都能读到同一个值——不是只写在代码里；
2. **改完托盘必须 bump**：跑 `python 脚本/persona_tray_version.py bump "改了什么"`，
   它把 patch 号 +1、重算 build（内容哈希）、写回 json。托盘启动时会读这个文件，
   build 与自身内容不一致就说明"这台机器上的还是旧的"。

用法：
    python 脚本/persona_tray_version.py            # 看当前版本 + 本机文件是否与记录一致
    python 脚本/persona_tray_version.py bump "加唤醒 NAS"   # 改完托盘后跑
    python 脚本/persona_tray_version.py stamp       # 只重算 build（不升版本号）
"""

import hashlib
import io
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRAY = os.path.join(ROOT, "脚本", "persona_tray_multi.py")
VER = os.path.join(ROOT, "配置", "客户端", "tray-version.json")

DEFAULT = {"name": "persona_tray_multi", "version": "1.0.0", "build": "", "at": "", "note": ""}


def file_build(path=TRAY):
    """托盘文件的 build 指纹：内容 sha256 前 10 位（改一个字节就变）。"""
    h = hashlib.sha256()
    with io.open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:10]


def load():
    try:
        with io.open(VER, encoding="utf-8") as f:
            d = json.load(f)
        return {**DEFAULT, **d}
    except Exception:                          # noqa: BLE001
        return dict(DEFAULT)


def save(d):
    os.makedirs(os.path.dirname(VER), exist_ok=True)
    tmp = VER + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(d, ensure_ascii=False, indent=2) + "\n")
    os.replace(tmp, VER)


def mid():
    try:
        import persona_owned as PO
        return PO.machine_id()
    except Exception:                          # noqa: BLE001
        return ""


def bump(note="", kind="patch"):
    d = load()
    parts = (d.get("version") or "1.0.0").split(".")
    while len(parts) < 3:
        parts.append("0")
    major, minor, patch = (int(x) if x.isdigit() else 0 for x in parts[:3])
    if kind == "major":
        major, minor, patch = major + 1, 0, 0
    elif kind == "minor":
        minor, patch = minor + 1, 0
    else:
        patch += 1
    d["version"] = "%d.%d.%d" % (major, minor, patch)
    d["build"] = file_build()
    d["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    d["by"] = mid()
    d["note"] = note or d.get("note", "")
    save(d)
    return d


def main(argv=None):
    a = sys.argv[1:] if argv is None else argv
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                          # noqa: BLE001
        pass

    if a and a[0] == "bump":
        note = a[1] if len(a) > 1 else ""
        kind = "patch"
        for k in ("major", "minor"):
            if "--" + k in a:
                kind = k
        d = bump(note, kind)
        print("OK   托盘版本 → v%s（build %s）%s" % (d["version"], d["build"],
                                                   ("｜" + note) if note else ""))
        return 0

    if a and a[0] == "stamp":
        d = load()
        d["build"] = file_build()
        d["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        save(d)
        print("build 已重算：%s" % d["build"])
        return 0

    d = load()
    cur = file_build()
    same = bool(d.get("build")) and d["build"] == cur
    print("托盘版本：v%s  build %s  记录于 %s（%s）"
          % (d.get("version"), d.get("build") or "?", d.get("at") or "?", d.get("by") or "?"))
    if d.get("note"):
        print("最近改动：%s" % d["note"])
    print("本机文件：build %s → %s" % (cur, "一致 ✅" if same else "**不一致 ❌（这台跑的不是最新版）**"))
    if not same:
        print("处理：确认代码是最新之后跑  python 脚本/persona_tray_version.py stamp")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
