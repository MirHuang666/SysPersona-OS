import os as _os_shim, sys as _sys_shim
import persona_io as _pio  # 原子读写（v6.11.0，B85 第二批）
_sys_shim.path.insert(0, _os_shim.path.join(_os_shim.path.dirname(_os_shim.path.abspath(__file__)), "脚本"))
# -*- coding: utf-8 -*-
# SysPersona-OS · 本文件：persona_selfcheck.py — 心跳自检：14 项，单项失败不拖垮整体，目标 < 8 秒
r"""persona_selfcheck.py — 心跳自检：14 项，单项失败不拖垮整体，目标 < 8 秒

每次心跳（守护 TASK selfcheck，120 秒）跑一遍，逐项判定后写 `ops\selfcheck.json`：
    {at, items: {名: {ok, status, detail}}, summary}
status 三态：`ok` / `skip`（平台不符，非 Windows 上判不了）/ `fail`。

八项：
    tray      托盘进程在不在（命令行含 persona_tray.py）       ← 仅 Windows
    daemon    守护进程 + ops\persona.log 最后一行是否 < 5 分钟  ← 仅 Windows（进程表）
    watermark 壳口径 pressureTokens 与 ops\dsh-ctx.json 最近一次读数是否一致（≤5%）
    cdp       127.0.0.1:9222/json/version 是否有响应；recapture.lock 超 35 分钟只报 lock_stale
    inject    注入通道：插件 persona-inbox 的请求/回执（键盘注入 2026-11-15 退役）← 仅 Windows
    queue     inflight 条数 / tasks 未完成条数（只报数字，不判红）
    cost      今日 ¥ + calls + 余额（ledger 读不到就判红；余额 < ¥5 每天一次 warn）
    capture   最近两天最新会话正文的年龄 < 6 小时（当地 0–8 点放宽到 12 小时）
    index     index\L1.jsonl 的 mtime 同上阈值（索引停建＝检索悄悄变瞎）
    backup    第二备份根里最新的文件 < 48 小时（没配 backup2_root 判红，不猜）
    disk      C: / D: 剩余空间 > 10%
    tasks     SysPersona-* 计划任务数量（首次跑记基线，之后只防丢，新增不算异常）← 仅 Windows
    model     本地档 / 免费档能否 GET /v1/models（1 小时探一次、不花 token，有一档活即 OK）
    manifest  代码/配置与自检基线（ops\manifest.json）是否一致：改了没登记就红
              （登记：`python 脚本/persona_manifest.py --record "说明"`）

平台归类：托盘/守护靠 wmic/PowerShell 枚举进程表，注入闸依赖 ctypes.wintypes + user32，
这三项在非 Windows 上判 `skip`（detail 写「平台不符（非 Windows）：…」），不判 `fail`；
Windows 上照旧判 fail，真失败不许降级成 skip。

用法：
    python persona_selfcheck.py --once     # 一行汇总（心跳用 --quiet）
    python persona_selfcheck.py --json     # 整份 JSON
    python persona_selfcheck.py --quiet    # 只在状态**变化**时追加 ops\persona.log 一行
"""
import argparse
import csv
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request

NO_WINDOW = 0x08000000 if os.name == "nt" else 0
WIN = os.name == "nt"

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)


# 控制台 / 守护的 run() 都按 utf-8 解；pythonw 下 stdout 可能是 None。
for _stream in (sys.stdout, sys.stderr):
    try:
        if _stream is not None:
            _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


# ── 路径：一律走 persona_root，不写死盘符 ────────────────────────────
def _resolve_root():
    env = (os.environ.get("SYSPERSONA_ROOT") or os.environ.get("PERSONA_ROOT")
           or os.environ.get("MEM") or os.environ.get("SYSPERSONA_MEM"))
    if env and os.path.isdir(env):
        return env
    cur = BASE
    for _ in range(6):
        if os.path.isdir(__import__("persona_ops").dir()):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    try:
        from persona_root import root as _r
        found = _r(strict=False)
        if found and os.path.isdir(found):
            return found
    except Exception:
        pass
    try:
        from persona_root import find_root as _f
        found = _f()
        if found and os.path.isdir(found):
            return found
    except Exception:
        pass
    return BASE


ROOT = _resolve_root()
try:                                  # 运行态按机器分家（各写各的，但照样同步，能看对方历史）
    import persona_ops as _po
except Exception:                     # 半同步状态（新文件还没到）也不许把守护搞死
    class _po:                        # noqa: N801
        @staticmethod
        def dir():
            return os.path.join(ROOT, "ops")
        @staticmethod
        def shared():
            return os.path.join(ROOT, "ops")
OPS = _po.dir()                      # 本机运行态：ops\<机器id>\
OPS_SHARED = _po.shared()            # 共享层：ops\ 根（配置放这）
OUT = os.path.join(OPS, "selfcheck.json")
ALERT_MARK = os.path.join(OPS, "selfcheck-alert.json")
ALERTS = os.path.join(OPS, "alerts.jsonl")
LOG = os.path.join(OPS, "persona.log")
STATUS = os.path.join(OPS, "persona-status.json")
DSH_CTX = os.path.join(OPS, "dsh-ctx.json")
INJECT_STATE = os.path.join(OPS, "inject-state.json")
INFLIGHT = os.path.join(OPS, "inflight.json")
TASKS = os.path.join(OPS, "tasks.jsonl")
RECAPTURE_LOCK = os.path.join(OPS, "recapture.lock")

CDP_URL = "http://127.0.0.1:9222/json/version"
LOG_STALE = 300          # 秒：心跳 30s，5 分钟不动就是死了
LOCK_STALE = 35 * 60     # 秒：抓取锁超过 35 分钟只报，不删
BALANCE_LOW = 5.0        # 元
SELF_CHECK_CYCLES = 3   # 同一问题连续 3 个**自检周期**还坏，才拉客户端并唤醒一次
CLIENT_LAUNCH_GAP = 600  # 秒：客户端不在时的拉起冷却（别每拍开一个新窗口）
# 口径（人 2026-11-15 纠正）：心跳=守护检测状态写日志；空闲唤醒=静久了叫醒；
# **环境正常不唤醒**；没客户端就拉；超过 3 个自检周期（120 秒/次 ≈ 6 分钟）
# 还是坏 → 拉客户端 + 唤醒一次，这是解决环境问题的。见 `_wake_on_fail`。
ORDER = ("tray", "daemon", "codefresh", "cmdq", "globalro", "paths", "watermark", "cdp", "inject", "queue", "cost",
         "index", "backup", "disk", "tasks", "model", "manifest", "memory",
         "config", "isolation", "dshcfg", "sffree", "todoscatter", "todo", "plugin", "docmap", "sync", "compact", "inbox", "client", "coverage", "pub")
LABEL = {"tray": "托盘", "daemon": "守护", "codefresh": "代码新鲜度",
         "globalro": "全局只读", "todoscatter": "待办集中", "cmdq": "客户端命令",
         "watermark": "水位", "cdp": "CDP",
         "inject": "注入", "queue": "队列", "cost": "成本",
         "capture": "抓取", "index": "索引", "backup": "备份", "disk": "磁盘",
         "tasks": "计划任务", "model": "模型", "paths": "路径变量", "manifest": "台账",
         "memory": "记忆副本", "config": "配置一致", "isolation": "运行态隔离",
         "dshcfg": "DSH配置", "sffree": "免费名单", "pub": "版本发布", "todo": "该做什么", "plugin": "插件副本", "docmap": "孤儿文档", "sync": "全局同步", "compact": "压缩线A/B", "inbox": "注入通道", "client": "客户端自启", "coverage": "可检索覆盖"}

# 平台归类：判据本身只能在 Windows 上取到的项。非 Windows 上判 skip、不判 fail——
# 把「平台没这个能力」记成缺陷，会把真正的失败淹掉。Windows 上这里完全不生效。
WIN_ONLY = {
    "tray": "托盘进程靠 wmic/PowerShell 枚举进程表，非 Windows 上取不到",
    "daemon": "守护进程靠 wmic/PowerShell 枚举进程表，非 Windows 上取不到",
    "inject": "注入闸依赖 ctypes.wintypes / user32，非 Windows 上导入不了",
    "tasks": "计划任务靠 schtasks（Windows 工具），非 Windows 上取不到",
}

STATUS_OK = "ok"
STATUS_SKIP = "skip"
STATUS_FAIL = "fail"


# ── 小工具 ───────────────────────────────────────────────────────────
def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _read_json(path):
    return _pio.read_json(path)


def _write_json(path, obj):
    """原子写：委托 persona_io（同样补末尾换行，按行读的人不受影响）。"""
    return _pio.write_json(path, obj, indent=1, newline=True)


def _human(n):
    n = int(n or 0)
    if n < 1000:
        return str(n)
    if n < 1000000:
        return "%.0fK" % (n / 1000.0)
    m = n / 1000000.0
    return ("%dM" % round(m)) if abs(m - round(m)) < 0.05 else ("%.1fM" % m)


def _tail(path, nbytes=8192):
    """读文件尾 nbytes：persona.log 已经几百 K，不整份读。"""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - nbytes))
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


# ── 进程表：wmic（实测 0.13s）→ 没有就 PowerShell Get-CimInstance ──────
_PROCS = None
_PROC_ERR = ""


def _decode(b):
    for enc in ("utf-8", "mbcs"):
        try:
            return (b or b"").decode(enc, "replace")
        except (LookupError, AttributeError):
            pass
    return (b or b"").decode("utf-8", "replace")


def proc_list():
    r"""返回 [(pid, commandline)]；读不到返回 [] 并在 _PROC_ERR 里写原因。

    2026-11-12 修（换机器后自检一直报「进程表读不到」）：
      - Win11 24H2 **没有 wmic.exe**，先判在不在，别再白等 5 秒超时；
      - 全表 `Get-CimInstance Win32_Process` 在慢机器上要 8 秒以上，6 秒超时必被打断——
        而这里只关心 python / pythonw（托盘、守护、注入器都是它），所以**查询时就过滤**。
    """
    global _PROCS, _PROC_ERR
    if _PROCS is not None:
        return _PROCS
    out = []

    if shutil.which("wmic"):
        try:
            r = subprocess.run(["wmic", "process", "get", "ProcessId,CommandLine", "/format:csv"],
                               capture_output=True, timeout=8, creationflags=NO_WINDOW)
            text = _decode(r.stdout)
            if r.returncode == 0 and "CommandLine" in text:
                for row in csv.reader(io.StringIO(text)):
                    if len(row) < 3:
                        continue
                    cmd, pid = row[1], row[2].strip()
                    if pid.isdigit():
                        out.append((int(pid), cmd))
        except (OSError, subprocess.SubprocessError) as e:
            _PROC_ERR = "wmic 不可用：%s" % str(e)[:60]
    if out:
        _PROCS, _PROC_ERR = out, ""
        return _PROCS

    ps = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe' or Name='pythonw.exe'\""
          " | Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress")
    try:
        r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
                           capture_output=True, timeout=30, creationflags=NO_WINDOW)
        text = _decode(r.stdout).strip()
        data = json.loads(text) if text else []
        if isinstance(data, dict):
            data = [data]
        for it in data or []:
            try:
                out.append((int(it.get("ProcessId")), str(it.get("CommandLine") or "")))
            except (TypeError, ValueError):
                continue
        _PROC_ERR = "" if out else "没有 python 进程（PowerShell 查询成功但为空）"
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        _PROC_ERR = "进程表读不到：%s" % str(e)[:80]
    _PROCS = out
    return _PROCS

def _py_cmd(cmd, script, flag=""):
    """这条命令行是不是「python(w).exe ...\\<script> [flag]」本身。

    不能只做 `"脚本/persona_daemon.py" in cmd`：守护自己的 PowerShell 启动器 / DSH 的
    subprocess runner 命令行里也会带这几个字，会被误当成进程在跑。
    """
    pat = r'pythonw?\.exe["\s]+[^;|<>"\n]*%s' % re.escape(script)
    if flag:
        pat += r'["\s]+%s' % re.escape(flag)
    return re.search(pat, cmd or "", re.I) is not None


# ── 1 托盘 ───────────────────────────────────────────────────────────
def item_tray():
    procs = proc_list()
    if not procs:
        return False, "进程表读不到（%s）" % (_PROC_ERR or "空")
    hits = [pid for pid, cmd in procs if _py_cmd(cmd, "persona_tray")]   # 单文件版与 multi 版都算
    if hits:
        return True, "PID %s · 命令行含 persona_tray" % ",".join(str(p) for p in hits[:3])
    return False, "进程表里没有 persona_tray（单文件版或 multi 版）"


