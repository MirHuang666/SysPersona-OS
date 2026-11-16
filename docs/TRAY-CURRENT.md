# 托盘 · 现行状态（TRAY-CURRENT）

> 库里 `README.md` 指向本页；**规格与实现冲突时以本页 + `tools/tray/` 代码为准**。
> 最后更新：2026-11-16 21:00（本机实测）。

## 一、一句话现状

**托盘已退休**（人 2026-11-16：「托盘退休了，现在换插件了」「没有托盘程序了，全靠启动项拉起，所有功能做插件」）。

- 启动项：`SysPersona-Tray.lnk` → 已改名 `.off`（不删，可恢复）
- 计划任务：`SysPersona-Daemon` → **Disabled**
- 运行中的托盘进程：**无**
- 功能去向：全部由 `@syspersona/persona-core` 插件 + 库内脚本承担

## 二、托盘原来的活，现在谁干

| 托盘原来的能力 | 现在谁做 | 备注 |
|---|---|---|
| 开机自启客户端 | 启动项 `SysPersona-DSH.lnk` | 登录即拉客户端 |
| 保活（客户端崩了拉起） | 插件（客户端崩了插件也没了）→ **需要时启回守护** | 这是托盘退休后唯一的真空，见 §三 |
| 状态面板（自检/水位/花费） | 库内 `selfcheck.json` + `persona_selfcheck.py`；界面上用 `dsh-cost-meter` / 任务板插件 | 托盘那套显示不再需要 |
| 动作入口（压缩/重读/重启/模型） | 会话里直接下命令（`persona_entry.py` 分支 + 脚本） | 不再需要点菜单 |
| 心跳/空闲唤醒 | 插件 C 段（随机 + 概率沉默） | 比托盘心跳更省 |
| 永不睡眠 / 网络 / 加密 / 余额 | **不做**（人 2026-11-16：这些不需要） | 需要时再启回托盘 |

## 三、唯一真空：客户端崩了没人拉

插件活在客户端进程里，**客户端死了插件也死**。所以：

1. 应急预案：启回 `SysPersona-Daemon` 计划任务（`--ensure` 每 5 分钟）＋ 恢复 `SysPersona-Tray.lnk`（可只用来保活）；
2. 或者加一条“看门狗”启动项：只做“客户端不在就拉起”，不做界面。

这一条尚未落地，已记进本机待办（"换机清单/保活"相关那条）。

## 四、不自己做的（用现成的）

| 类别 | 用谁的 | 为什么 |
|---|---|---|
| IM（微信/飞书/企微/钉钉/QQ…） | `@xmanrui/dsh-im` | 协议多变，官方/社区维护更好 |
| 花费与余额 | `dsh-cost-meter` | 170+ 模型价格目录它维护 |
| 桌面自动化 | `dsh-windows-mcp` | 系统接口，别人维护 |
| 任务看板 | `@linxin666/dsh-client-ui-task-board` | UI 层，官方生态维护 |
| 督促/到点催 | 不做 | 子程序少，再加就是三路同时叫 |

## 五、要恢复托盘时怎么做（幂等）

```powershell
# 1) 启动项恢复
Rename-Item "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\SysPersona-Tray.lnk.off" SysPersona-Tray.lnk
# 2) 计划任务启用
Enable-ScheduledTask -TaskName SysPersona-Daemon
# 3) 清掉“人主动退出”的标记（否则守护不会自动拉起）
Remove-Item C:\SysPersona\ops\<机器id>\stack-off, C:\SysPersona\ops\<机器id>\tray-off -ErrorAction SilentlyContinue
# 4) 起托盘（幂等，不弹窗）
C:\SysPersona\SysPersona-Runtime\python\pythonw.exe C:\SysPersona\脚本\persona_tray_multi.py --ensure
```

**开关在配置里**：`ops\<机器id>\persona.conf.json` 的 `tray: 0/1`、`trayguard: 0/1`——
0 时守护的两个托盘任务直接跳过、自检的 `tray` 项也跳过（不判红）。

## 六、历史

托盘完整设计（菜单、网络、加密、余额、退出全家停等）见 `docs/TRAY_SPEC.md`；
库里还有 `文档\托盘功能一览.md`、`文档\托盘与插件-分工.md` 作为过程资料。
