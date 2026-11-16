# -*- coding: utf-8 -*-
import os as _os_shim, sys as _sys_shim
_sys_shim.path.insert(0, _os_shim.path.join(_os_shim.path.dirname(_os_shim.path.abspath(__file__)), "脚本"))
# SysPersona-OS · 本文件：persona_tray.py — 托盘状态灯：多客户端并列显示 + 实时水位
r"""persona_tray.py — 托盘状态灯：多客户端并列显示 + 实时水位

设计（2026-11-10 重写）：
- 有几个 agent 客户端在跑，就并列显示几个，各自带自己的水位，互不覆盖。
  DSH 的水位**只认壳的 `pressureTokens`**（`persona_gauge.dsh_pressure()`，壳判压缩
  用的就是那个数）；Hermes 的来自守护写的 `ops\hctx.json`；没有水位源的客户端只报「在跑」。
  `ops\dsh-ctx.json`（账本增量）只留痕/审计，**不再参与显示**——同一个数只能有一个来源。
- 图标是同心进度环：最外圈第一个客户端、往里第二圈……环长=各自占用百分比。
- 悬停提示一行并列：`DSH 123K/1.0M 12% · Hermes 125K/1.0M 12%`。
- 菜单「状态」：逐个客户端 + 守护各任务最近一次结果。
- 只读不代办：托盘不派活、不改状态，只显示。

启动：
  <runtime>\python\pythonw.exe persona_tray.py      # 或 $env:PERSONA_PY
自检：
  同上 --probe（或 --status）只打印一帧状态、菜单项和当前图标颜色，然后退出（不进托盘）；
  --probe-compress 自检「立即压缩一次」：跳过确认框、子进程加 --dry-run（不注入）；
  --probe-cost 自检今日成本：只打印一行读数就退出（读 ops 账本，不启动托盘）。

成本尺子（2026-11-10 加）：tooltip 追加「今日 ¥x.xx」；单价没配（per_mtok 有 null）时
改报「今日 838K in / 796K out / 265M cache」，**不猜价、不变色**。日金额超 daily_warn
是独立于水位的一条线：图标整块变黄（与注入告警同色）、tooltip 标「成本超线」。

环境变量：SYSPERSONA_ROOT（旧名 PERSONA_ROOT / MEM / SYSPERSONA_MEM 也认）指记忆库根；
不设就按脚本自身目录向上找含 ops\ 与 sessions\ 的目录，再找不到直接报错——不再写死盘符。
"""
import json
import io
import os
import subprocess
import sys
import threading
import time

import pystray
from PIL import Image, ImageDraw
import persona_io as _pio  # stdio/时间统一（v6.13.0）