# ── 2 守护 ───────────────────────────────────────────────────────────
def item_daemon():
    procs = proc_list()
    hits = [pid for pid, cmd in procs
            if _py_cmd(cmd, "persona_daemon.py", "--daemon")]
    if not hits:
        return False, "没有 persona_daemon.py --daemon 进程"
    age, why = None, ""
    text = _tail(LOG)
    lines = [x for x in text.splitlines() if x.strip()]
    if lines:
        try:
            t = time.mktime(time.strptime(lines[-1][:19], "%Y-%m-%d %H:%M:%S"))
            age = max(0.0, time.time() - t)
        except (ValueError, OverflowError):
            why = "最后一行时间戳解析失败，用 mtime 兜底；"
    if age is None:
        try:
            age = max(0.0, time.time() - os.path.getmtime(LOG))
        except OSError:
            return False, "PID %s · 日志读不到" % ",".join(str(p) for p in hits[:3])
    detail = "PID %s · 日志最后一行 %d 秒前" % (",".join(str(p) for p in hits[:3]), age)
    if age > LOG_STALE:
        return False, "%s（>%d 秒，心跳停了）" % (detail, LOG_STALE)
    return True, why + detail


# ── 3 水位一致性（基准 = 托盘实际显示的那个数；同源比对）──────────────
_TRAY_MOD = None


