# -*- coding: utf-8 -*-
import os as _os_shim, sys as _sys_shim
_sys_shim.path.insert(0, _os_shim.path.join(_os_shim.path.dirname(_os_shim.path.abspath(__file__)), "脚本"))
# SysPersona-OS · 本文件：persona_tray_multi.py — 托盘：一个我们自己的图标 + **按客户端动态生成**的菜单
r"""persona_tray_multi.py — 托盘：一个我们自己的图标 + **按客户端动态生成**的菜单

人 2026-11-11 定的三条（这一版就是照这个写的）：
  1.「图标不需要一直换，一直显示我们图标，只需要异常时变色，然后点击手动显示异常」
     → 图标固定是我们那块 S 牌，只有异常才变绿/琥珀/红；菜单有「只显示异常」。
  2.「菜单一个是动态的，对应的客户端运行才有选项」
     → 菜单按**当前在跑的客户端**生成：DSH 没开就没有 DSH 那几项，Hermes 没开就没有 Hermes 那几项。
  3.「每个客户端单独的配置，因为客户端方法不通用，是我们现在没有定下主客户端」
     → 每个客户端的动作和"怎么执行"写在 `ops\tray-clients.json` 里，代码不写死：
       * DSH 没有命令入口，只能界面注入（`inject`）；
       * Hermes 更开放（CLI / CDP 调试口 / 本地 HTTP），可以走 `cdp`、`kernel`、`keeper`。
     所以这里不假设谁是主客户端；谁是主，只影响你点哪个图标、不看菜单结构。

配置形状（`ops\tray-clients.json`，没有就写一份默认的）：

    {
      "gateway_label": "微信网关",
      "refresh_seconds": 5,
      "clients": {
        "Hermes": {"on": true, "label": "Hermes",
                   "actions": {"status": true,
                               "restart": "watchdog:enact:restart-hermes",
                               "gateway": "hermes-cli"}}
      }
    }

`actions` 的取值（方法）：
    "inject"          走界面注入（DSH 那条路）
    "hermes-cli"      调官方 `hermes gateway ...`（微信网关的拉/重拉）
    "watchdog:a:b"    跑 `persona_watchdog.py a b`

2026-11-13 换官方版：社区版（两段式 desktop+runtime、CDP 调试口 9333、内核 :9120、
gatekeeper 守 runtime）全部删掉，所以 `cdp` / `restart-kernel` / `keeper` 三种方法
不再出现——Hermes 一侧现在只有「状态 / 重启 / 微信网关」，
压缩和重读走我们自己的 WS（persona_context / persona_wake），不再经界面。

2026-11-15 人定稿「托盘只放界面里点不了的事」，菜单结构就此固定（别再随手加）：

    运行状态 → 异常：N 项 →│ 压缩上下文 → 重读设定 → 空闲唤醒 → 立即归档会话
    →│ 版本 ▸ → 记录 ▸ →│ 节点网络 ▸ → 托盘设置 ▸ → 退出

  * **删掉的入口**：切换模型 / 思考等级（`_model_menu` / `_effort_menu` 生成的子菜单）、
    开新会话（`_new_session_dsh`）、发布到全局（任何形态的 publish 都不进托盘）。
    能力本身没删，还在命令行里（persona_model / persona_effort /
    persona_client_switch / persona_ver publish）。
  * 菜单结构**固定**：客户端跑没跑只影响状态文字和能不能点，**不许**拿"在跑"来藏项。
  * 往会话发文字一律走 `persona_inbox_client.send()`（写请求行给插件 persona-inbox），
    不再键盘注入；3 秒没回执就报「插件未响应（DSH 需要重启？）」，不偷偷回落。
  * 水位 / 异常 / 未归档 / 版本 / 交接 全部读现成文件，不自己造数：
    `ops\<机器>\waterline.json`、`waterline-all.json`、`selfcheck.json`、
    `persona_archive.pending()`、`persona_ver.current()/local()`、`persona_handoff.open_items()`。
  * `--dump-menu` 只打印菜单树 + 几行诊断（不弹窗、不起图标、不抢互斥量），改完菜单拿它对树。
"""

import functools
import io
import json
import os
import sys          # 2026-11-13 补：第 2 行只导了 `sys as _sys_shim`，本文件别处用的都是 `sys.executable`，
                  # 于是托盘里点「心跳总开关」「拉起看门狗」「认证」全报 name 'sys' is not defined。
import time
import threading   # 2026-11-13：改心跳这类子进程丢线程跑，别把菜单回调卡住

import pystray
from PIL import Image, ImageDraw
import persona_io as _pio  # stdio/时间统一（v6.13.0）
import persona_store as _store  # 运行态外置（B100）：日志/状态按机器分
_RT = _store.runtime_dir()

GREEN = (46, 125, 50, 255)
AMBER = (249, 168, 37, 255)
RED = (198, 40, 40, 255)
GRAY = (120, 120, 120, 255)

SIZE = 64                     # 画 64 再让系统缩到 16/24/32，边缘才不糊
TICK = 5.0                    # 秒：多久重算一次颜色/菜单

# 2026-11-13 补：本文件下面几处 spawn 用了 NO_WINDOW，但这个常量一直没定义，
# 于是托盘里点「空闲唤醒」→ 弹「改心跳失败：name 'NO_WINDOW' is not defined」。
# 与 `sys` 那个坑同源：从别处抄来的调用片段没带上它依赖的常量。
NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# realpath：C:\SysPersona 是 junction，解析成 D 盘真路径（自动审查按路径判，C 盘容易被拦）
ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
os.environ.setdefault("SYSPERSONA_CLIENT", "tray")   # 工作记录里托盘干的活记成 tray
# 2026-11-14：托盘客户端定义**只认 配置\客户端\tray-clients.json**（人改的那份，跨机同步）。
# 以前读的是 ops 里的运行时副本，结果人改了配置、ops 那份还是旧的（DSH 一直挂在菜单里）。
TRAY_CLIENTS_SRC = os.path.join(ROOT, "配置", "客户端", "tray-clients.json")
TRAY_CLIENTS = os.path.join(__import__("persona_ops").dir(), "tray-clients.json")
# 2026-11-15：老位置（机器分家之前）`ops\tray-clients.json`。那天人改配置改到了这里，
# 而 load_spec 只认上面两份（都还是 11-14 的 Hermes 版）→ 托盘认不到 DSH 进程。
# 现在它也进候选（见 load_spec），谁改得晚听谁的。
TRAY_CLIENTS_OLD = os.path.join(__import__("persona_ops").shared(), "tray-clients.json")
_SPEC_SRC = ""            # 上一次实际生效的那份（只用来"换了才写一行日志"，不参与判断）



def tray_version():
    """托盘版本号（全局可读）：读 配置\\客户端\\tray-version.json。

    那个文件由 脚本/persona_tray_version.py 维护（改完托盘必须 bump），
    任何机器同步之后都能读到同一个版本号。读不到就退回 "?"，不影响托盘启动。
    """
    try:
        import io as _io
        import json as _json
        import os as _os
        p = _os.path.join(ROOT, "配置", "客户端", "tray-version.json")
        with _io.open(p, encoding="utf-8") as f:
            d = _json.load(f)
        return "v%s（%s）" % (d.get("version") or "?", d.get("build") or "?")
    except Exception:                          # noqa: BLE001
        return "v?（版本文件读不到）"

def _client_installed(cfg):
    """本机到底装没装这个客户端：看 detect 里 exe/home/config 有没有一个真实存在。

    为什么不看进程：进程不在有两种完全不同的原因——「装着但没开」和「这台机器没装」。
    前者要提醒，后者提醒一百次也没用（换机器时最明显）。
    """
    det = (cfg or {}).get("detect") or {}
    for key in ("exe", "home", "config"):
        for p in (det.get(key) or []):
            try:
                if p and os.path.exists(os.path.expandvars(str(p))):
                    return True
            except (OSError, ValueError):
                pass
    return False
LOG = os.path.join(_RT, "tray-multi.log")
# 2026-11-13：人点「退出托盘」= 真的要退出（以前守护的 tray 任务 60 秒后又把它拉回来，
# 人看到的就是「每次退出都退不干净」）。这个标记一写，守护就不再自动拉；托盘自己启动时清掉。
TRAY_OFF = os.path.join(__import__("persona_ops").dir(), "tray-off")
STACK_OFF = os.path.join(__import__("persona_ops").dir(), "stack-off")   # 「全家退出」标记：守护/网络/代理一起停，重启入口会清掉它


def _ledgers(name):
    """同名账本的所有落点（机器根 / 各客户端层 / _legacy-*），按 mtime 旧→新。

    2026-11-15 记账分层：新账写 ops/<机器>/<客户端>/，老账还留在机器根，读的时候两边都算。
    """
    po = __import__("persona_ops")
    try:
        ps = po.records(name)
    except Exception:                          # noqa: BLE001
        ps = []
    if not ps:
        return [os.path.join(po.dir(), name)]
    try:
        return sorted(ps, key=os.path.getmtime)
    except OSError:
        return ps

# 开机自启 = Startup 里那个快捷方式在不在（.off 后缀 Windows 不认，等于关掉）
STARTUP_DIR = os.path.join(os.environ.get("APPDATA", ""),
                          r"Microsoft\Windows\Start Menu\Programs\Startup")
TRAY_LNK = os.path.join(STARTUP_DIR, "SysPersona-Tray.lnk")
TRAY_LNK_OFF = TRAY_LNK + ".off"


def autostart_on():
    return os.path.exists(TRAY_LNK)


def autostart_set(on):
    """开/关托盘的「登录时自动启动」。返回 (ok, 说明)。"""
    try:
        if on:
            if os.path.exists(TRAY_LNK):
                return True, "本来就是开着的"
            if os.path.exists(TRAY_LNK_OFF):
                os.replace(TRAY_LNK_OFF, TRAY_LNK)
                return True, "已开：登录会自动起托盘"
            return False, "开机自启项没装过（找不到 %s）" % TRAY_LNK
        if os.path.exists(TRAY_LNK):
            os.replace(TRAY_LNK, TRAY_LNK_OFF)
            return True, "已关：登录不再自动起托盘（守护仍靠计划任务自愈）"
        return True, "本来就是关着的"
    except OSError as e:                   # noqa: BLE001
        return False, str(e)[:120]

FONT_CANDS = (r"C:\Windows\Fonts\seguisb.ttf",      # Segoe UI Semibold
              r"C:\Windows\Fonts\segoeuib.ttf",
              r"C:\Windows\Fonts\arialbd.ttf")

WARN_PCT, ERR_PCT = 70, 85
COMPACT_WAIT_MIN = 25      # 水位过留意线后，超过这么久没真正压缩 = 超时（只亮红灯）

# ── 单实例：全局命名互斥量（2026-11-11 13:40） ──
# 为什么不用命令行判重：提权进程的 CommandLine 从非提权进程读出来是空的，
# 于是守护每 120 秒都以为"托盘没在跑"，重复拉起 → 人看到两个、三个图标。
# 命名互斥量跨权限级别有效：第二个实例拿到 ERROR_ALREADY_EXISTS 就直接退出。
MUTEX_NAME = r"Global\SysPersonaTray-v1"
ERROR_ALREADY_EXISTS = 183
_mutex_handle = None


def mutex_alive():
    """有人占着托盘互斥量 = 已经有一个托盘在跑。"""
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        h = k32.OpenMutexW(0x00100000, False, MUTEX_NAME)   # SYNCHRONIZE
        if h:
            k32.CloseHandle(h)
            return True
        return ctypes.get_last_error() == ERROR_ALREADY_EXISTS
    except Exception:                      # noqa: BLE001
        return False


def claim_mutex():
    """抢占托盘互斥量。返回 True = 我是唯一实例。"""
    global _mutex_handle
    try:
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        ctypes.set_last_error(0)
        h = k32.CreateMutexW(None, False, MUTEX_NAME)
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            if h:
                k32.CloseHandle(h)
            return False
        _mutex_handle = h
        return bool(h)
    except Exception:                      # noqa: BLE001
        return True                        # 抢不到锁就当能跑，别把托盘弄没了


DEFAULT_SPEC = {
    "_note": "托盘只显示【本机】任务池（ops\\tasks.jsonl，谁领谁干）；服务端/远端任务池不进托盘，"
             "要看得单独请求。会话/日志按客户端分：open.sessions。",
    "gateway_label": "微信网关",
    "refresh_seconds": 5,
    "clients": {
        "DSH": {
            "on": True, "label": "DSH",
            # 2026-11-15：Hermes 删除，托盘只留 DSH（人：「固定用这个客户端」）。
            # 进程名是 Desktop 的 "DeepSeek Harness.exe"；动作只留 DSH 真能做的：
            # 压缩走托盘自己的 compress_once、重读设定走注入、换模型/推理档走命令行。
            "img": ["DeepSeek Harness.exe"],
            "wm": "dsh",
            "open": {"sessions": ""},        # 空 = 按 DSH 会话目录推
            "actions": {"status": True,
                        "compact": True,
                        "reload": True,
                        "model": True,
                        "effort": True},
        },
    },
}


def procs_by_img():
    """{exe 小写: [pid, ...]}：toolhelp 快照直读，不起子进程、不闪窗。"""
    try:
        import persona_winwatch as W
        snap = W.snapshot()
    except Exception as e:                 # noqa: BLE001
        _p("快照失败：%s" % e)
        return {}
    out = {}
    for pid, (_pp, name) in snap.items():
        out.setdefault((name or "").lower(), []).append(pid)
    return out


# ── 版本：客户端更新了要能看出来（人 2026-11-11「客户端还要看版本，如果更新，可能也需要同步」）──
VERSIONS = os.path.join(__import__("persona_ops").dir(), "client-versions.json")


def exe_version(path):
    """读 exe 的 FileVersion（ctypes 直读，不起子进程、不弹窗）；读不到返回 ""。"""
    try:
        import ctypes
        from ctypes import wintypes
        import struct
        size = ctypes.windll.version.GetFileVersionInfoSizeW(path, None)
        if not size:
            return ""
        buf = ctypes.create_string_buffer(size)
        if not ctypes.windll.version.GetFileVersionInfoW(path, 0, size, buf):
            return ""
        r = ctypes.c_void_p()
        n = wintypes.UINT()
        if not ctypes.windll.version.VerQueryValueW(buf, "\\", ctypes.byref(r),
                                                   ctypes.byref(n)):
            return ""
        data = ctypes.string_at(r, 16)
        ms = struct.unpack_from("<I", data, 8)[0]
        ls = struct.unpack_from("<I", data, 12)[0]
        return "%d.%d.%d.%d" % (ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF)
    except Exception:                      # noqa: BLE001
        return ""


def _load(path, default):
    try:
        with io.open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:                      # noqa: BLE001
        return default


def _find_versions_dir(path):
    """从 exe 路径往上找 `data\\versions`（Hermes 的内核版本都在那儿）。"""
    d = os.path.dirname(path or "")
    for _ in range(6):
        if not d:
            break
        for cand in (os.path.join(d, "data", "versions"), os.path.join(d, "versions")):
            if os.path.isdir(cand):
                return cand
        d = os.path.dirname(d)
    return ""


def _save(path, obj):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except Exception:                      # noqa: BLE001
        pass


READONLY = "--readonly" in sys.argv     # 只读：不写库、不自动重载（给还没定角色的机器用）
LOCAL_LOG = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                         "SysPersona", "tray-readonly.log")