def _load_persona_root():
    """记忆库根的最后一道兜底：仓库那份是 `root(strict=False)`，记忆库里那份只有 `find_root()`。

    两个都拿不到就返回 None——托盘靠环境变量/向上找目录也能活，不该因为这一句 import 起不来
    （2026-11-10 踩：把托盘拷回记忆库目录后 `from persona_root import root` 直接 ImportError）。
    """
    try:
        from persona_root import root as _r
        return lambda: _r(strict=False)
    except ImportError:
        pass
    try:
        sys.path.insert(0, os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ctxpack"))
        from persona_root import root as _r
        return lambda: _r(strict=False)
    except ImportError:
        pass
    try:
        from persona_root import find_root as _f
        return lambda: _f()
    except ImportError:
        return None


_PERSONA_ROOT = _load_persona_root()


# ── 路径：以记忆库为准（托盘在代码仓库里，ops\ 在记忆库） ──────────────
def _resolve_root():
    env = os.environ.get("MEM") or os.environ.get("SYSPERSONA_MEM")
    if env and os.path.isdir(env):
        return env
    here = os.path.dirname(os.path.realpath(__file__))
    cur = here
    for _ in range(6):
        # 2026-11-12 修：只看 ops + sessions 不够——脚本目录下自己也会长出这两个
        # （今天实测 脚本\ops、脚本\sessions 都有），于是 ROOT 被认成「脚本」，
        # 水位读不到、守护状态文件「缺失」、托盘一直红。真正的库根还要有
        # memory / index / archive / 续接 其中至少一个。
        if (os.path.isdir(__import__("persona_ops").dir())
                and os.path.isdir(os.path.join(cur, "sessions"))
                and any(os.path.isdir(os.path.join(cur, m))
                        for m in ("memory", "index", "archive", "续接"))):
            return cur
        cur = os.path.dirname(cur)
    if _PERSONA_ROOT is not None:
        try:
            found = _PERSONA_ROOT()
        except Exception:
            found = None
        if found:
            return found
    raise SystemExit("找不到记忆库根：请设 SYSPERSONA_ROOT（或 MEM）指向记忆库目录；"
                     "托盘不再退回某台机器的路径。")


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
STATUS = os.path.join(OPS, "persona-status.json")
HCTX = os.path.join(OPS, "hctx.json")
LOG = os.path.join(OPS, "persona.log")
PY = os.path.join(os.path.dirname(sys.executable), "python.exe")
GAUGE = os.path.join(ROOT, "脚本/persona_gauge.py")
COMPACT = os.path.join(ROOT, "脚本/persona_compact.py")     # 菜单「立即压缩上下文」跑的脚本

# 注入告警（2026-11-10）：状态/三档判定/提示音都在记忆库的 persona_inject.py 里，
# 托盘只读它，阈值不在这里复制一份。
# 不能直接 `import persona_inject`：仓库 tools\ctxpack 下也有一份旧版，会抢先命中。
def _load_inject():
    path = os.path.join(ROOT, "脚本/persona_inject.py")
    if not os.path.exists(path):
        return None
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("persona_inject_live", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None


INJ = _load_inject()
if INJ is not None and not hasattr(INJ, "evaluate_state"):
    INJ = None          # 旧版 persona_inject（没有告警函数）：托盘照常跑，只是不显示注入档

STALE = 180          # 秒：状态文件多久没更新算守护挂了
GAUGE_EVERY = 60     # 秒：DSH 水位多久读一次（直接读壳的缓存文件，很轻；60 秒够用）
COST_EVERY = 60      # 秒：今日成本多久读一次（读记忆库 ops 账本，不碰壳的目录）
PROC_EVERY = 20      # 秒：进程表缓存
TICK = 5             # 秒：托盘刷新间隔

# ok=False 但「设计如此」的任务：agent 是保活项，配置为只报不拉，永远报 false。
BENIGN_FAIL = {"agent"}

# pythonw 是 GUI 进程，直接 spawn 控制台程序会弹黑窗——必须一律带上这个标志。
NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# 客户端识别：进程名 → 显示名（顺序即图标里环的顺序）
CLIENTS = [
    ("DeepSeek Harness.exe", "DSH"),
    ("hermes.exe", "Hermes"),          # 官方 CLI / 网关进程
    ("Hermes.exe", "Hermes"),          # 官方桌面（Electron，界面+后端同体）
    ("cherry-studio.exe", "Cherry"),
    ("Cursor.exe", "Cursor"),
]

_PROC = {"at": 0.0, "counts": {}}
_DSH = {"at": 0.0, "ctx": None, "max": None, "pct": None, "note": ""}
_LOCK = threading.Lock()

# 手动压缩（2026-11-10 加）：托盘点一下 → 子进程 `persona_compact.py check --force`。
# 压缩期间图标整块变黄、tooltip 写「正在压缩…」；失败原因在 tooltip 留 60 秒。
_BUSY = {"on": False, "note": "", "until": 0.0}
_LAST_COMPRESS = {"rc": None, "out": ""}     # 自检用：最近一次子进程输出
BUSY_COLOR = (230, 170, 40)                  # 黄（与水位"注意"同色）
BUSY_TITLE = "SysPersona · 正在压缩…（先镜像再发 /compact，期间别打字）"
BUSY_KEEP = 60                               # 失败原因在 tooltip 留多久（秒）


# ── 小工具 ───────────────────────────────────────────────────────────
def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def file_age(path):
    try:
        return time.time() - os.path.getmtime(path)
    except OSError:
        return 1e9


def human(n):
    if n is None:
        return "?"
    if n < 1000:
        return str(n)
    if n < 1000000:
        return "%.0fK" % (n / 1000.0)
    return "%.1fM" % (n / 1000000.0)


def human_win(n):
    """窗口大小：先按两位小数取整再显示一位（1.048576M → 1.1M）。

    壳的 contextWindow 真值是 1,048,576；旧版是把 gauge 打印的「上限 1.05M」再
    四舍五入成 1.1M。这里保持同一显示，**不因为换了数据源就改视觉**。
    """
    if n is None:
        return "?"
    if n >= 1000000:
        return "%.1fM" % round(n / 1000000.0, 2)
    return human(n)


def color_of(pct):
    if pct is None:
        return (60, 170, 90)
    if pct >= 80:
        return (220, 60, 60)
    if pct >= 35:
        return (230, 170, 40)
    return (60, 170, 90)


# ── 手动压缩的状态灯（2026-11-10 加）────────────────────────────────
def busy_state():
    """当前压缩状态 → (是否进行中, 提示语)。失败原因留 `BUSY_KEEP` 秒后自动清掉。"""
    with _LOCK:
        on, note, until = bool(_BUSY["on"]), _BUSY["note"], _BUSY["until"]
    if not on and note and time.time() > until:
        with _LOCK:
            if not _BUSY["on"] and _BUSY["note"] == note:
                _BUSY["note"] = ""
        note = ""
    return on, note


def set_busy(on, note=""):
    with _LOCK:
        _BUSY["on"] = bool(on)
        _BUSY["note"] = note
        _BUSY["until"] = 0.0 if on else time.time() + BUSY_KEEP


def icon_color_name(s=None):
    """当前图标会用的底色名（--probe 用它验证「压缩时变黄、完事恢复」）。"""
    on, _note = busy_state()
    if on:
        return "黄（压缩中）"
    s = snapshot() if s is None else s
    inj = s.get("inject") or {}
    if inj.get("level") == "error":
        return "红（注入告警）"
    if inj.get("level") == "warn":
        return "黄（注入告警）"
    if s.get("cost_over"):
        return "黄（成本超线）"
    if not s["bars"]:
        return "灰（没有客户端）"
    colors = [b[0] for b in s["bars"]]
    if (220, 60, 60) in colors:
        return "红"
    if (230, 170, 40) in colors:
        return "黄"
    return "绿"


# ── 进程表 ───────────────────────────────────────────────────────────
def proc_counts():
    now = time.time()
    with _LOCK:
        if now - _PROC["at"] < PROC_EVERY and _PROC["counts"]:
            return _PROC["counts"]
    counts = {}
    try:
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=8,
                             encoding="mbcs", errors="replace",
                             creationflags=NO_WINDOW).stdout
        for line in out.splitlines():
            parts = line.strip().strip('"').split('","')
            if not parts or not parts[0]:
                continue
            counts[parts[0]] = counts.get(parts[0], 0) + 1
    except Exception:
        pass
    with _LOCK:
        _PROC["at"], _PROC["counts"] = now, counts
    return counts


# ── 水位源 ───────────────────────────────────────────────────────────
# 2026-11-10：**显示只认壳的 pressureTokens**（persona_gauge.dsh_pressure()）。
# `ops\dsh-ctx.json` 是账本增量（两次快照相减/调用数），和壳判压缩用的那个数实测
# 差一百多 K；它现在只留痕/审计，**不再参与显示**——同一个数只能有一个来源。
WIN_FALLBACK = 1048576      # 壳的 contextWindow 真值（1 MiB）：窗口读不到时按它显示


def _load_gauge():
    """从记忆库直接加载 persona_gauge，用它的 `dsh_pressure()` 取数。

    不能 `import persona_gauge`：仓库 tools\\ctxpack 下也有一份旧版会抢先命中。
    也不能去解析 gauge 的 stdout：`[上下文]` 那行走的是兜底链
    （pressureTokens → 账本增量 → Hermes → 账本平均），pressureTokens 缺失时它会
    悄悄换成别处的数——那就又变成「两个入口两个水位」了。
    """
    path = os.path.join(ROOT, "脚本/persona_gauge.py")
    if not os.path.exists(path):
        return None
    try:
        import importlib.util
        # 2026-11-12 修：按文件路径 exec 时，脚本自己那层目录**不在** sys.path 里，
        # 而 persona_gauge 第一句就是 `from persona_root import …` → 必然 ImportError，
        # 于是整条链路悄悄降级成「旧读法 / 读数不可用」（实测这台机器一直显示 ?）。
        # 跑 __main__ 时是 cwd 兜的，所以以前没露出来。
        _d = os.path.dirname(path)
        if _d not in sys.path:
            sys.path.insert(0, _d)
        spec = importlib.util.spec_from_file_location("persona_gauge_live", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod if hasattr(mod, "dsh_pressure") else None
    except Exception:
        return None


GAUGE_MOD = _load_gauge()
# 阈值只在 persona_limits.py 定义；gauge 已经把它 import 进来了，这里借它的。
_LIMIT = getattr(GAUGE_MOD, "LIMIT", None) or 1000000


def _legacy_dsh():
    """**旧读法**（只在 import 失败时降级用）：跑 persona_gauge.py 解析 stdout。

    返回 (ctx, max, pct)，一行都解析不出来就全 None。注意这一行可能是兜底链给的数
    （含 ops\\dsh-ctx.json 的 last），所以调用方必须在 tooltip 上明标 `(旧读法)`，
    不许静默冒充壳口径。
    """
    ctx = mx = pct = None
    try:
        out = subprocess.run([PY, GAUGE], capture_output=True, text=True,
                             timeout=60, encoding="utf-8", errors="replace",
                             creationflags=NO_WINDOW).stdout or ""
        for line in out.splitlines():
            if "[上下文]" in line:
                seg = line.split("·")
                ctx = _parse_k(seg[0].split("]")[-1].strip().lstrip("~"))
                for s in seg[1:]:
                    if "%" in s:
                        try:
                            pct = float(s.split("（")[1].split("%")[0])
                        except Exception:
                            pass
                    if "上限" in s:
                        mx = _parse_k(s.split("上限")[-1].split("）")[0].strip())
                break
    except Exception:
        pass
    return ctx, mx, pct


def dsh_watermark(force=False):
    """DSH 水位：只认壳的 pressureTokens；读不到就 ctx=None，**不许拿旧数冒充**。"""
    now = time.time()
    with _LOCK:
        if not force and _DSH["at"] and now - _DSH["at"] < GAUGE_EVERY:
            return dict(_DSH)
    press = mx = pct = None
    note = ""
    if GAUGE_MOD is not None:
        try:
            press, _surface, win = GAUGE_MOD.dsh_pressure()
        except Exception:
            press, win = 0, 0
        mx = win or WIN_FALLBACK
        if press and press > 0:
            pct = 100.0 * press / (win or _LIMIT)
        else:
            press, pct = None, None      # 读数不可用：显示 ?，不冒充
            note = " 读数不可用"
    else:
        press, mx, pct = _legacy_dsh()   # 降级：旧读法，明标
        note = " (旧读法)"
        if mx is None:
            mx = WIN_FALLBACK            # 旧读法也拿不到：窗口仍按 1.1M 显示，不许 ?/?
        if press is None:
            note += " 读数不可用"
    with _LOCK:
        _DSH.update({"at": now, "ctx": press, "max": mx, "pct": pct, "note": note})
        return dict(_DSH)


def _parse_k(s):
    s = s.strip().replace(",", "")
    try:
        if s.upper().endswith("K"):
            return int(float(s[:-1]) * 1000)
        if s.upper().endswith("M"):
            return int(float(s[:-1]) * 1000000)
        return int(float(s))
    except Exception:
        return None


# ── 今日成本（只读记忆库 ops 账本；壳的目录一个字节都不写）─────────────
def _load_cost():
    """从记忆库加载 persona_cost，用它的 `report_data()` 取今日读数。

    与 _load_gauge 同理：不能 `import persona_cost`（仓库 tools\\ctxpack 下那份会抢先命中），
    按文件路径加载记忆库里的活版本；加载失败只是不显示成本，不影响托盘启动。
    """
    path = os.path.join(ROOT, "脚本/persona_cost.py")
    if not os.path.exists(path):
        return None
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("persona_cost_live", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod if hasattr(mod, "report_data") else None
    except Exception:
        return None


COST_MOD = _load_cost()
_COST = {"at": 0.0, "data": None}


def _cost_tokens(n):
    """token 显示：优先借 persona_cost 的紧凑格式（838K / 265M）。"""
    if COST_MOD is not None and hasattr(COST_MOD, "_human"):
        try:
            return COST_MOD._human(n)
        except Exception:
            pass
    return human(n)


def _cost_money(v, currency="CNY"):
    if COST_MOD is not None and hasattr(COST_MOD, "_money"):
        try:
            return COST_MOD._money(v, currency)
        except Exception:
            pass
    return "¥—" if v is None else "¥%.2f" % float(v)


def cost_state(force=False):
    """今日成本读数（缓存 COST_EVERY 秒）。读不到返回 None。"""
    now = time.time()
    with _LOCK:
        if not force and _COST["at"] and now - _COST["at"] < COST_EVERY:
            return _COST["data"]
    data = None
    if COST_MOD is not None:
        try:
            data = COST_MOD.report_data()
        except Exception:
            data = None
    with _LOCK:
        _COST.update({"at": now, "data": data})
    return data


def cost_over(data):
    """日金额超 daily_warn —— 独立于水位的一条线；单价没配就只看 token、不变色。"""
    return bool(data and data.get("over_daily"))


def cost_tip(data):
    """tooltip 里那一小段：`今日 ¥31.30 · 3,135 调用 [· 余额 ¥6.54]`。

    金额来自 DSH 官方账本（persona_cost.report_data 的 ledger 口径）；余额读不到就省略那段。
    """
    if not data:
        return ""
    if not data.get("priced"):
        t = data.get("totals") or {}
        return "今日 %s in / %s out / %s cache" % (
            _cost_tokens(t.get("uncached")), _cost_tokens(t.get("output")),
            _cost_tokens(t.get("cache_read")))
    parts = ["今日 " + _cost_money(data.get("money"), data.get("currency"))]
    if data.get("calls") is not None:
        parts.append("{:,} 调用".format(int(data["calls"])))
    if data.get("balance") is not None:
        parts.append("余额 " + _cost_money(data.get("balance"), data.get("currency")))
    return " · ".join(parts)


def hermes_watermark():
    d = read_json(HCTX)
    if not d or file_age(HCTX) > 600:
        return None
    return {"ctx": d.get("ctx"), "max": d.get("max"), "pct": d.get("pct")}


# ── 客户端列表：谁在跑 + 各自水位 ────────────────────────────────────
def client_rows():
    counts = proc_counts()
    rows = []

    d = dsh_watermark()
    n = counts.get("DeepSeek Harness.exe", 0)
    # 只显示「正在运行」的客户端：没运行的不进图标、不进提示。
    if n:
        rows.append({"name": "DSH", "alive": True, "procs": n, "wm": True,
                     "ctx": d.get("ctx"), "max": d.get("max"), "pct": d.get("pct"),
                     "note": d.get("note") or ""})

    h = hermes_watermark()
    hn = sum(counts.get(img, 0) for img, label in CLIENTS if label == "Hermes")
    if hn:
        rows.append({"name": "Hermes", "alive": True, "procs": hn, "wm": bool(h),
                     "ctx": (h or {}).get("ctx"), "max": (h or {}).get("max"),
                     "pct": (h or {}).get("pct"), "note": ""})

    for img, label in CLIENTS:
        if label in ("DSH", "Hermes"):
            continue
        c = counts.get(img, 0)
        if c:
            rows.append({"name": label, "alive": True, "procs": c, "wm": False,
                         "ctx": None, "max": None, "pct": None, "note": ""})
    return rows


def wm_part(r):
    """一个客户端在 tooltip 里的水位片段。

    正常：`DSH 392K/1.1M 37%`（格式与旧版一字不差）。
    读数不可用：`DSH ?/1.1M 读数不可用`——**宁可显示 ?，也不拿旧数冒充**。
    """
    if not r.get("wm"):
        return "%s 在跑" % r["name"]
    core = "%s %s/%s" % (r["name"],
                         human(r["ctx"]) if r["ctx"] is not None else "?",
                         human_win(r["max"]))
    if r["ctx"] is not None and r["pct"] is not None:
        core += " %.0f%%" % r["pct"]
    return core + (r.get("note") or "")


# ── 守护健康 ─────────────────────────────────────────────────────────
def _short(text, n=46):
    """把任务消息压成一行：去掉长路径、折叠空白、截断。

    2026-11-11 13:47 人报「排版不对」：一条消息里塞着 `D:\\4590-PC-文件\\…\\L1.md`
    这种整条路径，一撑就换行，列全乱了。
    """
    import re as _re
    s = str(text or "").replace("\r", " ").replace("\n", " ")
    s = _re.sub(r"[A-Za-z]:\\[^\s|）)]+", lambda m: m.group(0).split("\\")[-1], s)
    s = _re.sub(r"\s{2,}", " ", s).strip()
    return (s[:n] + "…") if len(s) > n else s


def daemon_health():
    st = read_json(STATUS)
    if not st:
        return False, "状态文件缺失", {}
    tasks = st.get("tasks") or {}
    failed = [k for k, v in tasks.items()
              if v.get("ok") is False and k not in BENIGN_FAIL]
    busy = [k for k, v in tasks.items() if v.get("busy")]
    age = file_age(STATUS)
    if age > STALE:
        return False, "守护可能已停（%d 分钟未更新）" % int(age // 60), tasks
    if failed:
        return False, "任务失败：%s" % ",".join(failed[:3]), tasks
    if busy:
        return True, "忙碌中：%s" % ",".join(busy[:3]), tasks
    return True, "正常", tasks


# ── 汇总一帧 ─────────────────────────────────────────────────────────
def snapshot():
    rows = client_rows()
    ok, msg, tasks = daemon_health()
    inj = INJ.evaluate_state() if INJ else None
    cost = cost_state()

    # 竖条顺序 = 客户端顺序（位置固定，便于辨认：左=DSH，右=下一个）
    bars = [(color_of(r["pct"]), r["pct"], r["ctx"] is not None) for r in rows]

    # 2026-11-12 人定（截图划重点）：**一行一件事**——
    #   第一行 名称
    #   第二行 金额
    #   之后   每个客户端各占一行（名称 + 水位 + 进程数）
    # 原来是一整行 "SysPersona · DSH … · Hermes … ｜ 今日 ¥…"，客户端一多就挤；
    # 而且金额在弹窗里会跟【成本】那段**重复显示两次**，这次一起去掉。
    parts = []
    for r in rows:
        seg = wm_part(r)
        if r.get("procs"):
            seg += " · %d 进程" % r["procs"]
        parts.append(seg)
    title = "SysPersona"
    ctip = cost_tip(cost)
    if ctip:
        title += "\n" + ctip
    title += "\n" + ("\n".join(parts) if parts else "没有客户端在跑")
    if cost_over(cost):
        title += "\n成本超线"
    if not ok:
        title += "\n守护异常"
    if inj and inj["level"] != "ok":
        title += "\n" + inj["text"]
    return {"bars": bars, "title": title, "rows": rows, "daemon": (ok, msg, tasks),
            "inject": inj, "cost": cost, "cost_over": cost_over(cost)}


# ── 画图标：一个客户端一个正方形，几个就并排几个 ─────────────────────
def make_icon(bars, alarm="ok"):
    """bars = [(color, pct, 有无水位)]；只放「正在运行」的客户端。

    n 个客户端 → 画布横向切成 n 个正方形（边长 = 64/n），从左到右排开：
    1 个 = 一个满尺寸正方形，2 个 = 两个并排，4 个 = 四个；正方形里从底部
    往上填充水位高度，颜色按各自水位（绿 / 黄 / 红）。

    alarm == "warn"/"error" 时整块底色变黄/红（注入发不出去），条块压成白色，
    保证一眼能看出「告警」而不是普通水位。
    """
    S = 64
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    n = len(bars)
    alarm_bg = {"warn": (230, 170, 40), "error": (220, 60, 60)}.get(alarm)
    if alarm_bg:
        d.rectangle((0, 0, S - 1, S - 1), fill=alarm_bg + (255,))
    if n == 0:
        d.rectangle((22, 22, S - 22, S - 22), outline=(120, 120, 120, 255), width=3)
        return img
    gap = 2 if n > 1 else 0
    side = max(4, (S - gap * (n - 1)) // n)
    total = side * n + gap * (n - 1)
    x0 = (S - total) // 2
    y0 = (S - side) // 2
    for i, (color, pct, has) in enumerate(bars[:8]):
        x = x0 + i * (side + gap)
        d.rectangle((x, y0, x + side - 1, y0 + side - 1), fill=(38, 38, 38, 255))
        if has and pct is not None:
            frac = max(0.0, min(1.0, pct / 100.0))
            h = max(1, int(round(frac * side)))
            d.rectangle((x, y0 + side - h, x + side - 1, y0 + side - 1),
                        fill=((255, 255, 255) if alarm_bg else color) + (255,))
        d.rectangle((x, y0, x + side - 1, y0 + side - 1),
                    outline=((30, 30, 30) if alarm_bg else color) + (255,), width=1)
    return img


def make_busy_icon():
    """压缩进行中：整块黄底 + 三道深色横杠（不画水位，一眼看出"在干活"）。"""
    S = 64
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, S - 1, S - 1), fill=BUSY_COLOR + (255,))
    for y in (21, 30, 39):
        d.rectangle((16, y, S - 17, y + 4), fill=(70, 52, 8, 255))
    return img


# ── 弹窗：详细状态 ───────────────────────────────────────────────────
# ── 等宽对齐用的小工具（2026-11-12）────────────────────────────────
def disp_width(s):
    """按**显示宽度**算：中文/全角算 2 格，ASCII 算 1 格。

    状态窗是 Consolas 等宽字体，但中文仍占两格；直接 len() 补空格会让"从未"比"OK"宽一格，
    第三列就跟着歪。要按格子算才对齐。
    """
    import unicodedata
    n = 0
    for ch in str(s):
        n += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return n


def pad_cells(s, cells):
    s = str(s)
    return s + " " * max(0, cells - disp_width(s))


def detail_text(s):
    out = [s["title"], ""]
    inj = s.get("inject")
    if inj:
        out.append("【注入】%s" % inj.get("text", ""))
        out.append("")
    cost = s.get("cost")
    if cost:
        t = cost.get("totals") or {}
        out.append("【成本】今日 in %s · out %s · cache_read %s · cache_write %s"
                   % (_cost_tokens(t.get("uncached")), _cost_tokens(t.get("output")),
                      _cost_tokens(t.get("cache_read")), _cost_tokens(t.get("cache_write"))))
        # 2026-11-12 人报「还有两处显示金额」：今日金额已经在标题第二行报过，
        # 这里平时不再重复；只有超线时才补一句，否则只列 token 与"单价未配置"这类状态。
        if cost.get("priced") and (cost.get("over_daily") or cost.get("over_hourly")):
            hour = cost.get("hour") or {}
            out.append("  超线：日 %s（线 %s%s）· 近 1 小时 %s%s" % (
                _cost_money(cost.get("money"), cost.get("currency")),
                _cost_money(cost.get("daily_warn"), cost.get("currency")),
                "，已超日线" if cost.get("over_daily") else "",
                _cost_money(hour.get("money"), cost.get("currency")),
                "，已超小时线" if cost.get("over_hourly") else ""))
        elif not cost.get("priced"):
            out.append("  单价未配置（%s）→ 只报 token、不判金额" % (
                cost.get("price_error") or "per_mtok 四个单价有 null"))
    # 【客户端】那段删了（2026-11-12）：标题里已经"每个客户端一行"，
    # 再来一段同样的清单就是重复；进程数已并进标题那一行。

    ok, msg, tasks = s["daemon"]
    out.append("")
    out.append("【守护】%s" % _short(msg, 60))
    fails = [(k, v) for k, v in tasks.items()
             if v.get("ok") is False and k not in BENIGN_FAIL]
    # 2026-11-12：状态窗现在是 **Consolas 等宽**（persona_status_dialog.py），
    # 所以用空格补齐就能真正对齐；tab 在等宽下反而依赖制表位设置，不如空格确定。
    for k, v in fails[:4]:
        out.append("✗ %s  %s" % (pad_cells(k, 8), _short(v.get("msg"), 56)))
    for name in ("hctx", "export", "index", "backup2", "todo", "proxy", "cost"):
        t = tasks.get(name)
        if not t:
            continue
        o = t.get("ok")
        state = "OK" if o is True else ("FAIL" if o is False else "待跑")
        body = t.get("msg") or ""
        if body == "never":
            state, body = "从未", "（还没跑过，或一直在等上一轮）"
        if name == "cost":
            # 金额只在第二行报一次（2026-11-12 人报「还有两处显示金额」）；
            # 这里那行是任务自己的读数，会跟上面重复且数字略旧，压成一句状态。
            body = "（今日已记账）"
        out.append("%s %s  %s" % (pad_cells(name, 8), pad_cells(state, 4), _short(body, 44)))
    return "\n".join(out)



_DLG_LOCK = None          # 同一个标题只开一个框（2026-11-11 人报"单击重复弹窗"）
_DLG_AT = {}


def _dialog(text, title):
    """独立线程弹框：托盘线程的消息泵不能被占住。

    2026-11-11 修的：原来每点一次就开一个 MessageBox（左键默认项就是「状态」），
    连点几下就叠一摞同样的窗。现在同标题只留一个：已开着就把它提到前面，不再新开。
    """
    import threading
    import time as _t
    import ctypes

    global _DLG_LOCK
    if _DLG_LOCK is None:
        _DLG_LOCK = threading.Lock()

    u32 = ctypes.windll.user32
    # 已经有一个同标题的框：拉前台，别开第二个
    hwnd = u32.FindWindowW(None, title)
    if hwnd:
        u32.ShowWindow(hwnd, 5)
        u32.SetForegroundWindow(hwnd)
        return
    now = _t.time()
    if now - _DLG_AT.get(title, 0) < 1.0:      # 一秒内的连点直接吞掉
        return
    _DLG_AT[title] = now

    def run():
        try:
            u32.MessageBoxW(0, text, title, 0x40 | 0x40000)
        except Exception:
            print(text)

    threading.Thread(target=run, daemon=True).start()


def show_status(icon=None, item=None):
    """状态窗：**等宽字体**的独立小窗（不再用 MessageBox）。

    2026-11-12 人报「能用 tab 把格式对齐下吗」——MessageBox 是比例字体 + 固定制表位，
    名字长短一差，整行的列就跳位，怎么调都对不齐（实测截图确认）。
    所以改由 `persona_status_dialog.py` 单独起一个进程画窗口：Consolas 等宽、能选中复制、
    Esc / 右上角都能关。跟「密码窗」同一个套路（托盘线程里建 tk 窗会关不掉，2026-11-11 踩过）。
    """
    text = detail_text(snapshot())
    try:
        path = os.path.join(OPS, "_status-text.txt")
        with io.open(path, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError as e:
        _dialog(text, "SysPersona 状态")       # 兜底：写不了文件就退回老弹窗
        _p("状态窗正文写不进去（%s），退回 MessageBox" % e)
        return
    py = sys.executable
    pyw = os.path.join(os.path.dirname(py), "pythonw.exe")
    exe = pyw if os.path.exists(pyw) else py
    try:
        subprocess.Popen([exe, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                            "persona_status_dialog.py"),
                          path, "SysPersona 状态"],
                         cwd=ROOT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:                 # noqa: BLE001
        _dialog(text, "SysPersona 状态")
        _p("状态窗起不来（%s），退回 MessageBox" % e)


def open_log(icon=None, item=None):
    try:
        subprocess.Popen(["notepad.exe", LOG])
    except Exception:
        pass


def open_mem(icon=None, item=None):
    r"""菜单「打开会话存档」：开**本地会话保存**目录（sessions\<日期>\），不是记忆库。"""
    target = os.path.join(ROOT, "sessions")
    try:
        os.startfile(target if os.path.isdir(target) else ROOT)
    except Exception:
        pass


def quit_app(icon, item=None):
    icon.stop()


# ── 主循环 ───────────────────────────────────────────────────────────
def alarm_level(s):
    """图标告警档：注入优先；没有注入告警时，日成本超线单独把图标变黄。"""
    level = (s.get("inject") or {}).get("level", "ok")
    if level == "ok" and s.get("cost_over"):
        return "warn"
    return level


def refresh(icon):
    while True:
        try:
            on, note = busy_state()
            if on:
                # 压缩中：整块黄 + 固定提示，水位先不画（子进程在导出/镜像/等空闲）
                icon.icon = make_busy_icon()
                icon.title = BUSY_TITLE[:120]
            else:
                s = snapshot()
                icon.icon = make_icon(s["bars"], alarm_level(s))
                icon.title = (s["title"] if not note else "SysPersona · " + note)[:120]
                if INJ and not note:
                    INJ.check_alert()  # 上升沿才响；marker 落盘，不会每轮都响
        except Exception:
            pass
        time.sleep(TICK)


def retry_inject(icon=None, item=None):
    """菜单「重读设定文件」：只跑探针（选窗+抢焦点），不真发内容。"""
    script = os.path.join(ROOT, "脚本/persona_inject.py")
    try:
        r = subprocess.run([PY, script, "--probe"], cwd=ROOT, capture_output=True,
                           text=True, timeout=40, encoding="utf-8", errors="replace",
                           creationflags=NO_WINDOW)
        msg = ((r.stdout or "") + (r.stderr or "")).strip() or ("exit %d" % r.returncode)
    except Exception as e:
        msg = "探针失败：%s" % e
    _dialog(msg[:800], "重读设定文件（只探测，不发内容）")


# ── 菜单「立即压缩一次」（2026-11-10 加）────────────────────────────
CONFIRM_COMPACT = ("立即压缩上下文？\n\n"
                   "会先把当前会话原文导出、镜像并校验 sha256，再等壳空闲后注入 /compact。\n"
                   "可能需要一两分钟，期间别打字、别切窗口（会抢一次前台焦点）。\n"
                   "镜像失败就不压。继续吗？")


def _note(action, extra=""):
    """把"点了什么、走到哪一步"记一行（ops/tray-actions.jsonl）。

    2026-11-11 15:47 人报「deep 的压缩还是点确定没反应」：
    界面上什么都没发生，日志里也什么都没留，等于摸黑。
    从此每个手动动作**进门先记一笔**，卡住就能看出是没进来、还是确认框返回了假。
    """
    try:
        rec = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "action": action,
               "extra": str(extra)[:200], "pid": os.getpid()}
        if OPS and os.path.isdir(OPS):
            with open(os.path.join(OPS, "tray-actions.jsonl"), "a",
                      encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:                      # noqa: BLE001
        pass


def _confirm(text, title="SysPersona", timeout=180):
    """原生「是/否」框：走 Win32，但**在独立线程里弹**。

    2026-11-11 15:47 修的：原来直接在 pystray 的菜单回调线程里调 MessageBoxW，
    托盘自己的消息泵被这个模态循环挡着，表现就是「点了确定没反应」。
    现在照 `_dialog` 的做法另起线程，主线程用 Event 等结果（等不到就当"否"，绝不卡死）。
    另外 6=是、1=确定 都算同意：不同 Windows 版本/按钮文字不一样，不跟它较劲。
    """
    import threading

    box = {}
    ev = threading.Event()

    def run():
        try:
            import ctypes
            flags = 0x04 | 0x20 | 0x40000 | 0x10000   # YESNO|QUESTION|TOPMOST|SETFOREGROUND
            box["r"] = ctypes.windll.user32.MessageBoxW(0, text, title, flags)
        except Exception as e:             # noqa: BLE001
            box["err"] = str(e)[:80]
            box["r"] = 0
        finally:
            ev.set()

    threading.Thread(target=run, daemon=True).start()
    got = ev.wait(timeout)
    _note("confirm", "%s → r=%s%s%s" % (title, box.get("r"),
                                        "" if got else "（超时）",
                                        (" err=" + box["err"]) if box.get("err") else ""))
    return box.get("r") in (6, 1)


def _fail_reason(out, rc):
    """把子进程输出压成一句人话，写进 tooltip。"""
    txt = out or ""
    if "镜像没落盘" in txt or "镜像校验失败" in txt or "镜像异常" in txt:
        return "镜像失败"
    if "前台焦点" in txt:
        return "没抢到前台焦点"
    if "壳一直在动" in txt:
        return "壳一直在动，没等到空闲"
    if "冷却" in txt:
        return "冷却中（刚压过，防连点）"
    for line in txt.splitlines():
        line = line.strip()
        if line.startswith("FAIL"):
            body = line[4:].strip()
            if body:
                return body[:40]
    return "exit %d" % rc


PROGRESS = os.path.join(ROOT, "脚本/persona_progress.py")


def run_with_progress(title, worker, countdown=8):
    """开一个进度窗 → 后台跑 worker() -> (ok, msg) → 把结果写给窗口。

    人 2026-11-11 定：所有压缩/重启都按这个来，**只有检测到成功才自动关**，
    失败必须人看完再点关闭。
    """
    import json
    import uuid
    rid = uuid.uuid4().hex[:8]
    flag = os.path.join(OPS, "progress-%s.json" % rid)
    try:
        if os.path.exists(flag):
            os.remove(flag)
    except OSError:
        pass
    try:
        subprocess.Popen([PY, PROGRESS, flag, title, str(countdown)],
                         cwd=ROOT, creationflags=NO_WINDOW)
    except Exception:
        pass

    def run():
        ok, msg = False, "没跑起来"
        try:
            ok, msg = worker()
        except Exception as e:             # noqa: BLE001
            ok, msg = False, str(e)[:300]
        try:
            with open(flag, "w", encoding="utf-8") as f:
                json.dump({"ok": bool(ok), "msg": str(msg)[:800]}, f, ensure_ascii=False)
        except OSError:
            pass

    threading.Thread(target=run, daemon=True).start()


def _compress_worker(ask=True, extra=()):
    """真正干活的那层：确认 → 子进程 `check --force`（不阻塞托盘）。

    图标/提示由 refresh 循环按 `_BUSY` 画：执行中整块黄 + 「正在压缩…」，
    完成恢复；失败把原因留在 tooltip（`BUSY_KEEP` 秒）。
    """
    PROGRESS = os.path.join(ROOT, "脚本/persona_progress.py")

    def work():
        set_busy(True)
        try:
            env = dict(os.environ, PYTHONIOENCODING="utf-8")
            r = subprocess.run([PY, COMPACT, "check", "--force", "--source", "tray"]
                               + list(extra),
                               cwd=ROOT, capture_output=True, timeout=900,
                               encoding="utf-8", errors="replace",
                               creationflags=NO_WINDOW, env=env)
            out = ((r.stdout or "") + (r.stderr or "")).strip()
            with _LOCK:
                _LAST_COMPRESS["rc"], _LAST_COMPRESS["out"] = r.returncode, out
            ok = r.returncode == 0 and ("OK " in out or "DRY" in out)
            if ok:
                set_busy(False)
                return True, "压缩完成\n\n" + (out[-600:] or "（无输出）")
            why = _fail_reason(out, r.returncode)
            set_busy(False, "压缩失败：" + why)
            return False, "压缩失败：%s\n\n%s" % (why, out[-600:])
        except subprocess.TimeoutExpired:
            set_busy(False, "压缩失败：超时（15 分钟没结束）")
            return False, "超时：15 分钟没结束"
        except Exception as e:
            set_busy(False, "压缩失败：%s" % str(e)[:60])
            return False, "压缩失败：%s" % str(e)[:200]

    if ask and not _confirm(CONFIRM_COMPACT, "SysPersona · 立即压缩上下文"):
        return
    run_with_progress("压缩上下文", work, countdown=8)


def compress_once(icon=None, item=None):
    """菜单「立即压缩上下文」的回调入口。

    pystray 只接受 0/1/2 个参数的回调，参数超过 2 个会抛 `ValueError(action)`
    （2026-11-10 踩：`compress_once(icon, item, ask, extra)` 四参 → 托盘启动即崩、
    开机自启静默失败，看上去像「托盘没起来」）。带参数的那层叫 `_compress_worker`。
    """
    _compress_worker()



# ── 任务池：托盘 ↔ 分身 的唯一通道（只走 ops/tasks.jsonl）────────────
TASKS = os.path.join(OPS, "tasks.jsonl")
TODO_PY = os.path.join(ROOT, "脚本/persona_tasks.py")


def _task_rows():
    """读任务池，按 id 去重，只留没结束的。"""
    if not os.path.exists(TASKS):
        return []
    latest = {}
    try:
        with open(TASKS, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                tid = r.get("id") or r.get("task_id")
                if tid:
                    latest[tid] = r
    except OSError:
        return []
    rows = [r for r in latest.values()
            if r.get("status") in ("pending", "claimed", "running")]
    rows.sort(key=lambda r: (r.get("prio", 5), r.get("at", "")))
    return rows


def pool_text(limit=8):
    rows = _task_rows()
    if not rows:
        return "任务池空"
    out = ["任务池 %d 条：" % len(rows)]
    for r in rows[:limit]:
        out.append("  p%s %-8s %s" % (r.get("prio", 5), r.get("status", ""),
                                      (r.get("title") or "")[:34]))
    if len(rows) > limit:
        out.append("  …还有 %d 条" % (len(rows) - limit))
    out.append("")
    out.append("（只读；派活由我 submit，认领由我 claim）")
    return "\n".join(out)


def show_pool(icon=None, item=None):
    _dialog(pool_text(), "任务池（只读）")


def _ask(prompt, title="SysPersona"):
    """原生输入框：用 WSH 的 InputBox（tkinter 在 pythonw 下会卡死）。"""
    import tempfile
    vbs = ("Set a = WScript.Arguments\n"
           "x = InputBox(\"\" \" & a(0), \"\" \" & a(1))\n"
           "If Len(x) > 0 Then WScript.Echo x\n")
    fd, path = tempfile.mkstemp(suffix=".vbs")
    os.close(fd)
    try:
        with open(path, "w", encoding="mbcs", errors="replace") as f:
            f.write(vbs)
        r = subprocess.run(["cscript.exe", "//nologo", path, prompt, title],
                           capture_output=True, text=True, timeout=180,
                           encoding="mbcs", errors="replace",
                           creationflags=NO_WINDOW)
        return (r.stdout or "").strip()
    except Exception:
        return ""
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def submit_task(icon=None, item=None):
    """在托盘里派一条活：写进任务池，下一轮我 pool 就能看到。"""
    title = _ask("派一条任务给分身（会写进任务池，我下一轮认领）：", "派活")
    if not title:
        return
    try:
        r = subprocess.run([PY, TODO_PY, "submit", "--title", title],
                           capture_output=True, text=True, timeout=60,
                           encoding="utf-8", errors="replace",
                           cwd=ROOT, creationflags=NO_WINDOW)
        msg = (r.stdout or r.stderr or "").strip()[:300] or "已提交"
    except Exception as e:
        msg = "提交失败：%s" % e
    _dialog(msg, "派活")


def _menu_items():
    """托盘菜单的唯一出处：main 建菜单、--probe/--status 打印菜单项都读它。"""
    return [
        ("状态", show_status, True),    # 左键默认项＝状态；连点已由 _dialog 去重挡掉
        ("立即压缩上下文", compress_once, False),
        ("重读设定文件（重新注入交接卡）", retry_inject, False),
        ("任务池（只读）", show_pool, False),
        ("派一条…", submit_task, False),
        ("打开日志", open_log, False),
        ("打开会话存档（本地会话保存）", open_mem, False),
        (None, None, False),                     # 分隔线
        ("退出托盘", quit_app, False),
    ]


def build_menu():
    items = []
    for label, fn, default in _menu_items():
        if label is None:
            items.append(pystray.Menu.SEPARATOR)
        else:
            items.append(pystray.MenuItem(label, fn, default=default))
    return pystray.Menu(*items)


def _print_menu():
    print("[菜单]")
    for label, _fn, default in _menu_items():
        print("  %s %s" % ("*" if default else " ", label or "—— 分隔线 ——"))


def _probe_cost():
    """自检今日成本：打印与 tooltip 成本行同样的内容就退出（不启动托盘、不写东西）。"""
    d = cost_state(force=True)
    if not d:
        print("[成本] 读不到 persona_cost.py 或 ops 账本")
        return
    print("[成本] %s %s  [来源] %s"
          % (d.get("day"), cost_tip(d),
             "ledger" if d.get("source") == "ledger" else "token折算"))
    if d.get("over_daily"):
        print("[成本] 日线已超：%s > %s"
              % (_cost_money(d.get("money"), d.get("currency")),
                 _cost_money(d.get("daily_warn"), d.get("currency"))))


def _probe_compress():
    """自检「立即压缩一次」：跳过确认框，子进程加 --dry-run，只验证走位与着色。"""
    s = snapshot()
    print("[自检] 手动压缩：跳过确认框；子进程加 --dry-run（不注入、不落盘原文）")
    print("[图标] 压缩前颜色=%s" % icon_color_name(s))
    set_busy(True)
    print("[图标] 压缩中颜色=%s（tooltip=%s）" % (icon_color_name(s), BUSY_TITLE))
    _compress_worker(ask=False, extra=("--dry-run",))
    t0 = time.time()
    while time.time() - t0 < 120:
        on, _note = busy_state()
        if not on:
            break
        time.sleep(0.2)
    with _LOCK:
        rc, out = _LAST_COMPRESS["rc"], _LAST_COMPRESS["out"]
    print("[压缩子进程] rc=%s" % rc)
    for line in (out or "").splitlines():
        print("  " + line)
    _on, note = busy_state()
    print("[图标] 压缩后颜色=%s%s"
          % (icon_color_name(), ("（tooltip：%s）" % note) if note else "（已恢复）"))


def main():
    if "--probe" in sys.argv or "--status" in sys.argv:
        s = snapshot()
        ev = INJ.check_alert() if INJ else None
        if ev:
            print("[托盘] 注入档位=%s → 图标底色=%s" % (
                ev.get("level"),
                {"error": "红", "warn": "黄"}.get(ev.get("level"), "无告警（按水位上色）")))
        print(detail_text(s))
        if ev:
            if ev.get("beeped"):
                print("[提示音] 响（上升沿 %s → %s；MessageBeep %s）" % (
                    ev.get("prev"), ev.get("level"),
                    "MB_ICONHAND" if ev.get("level") == "error" else "MB_ICONEXCLAMATION"))
            elif ev.get("level") == "ok":
                print("[提示音] 不响（正常/已恢复；上次报过 %s）" % ev.get("prev"))
            else:
                print("[提示音] 不响（当前 %s 与上次已报的 %s 同档，只响一次）" % (
                    ev.get("level"), ev.get("prev")))
        _print_menu()
        on, note = busy_state()
        print("[托盘] 压缩状态=%s；图标颜色=%s；提示=%s"
              % ("进行中" if on else "空闲", icon_color_name(s),
                 BUSY_TITLE if on else (note or "（正常，按水位上色）")))
        return
    if "--probe-compress" in sys.argv:
        _probe_compress()
        return
    if "--probe-cost" in sys.argv:
        _probe_cost()
        return
    if "--single" not in sys.argv:
        try:                                   # 一个进程、多个图标（2026-11-11 人定的）
            import persona_tray_multi as _M
            return _M.run_all(sys.modules[__name__])
        except Exception as _e:                # noqa: BLE001
            sys.stderr and sys.stderr.write("多图标起不来，退回单图标：%s\n" % _e)
    s = snapshot()
    icon = pystray.Icon(
        "SysPersona",
        icon=make_icon(s["bars"], alarm_level(s)),
        title=s["title"][:120],
        menu=build_menu(),
    )
    threading.Thread(target=refresh, args=(icon,), daemon=True).start()
    icon.run()


if __name__ == "__main__":
    _pio.setup_stdio()  # 管道捕获时别因 ⚠ 炸掉（v6.13.0）
    main()