def _load_tray():
    """按文件路径加载库根那份托盘，取 snapshot()["title"]（不启动托盘、不建图标）。"""
    global _TRAY_MOD
    if _TRAY_MOD is not None:
        return _TRAY_MOD
    path = os.path.join(ROOT, "脚本/persona_tray.py")
    if not os.path.exists(path):
        return None
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("persona_tray_selfcheck", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _TRAY_MOD = mod if hasattr(mod, "snapshot") else None
    except Exception:
        _TRAY_MOD = None
    return _TRAY_MOD


def _parse_wm_k(title, label="DSH"):
    """从托盘 title 里取 `<客户端> 392K/1.1M` 的那个 392K；`?` 或没有数返回 None。

    2026-11-14 修（人：「托盘里面所有的 dsh 都已经删除了」）：原来正则写死 `DSH`，
    客户端换成 Hermes 后托盘写的是 `Hermes 150K/1.1M`，永远匹配不到 → 每轮自检
    都报「托盘读数不可用」，变成一条**假 FAIL**，还让图标来回变琥珀色。
    现在按配置里当前客户端名取数（label 由 `_wm_target()` 给）。
    """
    m = re.search(r"%s\s+(\?|[0-9][0-9.,]*[KMG]?)\s*/" % re.escape(label), title or "")
    if not m or m.group(1) == "?":
        return None
    s = m.group(1).replace(",", "")
    try:
        if s.upper().endswith("K"):
            return int(float(s[:-1]) * 1000)
        if s.upper().endswith("M"):
            return int(float(s[:-1]) * 1000000)
        if s.upper().endswith("G"):
            return int(float(s[:-1]) * 1000000000)
        return int(float(s))
    except (TypeError, ValueError):
        return None


def _water_state(path):
    d = _read_json(path) or {}
    try:
        return int(d.get("pressure") or 0), int(d.get("equal") or 0)
    except (TypeError, ValueError):
        return 0, 0


def _wm_target():
    """现在托盘在盯哪个客户端、用哪种水位源：(label, kind)。

    以 `ops\\tray-clients.json` 为准（托盘自己也读这份）；读不到就按 Hermes。
    2026-11-14：客户端从 DSH 换成 Hermes 后，这条判据没跟着改，才会一直报假 FAIL。
    """
    d = _read_json(os.path.join(OPS_SHARED, "tray-clients.json")) or {}
    for name, cfg in (d.get("clients") or {}).items():
        if cfg.get("on") is False:
            continue
        kind = str(cfg.get("wm") or "").lower()
        if kind in ("hermes", "dsh"):
            return str(cfg.get("label") or name), kind
    return "Hermes", "hermes"


def item_watermark():
    label, kind = _wm_target()

    mod = _load_tray()
    if mod is None:
        return False, "托盘读数不可用（加载 persona_tray.py 失败）"

    # 基准 = 托盘自己算的那个数（同源），不再另开一条通道另算一份
    if kind == "hermes":
        try:
            h = mod.hermes_watermark() or {}
        except Exception as e:
            return False, "读数不可用（hermes_watermark 出错：%s）" % str(e)[:80]
        if h.get("ctx") is None:
            return False, "读数不可用（Hermes 水位文件还没写出数）"
        press = int(h.get("ctx") or 0)
        win = h.get("max")
    else:
        try:
            import persona_gauge as g
            press, _surface, win = g.dsh_pressure()
        except Exception as e:
            return False, "读数不可用（persona_gauge 出错：%s）" % str(e)[:80]
        press = int(press or 0)

    try:
        title = str((mod.snapshot() or {}).get("title") or "")
    except Exception as e:
        return False, "托盘读数不可用（snapshot 出错：%s）" % str(e)[:60]
    tray_val = _parse_wm_k(title, label)
    if not tray_val:
        return False, "%s 读数不可用（title=%s）" % (label, title[:80])

    # 留痕：dsh-ctx.json 的 last 口径不同，**不参与判定**，只附在 detail 里。
    rec = _read_json(DSH_CTX) or {}
    try:
        trace = int(rec.get("last") or 0)
    except (TypeError, ValueError):
        trace = 0

    diff = abs(press - tray_val)
    rel = diff / float(max(press, tray_val))
    tol_abs = 52000          # 5% 窗口（1M）≈52K
    ok = rel <= 0.05 or diff <= tol_abs

    # 粘滞检查：连续 6 轮完全相等 = 疑似读死（正常一轮工具调用一定会变）。
    prev_p, equal = _water_state(os.path.join(OPS, "selfcheck-state.json"))
    # 2026-11-14：Hermes 空闲时水位本来就一动不动，粘滞判据只对「每轮工具调用必变」
    # 的 DSH 成立；对 Hermes 会报假的「读数读死」（实测空闲 12 轮全相等）。
    equal = (equal + 1) if (kind != "hermes" and prev_p and prev_p == press) else 0
    try:
        _write_json(os.path.join(OPS, "selfcheck-state.json"),
                    {"at": _now(), "pressure": press, "equal": equal})
    except OSError:
        pass

    detail = "托盘 %s / gauge %d / 留痕 %s（差 %s / %.1f%%，阈值 5%% 或 52K）" % (
        _human(tray_val), press, _human(trace) if trace else "—",
        _human(diff), rel * 100.0)
    if equal >= 6:
        return False, detail + " · 水位疑似读死（连续 %d 轮完全相等）" % equal
    return ok, detail


# ── 4 CDP ────────────────────────────────────────────────────────────
def _capture_off():
    """抓取总闸：`ops\\persona.conf.json` 里 `"capture": 0` = 后台不抓网页。

    2026-11-12 起就是这样（风控之后人定「以后只在前台按 F5 才抓」）。
    这时候 CDP 不通是**正常的**，不能判红——否则托盘永远黄、还天天触发转红唤醒。
    """
    try:
        d = _read_json(os.path.join(OPS_SHARED, "persona.conf.json")) or {}
        v = d.get("capture")
        return v is not None and float(v) <= 0
    except Exception:                      # noqa: BLE001
        return False


def item_cdp():
    ok, detail = False, ""
    try:
        with urllib.request.urlopen(CDP_URL, timeout=2) as r:
            doc = json.loads(r.read().decode("utf-8", "replace"))
        ok = isinstance(doc, dict) and ("Browser" in doc or "webSocketDebuggerUrl" in doc)
        detail = "9222 有响应（%s）" % (doc.get("Browser") or "JSON OK")
    except Exception as e:
        if _capture_off():
            return True, "抓取已停用（capture=0），浏览器没开是正常的"
        detail = "9222 无响应：%s" % (str(e)[:60] or type(e).__name__)
    if os.path.exists(RECAPTURE_LOCK):
        try:
            age = time.time() - os.path.getmtime(RECAPTURE_LOCK)
            if age > LOCK_STALE:
                detail += " · lock_stale（%.0f 分钟，未删锁只报）" % (age / 60.0)
        except OSError:
            pass
    return ok, detail


# ── 5 注入闸（阈值只认 persona_inject，不自创）────────────────────────
def item_inject():
    r"""注入通道（2026-11-15 改口径）：键盘注入已退役，只认插件 persona-inbox。

    人 2026-11-15：「键盘注入不再使用」——旧判据是 `inject-state.json` 那三档
    （连续失败 ≥3 / 60 分钟没成功），只在键盘通道里成立；通道退役后留着它，
    等于让一条不用的路一直亮红灯。现在判据统一走 `persona_inbox_client.health()`：
    写请求 → 插件 `agent.followup()` 投递 → 写回执；没回执才叫"插件未响应"。
    """
    try:
        import persona_inbox_client as IB
        level, detail = IB.health()
    except Exception as e:                              # noqa: BLE001
        return False, "persona_inbox_client 不可用：%s" % str(e)[:80]
    # 键盘通道已退役。判断"还在被调用"只能看 `inject-state.json`——它**只**由 record_result
    # 写（即真的走了键盘注入）；`inject.jsonl` 是各调用方自己记的账，插件通道也会写，不能当证据。
    try:
        age = (time.time() - os.path.getmtime(INJECT_STATE)) / 60.0
        if age <= 30:
            detail += "；注意：键盘注入 %d 分钟前还真的跑过（调用方没换完）" % age
    except Exception:                                   # noqa: BLE001
        pass
    return level == "ok", detail


# ── 6 队列（只报数字，不判红）────────────────────────────────────────
def item_queue():
    infl = _read_json(INFLIGHT) or {}
    n_in = len(infl.get("items") or []) if isinstance(infl.get("items"), list) else 0
    n_un = 0
    try:
        with io.open(TASKS, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    t = json.loads(line)
                except ValueError:
                    continue
                if isinstance(t, dict) and str(t.get("status") or "") not in (
                        "done", "cancelled", "canceled", "rejected", "dropped", "expired"):
                    n_un += 1
    except OSError:
        n_un = -1
    return True, "inflight %d · tasks 未完成 %d" % (n_in, n_un)


# ── 7 今日成本（ledger 权威；余额 < ¥5 每天一次 warn）──────────────────
def _alert_once(kind, rec):
    """同一天同类只报一次；marker 落 ops\\selfcheck-alert.json。"""
    day = time.strftime("%Y-%m-%d")
    try:
        mark = _read_json(ALERT_MARK) or {}
    except Exception:
        mark = {}
    marks = mark.get(day) if isinstance(mark.get(day), dict) else {}
    if marks.get(kind):
        return False
    marks[kind] = rec.get("at")
    mark[day] = marks
    try:
        _write_json(ALERT_MARK, mark)
    except OSError:
        pass
    try:
        os.makedirs(OPS, exist_ok=True)
        with io.open(ALERTS, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return True


def item_cost():
    try:
        import persona_cost as c
        d = c.report_data()
    except Exception as e:
        return False, "persona_cost 不可用：%s" % str(e)[:80]
    if d.get("source") != "ledger":
        # 2026-11-15：跨零点后当天还没有任何调用，账本里自然没有今天的行——
        # 这是「今天还没花钱」，不是故障。只有账本真的坏掉（读文件出错）才判红。
        err = d.get("ledger_error") or "未知"
        if "2026" in err or "没有" in err or "no row" in err.lower():
            return True, "跳过：今天还没有调用（%s）" % err[:60]
        return False, "账本读不到：%s" % err[:80]
    money = float(d.get("money") or 0.0)
    calls = d.get("calls")
    bal = d.get("balance")
    detail = "今日 ¥%.2f · %s 调用 · %s" % (
        money, "{:,}".format(int(calls)) if calls is not None else "—",
        ("余额 ¥%.2f" % float(bal)) if bal is not None else "余额不可读")
    if bal is not None and float(bal) < BALANCE_LOW:
        fired = _alert_once("cost_balance_low", {
            "at": _now(), "level": "warn", "kind": "cost_balance_low", "src": "selfcheck",
            "balance": round(float(bal), 4), "money": round(money, 4),
            "note": "DSH 余额 ¥%.2f < ¥%.0f（今日已花 ¥%.2f）" % (float(bal), BALANCE_LOW, money)})
        return False, detail + ("（已写 warn）" if fired else "")
    # 日预算闸（人 2026-11-12 定）：钱/token 到线就是红，空闲循环那边同一条闸会停手。
    try:
        import persona_budget as _B
        _ok, _why = _B.allow()
        detail += " · " + _why
        if not _ok:
            return False, detail
    except Exception:                            # noqa: BLE001
        pass
    return True, detail


# ── 9 抓取 ───────────────────────────────────────────────────────────
# 阈值口径（2026-11-12 定）：白天 6 小时；当地 0–8 点放宽到 12 小时——
# 凌晨没人聊天，抓不到新正文不是断链，用同一个阈值会天天误报。
CAPTURE_MAX_H = 6.0
QUIET_UNTIL_HOUR = 8
MODEL_PROBE = os.path.join(OPS, "model-probe.json")
TASKS_BASE = os.path.join(OPS, "selfcheck-tasks.json")


def _limit_hours():
    return 12.0 if time.localtime().tm_hour < QUIET_UNTIL_HOUR else CAPTURE_MAX_H


def _hours_since(ts):
    return (time.time() - float(ts or 0)) / 3600.0


def _hm(hours):
    return "%.1f 小时" % hours if hours < 48 else "%.0f 小时" % hours


def _recent_session_dirs(n=2):
    base = os.path.join(ROOT, "sessions")
    try:
        days = sorted((d for d in os.listdir(base)
                       if os.path.isdir(os.path.join(base, d))), reverse=True)
    except OSError:
        return []
    return [os.path.join(base, d) for d in days[:n]]


def _newest_md(dirs):
    best, ts = "", 0.0
    for d in dirs:
        for dirpath, _sub, files in os.walk(d):
            for f in files:
                if not f.lower().endswith(".md"):
                    continue
                p = os.path.join(dirpath, f)
                try:
                    t = os.path.getmtime(p)
                except OSError:
                    continue
                if t > ts:
                    best, ts = p, t
    return best, ts


def item_capture():
    """抓取链还活着吗：最近两天里最新一份会话正文的年龄。

    今天断过一次（影子 ops 抢了路径），而断链是**静默**的——抓不到就是没有新文件，
    不会有人报错。所以这一项只看「最新正文多旧」，不看进程。
    """
    newest, ts = _newest_md(_recent_session_dirs(2))
    if not ts:
        return False, "最近两天一条会话正文都没有（sessions/ 下没有 .md）"
    hours = _hours_since(ts)
    lim = _limit_hours()
    detail = "最新正文 %s 前 · %s" % (_hm(hours), os.path.relpath(newest, ROOT))
    if lim > CAPTURE_MAX_H:
        detail += "（凌晨放宽到 %s）" % _hm(lim)
    return hours <= lim, detail


# ── 10 索引 ──────────────────────────────────────────────────────────
def item_index():
    """一级索引新不新：停建了检索会悄悄变瞎，但所有进程看起来都正常。"""
    path = os.path.join(ROOT, "index", "L1.jsonl")
    if not os.path.exists(path):
        return False, "一级索引不存在：index\\L1.jsonl"
    hours = _hours_since(os.path.getmtime(path))
    lim = _limit_hours()
    detail = "L1.jsonl %s 前更新（%d KB）" % (_hm(hours), os.path.getsize(path) // 1024)
    return hours <= lim, detail


# ── 11 备份 ──────────────────────────────────────────────────────────
COMMON_BACKUP_H = 48.0


def item_backup():
    """第二份备份还新不新：只有出事那天才发现没备份，是最贵的一种沉默故障。"""
    try:
        import persona_root as pr
        root = pr.backup_root()
    except Exception as e:                       # noqa: BLE001
        return False, "解析备份根出错：%s" % str(e)[:80]
    if not root:
        # 2026-11-15 人：「不需要第二个备份，因为是两台电脑互相备份的」→ 不判红
        return True, "跳过：没配第二备份根（两台电脑互备，按人手定不算故障）"
    if not os.path.isdir(root):
        # 同上：根不存在（如换机后 F: 没了）只提示，不判红
        return True, "跳过：第二备份根 %s 不存在（两台互备，按人手定不算故障）" % root
    best, ts, n = "", 0.0, 0
    for dirpath, _sub, files in os.walk(root):
        for f in files:
            n += 1
            p = os.path.join(dirpath, f)
            try:
                t = os.path.getmtime(p)
            except OSError:
                continue
            if t > ts:
                best, ts = p, t
    if not ts:
        return False, "第二备份根是空的：%s" % root
    hours = _hours_since(ts)
    return hours <= COMMON_BACKUP_H, "最新 %s 前 · %d 个文件 · %s" % (
        _hm(hours), n, os.path.basename(best))


# ── 12 磁盘 ──────────────────────────────────────────────────────────
DISK_MIN_FREE = 10.0


def item_disk():
    """盘满会以各种奇怪方式坏，而且原因难查：只看还剩百分之几。"""
    import shutil as _sh
    drives = []
    sd = os.environ.get("SystemDrive") or "C:"
    drives.append((sd, sd + "\\"))
    rd = os.path.splitdrive(ROOT)[0] or "D:"
    if rd.lower() != sd.lower():
        drives.append((rd, rd + "\\"))
    bad, bits = [], []
    for name, path in drives:
        try:
            u = _sh.disk_usage(path)
        except OSError:
            continue
        pct = 100.0 * u.free / u.total if u.total else 0.0
        bits.append("%s 剩 %.0f%%（%.1fG 空闲）" % (name, pct, u.free / float(1 << 30)))
        if pct < DISK_MIN_FREE:
            bad.append(name)
    if not bits:
        return False, "取不到磁盘用量"
    return not bad, " · ".join(bits) + (" · 低于 %.0f%%：%s" % (DISK_MIN_FREE, ",".join(bad)) if bad else "")


# ── 13 计划任务 ──────────────────────────────────────────────────────
def _task_names():
    """枚举 SysPersona-* 计划任务名。取不到就返回 None（判 skip 由上层处理）。"""
    import subprocess as _sp
    try:
        r = _sp.run(["schtasks", "/query", "/fo", "csv", "/nh"],
                    capture_output=True, text=True, encoding="mbcs", errors="replace",
                    timeout=25)
    except (OSError, _sp.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return sorted(set(re.findall(r"SysPersona[-A-Za-z0-9_]+", r.stdout or "")))


def item_tasks():
    """计划任务还在不在：被删、被系统停用，肉眼看不出来，但开机就再也不自启了。

    第一次跑把当时的任务名记成基线（ops\\selfcheck-tasks.json）；以后只比较
    「基线里有的、现在少了的」。新增任务不算异常——只防丢，不妨碍加。
    """
    names = _task_names()
    if names is None:
        return False, "取不到计划任务列表（schtasks 不可用）"
    base = _read_json(TASKS_BASE) or {}
    known = base.get("names") or []
    if not known:
        try:
            _write_json(TASKS_BASE, {"at": _now(), "names": names})
        except OSError:
            pass
        return True, "已记基线：%d 个 SysPersona-* 任务" % len(names)
    lost = [n for n in known if n not in names]
    added = [n for n in names if n not in known]
    detail = "%d 个任务（基线 %d）" % (len(names), len(known))
    if added:
        detail += " · 新增 %s" % "、".join(added[:3])
    if lost:
        return False, detail + " · 少了 %s" % "、".join(lost[:3])
    return True, detail


# ── 14 模型 ──────────────────────────────────────────────────────────
MODEL_PROBE_TTL = 3600.0       # 1 小时探一次：够快发现问题，又不至于每分钟打网络


def item_model():
    """免费档/本地档还能不能用：换档后不知道能不能用，等你要用时才发现最糟。

    探测只 GET `/v1/models`——不花 token、不占并发；结果缓存 1 小时。
    两个档有一个活就算 OK：本地档优先（不走网络），再看免费档（要 key）。
    """
    cache = _read_json(MODEL_PROBE) or {}
    if cache and (_hours_since(cache.get("ts") or 0) * 3600.0) < MODEL_PROBE_TTL:
        return bool(cache.get("ok")), "（1h 内缓存）" + str(cache.get("detail") or "")
    # 本机策略：4G 内存的小主机不启用本地档（权重十几 GB，跑起来就把机器拖死）。
    # 这是人定的，不是故障——就不去探 8080 了；但**免费档照样要验**，
    # 因为这台机器真正在用的就是免费档（人 2026-11-12 给了 key）。
    try:
        import persona_root as _R
        local_on, local_why = _R.client_policy("local_model")
    except Exception:                             # noqa: BLE001
        local_on, local_why = True, ""
    bits, ok, rows = [], False, []
    if not local_on:
        bits.append("本地档本机不启用（%s）" % (local_why or "人定"))
    try:
        import persona_llm as L
    except Exception as e:                        # noqa: BLE001
        return False, "加载 persona_llm 失败：%s" % str(e)[:60]
    if local_on:
        try:
            rows = L.local_models() or []
        except Exception as e:                    # noqa: BLE001  本地档没起是常态，不是错误
            rows = []
            bits.append("本地档没起（%s）" % str(e)[:30])
    if rows:
        ok = True
        bits.insert(0, "本地档 %d 个模型" % len(rows))
    try:
        k = L.sf_key() if hasattr(L, "sf_key") else ""
        key = k[0] if isinstance(k, (tuple, list)) else k      # sf_key() 返回 (key, 来源)
        if key:
            data = L._json(L.SF_BASE.rstrip("/") + "/models",
                           headers={"Authorization": "Bearer " + key}, timeout=8)
            d = data[1] if isinstance(data, (tuple, list)) and len(data) == 2 else data
            n = len((d or {}).get("data") or [])
            if n:
                ok = True
            bits.append("免费档 %d 个模型" % n)
        else:
            bits.append("免费档没配 key")
    except Exception as e:                        # noqa: BLE001
        bits.append("免费档不通（%s）" % str(e)[:40])
    detail = " · ".join(bits)
    if not ok and not rows and any("没配 key" in b for b in bits):
        # 本机既没起本地档、也没配免费档 key：这是「这台机器不用这两档」，不是故障。
        detail = "跳过：本地档没起且没配 SILICONFLOW_API_KEY"
        ok = True
    try:
        _write_json(MODEL_PROBE, {"ts": time.time(), "ok": ok, "detail": detail})
    except OSError:
        pass
    return ok, detail


# ── 8 台账 ───────────────────────────────────────────────────────────
def item_manifest():
    """改动必须留痕（人 2026-11-12）：代码/配置和基线清单对不上、又没 --record 登记，这项就红。

    基线本体在 persona_manifest.py：ops\\manifest.json（指纹）+ ops\\manifest-changes.jsonl（台账）
    + 备份\\自检基线\\<rev>\\（改动前的版本）。这里只做一次比对，不动任何文件。
    """
    try:
        import persona_manifest as M
        return M.check(brief=True)
    except Exception as e:                       # 基线模块坏了也要说清楚，不能静默放过
        return False, "基线模块出错：%s" % str(e)[:100]


def item_paths():
    """路径变量和实际目录对不对得上（人 2026-11-12 定：「其他环境第一次重读，如果这个配置不对，
    就直接创建或者修改这个变量」）。

    这一项**会动手修**：变量指向不存在 / 过时的目录时，按本文件所在位置把用户环境变量写回去。
    换机器、搬目录之后不用人记得设变量——重读/自检各跑一次就自愈了。
    """
    try:
        import persona_root as R
        rows = R.heal_env(fix=True)
    except Exception as e:                       # noqa: BLE001
        return False, "路径模块出错：%s" % str(e)[:100]
    bad = [r for r in rows if r[3] not in ("ok", "修好了")]
    fixed = [r for r in rows if r[3] == "修好了"]
    if bad:
        return False, "；".join("%s %s" % (r[0], r[3]) for r in bad)
    if fixed:
        return True, "已修正 %d 个：%s" % (len(fixed), "、".join(r[0] for r in fixed))
    return True, "路径变量都对得上（%d 项）" % len(rows)


def item_memory():
    r"""记忆副本（人 2026-11-13 定：副本给他读，原本我们写，最后检测副本值得吸收的）。

    判三条：
      ① 副本目录在不在（不在 = 他从没拿到记忆，属于真故障）；
      ② `MEMORY.md` / `USER.md` 副本与原本是否一致——**不一致就当场用原本覆盖**
         （人 2026-11-13：「每次检查都要覆盖，别忘记了」；检查就顺手治好，
         不留给下一拍，也不只是报个红然后等人）；
      ③ 有没有待审的入库申请（**有不算红**，那是正常的"他想记点什么"，只提示）。
    """
    try:
        import persona_memsync as MS
    except Exception as e:                      # noqa: BLE001
        return False, "导入 persona_memsync 失败：%s" % str(e)[:80]
    try:
        copy = MS.mirror_dir()
        if not os.path.isdir(copy):
            return False, "副本目录不存在：%s（跑 persona_memsync.py sync）" % copy
        diffs = [n for n in MS.FILES
                 if MS._sha(os.path.join(MS.SRC_DIR, n)) != MS._sha(os.path.join(copy, n))]
        healed = ""
        if diffs:
            # ① 覆盖前先吸收（别把"他想记的"直接冲掉）
            try:
                MS.sync(quiet=True)
            except Exception:                   # noqa: BLE001
                pass
            # ② 再硬覆盖一次：sync 正常时这一步是空操作，异常时它是兜底
            for n in diffs:
                try:
                    with io.open(os.path.join(MS.SRC_DIR, n), "r", encoding="utf-8-sig") as f:
                        text = f.read()
                    MS._write(os.path.join(copy, n), text)
                except Exception:               # noqa: BLE001
                    pass
            still = [n for n in MS.FILES
                     if MS._sha(os.path.join(MS.SRC_DIR, n)) != MS._sha(os.path.join(copy, n))]
            if still:
                return False, "副本与原本不一致，覆盖也失败：%s（查权限/占用）" % "、".join(still)
            healed = "已用原本覆盖 %s · " % "、".join(diffs)
        pend = [r for r in MS._read_inbox() if r.get("status") == "pending"]
        st = MS._load_state()
        if pend:
            return True, "%s副本一致 · 有 %d 条入库申请待审（persona_memsync.py inbox）" % (healed, len(pend))
        return True, "%s副本一致 · 无待审（上次同步 %s）" % (healed, st.get("at") or "—")
    except Exception as e:                      # noqa: BLE001
        return False, "查不动：%s" % str(e)[:80]


def item_config():
    r"""配置一致（2026-11-14 加）：版本号有唯一源；托盘客户端真源与副本不漂移；机器档在。

    为什么：多机以后「每个地方都不一样」是最难查的一类问题——先让它变成一条红/绿的检查项。
    """
    try:
        import persona_store as S
        vp = os.path.join(ROOT, "配置", "版本.json")
        if not os.path.isfile(vp):
            return False, "缺 配置\\版本.json（版本号没有唯一可读源）"
        ver = (_read_json(vp) or {}).get("version") or "?"
        notes = []
        # 2026-11-15：版本按时间算，且**内容指纹必须与登记一致**——
        # 不一致就说明"改了没 bump"，别的机器看库还以为没变（这正是最难查的那种）。
        try:
            import persona_ver as _V
            doc = _V._load()
            if (doc.get("hash") or "") != _V.fingerprint():
                notes.append("内容与登记的版本不一致（改了没 bump：跑 persona_ver.py bump \"说明\"）")
        except Exception:                  # noqa: BLE001
            notes.append("版本中心读不动（persona_ver 导入失败）")
        a = os.path.join(__import__("persona_ops").dir(), "tray-clients.json")
        b = os.path.join(ROOT, "配置", "客户端", "tray-clients.json")
        if os.path.isfile(a) and os.path.isfile(b):
            import hashlib
            ha = hashlib.sha256(open(a, "rb").read()).hexdigest()
            hb = hashlib.sha256(open(b, "rb").read()).hexdigest()
            if ha != hb:
                notes.append("tray-clients 真源与副本不一致（以 ops 为准，建议 export）")
        mc = os.path.join(ROOT, "配置", "机器", S.machine_id() + ".json")
        if not os.path.isfile(mc):
            notes.append("本机机器档缺失（跑 persona_machine.py new）")
        return (not notes), "版本 %s · 本机 %s%s" % (
            ver, S.machine_id(), ("；" + "；".join(notes)) if notes else "")
    except Exception as e:                     # noqa: BLE001
        return False, "查不动：%s" % str(e)[:80]


def item_isolation():
    r"""运行态隔离（2026-11-14 加）：别人机器目录里 24h 内有没有被本机写过。

    人原话：「每个新机器只能往自己的目录里面写，所以不会出现打架的问题」——这条要能被看见。
    """
    try:
        import persona_store as S
        bad = S.audit()
        if not bad:
            return True, "没有越权写入（本机只写 %s 自己的目录）" % S.machine_id()
        return False, "越权迹象 %d 个：%s" % (len(bad), os.path.basename(bad[0]))
    except Exception as e:                     # noqa: BLE001
        return False, "查不动：%s" % str(e)[:80]


# ── 19 DSH 配置与微信落点（2026-11-15 人：「那就要把这一项加到检查里面」「第一次部署不能漏」）──
def _dsh_sessions_root():
    home = os.environ.get("DSH_HOME") or os.path.join(os.path.expanduser("~"), ".dsh")
    return os.path.join(home, "sessions")


def item_dshcfg():
    r"""两件事：① 真源 `配置\dsh\` 与 DSH 侧副本一致；② 没有未归档的会话。

    ① 防的是「DSH 升级/重装把 profile 重置了」——真源在库里，`persona_dshsync` 会覆盖回去。
    ② 防的是「多个客户端/临时工作区的会话没进库」——人 2026-11-15 定 B 方案：
       不做工作区绑定，改成 `persona_archive` 横扫 `$DSH_HOME\sessions\*`，
       微信（dsh-im）、桌面、以及临时换过的目录一律自动归档。这里只判"还有几个没归档"。
    """
    bad = []
    try:
        import persona_dshsync as DS
        st = DS.status()
        for m in st.get("detail") or []:
            bad.append(m)
    except Exception as e:                     # noqa: BLE001
        return False, "查不动：%s" % str(e)[:80]
    try:
        import persona_archive as PA
        left = PA.pending()
        if left:
            bad.append("%d 个会话未归档（微信/临时工作区也算）" % len(left))
    except Exception as e:                     # noqa: BLE001
        bad.append("查归档失败：%s" % str(e)[:40])
    # ③ 库里给 DSH 吃的 JSON 不能带 BOM：PowerShell 5 的 Set-Content -Encoding UTF8
    #    会写 BOM，DSH 插件加载器用 JSON.parse 读会直接报 "Unexpected token ﻿"
    #    （2026-11-15 实测：persona-inbox 的 locale/*.json 就这么炸过一次）。
    try:
        import glob as _g
        bad_bom = []
        for p in _g.glob(os.path.join(ROOT, "配置", "dsh", "**", "*.json"), recursive=True):
            try:
                with open(p, "rb") as f:
                    if f.read(3) == b"\xef\xbb\xbf":
                        bad_bom.append(os.path.basename(p))
            except OSError:
                continue
        if bad_bom:
            bad.append("带 BOM 的 JSON 会加载失败：%s" % "、".join(bad_bom[:4]))
    except Exception:                          # noqa: BLE001
        pass
    if bad:
        return False, "；".join(bad)[:180]
    return True, "真源一致；会话全部已归档"


# ── 20 两段式（本机 → 全局）（2026-11-15 人：「先改自己的，再去改全局的」）──
def item_pub():
    r"""两个版本是否对齐：全局只读版本 / 本机版本 / 待发布记录 / 全局台账有没有过期。

    红的两类：
      * 内容变了却没有任何本机记录（改了没 `bump`）；
      * 有待发布记录，但本机不是使用中的机器（那说明该在使用中的那台发布）。
    """
    bad, okmsg = [], []
    try:
        import persona_ver as V
        g = V.current()
        loc = V.local()
        pend = V.pending()
        ok, why = V.allowed()          # 2026-11-16：判"允不允许"，不是"在不在跑"
        lag = (g.get("hash") != V.fingerprint())
    except Exception as e:                     # noqa: BLE001
        return False, "查不动：%s" % str(e)[:80]
    if lag and not pend:
        bad.append("内容变了但没有本机记录（跑 persona_ver.py bump）")
    if pend:
        if ok:
            okmsg.append("待发布 %d 条（本机被允许，守护会发布）" % len(pend))
        else:
            bad.append("%d 条没发布且本机未被允许改全局：%s" % (len(pend), why))
    fol = loc.get("follows") or {}
    if fol.get("rev") and g.get("rev") and fol["rev"] != g["rev"]:
        bad.append("本机跟随落后全局（%s → %s）" % (fol.get("version"), g.get("version")))
    # 全局只读台账要比各机器本机账新，否则就是"汇总没跟上"。
    # 但本机在用时会由守护自动 publish（300 秒一拍），这种"刚登记还没汇总"只提示、不算红。
    stale_sum = ""
    try:
        gm = os.path.join(ROOT, "ops", "变更台账.md")
        if os.path.isfile(gm):
            gm_t = os.path.getmtime(gm)
            for name in os.listdir(os.path.join(ROOT, "ops")):
                p = os.path.join(ROOT, "ops", name, "manifest-changes.jsonl")
                if os.path.isfile(p) and os.path.getmtime(p) > gm_t + 60:
                    stale_sum = name
                    break
        else:
            stale_sum = "（还没生成）"
    except Exception as e:                     # noqa: BLE001
        bad.append("查台账失败：%s" % str(e)[:40])
    if stale_sum:
        if ok:
            okmsg.append("全局台账待汇总（%s，守护会自动跑 --publish）" % stale_sum)
        else:
            bad.append("全局台账比 %s 的本机账旧（跑 --publish）" % stale_sum)
    if bad:
        return False, "；".join(bad)[:180]
    return True, "；".join(okmsg + ["全局 v%s" % (g.get("version") or "?")])[:120]


# ── 守护代码新鲜度（2026-11-15 加）────────────────────────────────────
def item_codefresh():
    r"""守护进程起得比脚本旧 = 它还在跑旧代码。

    起因（2026-11-15）：`persona_inbox_client.py` 15:44、`persona_selfcheck.py` 21:10 都改过，
    但守护 10:02 就起来了；Python 不重读已 import 的模块 → **文件是新的、跑的是旧的**，
    注入口径一直停在 11-10 那版。
    判据：`脚本\*.py` 里最新的 mtime 比守护启动晚 60 秒以上 → 红（重启守护即生效）。
    守护启动时间 = STATUS 的 `uptime_s` 反推；读不到就退到 `persona.lock` 的 mtime。
    """
    started = None
    st = _read_json(STATUS) or {}
    up = st.get("uptime_s")
    if up is not None:
        try:
            started = time.time() - float(up)
        except (TypeError, ValueError):
            started = None
    if started is None:
        try:
            started = os.path.getmtime(os.path.join(OPS, "persona.lock"))
        except OSError:
            return True, "跳过：读不到守护启动时间"
    try:
        names = [n for n in os.listdir(os.path.join(ROOT, "脚本"))
                 if n.endswith(".py") and not n.startswith("_")]
    except OSError:
        return True, "跳过：读不到脚本目录"
    late, newest, newest_name = [], -1.0, ""
    for n in names:
        try:
            m = os.path.getmtime(os.path.join(ROOT, "脚本", n))
        except OSError:
            continue
        if m > newest:
            newest, newest_name = m, n
        if m > started + 60:
            late.append(n)
    started_s = time.strftime("%m-%d %H:%M", time.localtime(started))
    if not late:
        return True, "守护起于 %s，脚本没有比它新的（最新 %s）" % (started_s, newest_name or "?")
    late.sort()
    return False, ("守护起于 %s，之后 %d 个脚本改过（%s…）——它还在跑旧代码，重启守护即生效"
                   % (started_s, len(late), "、".join(late[:3])))


# ── 待办是否分散（2026-11-16 加）────────────────────────────────────
def item_todoscatter():
    r"""待办只能有一处真源：`文档\BACKLOG.md`；摊平的只读副本：`续接\待办备忘.md`。

    人 2026-11-16：「现在太分散，没有一个统一的入口，所以目前不同的机器不同的写法，
    每次待办也没添加到全局可读的位置」。
    所以：除上面两个 + **契约地址 `待办\inbox\<机器id>.md`（B102 ①，非节点只能写自己那份）**
    之外，任何名字像待办的文件（今日要做 / TODO / 待办备忘 / 待办清单…）都判红。
    """
    allow = {"续接/待办备忘.md"}
    try:
        import persona_ops as _po
        me = _po.dir_name()
    except Exception:                                   # noqa: BLE001
        me = ""
    skip = re.compile(r"\\(sessions|archive|备份|粘贴|index|清理|SysPersona-Runtime|node_modules|tools)\\", re.I)
    hits, others = [], []
    for dp, dns, fns in os.walk(ROOT):
        dns[:] = [d for d in dns if not skip.search(os.sep + d + os.sep)]
        for fn in fns:
            low = fn.lower()
            if not low.endswith((".md", ".jsonl")):
                continue
            if not re.search(r"(待办|今日要做|todo)", low):
                continue
            rel = os.path.relpath(os.path.join(dp, fn), ROOT).replace(os.sep, "/")
            if rel in allow or rel.startswith("文档/历史/") or rel.startswith("待办/inbox/"):
                continue
            # 别人机器的目录不归我管：B102 ①「写只写自己的」——我连提示都只提示，不判红
            parts = rel.split("/")
            if len(parts) > 1 and parts[0] == "ops" and me and parts[1] != me:
                others.append(rel)
                continue
            hits.append(rel)
    if hits:
        return False, ("待办散在 %d 处：%s —— 并进 `文档\\BACKLOG.md`"
                       "（`python 脚本\\persona_todo.py add \"标题\"`）"
                       % (len(hits), "、".join(sorted(hits)[:4])))
    if others:
        return True, ("本机与全局区没有分散；别的机器目录里还有 %d 份（不归我动，等那台自己并）：%s"
                      % (len(others), "、".join(sorted(others)[:3])))
    return True, "只有 BACKLOG（真源）+ 待办备忘（副本）+ 各机 inbox（契约地址）"


# ── 全局默认只读（2026-11-16 加）────────────────────────────────────
GLOBAL_RO = (
    "文档/BACKLOG.md", "配置/版本.json", "配置/全局写许可.json",
    "ops/变更台账.md", "ops/全局索引.md",
    "文档/索引-全部文档.md", "续接/待办备忘.md",
)
GLOBAL_WRITERS = ("persona_ver.py", "persona_manifest.py", "persona_gindex.py",
                  "persona_todo.py", "persona_docindex.py", "persona_handoff.py",
                  "persona_state.py", "persona_selfupdate.py")


def item_globalro():
    r"""全局**默认只读**（人 2026-11-16：「全局的默认是只读的」+「不是运行，是允许」）。

    **能不能写 = 有没有被允许**（`配置\全局写权限.json` 的 `holders`），不是"守护在跑/心跳新鲜"。
    默认只读清单：`文档\BACKLOG.md`、`配置\版本.json`、`配置\全局写权限.json`、`ops\变更台账.md`、
    `ops\全局索引.md`、`文档\索引-全部文档.md`、`续接\待办备忘.md`。
    就算被允许，也必须**跑脚本**（`guard_global` 那一关），不许手改、不许搬别人的文件。

    本项只查机械的事：
      1) 清单里的文件都在（没被搬走、没改名）；
      2) 每个**能写全局**的脚本源码里都问过那一句（`guard_global(` / `allowed(` / `in_use(`）；
      3) `holders` 只该有一台（两台同时写就是同步冲突的根源）。
    """
    missing = [rel for rel in GLOBAL_RO
               if not os.path.exists(os.path.join(ROOT, rel.replace("/", os.sep)))]
    unguarded = []
    for name in GLOBAL_WRITERS:
        p = os.path.join(ROOT, "脚本", name)
        try:
            src = io.open(p, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        if "guard_global(" not in src and "allowed(" not in src and "in_use(" not in src:
            unguarded.append(name)
    try:
        import persona_ver as _v
        can, why = _v.guard_global("全局")
        holders = list((_v._load(os.path.join(ROOT, "配置", "全局写权限.json")) or {}).get("holders") or [])
    except Exception as e:                              # noqa: BLE001
        can, why, holders = False, "判不了（%s）" % str(e)[:60], []
    head = why if can else ("本机未被允许：%s" % why)
    bad = []
    if missing:
        bad.append("%d 份清单文件不见了：%s" % (len(missing), "、".join(missing[:3])))
    if unguarded:
        bad.append("%d 个写全局的脚本没问「能不能写」：%s" % (len(unguarded), "、".join(unguarded)))
    if len(holders) > 1:
        bad.append("holders 有 %d 台（%s）——同时写就是同步冲突，只该留一台"
                   % (len(holders), "、".join(holders)))
    if bad:
        return False, head + " · " + " · ".join(bad)
    return True, head + " · %d 份清单齐 · %d 个写者都有守卫 · holders=%s" % (
        len(GLOBAL_RO), len(GLOBAL_WRITERS), "、".join(holders) or "空")


def item_cmdq():
    r"""**客户端命令队列**（人 2026-11-16：「自检加一个功能进去，用来接收客户端下的命令，重新拉起」）。

    命令通道早就有了：`ops\<机器>\node-cmd.jsonl` 写、`persona_node_cmd.py drain` 执行
    （守护的 `nodedrain` 每分钟跑一次）。这里让**自检也成为一个执行点**：
      · 队列里有东西 → 顺手 drain（带锁，跟守护不会重复执行）；
      · 队列空 → 报最近 3 条回执，方便看"上一次下的命令到底成没成"。
    支持的命令里已有 `client restart name=DSH`（= 杀 + 拉起，走 `persona_watchdog.enact('restart')`），
    也就是"通过命令重新拉起客户端"这条路是通的。
    """
    try:
        import glob
        import persona_node_cmd as NC
    except Exception as e:                              # noqa: BLE001
        return False, "导入命令队列失败：%s" % str(e)[:80]
    try:
        os.makedirs(NC.INBOX, exist_ok=True)
        pend = sorted(glob.glob(os.path.join(NC.INBOX, "*.json")))
    except Exception as e:                              # noqa: BLE001
        return False, "读不了收件箱：%s" % str(e)[:80]
    if not pend:
        rec = []
        try:
            rec = [r for r in (NC.recent(3) or []) if r.get("cmd")]
        except Exception:                               # noqa: BLE001
            pass
        tail = "；".join("%s=%s" % (r.get("cmd"), "OK" if r.get("ok") else "失败") for r in rec)
        return True, "命令队列空（守护每分钟 drain；最近：%s）" % (tail or "还没执行过")
    lock = os.path.join(OPS, "node-cmd.drain.lock")
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)   # 跟守护抢同一条命令
        os.close(fd)
    except FileExistsError:
        try:
            if time.time() - os.path.getmtime(lock) > 120:
                os.remove(lock)
                return item_cmdq()
        except OSError:
            pass
        return True, "队列里有 %d 条，但另一个 drain 正在跑（有锁），这拍不动手" % len(pend)
    try:
        done = NC.drain(limit=5)
    except Exception as e:                              # noqa: BLE001
        return False, "执行命令失败：%s" % str(e)[:100]
    finally:
        try:
            os.remove(lock)
        except OSError:
            pass
    bad = [r for r in done if not r.get("ok")]
    brief = "；".join("%s:%s" % (r.get("cmd"), (r.get("msg") or "")[:40]) for r in done)
    if bad:
        return False, "执行 %d 条，失败 %d 条（%s）" % (len(done), len(bad), brief)
    return True, "执行 %d 条命令都成功（%s）" % (len(done), brief)


def item_todo():
    r"""**该做什么**（人 2026-11-16：「需要做什么，的自检项有吗？」）——自检不只报"哪里坏了"，也要报"还有什么没做"。

    三处来源：
      ① 本机待办 `待办\inbox\<机器id>.md` 的未完成条数 + 最老那条多久前；
      ② 交接账未完成（`persona_handoff.open_items()`）；
      ③ 待批的全局写申请（`ops\<机器id>\全局写申请\*.json` 里 status=pending）。
    判红条件：有待批申请（等人点头）或本机待办最老的超过 24 小时。
    """
    mid = os.path.basename(OPS)
    mine = os.path.join(ROOT, "待办", "inbox", mid + ".md")
    n_inbox, oldest_h = 0, None
    try:
        if os.path.exists(mine):
            t = io.open(mine, encoding="utf-8").read()
            for ln in t.splitlines():
                if ln.strip().startswith("- [ ]"):
                    n_inbox += 1
            age = (time.time() - os.path.getmtime(mine)) / 3600.0
            oldest_h = round(age, 1) if n_inbox else None
    except OSError:
        pass
    n_hand = 0
    try:
        import persona_handoff as H
        n_hand = len(H.open_items() or [])
    except Exception:                                    # noqa: BLE001
        pass
    n_app = 0
    try:
        req_dir = os.path.join(OPS, "全局写申请")
        if os.path.isdir(req_dir):
            for f in os.listdir(req_dir):
                if not f.endswith(".json"):
                    continue
                try:
                    d = json.load(io.open(os.path.join(req_dir, f), encoding="utf-8"))
                    if (d.get("status") or "pending") == "pending":
                        n_app += 1
                except ValueError:
                    continue
    except OSError:
        pass
    n_an = 0
    try:
        import persona_anomaly as AN
        n_an = len(AN.pending())
    except Exception:                                    # noqa: BLE001
        n_an = 0
    parts = ["本机待办 %d 条%s" % (n_inbox, "" if oldest_h is None else "（最老 %.1f 小时）" % oldest_h),
             "交接未完成 %d 条" % n_hand,
             "全局申请待批 %d 条" % n_app,
             "全局异常待批 %d 条" % n_an]
    detail = "该做什么：" + " · ".join(parts)
    bad = bool(n_app) or bool(n_an) or (oldest_h is not None and oldest_h > 24)
    if bad:
        detail += "｜先办：%s" % ("批/撤全局申请" if n_app else "把超过 24 小时的待办收掉")
    return (not bad), detail


def item_plugin():
    r"""**插件副本一致性**（人 2026-11-16：「比如其他机器更新或新装了插件」）。

    库里的源在 `配置\客户端\plugins\<插件>\`（随库同步，别的机器看得见）；
    壳真正加载的是 `~\.dsh\profiles\desktop\node_modules\@syspersona\<插件>\`。
    两份不一致 = 改了没生效（2026-11-16 踩了整整一上午：改 6 次都不生效，因为壳读的是 node_modules 那份）。
    判红：库里有插件、运行副本缺失或 MD5 不同；或 profile 的 patch 没引用某个插件。
    """
    src_root = os.path.join(ROOT, "配置", "客户端", "plugins")
    nm = os.path.expanduser(os.path.join("~", ".dsh", "profiles", "desktop", "node_modules", "@syspersona"))
    patch = os.path.expanduser(os.path.join("~", ".dsh", "profiles", "desktop", "cordis.patch.yml"))
    import hashlib
    if not os.path.isdir(src_root):
        return True, "库里没有客户端插件目录（跳过）"
    try:
        names = [d for d in os.listdir(src_root) if os.path.isdir(os.path.join(src_root, d))]
    except OSError as e:
        return False, "读不了库内插件目录：%s" % str(e)[:80]
    if not names:
        return True, "库里没有客户端插件（0 个）"
    try:
        patch_text = io.open(patch, encoding="utf-8-sig").read()
    except OSError:
        patch_text = ""
    pkg = os.path.expanduser(os.path.join("~", ".dsh", "profiles", "desktop", "package.json"))
    try:
        bundle_text = io.open(pkg, encoding="utf-8-sig").read()
    except OSError:
        bundle_text = ""
    stale, missing, unreferenced = [], [], []
    for n in names:
        s = os.path.join(src_root, n, "index.js")
        r = os.path.join(nm, n, "index.js")
        if not os.path.exists(r):
            missing.append(n)
        elif os.path.exists(s):
            try:
                hs = hashlib.md5(io.open(s, "rb").read()).hexdigest()
                hr = hashlib.md5(io.open(r, "rb").read()).hexdigest()
                if hs != hr:
                    stale.append(n)
            except OSError:
                pass
        # 已加载 = 出现在 profile patch **或** profile package.json 的 dsh.profile.bundles（bundle 自带条目）
        if patch_text:
            in_patch = n in patch_text
            in_bundles = n in bundle_text
            if not (in_patch or in_bundles):
                unreferenced.append(n)
    bits = ["库内插件 %d 个" % len(names)]
    if stale:
        bits.append("待同步 %d（%s）" % (len(stale), "、".join(stale[:4])))
    if missing:
        bits.append("没部署 %d（%s）" % (len(missing), "、".join(missing[:4])))
    if unreferenced:
        bits.append("未加载 %d（%s）" % (len(unreferenced), "、".join(unreferenced[:4])))
    if not (stale or missing or unreferenced):
        bits.append("运行副本一致")
    bad = bool(stale or missing or unreferenced)
    detail = "插件：" + " · ".join(bits)
    # 规范 §八：这一类**自动做**——库内源 → 运行副本是无损、可重做的复制
    if stale or missing:
        fixed = []
        for n in (stale + missing):
            s = os.path.join(src_root, n, "index.js")
            d = os.path.join(nm, n, "index.js")
            try:
                if not os.path.isfile(s):
                    continue
                os.makedirs(os.path.dirname(d), exist_ok=True)
                if os.path.isfile(d):
                    shutil.copy2(d, d + ".bak-%s" % time.strftime("%Y%m%d-%H%M%S"))
                shutil.copy2(s, d)
                fixed.append(n)
            except OSError:
                continue
        if fixed:
            detail += "｜**已自动同步运行副本**：%s（重启壳后生效）" % "、".join(fixed)
            bad = bool(unreferenced)      # 同步完只剩"没被 patch 引用"才算真红
    if bad:
        detail += "｜同步法：把库内 index.js 复制到 node_modules\\@syspersona\\<插件>\\，再重启壳"
    return (not bad), detail


def item_docmap():
    r"""**孤儿文档**（人 2026-11-16：「做完什么事情，要写在哪里，别人才能读到，不会造成那种孤儿文档」）。

    判据：`文档\*.md` 里的每一份，至少要在这三处之一被提到——
      ① `README.md`（唯一入口）② `文档\00-入口.md`（详细地图）③ `memory\设定\00-索引.md` §3.2（AI 的文档地图）
    外加自动生成的 `文档\索引-全部文档.md` 也会收录。一处都没提到的 = 孤儿：写的人知道，别人永远找不到。
    判红：存在孤儿（列前 6 个）。
    """
    doc_dir = os.path.join(ROOT, "文档")
    if not os.path.isdir(doc_dir):
        return True, "没有 文档\\ 目录（跳过）"
    hubs = [os.path.join(ROOT, "README.md"),
            os.path.join(doc_dir, "00-入口.md"),
            os.path.join(ROOT, "memory", "设定", "00-索引.md"),
            os.path.join(doc_dir, "索引-全部文档.md")]
    blob = ""
    used = []
    for h in hubs:
        try:
            blob += io.open(h, encoding="utf-8-sig").read()
            used.append(os.path.basename(h))
        except OSError:
            continue
    try:
        names = [n for n in os.listdir(doc_dir)
                 if n.lower().endswith(".md") and os.path.isfile(os.path.join(doc_dir, n))]
    except OSError as e:
        return False, "读不了 文档\\：%s" % str(e)[:80]
    orphans = [n for n in names if n not in blob
               and n not in ("00-入口.md", "索引-全部文档.md")]
    detail = "文档 %d 份，地图（%s）覆盖 %d 份" % (
        len(names), "/".join(used) or "无", len(names) - len(orphans))
    if orphans:
        detail += "｜**孤儿 %d**：%s%s" % (
            len(orphans), "、".join(orphans[:6]), "…" if len(orphans) > 6 else "")
        detail += "｜补法：在 00-索引 §3.2 加一行（或并入更该在的那份），再跑 persona_docindex.py"
        return False, detail
    return True, detail


def item_sync():
    r"""**全局配置有没有更新、本机跟到哪**（人 2026-11-16：「什么时候取检测全局配置是否更新，在读取到本地」）。

    比对 `配置\版本.json` 的 rev 与本机 `ops\<机器id>\同步.json` 记的 rev；
    落后就判红，并列出落后的条目（谁改的、改了什么），提示去读变更台账。
    文件本身的搬运仍交给 FreeSurround 同步批处理（30 §十三），这里只比对 + 提醒。
    """
    try:
        import persona_sync as SY
        r = SY.check()
    except Exception as e:                               # noqa: BLE001
        return False, "同步检查失败：%s" % str(e)[:90]
    if r["ok"]:
        return True, "全局 rev %d · %s；本机已跟到 rev %d（最新）" % (
            r["global_rev"], r["global_version"], r["local_rev"])
    tail = "、".join("rev %s" % c.get("rev") for c in (r["behind"] or [])[-4:])
    return False, ("全局 rev %d · %s；本机只到 rev %d → **落后 %d 条**（%s）｜读 `ops\\变更台账.md` 最近几节，"
                   "跟完跑 `persona_sync.py --ack`" % (
                       r["global_rev"], r["global_version"], r["local_rev"], len(r["behind"]), tail))


def item_compact():
    r"""**A/B 压缩线**：A=壳自己 0.5 早压，B=我们 0.8 保底（人 2026-11-16 定）。

    只要 `ops\<机器id>\compact-anomaly.json` 存在，就说明水位真涨到了 0.8——
    A 本该在 0.5 就压掉 → **A 没生效**：判红，并把当时的数字报出来。
    """
    p = os.path.join(OPS, "compact-anomaly.json")
    if not os.path.exists(p):
        # 数字以 waterline.json 为准（它知道真实窗口；这里直接调 window_tokens() 会退回 131072）
        wl = os.path.join(OPS, "waterline.json")
        try:
            w = json.load(io.open(wl, encoding="utf-8-sig"))
            return True, "A/B 正常：壳线 %.0fK、保底线 %.0fK，当前 %.1f%%（%s）" % (
                float(w.get("shell_line") or 0) / 1000.0, float(w.get("limit") or 0) / 1000.0,
                float(w.get("pct") or 0), w.get("at", ""))
        except (OSError, ValueError):
            pass
        try:
            import persona_waterline as W
            return True, "A/B 正常：壳线 %.0fK，保底线 %.0fK（尚未触发）" % (
                W.compress_threshold() / 1000.0, W.warn_line() / 1000.0)
        except Exception:                                # noqa: BLE001
            return True, "A/B 正常：没有异常留痕"
    try:
        d = json.load(io.open(p, encoding="utf-8-sig"))
    except (OSError, ValueError):
        return False, "有 compact-anomaly.json 但读不出来（看一眼 ops\\<机器id>\\compact-anomaly.json）"
    return False, ("**A 没生效**：%s 水位到 %.1f%%（%s / 上限 %s），B 已代压｜"
                   "查壳的 compaction-basic 是否加载、thresholdRatio 是否 0.5" % (
                       d.get("at", ""), float(d.get("pct") or 0),
                       d.get("tokens"), d.get("limit")))


def item_inbox():
    r"""**注入通道异常**（人 2026-11-16：「如果同类被覆盖就要写日志，并报异常」）。

    两处信号：
      · `ops\<机器id>\inbox-anomaly.json` —— 有请求因"同类同目标"被后一条顶掉（累积计数）；
      · `ops\<机器id>\<通道>\inbox-superseded.jsonl` —— 每次被顶掉的明细。
    判红：存在 anomaly 且最近 6 小时内有新增（老账不反复报，但会写在 detail 里）。
    """
    ap = os.path.join(OPS, "inbox-anomaly.json")
    if not os.path.exists(ap):
        return True, "注入通道无异常（没有被同类覆盖的请求）"
    try:
        d = json.load(io.open(ap, encoding="utf-8-sig"))
    except (OSError, ValueError):
        return False, "有 inbox-anomaly.json 但读不出来（看一眼 ops\\<机器id>\\inbox-anomaly.json）"
    last = d.get("last") or {}
    kinds = d.get("kinds") or {}
    txt = "注入通道：累计 %d 条被同类覆盖（%s）｜最近 %s %s" % (
        int(d.get("count") or 0),
        "、".join("%s×%d" % (k, v) for k, v in kinds.items()) or "-",
        last.get("at", "-")[:19], last.get("kind", ""))
    fresh = False
    try:
        import datetime as _dt
        t = _dt.datetime.fromisoformat(str(last.get("at")).replace("Z", "+00:00"))
        fresh = (_dt.datetime.now(_dt.timezone.utc) - t).total_seconds() < 6 * 3600
    except Exception:                                    # noqa: BLE001
        fresh = False
    if fresh:
        txt += "｜**近 6 小时内有覆盖**：明细 `ops\\<机器id>\\<通道>\\inbox-superseded.jsonl`"
    return (not fresh), txt


def item_client():
    r"""**客户端自动启动**（人 2026-11-16：「客户端自动启动 好像还没测试，刚才都是你下的脚本」）。

    三件事分开报，别混成一句"配好了"：
      ① 配置就绪：`ops\tray-clients.json` 的 auto_launch、`ops\<机器id>\keepalive.json` 的 dsh 目标；
      ② 实测证据：`ops\<机器id>\client-launch.json`（watchdog 每次拉起/校验都写）有没有、多久前；
      ③ 结论：没证据 = **未验证**，不能说"能用"。
    """
    ev = os.path.join(OPS, "client-launch.json")
    cfg_ok = []
    bad = []
    try:
        tj = os.path.join(ROOT, "ops", "tray-clients.json")
        d = json.load(io.open(tj, encoding="utf-8-sig"))
        cli = (d.get("clients") or {}).get("DSH") or {}
        if (cli.get("detect") or {}).get("auto_launch"):
            cfg_ok.append("tray-clients.auto_launch=true")
        else:
            bad.append("tray-clients 里 DSH 的 auto_launch 没开")
    except (OSError, ValueError):
        bad.append("读不到 ops\tray-clients.json")
    try:
        ka = os.path.join(OPS, "keepalive.json")
        k = json.load(io.open(ka, encoding="utf-8-sig"))
        names = [t.get("name") for t in (k.get("targets") or [])]
        if "dsh" in [str(n).lower() for n in names if n]:
            cfg_ok.append("keepalive 有 dsh 目标")
        else:
            bad.append("keepalive.json 里没有 dsh 目标")
    except (OSError, ValueError):
        bad.append("读不到 ops\<机器id>\keepalive.json")
    ev_txt = "**未验证**（还没让守护自己拉起过）"
    if os.path.exists(ev):
        try:
            e = json.load(io.open(ev, encoding="utf-8-sig"))
            ago = round((time.time() - float(e.get("epoch") or 0)) / 60.0, 1)
            ev_txt = "实测 %s（%.0f 分钟前）%s" % (e.get("at", "?"), ago, e.get("result", ""))
        except (OSError, ValueError):
            ev_txt = "证据文件读不出来"
    detail = "客户端自动启动：配置 %s｜证据 %s" % (
        "、".join(cfg_ok) if cfg_ok else "不全", ev_txt)
    if bad:
        detail += "｜**缺**：" + "；".join(bad)
    ok = bool(cfg_ok) and not bad and os.path.exists(ev)
    if not os.path.exists(ev):
        detail += "｜怎么测：`python 脚本\\persona_client_autolaunch_test.py`（脱钩跑：杀 DSH → 等守护拉起 → 记证据）"
    return ok, detail


def item_coverage():
    r"""**全库可检索覆盖**（人 2026-11-16：「包括所有的项目，包括那些归档的会话，要能被索引到，
    或者搜索到，任何一条不成立，就是断链，孤儿」）。

    三类：文档（入口地图）、脚本（文档\索引-脚本.md）、会话与归档（index\L1.jsonl 指向真实存在、
    且 archive/sessions 目录能在 L1 里找到）。全过才绿。
    """
    try:
        import persona_coverage as CV
        r = CV.check()
    except Exception as e:                                # noqa: BLE001
        return False, "覆盖检查跑不起来：%s" % str(e)[:90]
    d, s2, ix = r["docs"], r["scripts"], r["index"]
    ar, ss = r["archive"], r["sessions"]
    parts = ["文档 %d（孤儿 %d）" % (d["total"], d["missing_n"]),
             "脚本 %d（未入索引 %d）" % (s2["total"], s2["missing_n"]),
             "索引 %d 条（指向缺失 %d）" % (ix["rows"], ix["file_missing"]),
             "archive 未索引 %d" % ar["missing_n"],
             "sessions 比索引新 %d" % ss["missing_n"]]
    detail = "可检索覆盖：" + " · ".join(parts)
    if not r["ok"]:
        detail += "｜**孤儿**：" + "；".join(filter(None, [
            ("文档 " + "、".join(d["missing"][:3])) if d["missing_n"] else "",
            ("脚本 " + "、".join(s2["missing"][:3])) if s2["missing_n"] else "",
            ("archive " + "、".join(ar["missing"][:2])) if ar["missing_n"] else "",
            ("sessions " + "、".join(ss["missing"][:2])) if ss["missing_n"] else "",
        ]))
        detail += "｜修法：文档挂地图 / 跑 persona_script_index.py / 重建 L1 索引"
    return r["ok"], detail


ITEMS = {"tray": item_tray, "daemon": item_daemon, "codefresh": item_codefresh,
         "watermark": item_watermark,
         "cdp": item_cdp, "inject": item_inject, "queue": item_queue, "cost": item_cost,
         "capture": item_capture, "index": item_index, "backup": item_backup,
         "disk": item_disk, "tasks": item_tasks, "model": item_model,
         "paths": item_paths, "manifest": item_manifest, "memory": item_memory,
         "config": item_config, "isolation": item_isolation, "dshcfg": item_dshcfg,
         "sffree": lambda: item_sffree(),          # 定义在下面，调用时再解析名字
         "todoscatter": item_todoscatter,
         "globalro": item_globalro, "cmdq": item_cmdq, "todo": item_todo, "plugin": item_plugin, "docmap": item_docmap, "sync": item_sync, "compact": item_compact, "inbox": item_inbox, "client": item_client, "coverage": item_coverage,
         "pub": item_pub}


def _sffree_path():
    r"""免费名单文件：先本机 `ops\<机器>\sf-free.json`，本机没有就退到任一机器目录里最新那份。

    为什么退：名单是**跨机器共享的抓取产物**（B102 契约：写只写自己、读可以读全部）。
    2026-11-15 本机就因为没有退回这一步，报过一次假红——名单明明在 4590 那台的目录里。
    """
    p = os.path.join(OPS, "sf-free.json")
    if os.path.exists(p):
        return p
    best, best_t = None, -1.0
    try:
        for name in os.listdir(OPS_SHARED):
            q = os.path.join(OPS_SHARED, name, "sf-free.json")
            if not os.path.isfile(q):
                continue
            t = os.path.getmtime(q)
            if t > best_t:
                best, best_t = q, t
    except OSError:
        pass
    return best or p



def item_sffree():
    r"""免费名单（人 2026-11-16 定：**节点每天抓一次 → 全局一份 → 其他机器只读**）。

    判红规则（按角色分，不再每台都抓）：
      · 本机是抓取节点（`ops\<机器id>\node-service.json` 的 serve/crawl）= 看自己今天抓没抓；
      · 普通机器：读共享名单 `配置\免费名单.json`；
          - 有且是今天的 → 绿；
          - 没有或过期 → **不报红**，直接"不用免费档"（降级，不花钱）；
          - 只有当 `require_free=true`（这台必须用免费档）时，缺名单才报红。
    """
    import datetime as _dt
    import json as _json

    ops = OPS
    try:
        ns = os.path.join(ops, "node-service.json")
        node = bool(_json.load(io.open(ns, encoding="utf-8-sig")).get("serve")
                    or _json.load(io.open(ns, encoding="utf-8-sig")).get("crawl"))
    except Exception:                                    # noqa: BLE001
        node = False
    try:
        conf = _json.load(io.open(os.path.join(ops, "persona.conf.json"), encoding="utf-8-sig")) or {}
    except Exception:                                    # noqa: BLE001
        conf = {}
    must = bool(conf.get("require_free"))

    def _fresh(d):
        at = str((d or {}).get("at") or "")
        try:
            t = _dt.datetime.strptime(at[:19], "%Y-%m-%dT%H:%M:%S")
        except Exception:                                # noqa: BLE001
            return False
        return t.date() == _dt.date.today()

    if node:
        p = os.path.join(ops, "sf-free.json")
        d = _read_json(p) or {}
        n = len(d.get("free_ids") or [])
        if _fresh(d):
            return True, "节点抓取正常：今天 %s，%d 个免费模型（写到 配置\\免费名单.json 供各机读）" % (
                str(d.get("at"))[:19], n)
        return False, ("**节点抓取失败**：本机名单停在 %s（%d 个）→ 跑 `python 脚本\\persona_sf_models.py --refresh`，"
                       "成功后覆盖 配置\\免费名单.json" % (str(d.get("at"))[:19] or "从未", n))

    shared = os.path.join(ROOT, "配置", "免费名单.json")
    d = _read_json(shared) or {}
    n = len(d.get("free_ids") or [])
    if _fresh(d):
        return True, "共享名单可用（节点 %s 抓的，%d 个免费模型）" % (str(d.get("at"))[:19], n)
    if must:
        return False, ("本机设了 require_free=true，但共享名单不可用（%s / %d 个）→ "
                       "**必须用免费档**：让节点抓一次，或关掉 require_free" % (
                           str(d.get("at"))[:19] or "没有文件", n))
    return True, "没有可用的共享名单 → **已停用免费档**（不影响正常模型，不报异常；要用就设 require_free=true）"

def run():
    items = {}
    for name in ORDER:
        why = WIN_ONLY.get(name)
        if why and not WIN:
            status, detail = STATUS_SKIP, "平台不符（非 Windows）：" + why
        else:
            try:
                ok, detail = ITEMS[name]()
            except Exception as e:                   # 单项失败不许拖垮整体
                ok, detail = False, "自检项异常：%s" % str(e)[:100]
            status = STATUS_OK if ok else STATUS_FAIL
        items[name] = {"ok": status == STATUS_OK, "status": status, "detail": str(detail)}
    doc = {"at": _now(), "items": items, "summary": ""}
    doc["summary"] = _summary(doc)
    return doc


def _status(v):
    """三态；兼容旧 selfcheck.json（只有 ok 布尔）。"""
    st = (v or {}).get("status")
    if st in (STATUS_OK, STATUS_SKIP, STATUS_FAIL):
        return st
    return STATUS_OK if (v or {}).get("ok") else STATUS_FAIL


def _short(name, detail):
    if name == "tray":
        return "缺"
    if name == "daemon":
        return "停" if "进程" in detail else "日志停"
    if name == "watermark":
        return "读数不可用"
    if name == "cdp":
        return "不通"
    if name == "inject":
        return "失败"
    if name == "capture":
        return "抓取断"
    if name == "index":
        return "索引旧"
    if name == "backup":
        return "备份旧"
    if name == "disk":
        return "盘快满"
    if name == "tasks":
        return "任务少了"
    if name == "model":
        return "模型不通"
    if name == "manifest":
        return "有未登记改动"
    if name == "queue":
        return "有积压"
    if name == "cost":
        if "余额 ¥" in detail:
            i = detail.find("余额 ¥")
            return detail[i:i + 12].strip()
        return "账本读不到"
    return detail[:20]


def _summary(doc):
    items = doc.get("items") or {}
    total = len(items)
    counts = {STATUS_OK: 0, STATUS_SKIP: 0, STATUS_FAIL: 0}
    for name in ORDER:
        counts[_status(items.get(name))] += 1
    # Windows 全绿时保持老格式（守护面板/log 都按 7/7 读）。
    if total and counts[STATUS_OK] == total:
        return "%d/%d OK" % (counts[STATUS_OK], total)
    line = "OK %d · SKIP %d · FAIL %d" % (
        counts[STATUS_OK], counts[STATUS_SKIP], counts[STATUS_FAIL])
    skipped = [LABEL.get(n, n) for n in ORDER
               if _status(items.get(n)) == STATUS_SKIP]
    if skipped:
        line += " · 跳过 " + "/".join(skipped)
    bad = []
    for name in ORDER:
        v = items.get(name) or {}
        if _status(v) != STATUS_FAIL:
            continue
        short = _short(name, v.get("detail") or "")
        if name == "cost" and short.startswith("余额 "):
            bad.append(short)          # 例：[自检] 5/7 · 托盘 缺 · 余额 ¥4.20
        else:
            bad.append("%s %s" % (LABEL.get(name, name), short))
    if bad:
        line += " · " + " · ".join(bad)
    return line


def _append_log(line):
    try:
        os.makedirs(OPS, exist_ok=True)
        fresh = (not os.path.exists(LOG)) or os.path.getsize(LOG) == 0
        with open(LOG, "a", encoding="utf-8") as f:
            if fresh:
                f.write("\ufeff")
            f.write("%s  %s\n" % (_now(), line))
    except OSError:
        pass


def _client_running():
    r"""DSH 客户端在不在：True / False / None（查不了）。

    走 `persona_keepalive.running()` 的 ctypes 进程名快照——毫秒级、不起 PowerShell、
    不弹窗、不花 token；自检 120 秒一拍，用 detect()（两趟 PowerShell，十几秒）会把节拍拖垮。
    **None 必须和 False 分开**：受限令牌下「看不见」和「没在跑」长得一样，
    照 None 去拉起会开出第二份客户端——同一台机器两个壳，比不保活糟。
    """
    try:
        import persona_keepalive as K
        return K.running("DeepSeek Harness")
    except Exception:                           # noqa: BLE001
        return None


# ── 自检开关（人 2026-11-15：「自检做一个开关，设置自检周期是否唤醒解决环境问题，
#    几个周期需要唤醒」）────────────────────────────────────────────────
# 真源 `ops\selfcheck.json`（共享层，两台机器各读各的同一份配置）。
# **刻意不放进 `配置\` / `persona.conf.json`**：那些在 persona_ver 的版本指纹里，
# 每翻一次开关就得 bump + publish 一次；开关属于运行策略，不该改写版本号。
SELFCHECK_CONF = os.path.join(OPS_SHARED, "selfcheck.json")
CONF_DEFAULTS = {
    "wake_env": True,            # 自检周期是否唤醒（解决环境问题）
    "wake_cycles": SELF_CHECK_CYCLES,   # 连续几个周期还坏才唤醒（1 = 一拍就叫）
    "client_autolaunch": True,   # 客户端不在就拉（关掉＝"我关了就是关了"）
    "client_cooldown": CLIENT_LAUNCH_GAP,   # 拉起冷却（秒），别每拍开一个新窗口
}
CONF_HINT = {
    "wake_env": "自检周期是否唤醒（true/false）",
    "wake_cycles": "连续几个周期还坏才唤醒（整数，1 = 一拍就叫）",
    "client_autolaunch": "客户端不在就拉起（true/false）",
    "client_cooldown": "拉起冷却秒数（整数）",
}


def _conf():
    r"""读自检开关：文件缺项按默认补；值坏了退回默认（不让一个坏 JSON 把自检搞红）。"""
    raw = _read_json(SELFCHECK_CONF) or {}
    out = dict(CONF_DEFAULTS)
    for k, dv in CONF_DEFAULTS.items():
        v = raw.get(k)
        try:
            if isinstance(dv, bool):
                if isinstance(v, str):
                    out[k] = v.strip().lower() in ("1", "true", "on", "yes", "开")
                elif v is not None:
                    out[k] = bool(v)
            elif isinstance(dv, int):
                if v is not None and str(v).strip() != "":
                    out[k] = max(1, int(float(v)))
        except (TypeError, ValueError):
            pass
    return out


def _save_conf(updates):
    d = _read_json(SELFCHECK_CONF) or {}
    d.update(updates)
    _write_json(SELFCHECK_CONF, d)
    return d


def set_wake_env(on):
    r"""对外唯一的写法：开/关**自检唤醒**。

    联动（人 2026-11-15）：「自检唤醒和空闲唤醒是两个开关，后者影响前者；
    前者开后者关，就是只自检唤醒，不空闲唤醒；但是开了后者，默认是同时开前者的」。
    所以 `persona_machine.py heartbeat --on` 会调这个函数把自检唤醒一起打开；
    反过来关空闲唤醒**不动**自检唤醒。返回当前值。
    """
    d = _save_conf({"wake_env": bool(on)})
    return bool((_conf() or {}).get("wake_env", d.get("wake_env")))


def _launch_client(st, now):
    r"""没客户端就拉。冷却 `client_cooldown` 秒，别每拍都开一个新窗口。

    返回 (ok, msg)；冷却/失败都记进 `selfcheck-wake.json`，不覆盖别家的状态文件。
    """
    cf = _conf()
    if not cf["client_autolaunch"]:
        return False, "开关关着：客户端不在也不拉（client_autolaunch=false）"
    gap = int(cf["client_cooldown"])
    last = float(st.get("client_launch_ts") or 0)
    if now - last < gap:
        return False, "刚试过拉起（%d 秒前，冷却 %d 秒）" % (
            int(now - last), gap)
    st["client_launch_ts"] = now
    try:
        import persona_client_probe as P
        ok, msg = P.launch("DSH")
    except Exception as e:                      # noqa: BLE001
        ok, msg = False, "拉起失败：%s" % str(e)[:100]
    st["client_msg"] = ("OK " if ok else "FAIL ") + msg
    _append_log("[环境] " + msg)
    return ok, msg


def _queue_wake(purpose, delay=45):
    r"""排一次**真注入**的唤醒（和守护空闲唤醒同一种闹钟，只是来源写 selfcheck）。"""
    import persona_wake as W
    now = time.time()
    wid = "sc%d" % (int(now * 1000) % 100000000)
    items = W.load()
    # ① 同类同源、还没跑过的旧唤醒 → 顶掉（人 2026-11-16：「很多唤醒重复的」）
    key = purpose.split("（")[0]
    keep = []
    dropped = 0
    for it in items:
        same = (it.get("source") == "selfcheck" and not it.get("last_run")
                and str(it.get("purpose", "")).split("（")[0] == key)
        if same:
            dropped += 1
            continue
        keep.append(it)
    # ② 封顶 30 条：超了丢最老的未跑条目
    pending_idx = [i for i, it in enumerate(keep) if not it.get("last_run")]
    over = max(0, len(keep) - 30)
    if over and pending_idx:
        for i in sorted(pending_idx)[:over]:
            keep[i] = None
        dropped += over
        keep = [it for it in keep if it]
    items = keep
    items.append({"id": wid, "due": now + delay, "every": 0, "purpose": purpose,
                  "agent": "", "run": "", "enabled": True, "inject": True,
                  "rearm": False, "source": "selfcheck",
                  "created": time.strftime("%Y-%m-%d %H:%M:%S"), "last_run": 0})
    W.save(items)
    if dropped:
        try:
            _append_log("[自检] 排唤醒前清掉 %d 条同类/超额待跑唤醒（队列 %d 条）" % (dropped, len(items)))
        except Exception:                   # noqa: BLE001
            pass
    return wid


def _wake_on_fail(doc, prev):
    r"""环境正常**不唤醒**；没客户端就拉；连续 N 个周期还是坏 → 拉客户端 + 唤醒一次。

    人 2026-11-15 原话：「也不是自检不唤醒了，只是环境正常不唤醒了，没客户端就拉，
    超过 3 个周期还是拉客户端并唤醒，这是解决环境问题的」；随后又定：
    「自检做一个开关，设置自检周期是否唤醒解决环境问题，几个周期需要唤醒」。

    **开关在 `ops\selfcheck.json`**（`python persona_selfcheck.py config` 看/改）：
      `wake_env`（默认 true）＝自检周期是否唤醒；`wake_cycles`（默认 3）＝连续几个周期才唤醒；
      `client_autolaunch`（默认 true）＝客户端不在就拉；`client_cooldown`（默认 600 秒）。

    三条腿，判据都是**连续周期数**（自检 120 秒一拍）：
      ① 自检全绿 + 客户端在跑 → **什么都不做**（不打扰，这是大多数时候）；
      ② 客户端不在 → 立刻拉（冷却内不重复）；连续 N 拍还不在 → 补一次唤醒；
      ③ 自检红（同一组红）连续 N 拍 → 拉起客户端 + 排一次注入唤醒去修。

    恢复（不红了/客户端回来了）把计数清零；同一组状态只叫一次（`woken` 去重），
    除非中间恢复过又坏。计数与冷却都在 `selfcheck-wake.json`，重启不丢。

    **和空闲唤醒的关系**（人 2026-11-15）：两个开关各管各的，自检唤醒不再看空闲唤醒——
      · 自检唤醒开、空闲唤醒关 → **只自检唤醒**，静多久都不叫；
      · 开了空闲唤醒 → **默认同时打开自检唤醒**（由 `set_wake_env` 联动，见 persona_machine）。
    """
    cf = _conf()
    need = int(cf["wake_cycles"])
    # 托盘「自检」菜单的三个开关（人 2026-11-15）：周期数、是否唤醒修复。
    n_cycles = SELF_CHECK_CYCLES
    do_wake = False
    try:
        import json as _json
        c = _json.load(io.open(os.path.join(OPS, "persona.conf.json"), encoding="utf-8-sig"))
        n_cycles = int(c.get("selfcheck_cycles") or SELF_CHECK_CYCLES)
        do_wake = bool(c.get("selfcheck_wake"))
    except Exception:                          # noqa: BLE001
        pass

    items = (doc or {}).get("items") or {}
    bad = [n for n in ORDER if _status(items.get(n)) == STATUS_FAIL]
    sig = ",".join(bad)
    path = os.path.join(OPS, "selfcheck-wake.json")
    st = _read_json(path) or {}
    now = time.time()
    old_sig = str(st.get("sig") or "")
    cycles = int(st.get("cycles") or 0)
    woken = str(st.get("woken") or "")

    # ① 客户端在不在（每拍都看，毫秒级）
    alive = _client_running()
    cc = int(st.get("client_cycles") or 0)
    if alive is False:
        cc += 1
        # 刚发现就拉；之后每到 need 的整数倍补拉一次（冷却在 _launch_client 里兜）
        if cf["client_autolaunch"] and (cc == 1 or cc % max(1, need) == 0):
            _launch_client(st, now)
    elif alive is True:
        cc = 0
        st.pop("client_msg", None)

    # 环境正常（没有红 + 客户端在跑）→ 不唤醒，清零
    if not sig and alive is not False:
        st.update({"at": _now(), "sig": "", "cycles": 0, "woken": "", "client_cycles": 0})
        try:
            _write_json(path, st)
        except OSError:
            pass
        return

    # 人 2026-11-16：同一原因已经挂在"待审批"里 → 短期内不再重复唤醒；
    # 但**抑制有时效**（待批 6 小时 / 复发静默 12 小时），过时照常叫，绝不"一次加入永远忽略"。
    try:
        import persona_anomaly as _AN
        _t_now = time.time()          # 注意：别叫 _now，会遮住模块里的 _now() 函数
        _fresh = []
        for _a in _AN.pending():
            _age = _t_now - float(_a.get("epoch") or 0)
            # 人 2026-11-16：待批的**每天最多提醒一次**（24 小时）；复发静默 12 小时
            _ttl = 12 * 3600 if str(_a.get("state")) == "repeat-quiet" else 24 * 3600
            if _age < _ttl:
                _fresh.append(_a)
        _covered = set()
        for _a in _fresh:
            _covered |= set(_a.get("labels") or [])
        _names = [LABEL.get(n, n) for n in bad]
        if bad and _names and _covered and set(_names) <= _covered:
            return        # 当前红项已被"待批异常"整批覆盖 → 24 小时内不再叫
        if bad and _names and not _fresh:
            pass          # 没有新鲜的待批异常 → 正常往下走，该叫就叫
    except Exception:                                    # noqa: BLE001
        pass

    sig_all = sig + ("|client" if alive is False else "")
    cycles = cycles + 1 if sig_all == old_sig else 1
    fire = cf["wake_env"] and cycles >= need and sig_all != woken
    st.update({"at": _now(), "sig": sig_all, "cycles": cycles, "client_cycles": cc,
               "woken": sig_all if fire else woken,
               "need": need, "wake_env": bool(cf["wake_env"])})
    try:
        _write_json(path, st)
    except OSError:
        pass
    if not fire:
        return

    # ② 该唤醒一次了：只看**自检唤醒**这个开关（它和空闲唤醒是两回事）
    why = "、".join([LABEL.get(n, n) for n in bad] + (["客户端不在"] if alive is False else []))
    if not cf["wake_env"]:
        try:
            _append_log("[自检] 连续 %d 个周期仍异常（自检唤醒开关关着，只拉起）：%s" % (cycles, why))
        except Exception:                       # noqa: BLE001
            pass
        return
    # 人 2026-11-15：「前者开后者关，就是只自检唤醒，不空闲唤醒」——
    # 所以这里**不再看空闲唤醒总开关**：两个开关各管各的，自检照叫。
    try:
        # 需要人点头才能修的项 → 记进**全局异常台账**（只追加不修改），别的机器读到就能远程批
        NEED_PERMIT = {"globalro", "config", "manifest", "pub", "index", "todo", "docmap"}
        need = [n for n in bad if n in NEED_PERMIT]
        if need:
            try:
                import persona_anomaly as AN
                _labels = [LABEL.get(n, n) for n in bad]
                _what = "自检修复需要许可：%s" % "、".join(_labels)
                _have = set()
                for _a in AN.pending():
                    if str(_a.get("what", "")).startswith("自检修复需要许可"):
                        _have |= set(_a.get("labels") or [])
                if set(_labels) <= _have:
                    raise RuntimeError("already-logged")
                AN.log("need-permit",
                       "自检修复需要许可：%s" % "、".join(LABEL.get(n, n) for n in need),
                       "自检连续 %d 个周期异常（%s）；这些项要写全局或要人批，机器人不能自行动手"
                       % (cycles, why),
                       need="grant 60 分钟 或 撤掉对应申请", ref=wid)
            except Exception:               # noqa: BLE001
                pass
        wid = _queue_wake("自检修复：环境异常 %d 个周期（%s）｜本条只修异常（含统一发版），不挑活" % (cycles, why))
        _append_log("[自检] 连续 %d 个周期仍异常 → 已排唤醒 %s：%s" % (cycles, wid, why))
    except Exception as e:                      # noqa: BLE001
        _append_log("[自检] 排唤醒失败：%s" % str(e)[:80])


def cmd_config(argv):
    r"""自检开关：看 / 改 `ops\selfcheck.json`。改完**下一拍生效**，不用重启守护。

        python persona_selfcheck.py config                    # 看
        python persona_selfcheck.py config --wake off          # 自检周期不唤醒
        python persona_selfcheck.py config --cycles 5          # 连续 5 个周期才唤醒
        python persona_selfcheck.py config --autolaunch off    # 客户端不在也不拉
        python persona_selfcheck.py config --gap 300           # 拉起冷却 300 秒
    """
    ap = argparse.ArgumentParser(prog="persona_selfcheck.py config",
                                 description="自检开关（ops\\selfcheck.json）")
    ap.add_argument("--wake", choices=["on", "off"], help=CONF_HINT["wake_env"])
    ap.add_argument("--cycles", type=int, help=CONF_HINT["wake_cycles"])
    ap.add_argument("--autolaunch", choices=["on", "off"], help=CONF_HINT["client_autolaunch"])
    ap.add_argument("--gap", type=int, help=CONF_HINT["client_cooldown"])
    a = ap.parse_args(argv)

    upd = {}
    if a.wake:
        upd["wake_env"] = (a.wake == "on")
    if a.cycles is not None:
        upd["wake_cycles"] = max(1, int(a.cycles))
    if a.autolaunch:
        upd["client_autolaunch"] = (a.autolaunch == "on")
    if a.gap is not None:
        upd["client_cooldown"] = max(60, int(a.gap))
    if upd:
        _save_conf(upd)
        print("已写 %s：%s" % (SELFCHECK_CONF, json.dumps(upd, ensure_ascii=False)))
    cf = _conf()
    print("自检开关（下一拍生效）：")
    print("  wake_env          = %s    连续几个周期唤醒 = %d（≈ %d 分钟）"
          % ("on" if cf["wake_env"] else "off", cf["wake_cycles"],
             int(cf["wake_cycles"] * 120 / 60)))
    print("  client_autolaunch = %s    拉起冷却 = %d 秒"
          % ("on" if cf["client_autolaunch"] else "off", cf["client_cooldown"]))
    return 0


def _same_state(a, b):
    if not isinstance(a, dict) or not isinstance(b, dict):
        return False
    ai, bi = a.get("items") or {}, b.get("items") or {}
    if set(ai) != set(bi):
        return False
    # 比三态而不是 ok 布尔：fail → skip（或反过来）也算状态变了，要落一行日志。
    return all(_status(ai[k]) == _status(bi[k]) for k in ai)


def main(argv):
    if len(argv) > 1 and argv[1] == "config":
        return cmd_config(argv[2:])
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv[1:])

    prev = _read_json(OUT)
    doc = run()
    try:
        _write_json(OUT, doc)
    except OSError:
        pass
    _wake_on_fail(doc, prev)

    if a.quiet:
        # 心跳用：只在状态**变化**时写 persona.log 一行，不每 30 秒刷屏。
        if not _same_state(prev, doc):
            _append_log("[自检] %s" % doc["summary"])
        return 0
    if a.json:
        print(json.dumps(doc, ensure_ascii=False, indent=1))
        return 0
    print("[自检] %s" % doc["summary"])
    return 0

if __name__ == "__main__":
    _pio.setup_stdio()  # 管道捕获时别因 ⚠ 炸掉（v6.13.0）
    sys.exit(main(sys.argv))
