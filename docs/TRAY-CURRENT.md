# 现行托盘程序（形态 A · 最新实现）

> 本页会随勘误就地更新。规范全文以 `main` 为准。
>
> 本页描述的是**托盘程序当前的样子**，不是 2026-11-12 的旧版。
> 旧规格页（`LAUNCHER_SPEC.md` / `MIDDLEWARE_LAUNCHER.md` / `TRAY_SPEC.md`）保留作设计来源，
> 与实现不一致处以本页 + 代码为准。

## 什么时候用托盘，什么时候用插件

| 你的情况 | 用哪个 |
|---|---|
| 用 DeepSeek Harness（DSH），希望状态/自检/节点都跟着客户端走 | **插件**（形态 B，见 [`ARCHITECTURE-OVERVIEW.md`](ARCHITECTURE-OVERVIEW.md)） |
| 不跑 DSH，或要一个**独立常驻**的东西（客户端崩了还能拉起它） | **托盘**（本页） |
| 两台机器同时装 | 可以，但**不要两套都管同一件事**：开口/自检/节点各自只留一套（见下「并存的代价」） |

## 组成（`tools/tray/`）

| 文件 | 行数 | 作用 |
|---|---|---|
| `persona_tray_multi.py` | 3988 | **现行主程序**（多客户端 / 多机器） |
| `persona_tray.py` | ~1100 | 单机版（保留，简化场景可用） |
| `persona_tray_version.py` | ~120 | 「程序版本」小窗：全局版本 / 本机 rev / 是否跟随 |
| `persona_client_probe.py` | ~330 | 客户端探测：进程 + 窗口枚举，判「在不在跑」 |
| `persona_tray_clients.py` | ~900 | 客户端定义与拉起（多客户端版的一部分，历史遗留） |
| `persona_hermes_kernel.py` | ~250 | Hermes 时期的探测（Hermes 已下线，仅留档） |

## 菜单（现行七组，顺序即定稿）

```
状态（正常 / 异常 N 项）     双击图标同样进主窗口
程序自检 ▸                  自检周期 / 自动启动客户端 / 异常后自动唤醒修复 / 唤醒修复异常周期
空闲唤醒 ▸                  关闭（只检测）/ 静默 5·10·30·60 分钟后唤醒 / 唤醒后自动排序
节点网络 ▸                  网络与成员 / 创建 / 发现 / 管理
会话显示时长 ▸              30 分钟 / 1·2·4·12 小时
查看记录 ▸                  今日心跳 / 变更台账 / 全局台账 / 交接账
程序版本 ▸                  全局版本 / 本机 rev / 是否跟随
```

## 配置与状态

| 文件 | 说明 |
|---|---|
| `配置\客户端\tray-clients.json` | **真源**：客户端定义（exe / 探测方式 / auto_launch / keepalive） |
| `ops\<机器id>\tray-clients.json` | 本机运行态副本（读得快；改了要 `export` 回真源） |
| `ops\<机器id>\tray-multi.log` | 托盘自己的日志（图标消失、热键、菜单动作都在这） |
| `ops\<机器id>\uiskin.heartbeat` | 存活心跳（自检/别的脚本据此判「托盘在不在」） |
| `ops\<机器id>\applied-rev.json` | 已应用的全局 rev（**落后会导致静默自杀更新**，见坑 3） |
| `ops\<机器id>\client-launch.json` | 每次拉起客户端的证据（`{at, ok, result, target, by}`） |

## 启动方式

1. **开机自启**：计划任务 `SysPersona-Tray`（登录时启动 `pythonw persona_tray_multi.py`）——注意"计划任务"不是"启动文件夹"，后者在 UAC/多用户下不稳；
2. **拉起客户端**：托盘 watchdog 按 `tray-clients.json` 的 `auto_launch` 判定，拉起后写 `client-launch.json`；
3. **2026-11-16 起本机改法**：DSH 由计划任务 `SysPersona-Client`（5 分钟一次，单实例）拉起，托盘不再负责这一项 —— 这是「托盘 + 插件并存」时避免重复拉起的做法。

## 判据（看产物，不看日志说 OK）

| 想确认 | 看什么 |
|---|---|
| 托盘在跑 | `uiskin.heartbeat` 的 mtime（< 90 秒） |
| 客户端被拉起过 | `client-launch.json` 里有记录（`ok:true` 且 `at` 新） |
| 自检真的跑了 | 自检脚本写的产物（`ops\<机器id>\selfcheck.json`）的 mtime |
| 图标该显示 | `tray-multi.log` 有 "icon" / "TaskbarCreated" 相关行 |

## 已知坑（都踩过，别再踩）

1. **explorer 重启后图标消失**：必须处理 `TaskbarCreated` 消息重加图标；
2. **两个 explorer 进程**会让托盘 flyout 失效（`flyout` 点不开、菜单卡住）——不是代码 bug，先看进程数；
3. **`applied-rev.json` 落后**：托盘会以为自己该升级 → 拉起升级器 → 自己 `quit()` 写 `ops\tray-off`，表现是"图标每 30 秒死一次"。修法：把 `applied-rev.json` 记成当前全局 rev；
4. **`.ps1` 要 BOM、JSON 不要 BOM**：PowerShell `Set-Content -Encoding utf8` 写的 JSON 会带 BOM，DSH 会报「无法使用」；
5. **托盘和插件不要同时管开口**：插件的 C 段（空闲唤醒）与 `dsh-iter-memory` 会双重开口；同理托盘的空闲唤醒与插件 C 段也会。**同一台只留一套**。

## 与插件并存时的分工（推荐）

| 职责 | 托盘 | 插件 |
|---|---|---|
| 拉起客户端 | 可选（计划任务可替代） | 计划任务 `SysPersona-Client` |
| 自检 | 菜单可手动触发 | B 段每 120 秒 |
| 空闲唤醒 | 菜单里可关 | C 段（或交给 `dsh-iter-memory`） |
| 节点服务 | `persona_lanbus`（8737） | 插件 H 段（8738） |
| 状态展示 | 托盘图标 + 小窗 | 无界面（`/health`、`/clients`） |

**原则**：托盘负责"独立存活与可见"，插件负责"接口与数据"。同一件事只让一边做。

## 版本

- 托盘自身版本：`persona_tray_version.py` 读取本机 `applied-rev.json` 与全局 `配置\版本.json`；
- 版本逻辑与库一致：全局只读、本机跟随、**只有人在用的那台能 `publish`**；
- 本页与 `tools/tray/` 的代码同步于 **2026-11-16 20:3x**（`persona_tray_multi.py` 3988 行 / 198 KB）。