def _p(msg):
    """pythonw 没有 stdout，一律写文件。只读模式下只写本机日志，一个字节都不进库。"""
    path = LOCAL_LOG if READONLY else LOG
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with io.open(path, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:                      # noqa: BLE001
        pass


# ── 画我们自己的图标 ─────────────────────────────────────────────────
def clip_tip(title, n=124):
    """tooltip 截断：按**行**截，不在句子中间切。

    2026-11-12 改多行 tooltip 之后，原来直接 `title[:120]` 会把最后一行切成半句；
    Windows 的 szTip 上限是 128 个字符，留 4 个字符余量。
    """
    s = str(title or "")
    if len(s) <= n:
        return s
    out = []
    used = 0
    for line in s.split("\n"):
        if used + len(line) + (1 if out else 0) > n - 1:
            break
        out.append(line)
        used += len(line) + (1 if len(out) > 1 else 0)
    return "\n".join(out) + "…" if out else s[:n - 1] + "…"


def _font(px):
    from PIL import ImageFont
    for p in FONT_CANDS:
        try:
            if os.path.exists(p):
                return ImageFont.truetype(p, px)
        except Exception:                  # noqa: BLE001
            pass
    return ImageFont.load_default()


def main_art(color, size=SIZE):
    """主程序图标：深色圆角牌 + 白 S，外圈按状态上色（绿/琥珀/红/灰）。

    4 倍绘制再缩回来，边缘才有抗锯齿；不抽任何客户端 exe 的图标——
    人 2026-11-11 说的「一直显示我们图标」。
    """
    big = size * 4
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r = int(big * 0.22)
    d.rounded_rectangle([0, 0, big - 1, big - 1], radius=r, fill=(26, 28, 34, 255))
    pad = max(1, big // 40)
    d.rounded_rectangle([pad, pad, big - 1 - pad, big - 1 - pad], radius=max(2, r - pad),
                        outline=(80, 84, 96, 255), width=max(1, big // 80))
    f = _font(int(big * 0.62))
    d.text((big // 2, big // 2 - int(big * 0.02)), "S", font=f,
           fill=(238, 240, 246, 255), anchor="mm")
    d.rounded_rectangle([0, 0, big - 1, big - 1], radius=r, outline=color,
                        width=max(2, int(big * 0.09)))
    return img.resize((size, size), Image.LANCZOS)


# ── 配置 ─────────────────────────────────────────────────────────────
def save_spec(spec, bump=True, why="托盘配置变更"):
    r"""保存托盘配置：**本机一份 + 共享一份 + 版本号 +1**（人 2026-11-15）。

    原话：「每次无论是在哪一台修改了，自己保存还要替换全局可读的那个，并修改版本；
            其他的每次查一次版本，就知道本机版本了」。

    所以这里不能只写本机副本：
      * `TRAY_CLIENTS`      本机运行态副本（读得快，离线也能用）
      * `TRAY_CLIENTS_SRC`  库里的共享那份（**这才是别的机器读的**）
      * `配置\版本.json`     rev+1（托盘/守护每拍比对它就知道"该重载了"）
    """
    data = json.dumps(spec, ensure_ascii=False, indent=1)
    for path in (TRAY_CLIENTS, TRAY_CLIENTS_SRC):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with io.open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(data)
        except Exception as e:                 # noqa: BLE001
            _p("写 %s 失败：%s" % (os.path.basename(path), str(e)[:60]))
    if bump:
        try:
            import persona_ver as _V
            rec = _V.bump(why)
            _p("已保存并发布：rev %s（%s）" % (rec.get("rev"), why))
        except Exception as e:                 # noqa: BLE001
            _p("版本 bump 失败：%s" % str(e)[:60])


def _save_runtime(spec):
    r"""只写**本机运行态**那份：`ops\<机器>\tray-clients.json`。

    为什么不连带写 `配置\客户端\tray-clients.json`：那是人改的共享真源，
    托盘自己补字段 / 改刷新间隔不该去动它（人 2026-11-15：「版本来我来 bump」，
    配置也由人自己同步）；也不 bump 版本——这里的写只是**收敛**，不是"改了配置"。

    写完这行的 mtime 就是最新的，于是下一拍 `load_spec()` 会以它为准，
    不会再被一份更旧的共享副本顶掉（这正是"托盘认不到 DSH"的病根）。
    """
    try:
        os.makedirs(os.path.dirname(TRAY_CLIENTS), exist_ok=True)
        tmp = TRAY_CLIENTS + ".tmp"
        with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(spec, ensure_ascii=False, indent=1))
        os.replace(tmp, TRAY_CLIENTS)
        return True
    except Exception as e:                 # noqa: BLE001
        _p("写运行态 tray-clients.json 失败：%s" % str(e)[:80])
        return False


def load_spec():
    r"""读托盘配置：**存在的那几份里取 mtime 最新的那份**；老格式自动迁移，不丢设置。

    2026-11-15 修「托盘认不到 DSH 进程」的根因：以前只认
    `配置\客户端\tray-clients.json`（共享真源，存在就用它），而人那次改配置
    改的是**老位置** `ops\tray-clients.json`（11-15 14:49，写的是 DSH +
    `img: DeepSeek Harness.exe`）；共享那份和运行态副本都还停在 11-14 的 Hermes 版。
    于是 `_rows()` 里没有一个客户端能对上正在跑的 `DeepSeek Harness.exe`，
    `_running("DSH")` 永远 False（进程明明有 9 个）。

    现在三份候选（共享 / 运行态 / 老位置）都读，**谁写得晚听谁的**，
    再把结果收敛到运行态那份（不写 `配置\`、不 bump 版本）。
    """
    global _SPEC_SRC
    spec = json.loads(json.dumps(DEFAULT_SPEC))       # deep copy
    cands = [p for p in (TRAY_CLIENTS_SRC, TRAY_CLIENTS, TRAY_CLIENTS_OLD)
             if os.path.exists(p)]
    if not cands:
        _save_runtime(spec)                   # 首次落地：不算"人改了"，不 bump
        return spec

    def _mt(p):
        try:
            return os.path.getmtime(p)
        except OSError:
            return 0.0

    # 同 mtime 时偏向共享真源（人改的那份），顺手也把"谁赢了"记一行日志（只在换了时写）
    src = max(cands, key=lambda p: (_mt(p), p == TRAY_CLIENTS_SRC))
    if src != _SPEC_SRC:
        _SPEC_SRC = src
        _p("托盘配置以最新那份为准：%s" % src)
    try:
        with io.open(src, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:                 # noqa: BLE001
        _p("读 %s 失败，用默认：%s" % (os.path.basename(src), e))
        return spec

    if isinstance(raw.get("clients"), dict):          # 新格式
        # 2026-11-14：文件里列了哪些客户端，就只保留哪些（否则默认表会把删掉的又加回来）
        spec["clients"] = {}
        for name, cfg in raw["clients"].items():
            if not isinstance(cfg, dict):
                continue
            cur = spec["clients"].setdefault(name, {"on": True, "label": name, "actions": {}})
            # 全量合并：白名单会把新字段直接砍掉（detect / open 就是这么丢的，2026-11-11 踩过）
            cur.update(cfg)
        for k, v in raw.items():
            if k != "clients":
                spec[k] = v
        # 收敛：读到的不是运行态那份、或补过字段 → 只补运行态那份（配置\ 一个字节都不动）
        if (os.path.abspath(src) != os.path.abspath(TRAY_CLIENTS)) or (raw != spec):
            _save_runtime(spec)
        return spec

    # 老格式：{"DSH": true, "Hermes": true, "rotate_seconds": 4}
    for name, val in raw.items():
        if name in spec["clients"] and isinstance(val, bool):
            spec["clients"][name]["on"] = val
    if isinstance(raw.get("rotate_seconds"), (int, float)):
        spec["refresh_seconds"] = raw["rotate_seconds"]
    if _save_runtime(spec):            # 就地迁移成新格式，下次直接读到结构
        _p("tray-clients.json 已迁移成新格式（clients/actions，只写运行态那份）")
    return spec


# ── 显示格式（水位那一行：人 2026-11-15 定的样子）──────────────────────
def _fmt_tok(n):
    """tokens / 窗口：`340K` / `1.05M`（只留人看得懂的两个量级）。"""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "?"
    if n >= 1e6:
        return "%.2fM" % (n / 1e6)
    if n >= 1000:
        return "%.0fK" % (n / 1e3)
    return "%.0f" % n


def _fmt_delta(n):
    """距压缩线：`+197K` 还差这么多、`-26K` 已经过线这么多。"""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "?"
    return "%+.0fK" % (n / 1e3) if abs(n) >= 1000 else "%+.0f" % n


# 「重读设定」发出去的那段话：文件名照 `memory\设定\` 里**真实存在**的写
# （是 `30-流程与运维.md`，不是 30-流程与治理.md），与 persona_wake.SETTINGS 一致。
REREAD_TEXT = (
    "设定重载：按序读 → memory\\设定\\00-索引.md / 10-人格与红线.md / "
    "20-交流与要求.md / 30-流程与运维.md / 40-事实与现状.md\n"
    "（只读设定；待办查 persona_handoff.py open，问「以前做过什么」再读 配置\\全局状态.md）\n"
    "读完整条跑：python 脚本/persona_wake.py reread --ack")


# ── 托盘本体 ─────────────────────────────────────────────────────────
class Tray(object):
    def __init__(self, tray_module):
        self.T = tray_module
        self.icon = None
        self.stop_flag = False
        self._img = {}                   # 颜色 → 成品图标
        self._title = ""
        self._sig = None                 # 上次的"在跑哪些客户端"，变了就重建菜单
        self.spec = load_spec()
        self.period = float(self.spec.get("refresh_seconds") or TICK)
        # 人 2026-11-15：界面刷新跟着自检周期走，不再单独设。
        try:
            self.period = self._period_from_selfcheck()
        except Exception:                # noqa: BLE001
            pass

    # ── 读数 ──
    def _rows(self):
        """按配置里的 `img`（进程名）自己算谁在跑。

        人 2026-11-11：「比如别人拿出配合他自己的客户端，就需要匹配他自己的」——
        所以**新客户端 = 加一段配置**（img + actions + 可选 wm），不用改代码：
        它按自己的 exe 名被认出来，按自己配的方法被操作；没有水位源就只报"在跑"，不算异常。
        """
        procs = procs_by_img()
        out = {}
        for name, cfg in (self.spec.get("clients") or {}).items():
            if cfg.get("on") is False:
                continue
            imgs = [str(x).lower() for x in (cfg.get("img") or [])]
            n = sum(len(procs.get(i, [])) for i in imgs)
            if not n:
                continue
            wm = cfg.get("wm") or ""
            d = {}
            try:
                if wm == "dsh":
                    d = self.T.dsh_watermark() or {}
                elif wm == "hermes":
                    d = self.T.hermes_watermark() or {}
            except Exception:              # noqa: BLE001
                d = {}
            out[name] = {"name": name, "label": cfg.get("label") or name, "alive": True,
                         "procs": n, "wm": bool(d),
                         "ctx": d.get("ctx"), "max": d.get("max"),
                         "pct": d.get("pct"), "note": d.get("note") or ""}
        return out

    def _running(self, name):
        cfg = (self.spec.get("clients") or {}).get(name) or {}
        if cfg.get("on") is False:
            return False
        return name in self._rows()

    # ── 读数：都读现成文件（人 2026-11-15 定的表，别自己发明）─────────────
    def _wl(self):
        r"""当前会话水位：`ops\<机器>\waterline.json`。

        `{tokens, window, pct, limit, source, session}`，`pct` 直接用。
        读不到就返回 {}——调用方显示 `?`，不拿别的数冒充。
        """
        p = os.path.join(__import__("persona_ops").dir(), "waterline.json")
        d = _load(p, {})
        return d if isinstance(d, dict) else {}

    def _wl_all(self):
        r"""多会话水位：`ops\<机器>\waterline-all.json` → `sessions[]`（在跑/最近/水位）。"""
        p = os.path.join(__import__("persona_ops").dir(), "waterline-all.json")
        d = _load(p, {})
        return d if isinstance(d, dict) else {}

    def _selfcheck(self):
        r"""异常项：`ops\<机器>\selfcheck.json`（`persona_selfcheck.py --once` 写的）。

        返回 `{"at","summary","total","fails":[(名字, 状态, 明细)]}`；
        FAIL 判据 = `status != "ok"` **或** `ok is False`（老文件只有 ok 字段）。
        """
        p = os.path.join(__import__("persona_ops").dir(), "selfcheck.json")
        d = _load(p, {})
        items = (d.get("items") or {}) if isinstance(d, dict) else {}
        fails = []
        for name, v in items.items():
            v = v if isinstance(v, dict) else {}
            st = str(v.get("status") or ("ok" if v.get("ok") is not False else "fail"))
            if st != "ok" or v.get("ok") is False:
                fails.append((name, st, str(v.get("detail") or "")))
        return {"at": (d.get("at") if isinstance(d, dict) else "") or "",
                "summary": (d.get("summary") if isinstance(d, dict) else "") or "",
                "total": len(items), "fails": fails}

    def _pending_archive(self):
        """未归档数：`persona_archive.pending()`；60 秒缓存（别每 5 秒把会话目录扫一遍）。"""
        now = time.time()
        c = getattr(self, "_arch_cache", None)
        if c and now - c[0] < 60:
            return c[1]
        n = None
        try:
            import persona_archive as A
            n = len(A.pending())
        except Exception as e:             # noqa: BLE001
            _p("读未归档数失败：%s" % str(e)[:120])
        self._arch_cache = (now, n)
        return n

    def _handoff_open(self):
        """交接未完成条数：`persona_handoff.open_items()`。"""
        try:
            import persona_handoff as H
            return len(H.open_items())
        except Exception as e:             # noqa: BLE001
            _p("读交接账失败：%s" % str(e)[:120])
            return None

    def _ver(self):
        """(全局 version, 全局 rev, 本机 rev, 本机是否跟随全局)。"""
        try:
            import persona_ver as V
            g = V.current() or {}
            loc = V.local() or {}
            grev = int(g.get("rev") or 0)
            f = loc.get("follows") or {}
            follows = bool(f.get("rev")) and int(f.get("rev") or 0) == grev
            return (g.get("version") or "未建立", grev, int(loc.get("rev") or 0), follows)
        except Exception as e:             # noqa: BLE001
            _p("读版本中心失败：%s" % str(e)[:120])
            return ("?", 0, 0, False)

    def _gver(self):
        return self._ver()[0]

    def _imgs(self, name="DSH"):
        """这个客户端可能用的进程名：配置里有就用配置的，再加默认表里的（去重保序）。"""
        cfg = (self.spec.get("clients") or {}).get(name) or {}
        dflt = ((DEFAULT_SPEC.get("clients") or {}).get(name) or {}).get("img") or []
        out = []
        for x in list(cfg.get("img") or []) + list(dflt):
            s = str(x).lower()
            if s and s not in out:
                out.append(s)
        return out

    def _dsh_procs(self):
        """正在跑的 DSH 进程数（按进程名直读快照，不看菜单该不该显示）。"""
        try:
            procs = procs_by_img()
        except Exception:                  # noqa: BLE001
            return 0
        return sum(len(procs.get(i) or []) for i in self._imgs("DSH"))

    def _dsh_alive(self):
        """DSH 在不在跑。

        2026-11-15：**不再用"客户端在跑"来决定菜单显不显示**——菜单结构是固定的，
        在不在跑只影响状态文字和能不能点。所以这里直接按进程名探，
        而且配置里没有 DSH 那一段也照样按默认表探（防配置再被旧副本顶掉）。
        """
        return self._dsh_procs() > 0

    def _waterline_line(self):
        """`水位 32.4% · 340K / 1.05M · 距压缩线 -26K`——运行状态第一行，也喂给 `--dump-menu`。"""
        d = self._wl()
        if not d:
            return ("上下文 读数不可用（ops\\%s\\waterline.json 没有）"
                    % __import__("persona_ops").dir_name())
        try:
            pct = "%.1f%%" % float(d.get("pct"))
        except (TypeError, ValueError):
            pct = "?"
        seg = "上下文 %s · %s / %s" % (pct, _fmt_tok(d.get("tokens")), _fmt_tok(d.get("window")))
        try:
            seg += " · 距压缩线 %s" % _fmt_delta(float(d.get("limit")) - float(d.get("tokens")))
        except (TypeError, ValueError):
            seg += " · 距压缩线 ?"
        return seg

    def _tip_line(self):
        """图标悬停的固定格式（人 2026-11-15）：`DSH · 水位 32.4% · v2026.11.15-1534`。"""
        d = self._wl()
        try:
            pct = "%.1f%%" % float(d.get("pct"))
        except (TypeError, ValueError):
            pct = "?"
        return "DSH · 水位 %s · v%s" % (pct, self._gver())

    # ── 版本与同步检查 ──
    def _facts(self):
        """每个客户端现在的版本/路径（只算在跑的）。"""
        procs = procs_by_img()
        try:
            import persona_winwatch as W
        except Exception:                  # noqa: BLE001
            W = None
        out = {}
        for name, cfg in (self.spec.get("clients") or {}).items():
            if cfg.get("on") is False:
                continue
            path = ""
            for img in [str(x).lower() for x in (cfg.get("img") or [])]:
                for pid in procs.get(img) or []:
                    path = (W.exe_path(pid) if W else "") or ""
                    if path:
                        break
                if path:
                    break
            if not path:
                continue
            out[name] = {"version": exe_version(path), "path": path}
            # Hermes 的内核是它自己那个 runtime：真版本号在 data\versions\<ver>\manifest.json 里
            if (cfg.get("wm") or "") == "hermes":
                try:
                    vd = _find_versions_dir(path)
                    if vd:
                        dirs = sorted(d for d in os.listdir(vd)
                                      if os.path.isdir(os.path.join(vd, d)))
                        if dirs:
                            man = _load(os.path.join(vd, dirs[-1], "manifest.json"), {})
                            out[name]["runtime"] = man.get("runtimeVersion") or dirs[-1]
                            out[name]["kernel"] = man.get("kernelVersion") or ""
                except Exception:          # noqa: BLE001
                    pass
        return out

    @staticmethod
    def _vkey(f):
        """一次"所见版本"的指纹：exe 版本 + 内核/runtime 版本（有就带）。"""
        return "%s|%s" % (f.get("version") or "", f.get("runtime") or "")

    def _version_changes(self):
        """[(说明)]：这次所见版本和上次记录不一样，且人还没确认过。"""
        facts = self._facts()
        st = _load(VERSIONS, {})
        acked = st.get("_acked") or {}
        changes = []
        dirty = False
        for name, f in facts.items():
            cfg = (self.spec.get("clients") or {}).get(name) or {}
            label = cfg.get("label") or name
            prev = (st.get(name) or {}).get("key") or ""
            key = self._vkey(f)
            if prev and key != prev and acked.get(name) != key:
                changes.append("%s 更新了：%s → %s（可能要同步：路径/方法/会话格式）"
                               % (label, prev.replace("|", " "), key.replace("|", " ")))
            if (not prev) or key != prev or (st.get(name) or {}).get("path") != f.get("path"):
                st[name] = {"key": key, "version": f.get("version") or "",
                            "runtime": f.get("runtime") or "",
                            "kernel": f.get("kernel") or "",
                            "path": f.get("path"),
                            "seen_at": time.strftime("%Y-%m-%d %H:%M:%S")}
                dirty = True
        if dirty:
            _save(VERSIONS, st)
        return changes

    def _show_versions(self, icon=None, item=None):
        """「客户端版本 / 同步检查」：逐个列出名字、版本、路径、上次记录。"""
        facts = self._facts()
        st = _load(VERSIONS, {})
        changes = self._version_changes()
        lines = []
        for name, cfg in (self.spec.get("clients") or {}).items():
            if cfg.get("on") is False:
                continue
            label = cfg.get("label") or name
            f = facts.get(name)
            if not f:
                rec = st.get(name) or {}
                lines.append("%s：没在运行（上次记录 %s%s）"
                             % (label, rec.get("version") or "无",
                                ("，内核 %s" % rec["runtime"]) if rec.get("runtime") else ""))
                continue
            head = "%s：%s" % (label, f.get("version") or "版本读不到")
            if f.get("runtime"):
                head += "（内核 %s%s）" % (f["runtime"],
                                        (" / 基础版 %s" % f["kernel"]) if f.get("kernel") else "")
            lines.append(head)
            lines.append("   %s" % f.get("path"))
            rec = st.get(name) or {}
            if rec.get("key") and rec.get("key") != self._vkey(f):
                lines.append("   上次记录：%s" % (rec.get("key") or "").replace("|", " "))
        body = ["【客户端版本】", ""] + lines
        if changes:
            body += ["", "【需要同步】"] + ["· " + c for c in changes]
            body += ["", "确认已经适配过，点菜单「版本已确认（清提醒）」"]
        else:
            body += ["", "版本和人确认过的一致。"]
        self.T._dialog("\n".join(body), "SysPersona · 客户端版本")

    def _ack_versions(self, icon=None, item=None):
        """「版本已确认」：把当前版本记成"看过了"，异常提示消失。"""
        facts = self._facts()
        st = _load(VERSIONS, {})
        acked = st.get("_acked") or {}
        for name, f in facts.items():
            if f.get("version"):
                acked[name] = self._vkey(f)
        st["_acked"] = acked
        _save(VERSIONS, st)
        self.T._dialog("已记下当前版本，之后它们再更新还会提醒。", "SysPersona · 客户端版本")


    def _compact_overdue(self):
        """压缩超时了吗？返回要说的一行（空=没事）。

        两条判据，任一命中就算：
          ① `ops\\compact-alert.json` 是 6 小时内的"连压不降"告警；
          ② 水位已经在留意线以上，但 `ops\\<机器id>\\<客户端>\\compact.jsonl` 里最近一次真正 fired
             已经超过 `COMPACT_WAIT_MIN` 分钟（该压没压成：排队、窗口忙、注入失败都算）。
        只报不做：**图标变红就够了**，不弹窗、不抢焦点。
        """
        base = ROOT
        now = time.time()
        # ① 连压不降
        try:
            with io.open(os.path.join(base, "ops", "compact-alert.json"), encoding="utf-8") as f:
                al = json.load(f)
            at = time.mktime(time.strptime(al.get("at") or "", "%Y-%m-%d %H:%M:%S"))
            if now - at < 6 * 3600:
                return "压缩：连压不降（%s，水位没下来）" % (al.get("at") or "?")
        except Exception:                  # noqa: BLE001
            pass
        # ② 该压没压
        try:
            rows = self._rows()
            pct = max([(r.get("pct") or 0) for r in rows.values()] or [0])
        except Exception:                  # noqa: BLE001
            pct = 0
        if pct < WARN_PCT:
            return ""
        last = None
        for _p in reversed(_ledgers("compact.jsonl")):   # 先客户端层，再回落机器根/历史
            try:
                with io.open(_p, encoding="utf-8") as f:
                    lines = f.read().strip().splitlines()[-80:]
            except Exception:                  # noqa: BLE001
                continue
            for ln in reversed(lines):
                try:
                    d = json.loads(ln)
                except ValueError:
                    continue
                if d.get("action") == "fired":
                    last = time.mktime(time.strptime(d.get("at") or "",
                                                     "%Y-%m-%d %H:%M:%S"))
                    break
            if last is not None:
                break
        if last is None:
            return "压缩：水位 %.0f%% 但查不到压缩记录（巡检可能停了）" % pct
        mins = (now - last) / 60.0
        if mins >= COMPACT_WAIT_MIN:
            return "压缩：水位 %.0f%%，上次压缩在 %.0f 分钟前（超时未压）" % (pct, mins)
        return ""

    def _pending_manual(self):
        """有没有"自动连拦 3 次、等着人补发"的欠账（C 方案，读 reloader 写的那份）。"""
        base = ROOT
        try:
            with io.open(os.path.join(base, "ops", "pending-manual.json"), encoding="utf-8") as f:
                d = json.load(f)
            return d if d.get("at") else None
        except Exception:                  # noqa: BLE001
            return None

    @staticmethod
    def _worse(a, b):
        """红 > 琥珀 > 绿 > 灰：取更严重那个。"""
        rank = {GRAY: 0, GREEN: 1, AMBER: 2, RED: 3}
        return a if rank.get(a, 0) >= rank.get(b, 0) else b

    def _status(self):
        r"""算一次状态：返回 (颜色, 异常行, rows, tooltip)。

        2026-11-15 数据源定稿（人定的表，别自己发明）：
          * 水位 → `ops\<机器>\waterline.json`（`pct` 直接用）
          * 异常 → `ops\<机器>\selfcheck.json` 的 FAIL 项（status != ok）
          * 在没在跑 → 按进程名直读（`_dsh_alive()`），**不拿"在跑"去藏菜单**
        tooltip 固定一行：`DSH · 水位 32.4% · v2026.11.15-1534`。

        这里还留着两条旧判据（都是"该做的事没做成"，人 2026-11-11 定：只亮红灯、不弹窗）：
        压缩超时、自动重载连拦 3 次的欠账。守护/注入/成本三条不再在这判——
        selfcheck.json 里本来就有 daemon / inject / cost 三项，两个来源只会打架。
        """
        # 2026-11-14：真源配置改了，进程里缓存的 spec 还是旧的，tooltip 就会一直报
        # 「DSH 没在跑」。每拍重读一次小文件（几 KB），改完配置即时生效。
        try:
            self.spec = load_spec()
        except Exception:                  # noqa: BLE001
            pass

        rows = self._rows()
        lines = []
        color = GREEN

        # 0) 在不在跑：灰 = 没有客户端（只影响颜色与可点，不影响菜单结构）
        alive = self._dsh_alive()
        if not alive:
            color = GRAY

        # 1) 水位（真源：waterline.json）
        d = self._wl()
        try:
            pct = float(d.get("pct"))
        except (TypeError, ValueError):
            pct = None
        if pct is None:
            lines.append("水位：读数不可用（waterline.json 没有或没有 pct）")
            if alive:
                color = self._worse(color, AMBER)
        elif pct >= ERR_PCT:
            lines.append("上下文 %.1f%%（告警线 %d%%）" % (pct, ERR_PCT))
            color = RED
        elif pct >= WARN_PCT:
            lines.append("上下文 %.1f%%（留意线 %d%%）" % (pct, WARN_PCT))
            color = self._worse(color, AMBER)

        # 2) 自检 FAIL 项（真源：selfcheck.json）
        try:
            sc = self._selfcheck()
        except Exception as e:             # noqa: BLE001
            sc = {"fails": [], "at": "", "summary": "", "total": 0}
            _p("读自检失败：%s" % e)
        for nm, st, det in sc["fails"]:
            lines.append("自检 %s（%s）：%s" % (nm, st, det or "没有明细"))
            color = self._worse(color, AMBER)

        # 3) 客户端版本：更新了但还没人确认过（人 2026-11-11：更新可能要同步）
        try:
            for c in self._version_changes():
                lines.append(c)
                color = self._worse(color, AMBER)
        except Exception as e:             # noqa: BLE001
            _p("版本检查失败：%s" % e)

        # 4) 压缩超时：该压却没压成，只把图标变红，不弹窗、不自己动手
        try:
            why = self._compact_overdue()
        except Exception as e:             # noqa: BLE001
            why = ""
            _p("压缩超时判断失败：%s" % e)
        if why:
            lines.append(why)
            color = self._worse(color, RED)

        # 5) 自动重载连续 3 次没发出去 → 转成待人补的欠账（2026-11-11 人定 C 方案）
        try:
            pm = self._pending_manual()
        except Exception as e:             # noqa: BLE001
            pm = None
            _p("待办判断失败：%s" % e)
        if pm:
            lines.append("待你补发：设定重载连续 %s 次没发出去（%s 起）"
                         % (pm.get("count") or "?", pm.get("at") or "?"))
            color = self._worse(color, RED)

        return color, lines, rows, self._tip_line()

    def _beat_tip(self):
        r"""心跳那一行：**报时间**（人 2026-11-13：「每一分钟先检测…然后在报一个时间」）。

        不看输入框、不进壳，只让人一眼看见：心跳还跳不跳、这一拍判的什么、静了多久。
        按时间报，不报"第几拍"——人原话：「以后次数多了，那个数字要涨多大？」。
        当天第一次带年月日，之后只带时分秒。
        """
        p = os.path.join(__import__("persona_ops").dir(), "selfwake.json")
        try:
            with io.open(p, encoding="utf-8-sig") as f:
                d = json.load(f) or {}
        except (OSError, ValueError):
            return ""
        at = str(d.get("at") or "")
        if len(at) < 19:
            return ""
        stamp = at[11:19] if at[:10] == time.strftime("%Y-%m-%d") else at
        if not d.get("wake_on"):
            return "心跳 %s · 关着（只检测）" % stamp
        need_min = max(1, int(d.get("need_sec") or 1800) // 60)
        if d.get("busy"):
            return "心跳 %s · 在干活，静 0 分钟（%s）" % (
                stamp, str(d.get("why") or "")[:24])
        idle_min = int(d.get("idle_sec") or 0) // 60
        return "心跳 %s · 已静 %d 分钟（%d 分钟才唤醒）" % (stamp, idle_min, need_min)

    def _image(self, color):
        if color not in self._img:
            self._img[color] = main_art(color)
        return self._img[color]

    # ── 动作：统一按配置里的"方法"分发 ──
    def _run_watchdog_sync(self, *args):
        """同步跑一个 watchdog 动作，拿回 (ok, 输出尾巴)，供进度窗显示结果。"""
        import subprocess
        import sys
        here = ROOT
        try:
            r = subprocess.run([sys.executable, os.path.join(here, "脚本/persona_watchdog.py")] + list(args),
                               cwd=here, capture_output=True, timeout=300,
                               creationflags=0x08000000,
                               encoding="utf-8", errors="replace")
            out = ((r.stdout or "") + (r.stderr or "")).strip()
            return r.returncode == 0, (out[-600:] or ("exit %d" % r.returncode))
        except Exception as e:             # noqa: BLE001
            return False, "跑不动：%s" % str(e)[:200]

    def _reload_via_inbox(self, label="DSH"):
        """托盘 → 壳：写一条 `kind=wake` 请求，插件把它投进会话（2026-11-16 人：「再来做托盘到壳的通道」）。

        与老 `_reload_via` 的区别：不走键盘、不抢焦点、不需要壳在前台；
        壳在托盘/后台也能收。没回执就老实报"插件未响应"，不回落键盘。
        """
        text = ("重读设定（托盘点「重读设定」）：按 memory\\设定\\00-索引 的顺序把 10/20/30/40 正文读完，"
                "读完跑 `python 脚本\\persona_wake.py reread --ack`，再回一句当前状态。")

        def work():
            import sys as _sys
            if ROOT not in _sys.path:
                _sys.path.insert(0, ROOT)
            try:
                import persona_inbox_client as IB
                ok, msg = IB.send(text, kind="wake", timeout=8)
            except Exception as e:             # noqa: BLE001
                return False, "✘ 通道不可用：%s" % str(e)[:160]
            return bool(ok), (("✔ " if ok else "✘ ") + str(msg)[:200])

        return work

    def _reload_via(self, target):
        """重读设定（第 8 步）。target: dsh / hermes。"""
        def work():
            import sys as _sys
            here = ROOT
            if here not in _sys.path:
                _sys.path.insert(0, here)
            from steps import reload_settings as R
            try:
                ok, msg = R.run({}, target, force=True)
            except Exception as e:         # noqa: BLE001
                ok, msg = False, "跑不动：%s" % str(e)[:160]
            return bool(ok), ("✔ " if ok else "✘ ") + str(msg)
        return work

    def _reload_forced(self):
        """补发被自动拦下的那次重载（C 方案的手动出口）：走 reloader --manual。

        手动 = 不看任何闸门（他刚点完菜单，30 秒打字那条必然命中），
        发成功就清账，托盘红灯自己灭。
        """
        def work():
            import subprocess
            import sys as _sys
            here = ROOT
            try:
                r = subprocess.run([_sys.executable,
                                    os.path.join(here, "脚本/persona_constitution_reloader.py"),
                                    "--reason", "compact", "--manual"],
                                   cwd=here, capture_output=True, timeout=300,
                                   creationflags=0x08000000,
                                   encoding="utf-8", errors="replace")
                out = ((r.stdout or "") + (r.stderr or "")).strip()
                ok = r.returncode == 0 and "OK " in out
                return bool(ok), (("✔ " if ok else "✘ ") + (out[-500:] or "没有输出"))
            except Exception as e:         # noqa: BLE001
                return False, "跑不动：%s" % str(e)[:200]
        return work

    def _compress_cdp(self):
        """Hermes 的压缩：CDP 打 `/compress`（**它不是 DSH，命令不一样**）。

        2026-11-11 15:50 人纠正：DSH 用 `/compact`，Hermes 用 `/compress`。
        这里原来跟着 DSH 一起打成 `/compact`，等于给 Hermes 发了一条它不认识的话。
        """
        def work():
            import persona_hermes_poke as poke
            cmd = "/compress"
            ok, msg = poke.poke(cmd)
            return bool(ok), ("已把 %s 发给 Hermes：%s" % (cmd, msg)) if ok else ("发送失败：%s" % msg)
        return work

    def _start_gateway(self):
        """微信网关：直接调官方 `hermes gateway start`。

        2026-11-13 换壳：原来这里走社区版的 persona_hermes_keeper（守着
        hermes-agent-cn-runtime 那个进程、读 ops\\hermes-keeper.json）。
        社区版删掉后那条路是死的，现在和登录项拉起的是**同一个**官方网关，
        单实例由 Hermes 自己保证（同一 token 只许一个轮询者）。
        """
        def work():
            import persona_wechat as w
            return w.gateway_ensure(30)
        return work

    def _switch_model(self, model_name):
        """换模型：直接干，**不弹确认框**。

        2026-11-11 16:05 人纠正：「换模型不需要确认，是改权限需要确认」。
        所以这里原来那个 _confirm 已删掉——换模型可逆、不涉权限；
        以后真去动 /permission（权限预设）那类，才照旧先问。
        """
        def work():
            import subprocess
            import sys as _sys
            here = ROOT
            # 壳正在生成时别抢输入框（和压缩/重读共用同一个框）
            try:
                import persona_inject as pi
                idle, waited, imsg = pi.wait_shell_idle(stable=5, timeout=60)
                if not idle:
                    return False, "壳在说话（等了 %.0f 秒：%s），等它说完再切" % (waited, imsg)
            except Exception:              # noqa: BLE001
                pass
            try:
                r = subprocess.run([_sys.executable,
                                    os.path.join(here, "脚本/persona_model.py"),
                                    "--name", model_name, "--force"],
                                   cwd=here, capture_output=True, timeout=120,
                                   creationflags=0x08000000,
                                   encoding="utf-8", errors="replace")
                out = ((r.stdout or "") + (r.stderr or "")).strip()
                ok = r.returncode == 0 and "OK " in out
                return bool(ok), (("✔ " if ok else "✘ ") + (out[-500:] or "没有输出"))
            except Exception as e:         # noqa: BLE001
                return False, "跑不动：%s" % str(e)[:200]
        return ("换模型（%s）" % model_name, work)

    def _switch_effort(self, level):
        """切推理档位（Off/Low/High/Max）：不弹确认（可逆），走 persona_effort.py。

        2026-11-11 17:10 人定：「你加进去，后期可以自动切」——所以这里只是**手工入口**，
        后期自动切档由一个上层判断调用同一个脚本（`persona_effort.py --set X`）即可。
        """
        def work():
            import subprocess
            import sys as _sys
            here = ROOT
            try:
                r = subprocess.run([_sys.executable, os.path.join(here, "脚本/persona_effort.py"),
                                    "--set", level, "--force"],
                                   cwd=here, capture_output=True, timeout=180,
                                   creationflags=0x08000000,
                                   encoding="utf-8", errors="replace")
                out = ((r.stdout or "") + (r.stderr or "")).strip()
                ok = r.returncode == 0 and "OK " in out
                return bool(ok), (("✔ " if ok else "✘ ") + (out[-500:] or "没有输出"))
            except Exception as e:             # noqa: BLE001
                return False, "跑不动：%s" % str(e)[:200]
        return ("切推理档位（%s）" % level, work)

    def _effort_menu(self, name):
        """「推理等级」二级菜单：顶上一行显示现在哪档，下面 Off/Low/High/Max 可选。"""
        items = []
        cur = ""
        try:
            import persona_effort as PE
            cur = (PE.current()[1] or PE.current()[0]) or ""
        except Exception:                      # noqa: BLE001
            pass
        if cur:
            items.append(pystray.MenuItem("现在：%s" % cur, None, enabled=False))
            items.append(pystray.Menu.SEPARATOR)
        # 只给 low/high/max：**off 不上菜单**（2026-11-11 人定：不许把推理等级切到关）
        for lv in ("low", "high", "max"):
            items.append(pystray.MenuItem(lv, functools.partial(
                self._dispatch_worker, name, "effort:" + lv)))
        return pystray.Menu(*items)

    def _model_menu(self, name):
        """「换模型」二级菜单：顶上一行是**现在用的是哪个**，下面才可选。

        当前模型从会话缓存里读（`modelSelection.val.lastUsed`），离线可见，
        不用看屏幕。免费档（硅基流动）不进菜单：要另配 key，历史上欠费直接 402。
        """
        items = []
        cur = ""
        names = ["DeepSeek-V4.1-Flash", "DeepSeek-V4-Pro"]
        try:
            import persona_model as PM
            names = PM.model_names("账号") or names
            c = PM.current_model() or {}
            cur = c.get("model") or ""
        except Exception:                  # noqa: BLE001
            pass
        if cur:
            items.append(pystray.MenuItem("现在：%s" % cur, None, enabled=False))
            items.append(pystray.Menu.SEPARATOR)
        items.extend([
            pystray.MenuItem(n, functools.partial(self._dispatch_worker, name,
                                                  "model:" + n))
            for n in names])
        return pystray.Menu(*items)

    def _do(self, name, action):
        """按配置执行一个动作；返回要开进度窗的 worker，或 None（已就地处理）。"""
        cfg = (self.spec.get("clients") or {}).get(name) or {}
        method = (cfg.get("actions") or {}).get(action) or ""
        label = cfg.get("label") or name
        T = self.T
        if action == "status":
            self._show(name)
            return None
        if action.startswith("model:"):
            return self._switch_model(action.split(":", 1)[1])
        if action.startswith("effort:"):
            return self._switch_effort(action.split(":", 1)[1])
        if action == "compact":
            if method == "cdp":
                return ("压缩 %s 上下文" % label, self._compress_cdp())
            T.compress_once()              # DSH：确认框 / busy 图标都在它里面
            return None
        if action == "reload":
            # 2026-11-16：改走**注入通道**（ops\<机器>\<通道>\inject-request.jsonl → persona-inbox 插件），
            # 不再用键盘往输入框打命令（那条路 11-15 已弃用，且会抢焦点）。
            return ("重读设定（%s）" % label, self._reload_via_inbox(label))
        if action == "reload-force":
            return ("补发设定重载（手动，不看闸门）", self._reload_forced())
        if action == "gateway" or method in ("keeper", "hermes-cli"):
            return (self.spec.get("gateway_label") or "微信网关", self._start_gateway())
        if action == "restart":
            if not T._confirm("重启 %s？\n\n会关掉它再拉起（正在跑的一轮会断，微信网关会跟着断一下再重连）。"
                              % label, "SysPersona · 重启 %s" % label):
                return None
            parts = method.split(":")
            args = parts[1:] if parts and parts[0] == "watchdog" else ["enact", "restart"]
            return ("重启 %s" % label, lambda: self._run_watchdog_sync(*args))
        return None

    # ── 菜单：一级只放"给谁操作"，每个客户端自己一个二级菜单 ──
    def _client_menu(self, name, cfg):
        """某个客户端的二级菜单：状态 / 压缩 / 重读 / 重启…（按它配置里有的动作生成）。"""
        acts = cfg.get("actions") or {}
        sub = []
        if acts.get("status"):
            sub.append(pystray.MenuItem("看状态",
                                        functools.partial(self._dispatch_worker, name, "status")))
        if acts.get("compact"):
            sub.append(pystray.MenuItem("立即压缩上下文",
                                        functools.partial(self._dispatch_worker, name, "compact")))
            if (cfg.get("wm") or name).lower() == "dsh":
                sub.append(pystray.MenuItem("压缩时机", self._compact_when_menu()))
        if acts.get("reload"):
            sub.append(pystray.MenuItem("重读设定（让它读 MEMORY）",
                                        functools.partial(self._dispatch_worker, name, "reload")))
        # 2026-11-11 16:06 修：这里原来比的是 `name == "dsh"`，但配置里的键是 **"DSH"**，
        # 大小写对不上 → 这两项在真实菜单里根本没出现过（人报「没有切换模型的选项」才发现）。
        # 现在用配置里的 `wm`（小写）判客户端，别再拿显示名比。
        wm = (cfg.get("wm") or name).lower()
        # C 方案（2026-11-11 人定）：自动连续 3 次被拦下来的那次重载，由人点这里补发。
        # 名字带「补发」两个字，好和上面那条"重读设定"分开：这条专治欠账。
        if acts.get("reload") and wm == "dsh":
            sub.append(pystray.MenuItem("补发被拦的设定重载（自动连续 3 次没发出去时用）",
                                        functools.partial(self._dispatch_worker, name, "reload-force")))
        # 换模型（2026-11-11 加）：DSH 才有 /model；点了直接换，不弹确认（人 16:05 纠正）
        if acts.get("model") and wm == "dsh":
            sub.append(pystray.MenuItem("换模型", self._model_menu(name)))
        # 推理档位（2026-11-11 加）：切档可逆、不涉权限，所以不弹确认
        if acts.get("effort") and wm == "dsh":
            sub.append(pystray.MenuItem("推理等级", self._effort_menu(name)))
        if acts.get("kernel") or acts.get("restart") or acts.get("gateway"):
            sub.append(pystray.Menu.SEPARATOR)
        if acts.get("kernel"):
            sub.append(pystray.MenuItem("重启内核（最快，不动窗口）",
                                        functools.partial(self._dispatch_worker, name, "kernel")))
        if acts.get("restart"):
            sub.append(pystray.MenuItem("重启窗口",
                                        functools.partial(self._dispatch_worker, name, "restart")))
        if acts.get("gateway"):
            sub.append(pystray.MenuItem("%s：重拉" % (self.spec.get("gateway_label") or "微信网关"),
                                        functools.partial(self._dispatch_worker, name, "gateway")))
        # 会话/日志是"这个客户端自己的"，所以也放它自己的二级菜单里（人 2026-11-11 要求）
        opens = cfg.get("open") or {}
        if opens.get("sessions") is not None:
            sub.append(pystray.Menu.SEPARATOR)
            sub.append(pystray.MenuItem("打开这个客户的会话存档",
                                        functools.partial(self._open_client_dir, name, "sessions")))
        if opens.get("log"):
            sub.append(pystray.MenuItem("打开这个客户的日志",
                                        functools.partial(self._open_client_dir, name, "log")))
        if (cfg.get("detect") or {}):
            sub.append(pystray.Menu.SEPARATOR)
            sub.append(pystray.MenuItem("打开这个客户的配置文件",
                                        functools.partial(self._open_client_cfg, name)))
            sub.append(pystray.MenuItem("看它认路的结果（exe/配置/工作目录）",
                                        functools.partial(self._show_client_paths, name)))
        return pystray.Menu(*sub)

    def _client_probe(self, name):
        """客户端认路：exe / 配置 / 工作目录 / 是否在跑（2026-11-11 加）。"""
        try:
            import persona_client_probe as P
            return P.detect(name)
        except Exception as e:             # noqa: BLE001
            _p("客户端探测失败：%s" % e)
            return {}

    def _open_client_cfg(self, name, icon=None, item=None):
        st = self._client_probe(name)
        p = (st.get("config") or "")
        if not p or not os.path.exists(p):
            self.T._dialog("%s 没认出配置文件（detect.config 没配或路径变了）" % name,
                           "SysPersona · %s" % name)
            return
        try:
            subprocess.Popen(["explorer.exe", "/select,", p], creationflags=0x08000000)
        except Exception:                  # noqa: BLE001
            os.startfile(os.path.dirname(p))

    def _show_client_paths(self, name, icon=None, item=None):
        st = self._client_probe(name)
        if not st:
            return
        body = ("exe：%s\n版本：%s\n在跑：%s（pid %s）\n\n配置：%s\n工作目录：%s\nauto_launch：%s"
                % (st.get("exe") or "没认出", st.get("version") or "?",
                   "是" if st.get("running") else "否",
                   ",".join(map(str, (st.get("pids") or [])[:3])) or "-",
                   st.get("config") or "没认出", st.get("workdir") or "没认出",
                   st.get("auto_launch")))
        self.T._dialog(body, "SysPersona · %s 认路" % name)

    def _client_dir(self, name, kind):
        """某个客户端要打开的目录：相对路径按仓库根解析；空值按客户端类型推。"""
        cfg = (self.spec.get("clients") or {}).get(name) or {}
        raw = ((cfg.get("open") or {}).get(kind) or "").strip()
        if raw:
            return raw if os.path.isabs(raw) else os.path.join(ROOT, raw)
        if kind == "sessions" and (cfg.get("wm") or "") == "hermes":
            try:
                import persona_locate as _loc
                home = _loc.hermes_home()
            except Exception:              # noqa: BLE001
                home = ""
            if home:
                p = os.path.join(home, "sessions")
                if os.path.isdir(p):
                    return p
        return os.path.join(ROOT, "sessions")

    def _open_client_dir(self, name, kind, icon=None, item=None):
        cfg = (self.spec.get("clients") or {}).get(name) or {}
        label = cfg.get("label") or name
        p = self._client_dir(name, kind)
        if not os.path.isdir(p):
            self.T._dialog("%s 的%s目录还没生成：\n%s" % (
                label, "会话存档" if kind == "sessions" else "日志", p), "SysPersona · %s" % label)
            return
        try:
            os.startfile(p)
        except Exception as e:             # noqa: BLE001
            self.T._dialog("打不开：%s" % e, "SysPersona · %s" % label)

    def _menu_txt(self, menu, ind=0):
        r"""把当前菜单摊平成文本（查菜单/报问题用，不参与点击）。

        2026-11-15：格式改成和 `--dump-menu` 同一份——分隔线画出来、有子菜单的挂 `▸`、
        勾选中的挂 `✓`。这样 `ops\<机器>\tray-menu.txt` 和命令行打印的能直接对得上。
        """
        out = []
        pad = "    " * ind
        for it in menu:
            if it is pystray.Menu.SEPARATOR:
                out.append(pad + "─" * 21)
                continue
            text = str(getattr(it, "text", "") or "?")
            try:
                if getattr(it, "checked", None) is True:
                    text += "  ✓"
            except Exception:              # noqa: BLE001
                pass
            sub = getattr(it, "submenu", None)
            if sub is not None:
                text += " ▸"
            out.append(pad + text)
            if sub is not None:
                out.append(self._menu_txt(sub, ind + 1))
        return "\n".join(out)

    def _dump_menu(self, menu):
        """菜单一变就落一份 `ops\\tray-menu.txt`。

        2026-11-11 16:05 人报「没有切换模型的选项」：光看屏幕分不清
        「代码没加进去」和「他点的是旧/幽灵图标」。落这份文件，就能直接对证。
        """
        try:
            if READONLY:                   # 只读：不往库里落任何文件（含这份对证用的菜单快照）
                return
            txt = self._menu_txt(menu)
            if txt == getattr(self, "_menu_txt_last", None):
                return
            self._menu_txt_last = txt
            with io.open(os.path.join(_RT, "tray-menu.txt"), "w",
                         encoding="utf-8", newline="\n") as f:
                f.write(txt + "\n")
        except Exception:                  # noqa: BLE001
            pass

    def _launch_client(self, name, icon=None, item=None):
        """拉起一个**没在跑**的客户端（2026-11-11 人：「后台程序还少了个拉起客户端的选项」）。

        走它的 `detect.exe` 第一个存在的路径；工作目录用它 `detect.workdir`。
        拉起属运维动作，不需要人点头（改它的配置/提权才要）。
        """
        import subprocess
        st = self._client_probe(name)
        exe = (st.get("exe") or "")
        cfg = (self.spec.get("clients") or {}).get(name) or {}
        label = cfg.get("label") or name
        if not exe or not os.path.exists(exe):
            self.T._dialog("%s 没认出可执行文件（detect.exe 没配或路径变了）。\n\n"
                           "先点「看它认路的结果」确认。" % label, "SysPersona · 拉起 %s" % label)
            return
        workdir = (st.get("workdir") or "")
        try:
            subprocess.Popen([exe], cwd=(workdir if workdir and os.path.isdir(workdir) else None),
                             creationflags=0x00000008)      # DETACHED_PROCESS：别跟着托盘一起死
        except Exception as e:             # noqa: BLE001
            self.T._dialog("拉起失败：%s" % str(e)[:200], "SysPersona · 拉起 %s" % label)
            return
        self.T._dialog("已发起拉起：%s\n\n路径：%s\n等它起来后，菜单里才会出现它的操作项。"
                       % (label, exe), "SysPersona · 拉起 %s" % label)

    # ── 叫醒客户端（2026-11-12 人定）───────────────────────────────────
    # 人原话：「窗口被毁的拉起，需要托盘有一个显示的开关，比如我手动退出的，你不能无限强拉，
    #   而且有时候崩溃了，还有报错弹窗，需要托盘程序去掉报错，或者强杀进程，在重拉」。
    def _wake_allowed(self):
        try:
            import persona_inject as _pi
            return _pi.wake_allowed()
        except Exception:                  # noqa: BLE001
            return True

    def _wake_toggle(self, icon=None, item=None):
        """开关：允许/不许我自动叫醒客户端。手动退出了就关掉它。"""
        try:
            import persona_inject as _pi
            cur = _pi.wake_allowed()
            ok = _pi.set_wake_allowed(not cur)
            self.T._dialog("自动叫醒客户端：%s%s"
                           % ("已关掉（你手动退出时就这样，我不强拉）" if cur else "已打开（压缩/切档会自己把窗口叫回来）",
                              "" if ok else "\n写配置失败，下次再试"),
                           "SysPersona · 叫醒客户端")
        except Exception as e:             # noqa: BLE001
            self.T._dialog("改不了：%s" % str(e)[:200], "SysPersona · 叫醒客户端")
        self.icon.menu = self._menu()

    def _wake_close_dialogs(self, icon=None, item=None):
        """关掉客户端的报错/崩溃弹窗（先 WM_CLOSE，再点「确定」）。"""
        try:
            import persona_inject as _pi
            n0, left = _pi.close_error_dialogs()
            if not n0:
                self.T._dialog("没看到客户端的报错窗", "SysPersona · 报错窗")
                return
            self.T._dialog("报错窗：处理 %d 个，还剩 %d 个%s"
                           % (n0, left, "" if not left else "\n关不掉就点「强杀并重拉」"),
                           "SysPersona · 报错窗")
        except Exception as e:             # noqa: BLE001
            self.T._dialog("出错：%s" % str(e)[:200], "SysPersona · 报错窗")

    def _wake_force(self, icon=None, item=None):
        """强杀客户端 → 重新拉起（报错卡死 / 窗口毁了时用）。"""
        try:
            import persona_inject as _pi
            kok, kmsg = _pi.kill_client()
            rok, rmsg = _pi.relaunch_client()
            self.T._dialog("强杀：%s\n重拉：%s" % (kmsg, rmsg), "SysPersona · 强杀并重拉")
        except Exception as e:             # noqa: BLE001
            self.T._dialog("出错：%s" % str(e)[:200], "SysPersona · 强杀并重拉")

    def _wake_menu(self):
        return pystray.Menu(
            pystray.MenuItem("允许自动叫醒（关掉=我手动退出了）", self._wake_toggle,
                             checked=lambda item: self._wake_allowed()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("关掉报错窗", self._wake_close_dialogs),
            pystray.MenuItem("强杀并重拉（窗口会关掉，会话不丢）", self._wake_force))

    def _launch_menu(self):
        """「拉起客户端」二级菜单：列所有配置里的客户端（不管在不在跑）。"""
        items = []
        for name, cfg in (self.spec.get("clients") or {}).items():
            if cfg.get("on") is False:
                continue
            label = cfg.get("label") or name
            items.append(pystray.MenuItem("拉起 / 切到 %s（%s）" % (label, "在跑" if self._running(name) else "没在跑"),
                                          functools.partial(self._launch_client, name)))
        return pystray.Menu(*items) if items else None

    def _power(self, args, title):
        """跑 persona_power.py（关机 / 取消），把结果弹给人看。

        2026-11-11 人：「关机也可以添加到后台程序中」。关机有 60 秒延迟，
        响铃期间再点「取消关机」就撤销（`shutdown /a`）。
        """
        import subprocess
        import sys as _sys
        import time as _t
        import uuid as _uuid
        import json as _json
        here = ROOT
        # 人点这一下 = 下单：写一张一次性单子（动作 + 时间 + nonce），关机脚本核对后才动手。
        order = os.path.join(here, "ops", "power-order.json")
        act = "shutdown"
        for i, a in enumerate(args):
            if a in ("--shutdown", "--restart", "--sleep", "--logoff"):
                act = a.lstrip("-")
                break
        try:
            os.makedirs(os.path.dirname(order), exist_ok=True)
            with open(order, "w", encoding="utf-8") as f:
                _json.dump({"action": act, "ts": _t.time(), "nonce": _uuid.uuid4().hex},
                           f, ensure_ascii=False)
            args = args + ["--order", order]
        except Exception:                  # noqa: BLE001
            pass
        try:
            r = subprocess.run([_sys.executable, os.path.join(here, "脚本/persona_power.py")] + args,
                               cwd=here, capture_output=True, timeout=90,
                               creationflags=0x08000000, encoding="utf-8", errors="replace")
            out = ((r.stdout or "") + (r.stderr or "")).strip()
        except Exception as e:             # noqa: BLE001
            out = "跑不动：%s" % str(e)[:200]
        self.T._dialog(out[-800:] or "没有输出", title)

    def _power_shutdown(self, icon=None, item=None):
        delay = 60
        self._power(["--shutdown", "--delay", str(delay), "--why", "托盘点"],
                    "SysPersona · 关机")
        # 排完再开一个**倒计时窗**：显示还剩多少秒 + 「取消关机」按钮。
        # 起因：人点了关机、把弹出的结果框关掉就以为取消了，60 秒后真关机。
        try:
            import subprocess
            import sys as _sys
            here = ROOT
            subprocess.Popen([_sys.executable, os.path.join(here, "脚本/persona_power.py"),
                              "--countdown", "--delay", str(delay)],
                             cwd=here, creationflags=0x08000000)
        except Exception:                  # noqa: BLE001
            pass

    def _power_cancel(self, icon=None, item=None):
        self._power(["--cancel"], "SysPersona · 取消关机")

    def _run_script_bg(self, title, args, notify_ok=True):
        """后台跑一个 python 脚本，完了把结果尾巴播出来（不卡菜单）。"""
        def work():
            import subprocess
            try:
                r = subprocess.run([sys.executable] + args, cwd=ROOT, capture_output=True,
                                   timeout=600,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                out = ((r.stdout or b"") + (r.stderr or b"")).decode("utf-8", "replace")
                tail = "\n".join([ln for ln in out.splitlines() if ln.strip()][-6:])
                _p("[%s] rc=%d\n%s" % (title, r.returncode, tail))
                self._refresh_menu()          # 网络/成员/令牌都可能变了，重画一遍
                if notify_ok and self.icon is not None:
                    msg = tail[-200:] if tail else ("rc=%d" % r.returncode)
                    self.icon.notify(msg, title)
            except Exception as e:         # noqa: BLE001
                _p("[%s] 失败：%s" % (title, e))
                try:
                    if self.icon is not None:
                        self.icon.notify(str(e)[:200], title + " 失败")
                except Exception:          # noqa: BLE001
                    pass
        threading.Thread(target=work, daemon=True).start()

    # ── 网络（v5.8 记忆网络）：创建 / 发现 / 密码加入 / 成员 ─────────────
    def _balance_text(self):
        """托盘上那行余额：DeepSeek 优先（人最关心这个），硅基流动有就顺带写上。"""
        import io as _io, json as _json, os as _os, time as _t
        parts = []
        try:
            import persona_ops as _po
            base = _po.dir()
        except Exception:                      # noqa: BLE001
            return "余额：读不到运行目录"
        ds = _os.path.join(base, "ds-balance.json")
        try:
            d = _json.load(_io.open(ds, encoding="utf-8"))
            if d.get("available") is not False and d.get("balance") is not None:
                parts.append("DeepSeek ¥%.2f" % float(d["balance"]))
            else:
                parts.append("DeepSeek 查不到")
        except Exception:                      # noqa: BLE001
            parts.append("DeepSeek 未查")
        sf = _os.path.join(base, "sf-balance.json")
        try:
            s = _json.load(_io.open(sf, encoding="utf-8"))
            v = s.get("balance")
            if v is not None:
                parts.append("硅基流动 ¥%.2f" % float(v))
        except Exception:                      # noqa: BLE001
            pass
        return "余额：" + " · ".join(parts) + "（点我刷新）"

    def _balance_refresh(self, icon=None, item=None):
        """点余额 → 后台跑一次 dsbalance；跑完菜单自己会重画。"""
        self._run_script_bg("查余额", ["脚本/persona_daemon.py", "--once", "dsbalance"])

    def _refresh_menu(self):
        """让托盘菜单按最新状态重画（pystray 默认会缓存，退出网络后不重画就还显示旧网）。"""
        try:
            if self.icon is not None:
                self.icon.menu = self._menu()
                self.icon.update_menu()
        except Exception as e:                 # noqa: BLE001
            _p("[refresh] 菜单没刷新：%s" % e)

    def _net_link(self):
        import persona_node_link as NL
        return NL

    def _net_state(self):
        """当前网络身份：名字、角色、成员数（读不到就空）。"""
        try:
            import persona_join as J
            return J.net_load() or {}
        except Exception:                      # noqa: BLE001
            return {}

    def _ask(self, title, prompt, show=""):
        r"""要一段文字输入：**在独立进程里弹 tkinter 对话框**。

        2026-11-14 人报：「创建网络的窗口，什么都不能操作，不能关闭 不能输入」——
        原因是在 pystray 的菜单回调线程里直接开会话框：tkinter 要有自己的主循环，
        挂在托盘的线程上就变成一个僵尸窗口（不响应键盘、点 X 也不关）。
        所以改成另起一个 python 进程（自己一套 tkinter），主进程只等它的 stdout。
        """
        import subprocess as _sp
        code = chr(10).join([
            "import sys",
            "import tkinter as tk",
            "from tkinter import simpledialog",
            "title, prompt, show = sys.argv[1], sys.argv[2], sys.argv[3]",
            "root = tk.Tk()",
            "root.withdraw()",
            "try:",
            "    root.attributes('-topmost', True)",
            "except Exception:",
            "    pass",
            "root.update()",
            "v = simpledialog.askstring(title, prompt, show=(show or None), parent=root)",
            "sys.stdout.write('@@CANCEL@@' if v is None else v)",
            "try:",
            "    root.destroy()",
            "except Exception:",
            "    pass",
        ])
        try:
            r = _sp.run([sys.executable, "-c", code, title, prompt, show],
                        capture_output=True, timeout=600)
            out = ((r.stdout or b"").decode("utf-8", "replace")).strip()
            return None if out == "@@CANCEL@@" else out
        except Exception as e:                 # noqa: BLE001
            _p("[ask] 输入框失败：%s" % e)
            return ""

    def _net_create(self, icon=None, item=None):
        """创建网络：设网络名 + 密码，本机当节点（人：「先创建网络，设名和密码」）。"""
        name = self._ask("创建网络", "给这个网络起个名（例：家里的网）：")
        if not name:
            return
        pw = self._ask("创建网络", "设一个密码（别的机器加入时要输）：", show="*")
        if not pw:
            if not self.T._confirm("不给密码的话，同网段谁都能连。继续吗？", "创建网络"):
                return
        # 人 2026-11-14：「创建网络之前，应该询问是否加密」——节点明文，全网就明文
        enc = self.T._confirm(
            "这个网络要不要加密？" + chr(10) + chr(10)
            + "要：写入库里的数据是密文，别的设备/备份/网盘拿到也读不了。" + chr(10)
            + "不要：全明文，谁拿到文件谁就能读。" + chr(10) + chr(10)
            + "选「是」= 加密网；选「否」= 明文网。", "创建网络 · 加密")
        args = ["脚本/persona_node_link.py", "create", name, pw,
                "1" if enc else "0"]
        self._run_script_bg("创建网络「%s」" % name, args)

    def _net_discover(self, icon=None, item=None):
        """搜寻网络：听节点的 UDP 广播（不是扫 IP）。"""
        def work():
            try:
                NL = self._net_link()
                rows = [r for r in NL.discover() if r.get("url") != NL.my_url()]
                self._net_found = rows
                if self.icon is not None:
                    txt = chr(10).join("%s  %s" % (r.get("name") or "(无名网)", r["node"])
                                       for r in rows) or "没搜到网络"
                    self.icon.notify(txt, "发现网络")
                self._rebuild_menu()
            except Exception as e:             # noqa: BLE001
                try:
                    if self.icon is not None:
                        self.icon.notify(str(e)[:200], "发现网络 失败")
                except Exception:              # noqa: BLE001
                    pass
        threading.Thread(target=work, daemon=True).start()

    def _net_join(self, name, url, icon=None, item=None):
        pw = self._ask("加入网络", "输入「%s」的密码：" % name, show="*")
        if pw is None:
            return
        self._run_script_bg("加入网络「%s」" % name,
                            ["脚本/persona_node_link.py", "join", url, pw])

    def _net_join_code(self, icon=None, item=None):
        code = self._ask("用口令加入", "把对端那行 SPLINK1:… 整个粘进来：")
        if code.strip():
            self._run_script_bg("用口令加入", ["脚本/persona_node_link.py", "join", code.strip()])

    def _net_run(self, peer, cmd, icon=None, item=None):
        extra = ["action=status"] if cmd in ("daemon", "tray") else []
        self._run_script_bg("%s → %s" % (peer, cmd),
                            ["脚本/persona_node_link.py", "run", peer, cmd] + extra)

    # ── 管理网络：成员 / 断开（吊销+轮换令牌+封存加密）/ 退出网络 ──────────
    def _net_kick(self, node, icon=None, item=None):
        msg = chr(10).join(["断开 %s？" % node, "",
                            "会：删它的对端登记 → 轮换本机令牌（它的旧令牌立刻失效）",
                            " → 把库里的明文重新封成密文。",
                            "它想再连，得重新加入一次。"])
        if not self.T._confirm(msg, "管理网络"):
            return
        self._run_script_bg("断开 %s" % node, ["脚本/persona_node_link.py", "kick", node])

    def _net_rotate(self, icon=None, item=None):
        msg = chr(10).join(["轮换本机令牌？", "",
                            "所有设备都要重新加入一次（旧的令牌全部失效）。"])
        if not self.T._confirm(msg, "管理网络"):
            return
        self._run_script_bg("轮换令牌", ["脚本/persona_node_link.py", "kick", ""])

    def _net_seal(self, icon=None, item=None):
        self._run_script_bg("加密封存", ["脚本/persona_node_link.py", "seal"])

    def _net_leave(self, icon=None, item=None):
        msg = chr(10).join(["本机退出网络？", "",
                            "会清掉本机的网络身份和对端登记（不动别的机器）。"])
        if not self.T._confirm(msg, "管理网络"):
            return
        self._run_script_bg("退出网络", ["脚本/persona_node_link.py", "leave"])

    def _net_set_node(self, peer="", icon=None, item=None):
        msg = chr(10).join(["让 %s 当节点？" % (peer or "本机"), "",
                            "它会开始广播、接受新设备加入；本机改当成员。",
                            "网络名和密码一起搬过去，成员不用重新加入。"])
        if not self.T._confirm(msg, "管理网络"):
            return
        self._run_script_bg("设为节点：%s" % (peer or "本机"),
                            ["脚本/persona_node_link.py", "node"] + ([peer] if peer else []))

    def _net_cleanup(self, icon=None, item=None):
        """迁移后清理旧副本：删 / 留 / 留最近 N 天（人定的三选一）。"""
        import subprocess as _sp
        days = self._ask("清理旧副本", "保留最近多少天的会话？（留空 = 全部清理）")
        if days is None:
            return
        args = ["脚本/persona_node_link.py", "cleanup"]
        if str(days).strip():
            args.append("--days=" + str(days).strip())
        try:
            r = _sp.run([sys.executable] + args, cwd=ROOT, capture_output=True, timeout=600)
            out = ((r.stdout or b"") + (r.stderr or b"")).decode("utf-8", "replace").strip()
        except Exception as e:                 # noqa: BLE001
            out = "预演失败：%s" % e
        if not self.T._confirm(out + chr(10) + chr(10) + "确认搬走这些本机副本？（先进 清理/待删，7 天后才真删）",
                               "清理旧副本"):
            return
        self._run_script_bg("清理旧副本", args + ["--yes"])

    def _net_admin_menu(self):
        items = []
        try:
            NL = self._net_link()
            rows = NL.members()
        except Exception as e:                 # noqa: BLE001
            return pystray.Menu(pystray.MenuItem("读不到成员：%s" % str(e)[:40], None, enabled=False))
        role = ""
        try:
            import persona_join as _J
            net = _J.net_load()
            role = str(net.get("role") or "").lower()
            if role == "member":
                items.append(pystray.MenuItem(
                    "本机是成员：令牌/成员管理由节点做（索引、归档也在节点）", None, enabled=False))
                items.append(pystray.Menu.SEPARATOR)
        except Exception:                      # noqa: BLE001
            pass
        if rows:
            for r in rows:
                items.append(pystray.MenuItem(
                    "%s（%s）→ 断开" % (r["node"], r["online"] or "?"),
                    functools.partial(self._net_kick, r["node"])))
        else:
            items.append(pystray.MenuItem("还没有设备加入", None, enabled=False))
        items.append(pystray.Menu.SEPARATOR)
        items.append(pystray.MenuItem("重新看成员（刷新）", lambda *a: self._refresh_menu()))
        items.append(pystray.MenuItem("本机当节点（开始广播）",
                                      functools.partial(self._net_set_node, "")))
        for r in rows:
            items.append(pystray.MenuItem("%s → 设为节点" % r["node"],
                                          functools.partial(self._net_set_node, r["node"])))
        items.append(pystray.Menu.SEPARATOR)
        items.append(pystray.MenuItem("轮换本机令牌（所有设备要重新加入）", self._net_rotate))
        items.append(pystray.MenuItem("立即加密封存（把库里明文封成密文）", self._net_seal))
        items.append(pystray.MenuItem("清理旧副本…（保留最近 N 天）", self._net_cleanup))
        items.append(pystray.MenuItem("本机退出网络", self._net_leave))
        return pystray.Menu(*items)

    def _net_menu(self):
        items = []
        try:
            NL = self._net_link()
            import persona_join as J
            me = NL.my_url()
            peers = NL.peers()
            net = J.net_load()
        except Exception as e:                 # noqa: BLE001
            return pystray.Menu(pystray.MenuItem("网络用不了：%s" % str(e)[:60], None, enabled=False))
        if net.get("name"):
            items.append(pystray.MenuItem(
                "网络：%s（%s%s）" % (net["name"],
                                     "节点" if net.get("role") == "host" else "成员",
                                     " · 有密码" if net.get("pw_hash") else " · 没密码"),
                None, enabled=False))
        else:
            items.append(pystray.MenuItem("还没有网络（先「创建网络」或用「发现网络」加入）", None,
                                          enabled=False))
        items.append(pystray.MenuItem("本机：%s · 成员 %d" % (me, len(peers) + (1 if net.get("name") else 0)),
                                      None, enabled=False))
        items.append(pystray.Menu.SEPARATOR)
        items.append(pystray.MenuItem("创建网络…（设网络名 + 密码，本机当节点）", self._net_create))
        items.append(pystray.MenuItem("发现网络（听节点广播，约 5 秒）", self._net_discover))
        found = [r for r in getattr(self, "_net_found", []) if r.get("url") != me]
        if found:
            items.append(pystray.MenuItem("搜到的网络 →", pystray.Menu(*[
                pystray.MenuItem("%s  %s%s" % (r.get("name") or "(无名网)", r["node"],
                                               "  🔒" if r.get("pw") else ""),
                                 functools.partial(self._net_join, r.get("name") or r["node"], r["url"]))
                for r in found])))
        if peers:
            sub = []
            for name in sorted(peers):
                sub.append(pystray.MenuItem("%s → 诊断卡" % name,
                                            functools.partial(self._net_run, name, "diag")))
                sub.append(pystray.MenuItem("%s → 守护状态" % name,
                                            functools.partial(self._net_run, name, "daemon")))
                sub.append(pystray.MenuItem("%s → 部署 / 自检" % name,
                                            functools.partial(self._net_run, name, "deploy")))
            items.append(pystray.MenuItem("网络成员（%d）" % len(peers), pystray.Menu(*sub)))
        items.append(pystray.Menu.SEPARATOR)
        items.append(pystray.MenuItem("管理网络…（成员 · 断开 · 轮换 · 加密）", self._net_admin_menu()))
        items.append(pystray.MenuItem("用口令加入…（粘 SPLINK1:…）", self._net_join_code))
        items.append(pystray.MenuItem("网络状态（写到日志）",
                                      lambda i=None, it=None: self._run_script_bg(
                                          "网络状态", ["脚本/persona_node_link.py", "net"])))
        return pystray.Menu(*items)

    def _deploy_once(self, icon=None, item=None):
        r"""一个入口：部署 + 自检。

        2026-11-14 人：「非要维护两个干嘛？后期可以随意切换」——
        所以托盘里只留这一项：认机器 → 铺客户端配置 → 落盘口径 → 自检，
        全部由 `persona_deploy_once.py` 一次做完（它本身就是幂等的，重复点没问题）。
        """
        self._run_script_bg("部署 / 自检",
                            [os.path.join(ROOT, "脚本", "persona_deploy_once.py")])

    def _balance_on(self):
        r"""「余额」那一行显不显示：人 2026-11-14 定 **默认关**。

        他问的原话：「这个是从变量里面读，然后查询吗？每个托盘都查，会不会风控？
        要么删掉，要么默认关闭，Hermes 里面可以看，必须查询就只能服务节点查询」。
        事实：托盘自己不查接口，只读 `ops\<机器id>\ds-balance.json`（守护任务
        dsbalance 每 1800 秒查一次官方接口写的）；**点那一行**才真查一次
        （`persona_daemon.py --once dsbalance`）。所以「每台都查」= 每台守护各查一次
        + 人手点几次。默认关，要开会：`ops\<机器id>\persona.conf.json` 里
        `"tray_balance": 1`。
        """
        try:
            import persona_ops as _po
            with io.open(os.path.join(_po.dir(), "persona.conf.json"), encoding="utf-8") as f:
                return int(json.load(f).get("tray_balance", 0)) == 1
        except Exception:                  # noqa: BLE001
            return False

    def _model_and_effort_menu(self):
        """「切换模型 ▸」= 模型 + 思考等级 合成一组（人 2026-11-14：「思考等级改到换模型里面」）。

        2026-11-15 人定：**这一组不再进托盘**——切模型 / 思考等级属于"界面里点得了的事"。
        留着不删是因为 `_model_menu` / `_effort_menu` / `persona_model` / `persona_effort`
        都还是可用的能力入口（命令行照旧），哪天要接回来直接挂上就行；只是别往 `_menu()` 里挂。
        原来这里还挂着「开新会话（唤醒一次）」——同样按人 2026-11-15 的话从菜单拿掉了，
        `_new_session_dsh()` 本身保留（CLI 路径 `persona_client_switch --to dsh` 还在）。
        """
        mm = self._model_menu("DSH")
        em = self._effort_menu("DSH")
        items = list(getattr(mm, "items", []) or [])
        items += [
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("思考等级", em),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("开新会话（唤醒一次）", self._new_session_dsh),
        ]
        return pystray.Menu(*items)

    # ── 菜单：2026-11-15 人定稿的最终结构 ──────────────────────────────
    def _menu(self):
        r"""一级菜单（2026-11-15 人定稿，就照这个顺序，别再随手加）：

            运行状态 → 异常：N 项 →│ 压缩上下文 → 重读设定 → 空闲唤醒 → 立即归档会话
            →│ 版本 ▸ → 记录 ▸ →│ 节点网络 ▸ → 托盘设置 ▸ → 退出

        三条硬规矩：
          ① 托盘只放「界面里点不了的事」——切模型 / 思考等级 / 开新会话 / 发布全局
             **都不在这**（能力留在命令行，不删脚本）；
          ② 结构固定：跑没跑只影响状态文字和 `enabled=`，**不许**用"在跑"来藏项；
          ③ 所有"往会话发文字"走 `persona_inbox_client.send()`，不再键盘注入。
        """
        alive = self._dsh_alive()
        sc = self._selfcheck()
        n_arch = self._pending_archive()
        n_fail = len(sc["fails"])
        # 2026-11-15 人定：**有待归档也算异常**——归档没做完就不算正常。
        try:
            n_arch_now = self._pending_archive()
        except Exception:                       # noqa: BLE001
            n_arch_now = None
        n_bad = n_fail + (1 if (n_arch_now or 0) > 0 else 0)
        items = [
            # 默认项：左键一下弹状态框，第一行是水位（人 2026-11-15 特意点明的格式）。
            # 2026-11-15 再改：异常并进这一项——标题直接显示「状态（正常/异常 N）」，
            # 明细在窗口里看，窗口里有「立即刷新」，不再单独占一行「异常：N 项」。
            pystray.MenuItem(
                ("状态（%s）" % ("异常 %d 项" % n_bad if n_bad else "正常"))
                + ("" if alive else "（DSH 没在跑）"),
                self._show_status, default=True),
            pystray.Menu.SEPARATOR,
            # 两项"发文字"：客户端没在跑就灰着（但**仍然显示**，不再藏起来）
            pystray.MenuItem("程序自检", self._selfcheck_menu()),
            pystray.MenuItem("空闲唤醒", self._idle_wake_menu()),
            # 5. 节点网络（人 2026-11-15：归档那项删了，节点网络顶上来当第 5 项）
            pystray.MenuItem("节点网络", self._net_menu_with_nas()),
            pystray.MenuItem("会话显示时长", self._sess_menu()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("查看记录", self._records_menu()),
            pystray.MenuItem("程序版本", self._version_menu()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("托盘设置", self._settings_menu()),
            pystray.Menu.SEPARATOR,
            # 2026-11-14 人定：「退出只退托盘没有任何意义…既然要退出肯定是全退出」
            pystray.MenuItem("退出", self.quit_full),
        ]
        menu = pystray.Menu(*items)
        self._dump_menu(menu)
        return menu

    def _net_menu_with_nas(self):
        """「节点网络 ▸」：人 2026-11-15「现有那套原样保留」+ 原来就挂在它下面的 NAS 项。"""
        _net_items = list(getattr(self._net_menu(), "items", []) or [])
        _net_items += [
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("唤醒 NAS（魔术包）", self._wake_nas),
            pystray.MenuItem("让 NAS 睡（S3）", self._sleep_nas),
        ]
        return pystray.Menu(*_net_items)

    def _version_menu(self):
        """「版本 ▸」：只展示（人 2026-11-15：**不要发布入口**）。

        全局 = `persona_ver.current()`（别的机器读的那份）；本机 = `persona_ver.local()`。
        `follows.rev == 全局 rev` 才算「跟随 ✓」，否则写「✗（等同步）」。
        待发布条数只进日志，不进菜单——发布不在托盘做。
        """
        gver, _grev, lrev, follows = self._ver()
        try:
            import persona_ver as V
            n_pending = len(V.pending())
        except Exception:                  # noqa: BLE001
            n_pending = 0
        if n_pending:
            _p("版本：本机有 %d 条待发布（托盘只显示，不发布）" % n_pending)
        return pystray.Menu(
            pystray.MenuItem("全局 v%s（只读）" % gver, None, enabled=False),
            pystray.MenuItem("本机 rev %d · 跟随 %s" % (lrev, "✓" if follows else "✗（等同步）"),
                             None, enabled=False),
        )

    def _records_menu(self):
        """「记录 ▸」：今日心跳 / 本机台账 / 全局台账（只读）/ 交接账。"""
        n = self._handoff_open()
        return pystray.Menu(
            pystray.MenuItem("今日心跳", self._show_beats),
            pystray.MenuItem("变更台账（本机）", self._open_ledger_local),
            pystray.MenuItem("全局台账（只读）", self._show_ledger_global),
            pystray.MenuItem("交接账（未完成 %s 条）" % ("?" if n is None else n),
                             self._show_handoff),
        )

    def _selfcheck_menu(self):
        """「自检 ▸」：顶层第 2 项（人 2026-11-15：别嵌那么深）。"""
        return pystray.Menu(
            pystray.MenuItem("自检周期", pystray.Menu(*[
                pystray.MenuItem("%d 秒" % s, self._sc_mk("selfcheck_every", s),
                                 checked=(lambda item, v=s:
                                          self._sc_get("selfcheck_every", 120) == v))
                for s in (60, 120, 300)])),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("自动启动客户端", self._sc_toggle_launch,
                             checked=lambda item: self._sc_get("selfcheck_launch", True)),
            pystray.MenuItem("异常后自动唤醒修复", self._sc_toggle_wake,
                             checked=lambda item: self._sc_get("selfcheck_wake", False)),
            pystray.MenuItem("唤醒修复异常周期", pystray.Menu(*[
                pystray.MenuItem("%d 个周期" % c, self._sc_mk("selfcheck_cycles", c),
                                 checked=(lambda item, v=c:
                                          self._sc_get("selfcheck_cycles", 3) == v))
                for c in (2, 3, 5)])),
        )

    def _sess_menu(self):
        """「会话显示时长 ▸」：顶层（放在节点网络后面），不嵌在自检里。"""
        return pystray.Menu(*[
            pystray.MenuItem(("%d 小时" % (m // 60) if m >= 60 else "%d 分钟" % m),
                             self._sc_mk("session_fresh_min", m),
                             checked=(lambda item, v=m: self._sess_minutes() == v))
            for m in (30, 60, 120, 240, 720)])

    def _settings_menu(self):
        """「托盘设置 ▸」：只剩常驻设置（开机启动 / 电源）。"""
        return pystray.Menu(
            pystray.MenuItem("开机自启", self._autostart_toggle,
                             checked=lambda item: autostart_on()),
            # 人 2026-11-15：跟开机自启放一起——都是"这台机器常驻"的设置。
            # 人 2026-11-15：叫「永不睡眠」就行（这台是台式机，没有电池那一说）；
            # 空闲唤醒开着时**不再是灰的**——点了给一句提示，比点不动好懂。
            pystray.MenuItem("永不睡眠", self._no_sleep_toggle,
                             checked=lambda item: (self._no_sleep_on()
                                                   or bool(self._hb().get("wake")))),
        )

    def _period_from_selfcheck(self):
        r"""界面刷新间隔 = 自检周期（人 2026-11-15：别放两个"多久动一次"）。

        自检 60/120/300 秒，界面就跟着同频；自检一改，这里立刻同步。
        """
        try:
            return float(self._sc_get("selfcheck_every", 120))
        except Exception:                       # noqa: BLE001
            return 120.0

    def _set_period(self, sec):
        """改刷新间隔：只写**本机运行态**那份（不 bump 版本、不动 配置\\ 的共享配置）。"""
        self.period = float(sec)
        self.spec["refresh_seconds"] = int(sec)
        _save_runtime(self.spec)
        _p("刷新间隔 → %d 秒" % int(sec))
        self._rebuild_menu()

    # ── 新菜单里的动作（"发文字"一律走 persona-inbox 请求行）─────────────
    def _inbox(self):
        """插件通道模块 `persona_inbox_client`：写请求行 → persona-inbox → 回执。"""
        here = os.path.dirname(os.path.abspath(__file__))
        if here not in sys.path:
            sys.path.insert(0, here)
        import persona_inbox_client as C
        return C

    def _compact_via_inbox(self, icon=None, item=None):
        r"""「压缩上下文」：写一条 `/compact` 请求给插件，等回执（**不再键盘注入**）。

        DSH 里 `/compact` 是命令，发进会话就会执行；`kind="compact"` 只用来留痕（插件不看）。
        「进程内 0.3 线」= 壳自己的 compaction 到窗口约 0.3 就自己压；
        这一项是人**手动提前压一次**（不点也会到线自己压）。
        """
        def work():
            ok, msg = self._inbox().send("/compact", kind="compact")
            _p("压缩上下文：%s %s" % ("OK" if ok else "FAIL", msg))
            if ok:
                return True, ("已把 /compact 发进会话（%s）\n\n"
                              "壳自己到 0.3 线也会压；这一下是手动提前压。" % msg)
            return False, msg
        self.T.run_with_progress("压缩上下文（/compact → persona-inbox）", work, countdown=3)
        self._rebuild_menu()

    def _reread_via_inbox(self, icon=None, item=None):
        """「重读设定」：把固定那段话写进请求行（同样走 persona-inbox，不碰键盘）。"""
        def work():
            ok, msg = self._inbox().send(REREAD_TEXT, kind="reread")
            _p("重读设定：%s %s" % ("OK" if ok else "FAIL", msg))
            return ok, (msg if ok else (msg + "\n\n要发出去的正文：\n" + REREAD_TEXT))
        self.T.run_with_progress("重读设定（→ persona-inbox）", work, countdown=3)
        self._rebuild_menu()

    def _archive_now(self, icon=None, item=None):
        """「立即归档会话（待 N）」：跑 `persona_archive.py`，跑完把"待 N"重数一遍。"""
        def work():
            import subprocess
            try:
                r = subprocess.run([sys.executable,
                                    os.path.join(ROOT, "脚本", "persona_archive.py")],
                                   cwd=ROOT, capture_output=True, timeout=900,
                                   creationflags=NO_WINDOW,
                                   encoding="utf-8", errors="replace")
                out = ((r.stdout or "") + (r.stderr or "")).strip()
                ok = r.returncode == 0
            except Exception as e:         # noqa: BLE001
                ok, out = False, "跑不动：%s" % str(e)[:200]
            self._arch_cache = None        # 让菜单里的"待 N"重新数
            _p("[归档会话] %s %s" % ("OK" if ok else "FAIL", out[-300:]))
            self._rebuild_menu()
            return bool(ok), (out[-800:] or ("exit %d" % (0 if ok else 1)))
        self.T.run_with_progress("立即归档会话", work, countdown=3)

    def _show_beats(self, icon=None, item=None):
        """「今日心跳」：读 `ops\\<机器>\\<客户端>\\beat.jsonl`，报今天跳了几拍 + 最近几拍。"""
        ps = _ledgers("beat.jsonl")
        path = ps[-1] if ps else os.path.join(__import__("persona_ops").dir(), "beat.jsonl")
        today = time.strftime("%Y-%m-%d")
        rows = []
        try:
            with io.open(path, encoding="utf-8", errors="replace") as f:
                for ln in f:
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        d = json.loads(ln)
                    except ValueError:
                        continue
                    if str(d.get("at") or "").startswith(today):
                        rows.append(d)
        except OSError as e:
            self.T._dialog("读不到心跳账：\n%s\n\n%s" % (path, e), "SysPersona · 今日心跳")
            return
        out = ["今日心跳：%d 拍（%s）" % (len(rows), path), ""]
        for d in rows[-15:]:
            out.append("%s  %-6s %s" % (str(d.get("at") or "")[11:19],
                                        str(d.get("action") or ""),
                                        str(d.get("why") or "")[:56]))
        if not rows:
            out.append("今天还没有心跳记录（守护的 heartbeat 任务没跑？）")
        self._dialog_window("\n".join(out), "SysPersona · 今日心跳")

    def _open_ledger_local(self, icon=None, item=None):
        """「变更台账（本机）」：开 `ops\\<机器>\\变更台账.md`（这本是**能写**的）。"""
        p = os.path.join(__import__("persona_ops").dir(), "变更台账.md")
        if not os.path.isfile(p):
            self.T._dialog("本机台账还没有：\n%s" % p, "SysPersona · 变更台账")
            return
        try:
            import subprocess as _sp
            _sp.Popen(["notepad.exe", p], creationflags=NO_WINDOW)
        except Exception as e:             # noqa: BLE001
            self.T._dialog("打不开：%s" % str(e)[:200], "SysPersona · 变更台账")

    def _show_ledger_global(self, icon=None, item=None):
        """「全局台账（只读）」：`ops\\变更台账.md`——只摊最后 30 行，一个字节都不写。"""
        p = os.path.join(__import__("persona_ops").shared(), "变更台账.md")
        try:
            txt = io.open(p, encoding="utf-8-sig", errors="replace").read()
        except OSError as e:
            self.T._dialog("全局台账读不到：\n%s\n\n%s" % (p, e),
                           "SysPersona · 全局台账（只读）")
            return
        body = "\n".join(txt.splitlines()[-30:])
        self._dialog_window("全局台账（只读）：%s\n\n%s\n\n"
                            "（只显示最后 30 行；要看全文自己开文件，托盘不写它）" % (p, body),
                            "SysPersona · 全局台账（只读）")

    def _show_handoff(self, icon=None, item=None):
        """「交接账」：`persona_handoff.open_items()` 里还没办完的那几条。"""
        try:
            import persona_handoff as H
            rows = H.open_items()
        except Exception as e:             # noqa: BLE001
            self.T._dialog("读不到交接账：%s" % str(e)[:200], "SysPersona · 交接账")
            return
        out = ["交接账：未完成 %d 条" % len(rows), ""]
        for r in rows[-12:]:
            out.append("%s  %s" % (str(r.get("at") or "")[:16], str(r.get("text") or "")[:80]))
        if not rows:
            out.append("没有未完成项。")
        self._dialog_window("\n".join(out), "SysPersona · 交接账")

    # ── 空闲唤醒（人 2026-11-12：就一个空闲唤醒；每个客户端各自一个勾）────────────
    def _status_bar(self):
        r"""标题栏下面那条状态栏（人 2026-11-16 要的六项）。"""
        try:
            every = int(self._sc_get("selfcheck_every", 120))
        except Exception:                       # noqa: BLE001
            every = 120
        launch = bool(self._sc_get("selfcheck_launch", True))
        try:
            cycles = int(self._sc_get("selfcheck_cycles", 3))
        except Exception:                       # noqa: BLE001
            cycles = 3
        hb = self._hb() or {}
        wake_on = bool(hb.get("wake"))
        try:
            idle_min = int(float(hb.get("idle_min") or 30))
        except Exception:                       # noqa: BLE001
            idle_min = 30
        try:
            auto = autostart_on()
        except Exception:                       # noqa: BLE001
            auto = False
        try:
            nosleep = self._no_sleep_on()
        except Exception:                       # noqa: BLE001
            nosleep = False
        # 两行：前三项 / 后三项（一行太宽会被窗口截断，2026-11-16 人两次截图报"显示不全"）
        idx_on = False
        idx_cap = bool(__import__("shutil").which("nvidia-smi"))
        try:
            import json as _j
            _p = os.path.join(__import__("persona_ops").dir(), "persona.conf.json")
            idx_on = bool(_j.load(io.open(_p, encoding="utf-8-sig")).get("wake_index"))
        except Exception:
            idx_on = False
        return ("自检 每 %d 秒 ｜ 自动启动客户端 %s ｜ 异常修复唤醒 每 %d 个周期（≈%.0f 分钟）\n"
                "空闲唤醒 %s ｜ 唤醒后自动向量/重排 %s ｜ 开机自启 %s ｜ 永不睡眠 %s" % (
                    every, "开" if launch else "关", cycles, every * cycles / 60.0,
                    ("静 %d 分钟后" % idle_min) if wake_on else "关",
                    ("开" if idx_on else "关") if idx_cap else "不可用（本机无 GPU）",
                    "开" if auto else "关", "开" if nosleep else "关"))


    def _hb(self):
        try:
            import persona_root as R
            # fresh=True：开关是 persona_machine.py 在另一个进程里写的，
            # 不重读缓存就会「文件改了、勾一直不变」（2026-11-13 人报）。
            return R.heartbeat_cfg(fresh=True)
        except Exception:
            return {"wake": False, "clients": []}

    # ── 压缩时机（2026-11-13 人定）──────────────────────────────────
    # 人原话：「要不就改成，检测到我在使用是，弹出只提示，我手动压缩，
    #          或者等待超时后自动压缩」→ 三档全给，勾选当前那档。
    COMPACT_WHEN = (("wait", "提示后到期自动压（默认）"),
                    ("ask", "只提示，我手动压"),
                    ("borrow", "借用键盘，直接压"))

    def _compact_policy_cli(self, *args):
        import subprocess
        script = os.path.join(ROOT, "脚本/persona_compact.py")
        try:
            return subprocess.run([sys.executable, script, "policy"] + list(args),
                                  capture_output=True, cwd=ROOT,
                                  creationflags=NO_WINDOW, timeout=60)
        except Exception:                  # noqa: BLE001
            return None

    def _compact_when(self):
        r = self._compact_policy_cli("--json")
        try:
            return (json.loads((r.stdout or b"").decode("utf-8", "replace")) or {}).get("mode") or "wait"
        except Exception:                  # noqa: BLE001
            return "wait"


    # ── 模型 / 思考等级（2026-11-14 人报的唯一缺口）────────────────────
    def _hermes_cfg(self, *args):
        """改 config.yaml 里的模型/思考等级（通道＝**本机子进程**，不经网络、不经 CDP）。

        2026-11-14 人报「点了换模型没有任何反应」：原来这里 capture_output 之后
        把输出**扔了**，成功也没有任何回声，失败也看不见（pythonw 没有控制台）。
        现在：跑完把结果写托盘日志 + 弹一个系统通知；失败改成红字提示。
        """
        import subprocess

        def work():
            ok, msg = False, ""
            try:
                r = subprocess.run([sys.executable,
                                    os.path.join(ROOT, "脚本/persona_hermes_cfg.py")] + list(args),
                                   capture_output=True, timeout=180)
                out = ((r.stdout or b"") + (r.stderr or b"")).decode("utf-8", "replace").strip()
                ok = r.returncode == 0
                msg = out.splitlines()[-1] if out else ("exit %d" % r.returncode)
            except Exception as e:                 # noqa: BLE001
                msg = str(e)[:160]
            try:
                self.T._p("模型/思考 → %s：%s %s" % (" ".join(args), "OK" if ok else "FAIL", msg))
            except Exception:                      # noqa: BLE001
                pass
            # 让人看得见（托盘没有控制台）
            try:
                if self.icon is not None:
                    self.icon.notify(msg, "SysPersona · 模型/思考" + ("" if ok else "（失败）"))
            except Exception:                      # noqa: BLE001
                pass
            self._rebuild_menu()
        threading.Thread(target=work, daemon=True).start()

    def _new_session_dsh(self, icon=None, item=None):
        r"""开新会话 + 唤醒一次（2026-11-15：原 `_hermes_wake`，Hermes 删除后改走 DSH）。

        `persona_client_switch --to dsh` 就是「拉起客户端 → 开新会话 → 注入交接包」，
        配置改了（换模型/换推理档）必须开新会话才生效，否则旧会话永远还是老样子。
        """
        self._run_script_bg("开新会话（唤醒一次）",
                            [os.path.join(ROOT, "脚本", "persona_client_switch.py"),
                             "--to", "dsh"])

    def _hermes_cfg_now(self):
        """(model, provider, effort)；读不到就给问号，不挡菜单显示。"""
        try:
            import persona_hermes_cfg as HC
            return HC.current()
        except Exception:                          # noqa: BLE001
            return ("?", "?", "?")

    def _wake_nas(self, icon=None, item=None):
        r"""网络唤醒 NAS（人 2026-11-14：「nas 能网络唤醒」）。

        发魔术包 → 后台等 8737 通 → 弹通知。全程线程里跑，不占托盘。
        """
        import subprocess

        def work():
            try:
                script = os.path.join(ROOT, "脚本/persona_wol.py")
                r = subprocess.run([sys.executable, script, "nas", "--wait"],
                                   capture_output=True, timeout=200)
                out = ((r.stdout or b"") + (r.stderr or b"")).decode("utf-8", "replace").strip()
                msg = out.splitlines()[-1] if out else "无输出"
                ok = r.returncode == 0
            except Exception as e:                 # noqa: BLE001
                ok, msg = False, str(e)[:120]
            try:
                self.T._p("唤醒 NAS：%s %s" % ("OK" if ok else "FAIL", msg))
            except Exception:                      # noqa: BLE001
                pass
            try:
                if self.icon is not None:
                    self.icon.notify(msg, "SysPersona · 唤醒 NAS")
            except Exception:                      # noqa: BLE001
                pass
            self._rebuild_menu()
        threading.Thread(target=work, daemon=True).start()

    def _sleep_nas(self, icon=None, item=None):
        r"""让 NAS 休眠（人 2026-11-14 实测：休眠能被魔术包叫醒，关机不行）。

        走节点命令通道 `run <对端> power hibernate`，不碰 SMB、不碰 RDP。
        二次确认：休眠会让那台的 8737 断掉，别在同步/远程任务跑着时点。
        """
        import subprocess

        def work():
            try:
                script = os.path.join(ROOT, "脚本/persona_node_link.py")
                r = subprocess.run([sys.executable, script, "run", "NAS-1900:AI会话检索记录",
                                    "power", "sleep"],
                                   capture_output=True, timeout=180)
                out = ((r.stdout or b"") + (r.stderr or b"")).decode("utf-8", "replace").strip()
                msg = out.splitlines()[-1] if out else "无输出"
                ok = "OK" in out.splitlines()[0] if out else False
            except Exception as e:                 # noqa: BLE001
                ok, msg = False, str(e)[:120]
            try:
                self.T._p("让 NAS 睡（S3）：%s %s" % ("OK" if ok else "FAIL", msg))
            except Exception:                      # noqa: BLE001
                pass
            try:
                if self.icon is not None:
                    self.icon.notify(msg[:180], "SysPersona · 让 NAS 睡（S3）")
            except Exception:                      # noqa: BLE001
                pass
            self._rebuild_menu()
        threading.Thread(target=work, daemon=True).start()

    def _hermes_wake(self, icon=None, item=None):
        self._hermes_cfg("wake")

    def _hermes_restart(self, icon=None, item=None):
        self._hermes_cfg("restart")

    def _hermes_model_menu(self):
        cur = self._hermes_cfg_now()[0]
        try:
            import persona_hermes_cfg as HC
            table = HC.MODELS
        except Exception:                          # noqa: BLE001
            table = {}
        def mk_model(k):
            def f(icon=None, item=None):
                self._hermes_cfg("model", k, "--no-restart")
            return f
        items = []
        for key, (mid, _prov) in table.items():
            items.append(pystray.MenuItem(
                key, mk_model(key),
                checked=(lambda item, m=mid: self._hermes_cfg_now()[0] == m)))
        if not items:
            items = [pystray.MenuItem("（读不到模型表）", None, enabled=False)]
        items.append(pystray.Menu.SEPARATOR)
        items.append(pystray.MenuItem("改完重启客户端", self._hermes_restart))
        return pystray.Menu(*items)

    def _hermes_effort_menu(self):
        try:
            import persona_hermes_cfg as HC
            table = HC.EFFORTS
        except Exception:                          # noqa: BLE001
            table = []
        cur = self._hermes_cfg_now()[2]
        def mk_effort(lv):
            def f(icon=None, item=None):
                self._hermes_cfg("effort", lv, "--no-restart")
            return f
        items = []
        for label, val in table:
            items.append(pystray.MenuItem(
                label, mk_effort(label),
                checked=(lambda item, v=val: self._hermes_cfg_now()[2] == v)))
        if not items:
            items = [pystray.MenuItem("（读不到档位表）", None, enabled=False)]
        return pystray.Menu(*items)

    def _rebuild_menu(self):
        r"""请求重建一次菜单（**只打标记，不当场重建**）。

        2026-11-13 人报「总开关打不开」+「托盘卡死」：
        ① 勾选类菜单项（checked=）的值在建菜单那一刻求值，而 `refresh_icon()` 只在
           「在跑的客户端变了」时才重建 —— 点开关后配置改了、勾还是旧的，像"没反应"；
        ② 如果在菜单回调里直接 `self.icon.menu = ...`，pystray 在 Windows 上会**自己等自己**（死锁），
           表现就是整块托盘点不动。所以这里只置脏标记，由 loop 线程在下一拍安全重建。
        """
        self._menu_dirty = True

    def _set_compact_when(self, mode):
        r = self._compact_policy_cli("--mode", mode)
        if r is not None and r.returncode != 0:
            self.T._dialog("改压缩时机失败：%s"
                           % (r.stdout or b"").decode("utf-8", "replace")[-200:],
                           "SysPersona · 压缩时机")
        self._rebuild_menu()

    def _compact_when_menu(self):
        def mk(mode):
            def f(icon=None, item=None):
                self._set_compact_when(mode)
            return f
        return pystray.Menu(*[
            pystray.MenuItem(label, mk(mode),
                             checked=(lambda item, m=mode: self._compact_when() == m))
            for mode, label in self.COMPACT_WHEN])

    def _hb_run(self, *args):
        # 2026-11-13 人报「点一下唤醒总开关，鼠标转圈转半天，1 分钟，还不成功」：
        # subprocess.run 原来是**在菜单回调里同步跑**的，子进程不退，托盘就一直被占着
        # （连托盘都点不动）。现在丢线程：菜单立刻回手，跑完再刷勾。
        # 子进程那头的"一分钟"另修在 persona_machine.current_id()：改心跳不再跑
        # 硬件指纹、不再重设九个环境变量。
        import subprocess
        script = os.path.join(ROOT, "脚本/persona_machine.py")

        def work():
            try:
                r = subprocess.run([sys.executable, script, "heartbeat"] + list(args),
                                   capture_output=True, cwd=ROOT,
                                   creationflags=NO_WINDOW, timeout=120)
            except Exception as e:
                self.T._dialog("改心跳失败：%s" % str(e)[:160], "SysPersona · 空闲唤醒")
                return
            # 2026-11-13 加：子进程失败原来被吞掉，人只看到"勾了又变回去"。
            # 现在失败就把它的输出尾巴摆出来，别让人猜。
            if r.returncode != 0:
                tail = ((r.stdout or b"") + (r.stderr or b"")).decode("utf-8", "replace")
                self.T._dialog("改心跳失败（rc=%d）：\n%s"
                               % (r.returncode, tail[-400:] or "（没有输出）"),
                               "SysPersona · 空闲唤醒")
            self._rebuild_menu()

        threading.Thread(target=work, daemon=True).start()

    def _wake_sort_on(self):
        try:
            p = os.path.join(__import__("persona_ops").dir(), "persona.conf.json")
            with io.open(p, encoding="utf-8-sig") as f:
                return bool(json.load(f).get("wake_sort"))
        except Exception:                       # noqa: BLE001
            return False

    def _wake_sort_toggle(self, icon=None, item=None):
        p = os.path.join(__import__("persona_ops").dir(), "persona.conf.json")
        doc = {}
        try:
            with io.open(p, encoding="utf-8-sig") as f:
                doc = json.load(f) or {}
        except Exception:                       # noqa: BLE001
            doc = {}
        doc["wake_sort"] = 0 if self._wake_sort_on() else 1
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with io.open(p, "w", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps(doc, ensure_ascii=False, indent=1))
            self.T._p("唤醒后自动排序：%s" % ("开" if doc["wake_sort"] else "关"))
        except Exception as e:                  # noqa: BLE001
            self.T._p("写配置失败：%s" % e)
        self._rebuild_menu()

    def _power_follow(self, wake_on):
        r"""空闲唤醒开 → 插电不睡；关 → 恢复原样（人 2026-11-15）。

        只在开关动作时跑，不常驻、不占进程；原始值记在 `ops\<机器>\power-orig.json`。
        """
        import subprocess
        try:
            opsd = __import__("persona_ops").dir()
            mark = os.path.join(opsd, "power-orig.json")
            os.makedirs(opsd, exist_ok=True)

            def query(kind):
                name = "STANDBYIDLE" if kind == "standby" else "HIBERNATEIDLE"
                out = subprocess.run(["powercfg", "/query", "SCHEME_CURRENT",
                                      "SUB_SLEEP", name],
                                     capture_output=True, timeout=20
                                     ).stdout.decode("mbcs", "replace")
                import re as _re
                vals = [int(x, 16) for x in _re.findall(r"0x([0-9a-fA-F]{8})", out)]
                return vals[3] if len(vals) > 3 else None

            def setv(kind, sec):
                subprocess.run(["powercfg", "/change", "%s-timeout-ac" % kind, str(sec)],
                               capture_output=True, timeout=20)

            if wake_on:
                if not os.path.isfile(mark):
                    io.open(mark, "w", encoding="utf-8", newline="\n").write(
                        json.dumps({"standby": query("standby"),
                                    "hibernate": query("hibernate"),
                                    "at": time.strftime("%Y-%m-%d %H:%M:%S")},
                                   ensure_ascii=False, indent=1))
                setv("standby", 0)
                setv("hibernate", 0)
                self.T._p("空闲唤醒开 → 插电不睡眠")
            else:
                orig = {}
                try:
                    orig = json.load(io.open(mark, encoding="utf-8"))
                except Exception:               # noqa: BLE001
                    orig = {}
                for k in ("standby", "hibernate"):
                    v = orig.get(k)
                    if isinstance(v, int) and v >= 0:
                        setv(k, v)
                self.T._p("空闲唤醒关 → 睡眠恢复原值")
        except Exception as e:                  # noqa: BLE001
            self.T._p("改电源失败：%s" % str(e)[:120])

    def _heartbeat_on(self, icon=None, item=None):
        self._hb_run("--on")
        self._power_follow(True)
        self._rebuild_menu()

    def _heartbeat_off(self, icon=None, item=None):
        self._hb_run("--off")
        if self._no_sleep_on():                 # 前置撤掉：睡眠恢复原值
            self._no_sleep_toggle()
        self._rebuild_menu()

    def _heartbeat_toggle(self, icon=None, item=None):
        on = bool(self._hb().get("wake"))
        self._hb_run("--off" if on else "--on")
        self._rebuild_menu()

    # ── 空闲唤醒分档（人 2026-11-15：5/10/30/60 四档）────────────────────
    IDLE_TIERS = (5, 10, 30, 60)

    def _idle_min(self):
        try:
            return int((self._hb() or {}).get("idle_min") or 30)
        except Exception:                       # noqa: BLE001
            return 30

    def _set_idle_min(self, minutes, icon=None, item=None):
        self._hb_run("--on", "--idle", str(int(minutes)))
        # 永不睡眠是空闲唤醒的前置：一开就落实（人 2026-11-15）
        if not self._no_sleep_on():
            self._no_sleep_toggle()
        self._rebuild_menu()

    def _idle_wake_menu(self):
        r"""空闲唤醒：关 / 开，加 5·10·30·60 分钟四档（人 2026-11-15）。

        注意：pystray 会校验回调参数个数，`lambda icon, item, mm=m:` 这种三参数
        会被 `_assert_action` 判成非法（ValueError）。所以用闭包工厂造两参数回调。
        """
        on = bool(self._hb().get("wake"))
        cur = self._idle_min()

        def mk_set(mm):
            def f(icon=None, item=None):
                self._set_idle_min(mm)
            return f

        def mk_check(mm):
            return lambda item: bool(on and cur == mm)

        # 人 2026-11-15：「空闲唤醒了添加一个开关」——开/关各一项，勾在生效的那边。
        # 人 2026-11-15：不要单独的「开」——**选了时间就是开**，选「关」才是关。
        items = [
            pystray.MenuItem("关闭（只检测）", self._heartbeat_off,
                             checked=lambda item: not on),
        ]
        items.append(pystray.Menu.SEPARATOR)
        for m in self.IDLE_TIERS:
            items.append(pystray.MenuItem("静默 %d 分钟后唤醒" % m,
                                          mk_set(m), checked=mk_check(m)))
        items.append(pystray.Menu.SEPARATOR)
        # 人 2026-11-15：「空闲唤醒后自动排序」，而且「只在空闲唤醒开机后有效」——
        # 钩子只挂在空闲唤醒（selfwake 的 idle 分支）和开机恢复上，别的不触发。
        items.append(pystray.MenuItem("唤醒后自动排序", self._wake_sort_toggle,
                                      checked=lambda item: self._wake_sort_on()))
        return pystray.Menu(*items)

    def _heartbeat_set_client(self, name):
        cur = list(self._hb().get("clients") or [])
        if name in cur:
            cur.remove(name)
        else:
            cur.append(name)
        self._hb_run("--clients", ",".join(cur))
        self._rebuild_menu()

    def _heartbeat_menu(self):
        def mk(nm):
            def f(icon=None, item=None):
                self._heartbeat_set_client(nm)
            return f
        # 2026-11-15 人：「以后固定用这个客户端，不用 Hermes 了」→
        # 只留 dsh 一个勾（之前只剩 hermes，是因为当时以为只有它有可编程入口）。
        items = []
        for nm in ("dsh",):
            items.append(pystray.MenuItem(
                nm, mk(nm),
                checked=(lambda item, n=nm: n in (self._hb().get("clients") or []))))
        items.append(pystray.Menu.SEPARATOR)
        items.append(pystray.MenuItem(
            "总开关（开=没在跑就拉起并读设定）", self._heartbeat_toggle,
            checked=lambda item: bool(self._hb().get("wake"))))
        return pystray.Menu(*items)

    # ── 工具箱：把"有工具但没人点"的那些手动脚本放进托盘（人 2026-11-12 定）──────────
    def _toolbox_menu(self):
        r"""「工具箱（只读）」：每项都是**只读**命令，点了就跑，输出摊在一个框里。

        为什么只放只读的：托盘里的点击是"一次误触就执行"，凡是有副作用的
        （加密/解封、改权限档、清道夫真删、备份真打）都不放在这里——
        那些必须先在命令行里看清楚参数。宁可少给几个按钮，也不给"点了就改"的按钮。
        """
        return pystray.Menu(
            pystray.MenuItem("工具链体检", lambda i, it: self._tool(["persona_tools.py", "check"], "工具链体检")),
            pystray.MenuItem("备份轮转计划（只读）", lambda i, it: self._tool(["persona_backup_retention.py", "plan"], "备份轮转计划")),
            pystray.MenuItem("清道夫状态（只读）", lambda i, it: self._tool(["persona_janitor.py", "status"], "清道夫状态")),
            pystray.MenuItem("引导包清单（只读）", lambda i, it: self._tool(["persona_bootstrap.py", "list"], "引导包清单")),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("人在不在", lambda i, it: self._tool(["persona_presence.py"], "人在不在")),
            pystray.MenuItem("权限档位（读当前）", lambda i, it: self._tool(["persona_permission.py", "--current"], "权限档位")),
            pystray.MenuItem("漂移记录（看最近 10 条）", lambda i, it: self._tool(["persona_drift.py", "show", "--n", "10"], "漂移记录")),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("守护日志（最后 40 行）", lambda i, it: self._tool(["persona_logs.py", "--tail", "40"], "守护日志", 120)),
        )

    def _tool(self, argv, title, timeout=90):
        r"""跑一个**只读**工具，把输出摊在框里。

        三个约束：不抢焦点（NO_WINDOW）、有超时（托盘线程不能被一个卡住的脚本拖死）、
        输出截断（框里塞不下几万字，只留尾部，并说明被截断了）。
        """
        import subprocess
        script = os.path.join(ROOT, "脚本", argv[0])
        if not os.path.isfile(script):
            self.T._dialog("本机没有这个脚本：%s" % argv[0], "SysPersona · %s" % title)
            return
        try:
            r = subprocess.run([self.T.PY, script] + list(argv[1:]),
                               capture_output=True, timeout=timeout,
                               cwd=ROOT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            out = (r.stdout or b"").decode("utf-8", "replace").strip()
            err = (r.stderr or b"").decode("utf-8", "replace").strip()
        except subprocess.TimeoutExpired:
            self.T._dialog("超时（%d 秒还没跑完）：%s" % (timeout, " ".join(argv)), "SysPersona · %s" % title)
            return
        except Exception as e:                       # noqa: BLE001
            self.T._dialog("跑不起来：%s" % str(e)[:200], "SysPersona · %s" % title)
            return
        body = out or err or "（没有任何输出：命令正常但没话可说）"
        if len(body) > 6000:
            body = "…（前面省略 %d 字）\n" % (len(body) - 6000) + body[-6000:]
        self.T._dialog(body, "SysPersona · %s" % title)

    # ── 加密入口（人 2026-11-11 13:33「要有加解密入口，而且能输入密码，生成密钥文件」）──
    def _crypto_menu(self):
        return pystray.Menu(
            pystray.MenuItem("设置 / 更换密码（生成密钥文件）", self._crypto_setpw),
            pystray.MenuItem("看加密状态", self._crypto_stat),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("立即加密（封存明文）", self._crypto_seal),
            pystray.MenuItem("恢复明文（解封）", self._crypto_unseal),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("验证方式（令牌 / 邮箱 / 短信）", self._auth_menu()),
            pystray.MenuItem("验证加密与解密（自检）", self._verify_crypto_menu),
        )

    # ── 验证方式：令牌（能用）+ 邮箱/短信（只有框架） ──
    def _auth_menu(self):
        return pystray.Menu(
            pystray.MenuItem("看凭据 / 令牌", self._auth_status),
            pystray.MenuItem("生成一个新令牌（只显示一次）", self._auth_new_token),
            pystray.MenuItem("用手机令牌算码（TOTP）", self._auth_code),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("邮箱 / 短信：框架状态（未实现）", self._auth_providers),
        )

    def _auth_cli(self, *args, timeout=120):
        import subprocess
        import sys
        here = ROOT
        r = subprocess.run([sys.executable, os.path.join(here, "脚本/persona_auth.py")] + list(args),
                           cwd=here, capture_output=True, timeout=timeout,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                           encoding="utf-8", errors="replace")
        return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()

    def _auth_status(self, icon=None, item=None):
        try:
            _rc, out = self._auth_cli("status")
            self.T._dialog(out[-1500:] or "（没有输出）", "SysPersona · 凭据与令牌")
        except Exception as e:             # noqa: BLE001
            self.T._dialog("读不到：%s" % str(e)[:200], "SysPersona · 凭据与令牌")

    def _auth_new_token(self, icon=None, item=None):
        name = self._ask_text("SysPersona · 令牌", "给这个令牌起个名字（例如：笔记本 / NAS 同步）")
        if not name:
            return
        try:
            rc, out = self._auth_cli("add", "--kind", "token", "--label", name.strip(),
                                     "--scopes", "vault:read,vault:write")
            self.T._dialog(out[-1200:] + ("\n\n（只显示这一次，抄走再关窗）" if rc == 0 else ""),
                           "SysPersona · 新令牌")
        except Exception as e:             # noqa: BLE001
            self.T._dialog("生成失败：%s" % str(e)[:200], "SysPersona · 新令牌")

    def _auth_code(self, icon=None, item=None):
        cid = self._ask_text("SysPersona · 手机令牌", "凭据 id（见「看凭据 / 令牌」里的列表）")
        if not cid:
            return
        try:
            _rc, out = self._auth_cli("code", cid.strip())
            self.T._dialog(out[-400:] or "（没有输出）", "SysPersona · 手机令牌")
        except Exception as e:             # noqa: BLE001
            self.T._dialog("算码失败：%s" % str(e)[:200], "SysPersona · 手机令牌")

    def _auth_providers(self, icon=None, item=None):
        try:
            import subprocess
            import sys
            r = subprocess.run([sys.executable, os.path.join(ROOT, "脚本/persona_auth_providers.py"), "status"],
                               cwd=ROOT, capture_output=True, timeout=120,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                               encoding="utf-8", errors="replace")
            self.T._dialog(((r.stdout or "") + (r.stderr or "")).strip()[-1200:],
                           "SysPersona · 二次因子框架")
        except Exception as e:             # noqa: BLE001
            self.T._dialog("读不到：%s" % str(e)[:200], "SysPersona · 二次因子框架")

    def _ask_via_dialog(self, title, prompt, secret=True, minlen=0):
        """起一个**独立进程**弹输入窗，读它的 stdout。

        2026-11-11 13:45 踩坑：直接在托盘线程里 tk.Tk()，窗口能出来但事件循环
        不在主线程 → **关不掉、点不动**。独立进程就没这问题（Esc / 右上角 / 取消都灵）。
        取消 = 退出码非 0 = 读不到内容 = 返回 None。
        """
        import subprocess
        import sys
        py = sys.executable
        pyw = os.path.join(os.path.dirname(py), "pythonw.exe")
        exe = pyw if os.path.exists(pyw) else py
        cmd = [exe, os.path.join(ROOT, "脚本/persona_pw_dialog.py"), title, prompt]
        if not secret:
            cmd.append("--text")
        if minlen:
            cmd += ["--min", str(int(minlen))]
        try:
            r = subprocess.run(cmd, cwd=ROOT, capture_output=True, timeout=300,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                               encoding="utf-8", errors="replace")
        except Exception:                  # noqa: BLE001
            return None
        if r.returncode != 0:
            return None
        v = (r.stdout or "").strip()
        return v or None

    def _ask_pw(self, title, prompt):
        """遮口输入一行密码。取消返回 None。"""
        return self._ask_via_dialog(title, prompt, secret=True)

    def _ask_text(self, title, prompt):
        """输入一行普通文字（令牌名 / 凭据 id）。取消返回 None。"""
        return self._ask_via_dialog(title, prompt, secret=False)

    def _crypto_setpw(self, icon=None, item=None):
        p1 = self._ask_pw("SysPersona · 加密", "设置加密密码（至少 8 位，丢了就解不开）")
        if p1 is None:
            return
        p1 = p1.strip()
        if len(p1) < 8:
            self.T._dialog("密码太短（至少 8 位）。", "SysPersona · 加密")
            return
        p2 = self._ask_pw("SysPersona · 加密", "再输一次确认")
        if p2 is None:
            return
        if p1 != p2.strip():
            self.T._dialog("两次输入不一致，没改动。", "SysPersona · 加密")
            return

        def work():
            import persona_vault as V
            lines, ok = [], True
            try:
                if not os.path.exists(V.key_path()):
                    made, msg = V.init_key(True)
                    lines.append("生成密钥文件：%s" % msg)
                    if not made:
                        return False, "\n".join(lines)
                else:
                    lines.append("密钥文件已存在，沿用：%s" % V.fingerprint(V.load_key()))
                V.save_pass_copy(p1)
                lines.append("口令副本已更新：%s" % V.pass_path())
                lines.append("记牢这个密码：它是换机器时唯一的开门钥匙。")
            except Exception as e:         # noqa: BLE001
                ok, lines = False, lines + ["失败：%s" % str(e)[:200]]
            return ok, "\n".join(lines)

        self.T.run_with_progress("设置密码 / 生成密钥文件", work, countdown=8)

    def _crypto_stat(self, icon=None, item=None):
        try:
            import persona_vault as V
            self.T._dialog(V.status(), "SysPersona · 加密状态")
        except Exception as e:             # noqa: BLE001
            self.T._dialog("读不到：%s" % str(e)[:200], "SysPersona · 加密状态")

    def _set_switch(self, enabled, mode):
        import json
        p = os.path.join(__import__("persona_ops").dir(), "vault.json")
        rec = {"enabled": bool(enabled), "mode": mode,
               "note": "2026-11-11 托盘开关：%s / %s" % ("开" if enabled else "关", mode)}
        tmp = p + ".tmp"
        with io.open(tmp, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
        return rec

    def _crypto_seal(self, icon=None, item=None):
        try:
            import persona_vault as V
            V.load_key()
        except Exception as e:             # noqa: BLE001
            self.T._dialog("还没有密钥文件，先点「设置 / 更换密码」。\n\n%s"
                           % str(e)[:160], "SysPersona · 加密")
            return

        def work():
            import subprocess
            import sys
            here = ROOT
            self._set_switch(True, "local")
            r = subprocess.run([sys.executable, os.path.join(here, "脚本/persona_vault_bulk.py"), "seal"],
                               cwd=here, capture_output=True, timeout=3600,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                               encoding="utf-8", errors="replace")
            out = ((r.stdout or "") + (r.stderr or "")).strip()
            return r.returncode == 0, out[-900:]

        self.T.run_with_progress("立即加密（封存明文）", work, countdown=8)

    def _crypto_unseal(self, icon=None, item=None):
        def work():
            import subprocess
            import sys
            here = ROOT
            self._set_switch(False, "plain")
            r = subprocess.run([sys.executable, os.path.join(here, "脚本/persona_vault_bulk.py"), "unseal"],
                               cwd=here, capture_output=True, timeout=3600,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                               encoding="utf-8", errors="replace")
            out = ((r.stdout or "") + (r.stderr or "")).strip()
            return r.returncode == 0, out[-900:]

        self.T.run_with_progress("恢复明文（解封）", work, countdown=8)

    # ── 加密自检：人 2026-11-11「程序还要留手动验证解密的选项」 ──
    def _verify_crypto_menu(self, icon=None, item=None):
        try:
            title, worker = self._verify_crypto()
            self.T.run_with_progress(title, worker, countdown=8)
        except Exception as e:             # noqa: BLE001
            self.T._dialog("跑不动：%s" % str(e)[:200], "SysPersona · 加密自检")

    def _verify_crypto(self):
        def work():
            import subprocess
            import sys
            here = ROOT
            lines = []
            try:
                import persona_vault as V
                st = V.status()
                lines.append(st)
            except Exception as e:         # noqa: BLE001
                lines.append("读保险箱状态失败：%s" % str(e)[:120])
            try:
                r = subprocess.run([sys.executable, os.path.join(here, "脚本/persona_vault.py"), "selftest"],
                                   cwd=here, capture_output=True, timeout=240,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                                   encoding="utf-8", errors="replace")
                out = ((r.stdout or "") + (r.stderr or "")).strip()
                ok = r.returncode == 0 and "往返正确" in out
                lines.append(out[-700:])
            except Exception as e:         # noqa: BLE001
                ok, lines = False, lines + ["自检跑不动：%s" % str(e)[:160]]
            return ok, "\n".join(lines)
        return ("验证加密与解密（往返自检）", work)

    def _dispatch_worker(self, name, action, icon=None, item=None):
        """菜单点击的统一入口（进度窗版本）。"""
        try:
            res = self._do(name, action)
            if isinstance(res, tuple):
                title, worker = res
                self.T.run_with_progress(title, worker, countdown=8)
        except Exception as e:             # noqa: BLE001
            self.T._dialog("动作失败：%s" % str(e)[:200], "SysPersona")

    def _fmt(self, v):
        try:
            v = float(v)
        except (TypeError, ValueError):
            return "?"
        return "%.1fM" % (v / 1e6) if v >= 1e6 else "%.0fK" % (v / 1e3)

    def _show(self, name, icon=None, item=None):
        cfg = (self.spec.get("clients") or {}).get(name) or {}
        label = cfg.get("label") or name
        r = self._rows().get(name)
        if not r:
            self.T._dialog("%s 没在运行。" % label, "SysPersona · %s" % label)
            return
        lines = ["%s：在跑（%d 进程）" % (label, r.get("procs") or 0)]
        if r.get("pct") is None:
            lines.append("水位：读数不可用")
        else:
            lines.append("水位：%s / %s  %.0f%%"
                         % (self._fmt(r.get("ctx")), self._fmt(r.get("max")), r["pct"]))
        if r.get("note"):
            lines.append("备注：%s" % r["note"])
        self.T._dialog("\n".join(lines), "SysPersona · %s" % label)

    def _show_version(self, icon=None, item=None):
        r"""菜单「版本（库 / 本机）」：人 2026-11-15「2 直接做」。

        显示：库版本（时间）+ 谁登记的 + 内容指纹是否一致 + 本机是谁。
        换机器/怀疑读到旧东西时，点这一项就知道自己站在哪一版上。
        """
        try:
            import persona_ver as V
            doc = V._load()
            cur = V.fingerprint()
            same = (doc.get("hash") or "") == cur
            body = "\n".join([
                "库版本：%s" % (doc.get("version") or "?"),
                "登记时间：%s" % (doc.get("at") or "?"),
                "谁登记的：%s / %s" % (doc.get("by") or "?", doc.get("client") or "?"),
                "内容指纹：%s（%s）" % ((doc.get("hash") or "?")[:16],
                                   "与登记一致" if same else "不一致：有改动没 bump"),
                "",
                "本机：%s" % __import__("persona_ops").dir_name(),
                "核对命令：python 脚本/persona_ver.py status",
            ])
        except Exception as e:                     # noqa: BLE001
            body = "读版本中心失败：%s" % str(e)[:120]
        self.T._dialog(body, "SysPersona · 版本")

    def _show_digest(self, icon=None, item=None):
        r"""菜单「今日自检（只读）」：把已经生成好的日报摊开给你看。

        只读，三个"不"：不跑自检、不刷新、不改任何文件。自检本身由计划任务
        `SysPersona-Digest` 每天 23:50 跑；这里只是让人少开一次命令行。
        日报不在就直说"还没有"，并给出想现在就跑的那一行命令——不替人跑。
        """
        p = os.path.join(self.T.OPS, "daily-status.md")
        try:
            if not os.path.isfile(p):
                self.T._dialog(
                    "还没有日报。\n\n每天 23:50 会自动生成一份。\n"
                    "现在就想看，跑这一句（不是我替你跑）：\n\n"
                    "  python <库>\\脚本\\persona_digest.py --once",
                    "SysPersona · 今日自检")
                return
            txt = io.open(p, encoding="utf-8-sig").read().strip()
            age_h = (time.time() - os.path.getmtime(p)) / 3600.0
            when = time.strftime("%m-%d %H:%M", time.localtime(os.path.getmtime(p)))
            head = "生成于 %s（%.1f 小时前）· 只读\n\n" % (when, age_h)
            self.T._dialog(head + txt, "SysPersona · 今日自检")
        except Exception as e:             # noqa: BLE001
            self.T._dialog("日报读不出来：%s" % str(e)[:200], "SysPersona · 今日自检")

    # ── 新菜单的两个弹框：运行状态（第一行水位）与异常（selfcheck FAIL）──────
    def _status_send_cmd(self, text):
        r"""主窗口按钮用：把一句话交给 persona-inbox 通道（和菜单同一套）。"""
        py = sys.executable
        pyw = os.path.join(os.path.dirname(py), "pythonw.exe")
        exe = pyw if os.path.exists(pyw) else py
        safe = text.replace('"', '""')
        return '"%s" "%s" --send-inbox "%s"' % (
            exe, os.path.join(ROOT, "脚本", "persona_tray_multi.py"), safe)

    def _status_archive_cmd(self):
        r"""状态窗里「立即归档」要执行的命令：直接跑 persona_archive.py。"""
        py = sys.executable
        pyw = os.path.join(os.path.dirname(py), "pythonw.exe")
        exe = pyw if os.path.exists(pyw) else py
        return '"%s" "%s"' % (exe, os.path.join(ROOT, "脚本", "persona_archive.py"))

    def _status_refresh_cmd(self):
        r"""状态窗里「立即刷新」要执行的命令：重算正文并覆盖同一个文件。"""
        py = sys.executable
        pyw = os.path.join(os.path.dirname(py), "pythonw.exe")
        exe = pyw if os.path.exists(pyw) else py
        return '"%s" "%s" --status-text-out "%s"' % (
            exe, os.path.join(ROOT, "脚本", "persona_tray_multi.py"),
            os.path.join(_RT, "tray-status-text.txt"))

    def _dialog_window(self, text, title, refresh_cmd=None, archive_cmd=None,
                       compact_cmd=None, reread_cmd=None):
        """等宽、可复制的状态窗（`persona_status_dialog.py`）；起不来再退回 MessageBox。

        为什么另起进程：托盘回调在 pystray 线程里，tk 在那线程建窗会"关不掉/点不动"
        （2026-11-11 踩过）；persona_tray.show_status 也是这个套路。
        """
        try:
            p = os.path.join(_RT, "tray-status-text.txt")
            try:
                _bar = self._status_bar()
            except Exception:                  # noqa: BLE001
                _bar = ""
            with io.open(p, "w", encoding="utf-8", newline="\n") as f:
                if _bar:
                    for _ln in _bar.splitlines():
                        f.write("〖状态栏〗" + _ln + "\n")
                f.write(text)
            import subprocess
            py = sys.executable
            pyw = os.path.join(os.path.dirname(py), "pythonw.exe")
            exe = pyw if os.path.exists(pyw) else py
            argv = [exe, os.path.join(ROOT, "脚本", "persona_status_dialog.py"), p, title]
            if refresh_cmd:
                argv.append(refresh_cmd)          # 第三个参数：状态窗「立即刷新」要跑的命令
            if archive_cmd:
                argv.append(archive_cmd)          # 第四个参数：状态窗「立即归档」要跑的命令
            if compact_cmd:
                argv.append(compact_cmd)          # 第五个：压缩上下文
            if reread_cmd:
                argv.append(reread_cmd)           # 第六个：重读设定
            subprocess.Popen(argv,
                             cwd=ROOT, creationflags=NO_WINDOW, close_fds=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            return
        except Exception as e:             # noqa: BLE001
            _p("状态窗起不来（%s），退回 MessageBox" % e)
            self.T._dialog(text, title)

    def _sess_minutes(self):
        r"""主窗口里"多久没新消息就不显示"（分钟）。默认 120。"""
        try:
            return max(5, int(self._sc_get("session_fresh_min", 120)))
        except Exception:                       # noqa: BLE001
            return 120

    def _status_text(self):
        r"""「运行状态」的正文：**第一行必须是水位**（人 2026-11-15 点明的那一行）。

            水位 32.4% · 340K / 1.05M · 距压缩线 -26K

        后面的明细全部读现成文件（waterline-all / selfcheck / 版本 / 交接），不自己造数。
        """
        d = self._wl()
        sc = self._selfcheck()
        n_arch = self._pending_archive()
        gver, grev, lrev, follows = self._ver()
        out = [self._waterline_line(), ""]

        n_procs = self._dsh_procs()
        out.append("客户端：DeepSeek Harness · %s"
                   % (("在跑（%d 个进程）" % n_procs) if n_procs else "没在跑"))
        if d.get("session"):
            out.append("当前会话：%s" % d.get("session"))
        if d.get("at"):
            out.append("更新时间：%s · 来源 %s · 状态 %s"
                       % (d.get("at"), d.get("source") or "?", d.get("state") or "?"))

        a = self._wl_all()
        ss = a.get("sessions") or []
        # 2026-11-15 人定：只显示**60 分钟内**有新消息的会话（按最后一条消息时间）。
        # 超过 60 分钟说明没新消息，列出来只是噪音（11-10 那种老会话不该占屏）。
        def _fresh(s):
            if s.get("active"):
                return True
            la = str(s.get("lastActivity") or "")
            try:
                t = time.strptime("%d-%s" % (time.localtime().tm_year, la), "%Y-%m-%d %H:%M")
                return (time.time() - time.mktime(t)) <= self._sess_minutes() * 60
            except Exception:                       # noqa: BLE001
                return False
        ss_all = ss
        ss = [s for s in ss_all if _fresh(s)]
        if ss:
            n_act = sum(1 for s in ss if s.get("active"))
            out += ["", "【最近会话】%d 条（在跑 %d 条）" % (len(ss), n_act)]
            for s in ss[:8]:
                try:
                    pct = "%.1f%%" % float(s.get("pct") or 0)
                except (TypeError, ValueError):
                    pct = "?"
                out.append("  %6s  %7s  %s  %s" % (
                    pct, _fmt_tok(s.get("tokens")), str(s.get("session") or "")[:12],
                    "在跑" if s.get("active") else str(s.get("lastActivity") or "")))

        out += ["", "【程序自检】%s · %d 项 · %s" % (
            sc["at"] or "还没跑过", sc["total"], (sc["summary"] or "")[:56])]
        if sc["fails"]:
            try:
                from persona_selfcheck import LABEL as _LAB
            except Exception:                  # noqa: BLE001
                _LAB = {}
            for nm, st, det in sc["fails"][:10]:
                # 人 2026-11-16：显示中文、去掉 fail 字样（红点用符号表达就够）
                out.append("  · %s：%s" % (_LAB.get(nm, nm), det[:70]))
        else:
            out.append("  全部 ok")

        n_hand = self._handoff_open()
        # 2026-11-15 人：「版本主窗口里面重复了（菜单里也有），压缩和通道两个也不用显示」
        # 所以正文只留【归档】【交接】两行——版本看菜单，压缩/通道是说明文字，不进窗口。
        out += ["",
                "【未归档会话】%s 个 · 【交接未完成】%s 条"
                % ("?" if n_arch is None else n_arch,
                   "?" if n_hand is None else n_hand)]
        return "\n".join(out)

    def _show_status(self, icon=None, item=None):
        """菜单「运行状态」（默认项）：弹状态框，第一行是水位。"""
        try:
            body = self._status_text()
        except Exception as e:             # noqa: BLE001
            body = "算状态时出错：%s" % str(e)[:300]
        self._dialog_window(body, "SysPersona", self._status_refresh_cmd(),
                            self._status_archive_cmd(), self._status_send_cmd("/compact"),
                            self._status_send_cmd(REREAD_TEXT))

    def _manual_refresh(self, icon=None, item=None):
        r"""手动刷一次：立刻重读状态、重画图标、重建菜单（人 2026-11-15：主菜单加「刷新」）。"""
        try:
            self.refresh_icon()
        except Exception as e:                  # noqa: BLE001
            _p("刷新失败：%s" % str(e)[:120])
        self._rebuild_menu()
        _p("已手动刷新")

    def _show_anomalies(self, icon=None, item=None):
        """菜单「异常：N 项」：明细 = `selfcheck.json` 里 status != ok 的那些项。

        0 项也照样能点开（人 2026-11-15：「0 项时显示 0，点开看明细」）——
        明细里报自检时间与 summary，让人知道"这个 0 是什么时候的 0"。
        """
        try:
            sc = self._selfcheck()
            n_arch = self._pending_archive()
        except Exception as e:             # noqa: BLE001
            self.T._dialog("读自检时出错：%s" % str(e)[:200], "SysPersona · 异常")
            return
        if not sc["at"]:
            body = ("读不到 selfcheck.json。\n\n本机：%s\n\n"
                    "想现在就跑一次自检（不是我给你跑）：\n"
                    "  python 脚本\\persona_selfcheck.py --once"
                    % __import__("persona_ops").dir())
        elif not sc["fails"]:
            body = ("0 项异常。\n\n自检 %s：%s（共 %d 项）\n未归档会话：%s 个"
                    % (sc["at"], sc["summary"] or "?", sc["total"],
                       "?" if n_arch is None else n_arch))
        else:
            body = "%d 项异常（自检 %s · %s，共 %d 项）：\n\n" % (
                len(sc["fails"]), sc["at"], sc["summary"] or "?", sc["total"])
            body += "\n".join("· %s（%s）\n    %s" % (nm, st, det or "没有明细")
                              for nm, st, det in sc["fails"])
        self._dialog_window(body, "SysPersona · 异常")

    # ── 起 & 刷新 ──
    def _ensure_vault_gate(self):
        """托盘起来时顺手把 I/O 门拉起（已在听就不动）。

        为什么放托盘：门要"开机就在"，但注册登录触发式计划任务要管理员权限。
        托盘本来就是常驻且开机自启的，让它管这扇门最省事。
        """
        try:
            import socket
            import subprocess
            import sys as _sys
            base = ROOT
            port, bind = 8738, "127.0.0.1"
            try:
                with io.open(os.path.join(base, "ops", "vault-gate.json"), encoding="utf-8") as f:
                    c = json.load(f)
                port, bind = int(c.get("port") or port), c.get("bind") or bind
            except Exception:              # noqa: BLE001
                pass
            s = socket.socket()
            s.settimeout(0.6)
            try:
                s.connect((bind, port))
                return "门已在听 %s:%d" % (bind, port)
            except OSError:
                pass
            finally:
                s.close()
            pyw = os.path.join(os.path.dirname(_sys.executable), "pythonw.exe")
            if not os.path.exists(pyw):
                pyw = _sys.executable
            subprocess.Popen([pyw, os.path.join(base, "脚本/persona_vault_gate.py"), "serve"],
                             cwd=base, creationflags=0x08000000, close_fds=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            return "门已拉起 %s:%d" % (bind, port)
        except Exception as e:             # noqa: BLE001
            return "门没起来：%s" % e

    def build(self):
        if READONLY:                       # 只读模式不起 I/O 门（门会写 ops/lanbus 与端口状态）
            _p("只读模式：不拉起 I/O 门")
        else:
            _p(self._ensure_vault_gate())
        try:
            color, lines, _rows, title = self._status()
        except Exception as e:             # noqa: BLE001
            _p("算状态失败：%s" % e)
            color, lines, title = GRAY, [], "SysPersona · 状态读不到"
        menu = self._menu()
        self._sig = self._menu_sig()
        self.icon = pystray.Icon("SysPersona", icon=self._image(color),
                                 title=clip_tip(title), menu=menu)
        self._title = clip_tip(title)
        _p("tooltip：%r" % (self._title,))
        _p("建托盘（单图标，%s；DSH %s；%d 项异常）" % (
            {GREEN: "绿", AMBER: "琥珀", RED: "红", GRAY: "灰"}.get(color, "?"),
            "在跑" if self._dsh_alive() else "没在跑", len(lines)))

    def _check_version(self):
        r"""别处改了配置，这里要自己知道（人 2026-11-15：「包括托盘，在其中一个修改了托盘，
        其他的位置都不知道」）。

        信号只有一处：`配置\版本.json` 的 rev。谁改配置谁 bump，
        所以这里每拍只读那个小文件（不扫 154 个被监视文件，否则每 5 秒把磁盘读一遍）。
        rev 变了 → 重载托盘配置、重建菜单、日志留一行「配置已更新（来源 …）」。
        """
        try:
            import persona_ver as V
            doc = V._load()
            rev = int(doc.get("rev") or 0)
        except Exception:                      # noqa: BLE001
            return
        last = getattr(self, "_ver_rev", 0)
        self._ver_rev = rev
        if not rev or not last or rev == last:
            return
        try:
            self.spec = load_spec()
            self._sig = None               # 强制重建菜单
            src = "%s/%s" % (doc.get("by") or "?", doc.get("client") or "?")
            _p("配置已更新：rev %d（来源 %s）→ 已重载托盘配置" % (rev, src))
        except Exception as e:                 # noqa: BLE001
            _p("配置已更新（rev %d），但重载失败：%s" % (rev, str(e)[:80]))

    def refresh_icon(self):
        """重算一次：压缩中就整块黄，否则按状态上色；在跑的客户端变了就重建菜单。"""
        self._check_version()
        try:
            on, _note = self.T.busy_state()
        except Exception:                  # noqa: BLE001
            on = False
        if on:
            icon, title = self.T.make_busy_icon(), clip_tip(self.T.BUSY_TITLE)
        else:
            color, _lines, _rows, title = self._status()
            icon, title = self._image(color), clip_tip(title)
        if title != self._title:
            self.icon.title = title
            self._title = title
            _p("tooltip 变更：%r" % (title,))      # 排查"悬停看到的和代码不一样"就靠这行
        self.icon.icon = icon
        sig = self._menu_sig()
        if sig != self._sig or getattr(self, "_menu_dirty", False):
            self._sig = sig
            self._menu_dirty = False
            try:
                self.icon.menu = self._menu()      # setter 会 update_menu()
                _p("菜单重建：%s" % (sig,))
            except Exception as e:         # noqa: BLE001
                _p("重建菜单失败：%s" % e)

    def _menu_sig(self):
        """菜单里那些**会变的数**：变了才重建菜单（否则每 5 秒白重建一次）。

        2026-11-15 菜单结构固定之后，菜单文字里仍带实时数字（异常 N 项 / 待 N /
        交接 N 条 / 勾选状态 / 在跑没在跑），所以判据从"在跑哪些客户端"换成这一组。
        """
        try:
            sc = self._selfcheck()
            return (len(sc["fails"]), self._pending_archive(), self._handoff_open(),
                    bool(self._hb().get("wake")), self._dsh_alive(), autostart_on(),
                    round(float(self.period), 1), self._ver()[0], self._ver()[2])
        except Exception as e:             # noqa: BLE001
            _p("算菜单指纹失败：%s" % e)
            return (None,)

    def _die_for_reload(self):
        r"""自动重载用：放掉图标与互斥量后**硬退**（不写 `ops\tray-off`）。

        2026-11-14 修：原来只置 `stop_flag` 就 break，loop 结束后 pystray 的后台线程
        还挂着 → 进程不退 → 互斥量不释放 → `_relaunch_self()` 起的新实例
        「已有一个托盘在跑」立刻退出。表现就是「脚本改了，托盘还是老代码」，
        日志里成对出现「脚本已更新 → 重启托盘」+「已有一个托盘在跑」。
        """
        try:
            if self.icon is not None:
                self.icon.stop()
        except Exception:                  # noqa: BLE001
            pass
        time.sleep(0.3)
        os._exit(0)

    def loop(self):
        sig_seen = _self_sig()
        while not self.stop_flag:
            slept = 0.0
            while slept < max(1.0, self.period) and not self.stop_flag:
                time.sleep(0.25)
                slept += 0.25
            if self.stop_flag:
                break
            try:
                self.refresh_icon()
            except Exception as e:         # noqa: BLE001
                _p("刷新失败：%s" % e)
            # 2026-11-14：托盘「天天在修、每次都要重新部署」——脚本一改，自己换新代码。
            # 判据是文件 mtime+size 连续两次读到同一个值（躲开正在写盘的那一下）。
            if _autoreload_on():
                cur = _self_sig()
                if cur != sig_seen:
                    if getattr(self, "_sig_pending", None) == cur:
                        _p("脚本已更新（%s）→ 重启托盘载入新代码" % (cur,))
                        _relaunch_self()
                        self.stop_flag = True
                        self._die_for_reload()      # 硬退，把互斥量让给新实例
                        break
                    self._sig_pending = cur
                else:
                    self._sig_pending = None

    # ── 静默自动更新（人 2026-11-15：静默自动运行）──────────────────────
    UPDATE_FIRST = 30          # 秒：托盘起来多久做第一次检查
    UPDATE_EVERY = 300         # 秒：之后每 5 分钟一次

    def _selfupdate_check(self):
        """跑一次 `persona_selfupdate.py --check`。返回 (退出码, 输出尾巴)。

        退出码：0 = 需要更新，1 = 已是最新；其它（或跑不起来）当"这次没结论"，不更新。
        `--check` 是只读的（不落盘、不动配置），所以可以每 5 分钟跑一次。
        """
        import subprocess
        script = os.path.join(ROOT, "脚本", "persona_selfupdate.py")
        if not os.path.isfile(script):
            return None, "本机没有 persona_selfupdate.py"
        try:
            r = subprocess.run([sys.executable, script, "--check"],
                               cwd=ROOT, capture_output=True, timeout=180,
                               creationflags=NO_WINDOW,
                               encoding="utf-8", errors="replace")
            out = ((r.stdout or "") + (r.stderr or "")).strip()
            return r.returncode, out
        except Exception as e:             # noqa: BLE001
            return None, "跑不动：%s" % str(e)[:160]

    def _spawn_updater(self):
        """用 **pythonw + DETACHED_PROCESS** 起更新器（不带 `--check`）。返回 (ok, 说明)。

        为什么 pyw + detached：更新器要活过托盘退出（托盘 `quit()` 会 os._exit），
        而且它自己最后会把托盘拉回来——不能挂在托盘的进程/控制台上。
        """
        import subprocess
        py = sys.executable
        pyw = os.path.join(os.path.dirname(py), "pythonw.exe")
        exe = pyw if os.path.exists(pyw) else py
        try:
            subprocess.Popen([exe, os.path.join(ROOT, "脚本", "persona_selfupdate.py")],
                             cwd=ROOT,
                             creationflags=NO_WINDOW | 0x00000008,   # NO_WINDOW | DETACHED_PROCESS
                             close_fds=True,
                             stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            return True, exe
        except Exception as e:             # noqa: BLE001
            return False, "起不来：%s" % str(e)[:160]

    def watch_updates(self):
        r"""静默自动更新循环：**不进菜单**，人看不见、不被打扰。

        节奏：起来 30 秒第一次，之后每 5 分钟一次，只跑 `--check`。
        返回 0（需要更新）→ 写一行日志 → 起更新器 → 托盘自己干净退出
        （更新器更新完会自己把托盘拉回来；失败它自己回滚 + 写 `ops\更新记录.md`，
        托盘下次启动照旧，所以这里**不弹窗、不通知**）。

        上限：一个进程生命周期最多**真正触发 1 次**（`_upd_fired`），
        否则会出现「更新器拉起托盘 → 托盘 30 秒后又触发 → 又退出」的循环。
        更新器没起来（Popen 失败）不算触发，下一轮还会再试——那种情况什么也没发生。
        """
        time.sleep(self.UPDATE_FIRST)
        while not self.stop_flag:
            fired = getattr(self, "_upd_fired", False)
            if fired:
                _p("静默更新：本进程已触发过一次，跳过本轮检查")
            else:
                rc, out = self._selfupdate_check()
                if rc == 0:
                    _p("静默更新：需要更新（--check 返回 0）%s"
                       % (("： " + out.splitlines()[-1][:120]) if out else ""))
                    ok, msg = self._spawn_updater()
                    if ok:
                        self._upd_fired = True
                        _p("静默更新：已起更新器（%s），托盘退出让它干活" % msg)
                        self.quit()
                        return
                    _p("静默更新：更新器没起来（%s）→ 托盘继续跑，下一轮再试" % msg)
                elif rc == 1:
                    _p("静默更新：已是最新（--check 返回 1）")
                else:
                    _p("静默更新：检查没结论（rc=%s）%s" % (rc, (out or "")[-160:]))
            slept = 0.0
            while slept < self.UPDATE_EVERY and not self.stop_flag:
                time.sleep(1.0)
                slept += 1.0

    # ── 自检设置（人 2026-11-15：自检也得有菜单）──────────────────────
    def _sc_mk(self, key, value):
        r"""pystray 会校验回调参数个数：必须返回只吃 (icon, item) 的函数。"""
        def f(icon=None, item=None):
            self._sc_set(key, value)
        return f

    def _sc_conf_path(self):
        return os.path.join(__import__("persona_ops").dir(), "persona.conf.json")

    def _sc_conf(self):
        try:
            with io.open(self._sc_conf_path(), encoding="utf-8-sig") as f:
                return json.load(f) or {}
        except Exception:                       # noqa: BLE001
            return {}

    def _sc_get(self, key, default):
        v = self._sc_conf().get(key)
        return default if v is None else v

    def _sc_set(self, key, value):
        p = self._sc_conf_path()
        doc = self._sc_conf()
        doc[key] = value
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with io.open(p, "w", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps(doc, ensure_ascii=False, indent=1))
            _p("自检设置：%s = %s" % (key, value))
            if key == "selfcheck_every":        # 界面刷新同频跟随
                self.period = float(value)
        except Exception as e:                  # noqa: BLE001
            _p("写自检设置失败：%s" % e)
        self._rebuild_menu()

    def _sc_toggle_launch(self, icon=None, item=None):
        self._sc_set("selfcheck_launch", not bool(self._sc_get("selfcheck_launch", True)))

    def _sc_toggle_wake(self, icon=None, item=None):
        self._sc_set("selfcheck_wake", not bool(self._sc_get("selfcheck_wake", False)))

    def _no_sleep_on(self):
        r"""插电是否已设为不睡眠：读 powercfg（0 = 永不睡）。"""
        try:
            import subprocess
            out = subprocess.run(["powercfg", "/query", "SCHEME_CURRENT",
                                  "SUB_SLEEP", "STANDBYIDLE"],
                                 capture_output=True, timeout=20
                                 ).stdout.decode("mbcs", "replace")
            import re as _re
            vals = [int(x, 16) for x in _re.findall(r"0x([0-9a-fA-F]{8})", out)]
            return len(vals) > 3 and vals[3] == 0
        except Exception:                       # noqa: BLE001
            return False

    def _no_sleep_toggle(self, icon=None, item=None):
        # 人 2026-11-15：空闲唤醒开着时点这项，要明确告诉人为什么关不掉。
        if bool(self._hb().get("wake")):
            self.T._dialog(
                "空闲唤醒开启中。\n\n"
                "永不睡眠是空闲唤醒的前置条件——开着唤醒却让机器睡过去，就永远叫不醒了。\n\n"
                "要关掉它：先在「空闲唤醒」里选「关闭（只检测）」，再回来关这一项。",
                "SysPersona · 永不睡眠")
            return
        r"""永不睡眠开关：开 → 插电 standby/hibernate 设 0；关 → 恢复记下的原值。"""
        import subprocess
        try:
            opsd = __import__("persona_ops").dir()
            mark = os.path.join(opsd, "power-orig.json")

            def query(kind):
                name = "STANDBYIDLE" if kind == "standby" else "HIBERNATEIDLE"
                out = subprocess.run(["powercfg", "/query", "SCHEME_CURRENT",
                                      "SUB_SLEEP", name],
                                     capture_output=True, timeout=20
                                     ).stdout.decode("mbcs", "replace")
                import re as _re
                vals = [int(x, 16) for x in _re.findall(r"0x([0-9a-fA-F]{8})", out)]
                return vals[3] if len(vals) > 3 else None

            def setv(kind, sec):
                subprocess.run(["powercfg", "/change", "%s-timeout-ac" % kind, str(sec)],
                               capture_output=True, timeout=20)

            if self._no_sleep_on():
                orig = {}
                try:
                    orig = json.load(io.open(mark, encoding="utf-8"))
                except Exception:               # noqa: BLE001
                    orig = {}
                for k in ("standby", "hibernate"):
                    v = orig.get(k)
                    if isinstance(v, int) and v >= 0:
                        setv(k, v)
                _p("永不睡眠：关（恢复原值）")
            else:
                if not os.path.isfile(mark):
                    os.makedirs(opsd, exist_ok=True)
                    io.open(mark, "w", encoding="utf-8", newline="\n").write(
                        json.dumps({"standby": query("standby"),
                                    "hibernate": query("hibernate"),
                                    "at": time.strftime("%Y-%m-%d %H:%M:%S")},
                                   ensure_ascii=False, indent=1))
                setv("standby", 0)
                setv("hibernate", 0)
                _p("永不睡眠：开（插电不睡）")
        except Exception as e:                  # noqa: BLE001
            _p("改电源失败：%s" % str(e)[:120])
        self._rebuild_menu()

    def _autostart_toggle(self, icon=None, item=None):
        """开机自启开关（2026-11-13 人提：托盘里没有这个开关）。"""
        ok, msg = autostart_set(not autostart_on())
        _p("开机自启：%s" % msg)
        try:
            if self.icon is not None and not ok:
                self.icon.notify(msg, "开机自启")
        except Exception:                  # noqa: BLE001
            pass

    def quit_full(self, icon=None, item=None):
        r"""全家退出：托盘 + 守护 + lanbus + 代理 + 门 + 皮肤，一起停。

        人 2026-11-14：「这些退出托盘，不都是应该一起退出吗？」——原来只停托盘，
        守护/网络服务还在后台跑，人去任务管理器里看得见一堆 pythonw。
        现在写 stack-off（守护看到就不许再起），再按命令行精确停掉每一个。
        """
        import subprocess as _sp
        self.stop_flag = True
        me = os.getpid()
        try:
            with io.open(STACK_OFF, "w", encoding="utf-8") as f:
                f.write("%s 人点「退出（全家）」——守护、网络服务都别再起" % time.strftime("%Y-%m-%d %H:%M:%S"))
        except OSError:
            pass
        try:
            with io.open(TRAY_OFF, "w", encoding="utf-8") as f:
                f.write("%s 人点「退出（全家）」（守护不要再自动拉托盘）" % time.strftime("%Y-%m-%d %H:%M:%S"))
        except OSError:
            pass
        pats = ["persona_daemon.py", "persona_lanbus.py", "persona_proxy.py",
                "persona_vault_gate.py", "persona_proctree.py", "persona_uiskin.js"]
        killed = []
        for pat in pats:
            try:
                import persona_proctree as _pt
                for pid, v in _pt.snapshot().items():
                    if pid == me:
                        continue
                    cl = str(v.get("cmdline") or "")
                    if pat in cl:
                        _sp.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, timeout=10,
                                creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0))
                        killed.append(pid)
            except Exception:              # noqa: BLE001
                pass
        _p("已退出（全家）：停了 %d 个后台进程（%s）" % (len(killed), pats[0].split(".")[0] + " 等"))
        self.quit(icon, item)

    def quit(self, icon=None, item=None):
        """退出托盘 = **真的退干净**（人 2026-11-13：「每次退出都退不干净」）。

        以前的毛病：只 `icon.stop()` 一下，守护的 `tray` 任务 60 秒后又把托盘拉起来，
        人看到的就是「退不掉」。现在三步：
          ① 写 `ops\\tray-off` —— 守护看到它就不自动拉（托盘自己启动时清掉）；
          ② 杀掉自己的子进程（翻页门 vault_gate 等）；
          ③ 停图标 + `os._exit` 兜底（pystray 的后台线程不会自己停）。
        """
        import subprocess as _sp
        self.stop_flag = True
        if not READONLY:
            try:
                with io.open(TRAY_OFF, "w", encoding="utf-8") as f:
                    f.write("%s 人点「退出托盘」（守护不要再自动拉）\n"
                            % time.strftime("%Y-%m-%d %H:%M:%S"))
            except OSError:
                pass
        kids = []
        try:
            import persona_proctree as _pt
            me = os.getpid()
            kids = [pid for pid, v in _pt.snapshot().items() if v.get("ppid") == me]
        except Exception:                  # noqa: BLE001
            kids = []
        for pid in kids:
            try:
                _sp.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                        capture_output=True, timeout=10,
                        creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0))
            except Exception:              # noqa: BLE001
                pass
        try:
            if self.icon is not None:
                self.icon.stop()
        except Exception:                  # noqa: BLE001
            pass
        _p("已退出托盘（ops\\tray-off 已写，守护不会自动拉起；子进程清了 %d 个）" % len(kids))
        time.sleep(0.4)
        os._exit(0)


def _self_sig():
    """本脚本的「版本指纹」：mtime + 大小。改了就有变化。"""
    try:
        st = os.stat(os.path.abspath(__file__))
        return (int(st.st_mtime), st.st_size)
    except OSError:
        return (0, 0)


def _autoreload_on():
    """脚本一改，托盘自己换新代码（2026-11-14 人：「天天在修，每次都要先部署它」）。
    默认开；只读模式关（只读不该重启自己）。想关：`ops\\persona.conf.json` 里 `"tray_autoreload": 0`。"""
    if READONLY:
        return False
    try:
        with io.open(os.path.join(__import__("persona_ops").dir(), "persona.conf.json"), encoding="utf-8") as f:
            return int(json.load(f).get("tray_autoreload", 1)) == 1
    except Exception:                      # noqa: BLE001
        return True


def _relaunch_self():
    """延迟 2 秒拉起一个新的自己（等旧进程放开互斥量），旧进程随后退出。"""
    import subprocess
    helper = ("import subprocess,sys,time;time.sleep(4);"
              "subprocess.Popen([sys.executable, r'%s'], cwd=r'%s', "
              "creationflags=0x08000000, close_fds=True)" % (os.path.abspath(__file__), ROOT))
    try:
        subprocess.Popen([sys.executable, "-c", helper], close_fds=True,
                         creationflags=0x08000000 | 0x00000008,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    except Exception as e:                 # noqa: BLE001
        _p("重启自己失败：%s" % e)


def _self_check():
    r"""`--check`：不建图标，只把「起不来」的原因一条条打出来（diagnose without GUI）。"""
    ok = True
    print("托盘自检 %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    print("  python      :", sys.executable)
    print("  argv        :", sys.argv)
    for name in ("pystray", "PIL", "PIL.Image", "PIL.ImageDraw"):
        try:
            __import__(name)
            print("  import %-12s OK" % name)
        except Exception as e:             # noqa: BLE001
            ok = False
            print("  import %-12s FAIL %s" % (name, e))
    for name in ("persona_tray", "persona_io", "persona_store", "persona_root"):
        try:
            __import__(name)
            print("  import %-12s OK" % name)
        except Exception as e:             # noqa: BLE001
            ok = False
            print("  import %-12s FAIL %s" % (name, e))
    try:
        import persona_store as _s
        print("  机器 id     :", _s.machine_id())
        print("  运行态目录  :", _s.runtime_dir())
        print("  会话目录    :", _s.save_dir("sessions"))
    except Exception as e:                 # noqa: BLE001
        ok = False
        print("  persona_store FAIL:", e)
    print("  库根        :", ROOT)
    print("  互斥量状态  :", "已有实例" if mutex_alive() else "空闲")
    print("  日志位置    :", LOCAL_LOG if READONLY else LOG)
    print("\n结论：", "可以起（只差图标）" if ok else "上面标 FAIL 的就是起不来的原因")
    return 0 if ok else 3


def _dump_menu_cli():
    r"""`--dump-menu`：只把菜单树打到 stdout（不弹窗、不起图标、不抢互斥量、不拉门）。

    人 2026-11-15 要求：改完菜单要能"对树"。所以这里连诊断行一起打，
    水位数字必须看得见（否则又回到"只有图标颜色"）；有需要就 `| Out-File` 存档对账。
    """
    import persona_tray as tray_module
    t = Tray(tray_module)
    try:
        menu = t._menu()
    except Exception as e:                 # noqa: BLE001
        print("# 建菜单失败：%s" % e)
        return 3
    sc = t._selfcheck()
    print("# SysPersona 托盘菜单 · %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    print("# 诊断：DSH %s · 异常 %d 项 · 待归档 %s · 交接未完成 %s · 刷新 %g 秒 · 自检 %s"
          % ("在跑（%d 个进程）" % t._dsh_procs() if t._dsh_alive() else "没在跑",
             len(sc["fails"]),
             "?" if t._pending_archive() is None else t._pending_archive(),
             "?" if t._handoff_open() is None else t._handoff_open(),
             t.period, sc["at"] or "还没跑过"))
    print("# 状态第一行：%s" % t._waterline_line())
    print("# tooltip：%s" % t._tip_line())
    print("─" * 46)
    print(t._menu_txt(menu))
    return 0


def watch_waterline(icon):
    r"""水位通知：读 ops\<机器>\waterline.json，动作变了就弹一次（人 2026-11-15 定）。

    人：「以后托盘检测水位，到了，就发一个消息，然后你停止，等压缩，压缩完成，在唤醒」
    为什么放在托盘：托盘是唯一会弹窗的地方；守护只负责算和落盘，不骚扰人。
    """
    path = os.path.join(__import__("persona_ops").dir(), "waterline.json")
    npath = os.path.join(__import__("persona_ops").dir(), "waterline-notify.json")
    cpaths = _ledgers("compact.jsonl")     # 客户端层 + 机器根历史（2026-11-15 起）
    MIN_GAP = 900          # 同会话两次弹窗最小间隔（15 分钟）
    WAKE_MAX_GAP = 900     # 「压缩完成」只认 15 分钟内的那次「到水位」

    # 2026-11-15 12:49 人：「托盘现在一直提示水位」。
    # 真因：12:25 起 7 次通知、warn/wake 来回跳（12:32 warn 77% → 12:45 wake 46%
    # → 12:49 warn 75% → 12:50 wake 48%），每次真跨线都会真压缩，于是每条都算"动作变了"。
    # 去重不能只靠进程内变量：托盘一重启 last 就丢，立刻再弹一次。所以落盘 + 三道闸：
    #   ① 同一会话 15 分钟内只弹一条；② 3 分钟内真压过就不弹「到水位」；
    #   ③ 「压缩完成」只在本会话报过「到水位」且 15 分钟内才弹。
    def _note():
        try:
            with io.open(npath, encoding="utf-8") as f:
                return json.load(f) or {}
        except Exception:                    # noqa: BLE001
            return {}

    def _save_note(d):
        try:
            with io.open(npath, "w", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps(d, ensure_ascii=False))
        except Exception:                    # noqa: BLE001
            pass

    def _compact_recent(sec=180):
        """sec 秒内有没有真的压过（compact.jsonl 里的 fired/compressed/done）。"""
        for _cp in reversed(cpaths):           # 先客户端层，再回落机器根/历史
            try:
                with io.open(_cp, encoding="utf-8") as f:
                    for ln in f.read().splitlines()[-40:]:
                        ln = ln.strip()
                        if not ln:
                            continue
                        e = json.loads(ln)
                        if (e.get("action") or "") in ("fired", "compressed", "done"):
                            at = time.mktime(time.strptime(e.get("at") or "", "%Y-%m-%d %H:%M:%S"))
                            if time.time() - at < sec:
                                return True
            except Exception:                    # noqa: BLE001
                continue
        return False

    while True:
        try:
            with open(path, "r", encoding="utf-8") as f:
                d = json.load(f)
            act = d.get("action")
            sid = d.get("session") or ""
            pct = float(d.get("pct") or 0)
            now = time.time()
            st = _note()
            same = (st.get("session") == sid)
            last_at = float(st.get("at_epoch") or 0)
            last_act = st.get("action") or ""

            fire = None
            if act == "warn":
                if ((not same) or (now - last_at >= MIN_GAP)) and not _compact_recent():
                    fire = "warn"
            elif act == "wake":
                if same and last_act == "warn" and (now - last_at) <= WAKE_MAX_GAP:
                    fire = "wake"

            if fire == "warn":
                icon.notify(
                    "会话到水位了（%.0f%% · %d tokens / 上限 %d）。这条发完就停手，"
                    "等压缩约 1 分钟再继续。"
                    % (pct, d.get("tokens", 0), d.get("limit", 0)),
                    "SysPersona · 水位")
            elif fire == "wake":
                icon.notify("压缩完成，可以继续了。", "SysPersona · 水位")
            if fire:
                _save_note({"session": sid, "action": fire, "at_epoch": now,
                            "at": time.strftime("%Y-%m-%d %H:%M:%S"), "pct": pct})
                _p("水位通知：%s（%.1f%%）" % (fire, pct))
        except Exception:                        # noqa: BLE001
            pass
        time.sleep(20)


def run_all(tray_module=None):
    if "--check" in sys.argv:
        return _self_check()
    # 2026-11-15 人要求：改完菜单要能对树 → 只打印菜单，不起图标、不抢互斥量、不拉 I/O 门
    if "--send-inbox" in sys.argv:
        # 主窗口按钮走这条：写请求行给插件 persona-inbox（不抢键盘）
        i = sys.argv.index("--send-inbox")
        msg = sys.argv[i + 1] if len(sys.argv) > i + 1 else "/compact"
        try:
            import persona_inbox_client as _C
            ok, out = _C.send(msg, kind="main-window")
        except Exception as e:                  # noqa: BLE001
            ok, out = False, str(e)[:200]
        print(("OK " if ok else "FAIL ") + str(out)[:200])
        return 0 if ok else 1
    if "--status-text-out" in sys.argv:
        # 状态窗点「立即刷新」时跑这一条：重算正文、覆盖同一个文件，然后退出。
        i = sys.argv.index("--status-text-out")
        out = sys.argv[i + 1] if len(sys.argv) > i + 1 else ""
        try:
            import persona_tray as _tm
            _t = Tray(_tm)
            body = _t._status_text()
        except Exception as e:                  # noqa: BLE001
            body = "算状态时出错：%s" % str(e)[:300]
        if out:
            try:
                io.open(out, "w", encoding="utf-8", newline="\n").write(body)
            except OSError as e:                # noqa: BLE001
                print("写不进：%s" % e)
                return 2
        else:
            print(body)
        return 0
    if "--dump-menu" in sys.argv:
        return _dump_menu_cli()
    if not claim_mutex():
        _p("已有一个托盘在跑（互斥量），本实例退出")
        # 2026-11-14 人问「本机托盘怎么起不来？」——真相是旧的那个还占着互斥量，
        # 新实例静默退出，看起来像"点了没反应"。所以**人手双击时**要弹窗说清楚；
        # 但守护每 120 秒的 --ensure 是无人值守的，弹了就是每两分钟一次骚扰，
        # 所以那种情况只记日志。
        # 2026-11-14 人：「现在还有弹这个」——重复启动一律静默退出。
        # 只有显式带 --announce 才弹（调试时用），--ensure 与默认都静默。
        if "--announce" not in sys.argv or os.environ.get("SYSPERSONA_NO_UI") == "1":
            return 0
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                None,
                "托盘已经在跑了（右下角那个图标）。\n\n"
                "如果你是想让它载入新代码：\n"
                "  1) 右键那个图标 → 退出托盘\n"
                "  2) 再双击「启动 SysPersona 托盘」\n\n"
                "（双击启动不会杀掉正在跑的实例，这是故意的——免得把正在干活的那个打断。）",
                "SysPersona · 托盘已经在跑", 0x40)
        except Exception:                  # noqa: BLE001
            pass
        return 0
    # 我自己起来了 = 该自动拉（人点过「退出托盘」留下的标记到此作废）
    try:
        if os.path.exists(TRAY_OFF) and not READONLY:
            os.remove(TRAY_OFF)
        if os.path.exists(STACK_OFF) and not READONLY:
            os.remove(STACK_OFF)
            _p("清掉 stack-off 标记（人重新启动，全家退出作废）")
            _p("清掉 ops\\tray-off（这次是正常启动，守护可以继续保活）")
    except OSError:
        pass
    if tray_module is None:
        import persona_tray as tray_module
    t = Tray(tray_module)
    try:
        t.build()
    except Exception as e:                 # noqa: BLE001
        _p("build 失败：%s" % e)
    try:
        t.icon.run_detached()
    except Exception as e:                 # noqa: BLE001
        _p("图标起不来：%s" % e)
    threading.Thread(target=watch_waterline, args=(t.icon,), daemon=True).start()
    # 静默自动更新（2026-11-15 人定）：30 秒后第一次，之后每 5 分钟；不进菜单、不弹窗
    threading.Thread(target=t.watch_updates, daemon=True).start()
    _p("图标已就绪（互斥量 %s）" % ("可连" if mutex_alive() else "?"))
    t.loop()
    return 0


if __name__ == "__main__":
    _pio.setup_stdio()  # 管道捕获时别因 ⚠ 炸掉（v6.13.0）
    raise SystemExit(run_all())
