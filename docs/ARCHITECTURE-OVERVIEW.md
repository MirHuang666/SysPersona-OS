# 架构总览：两种形态并存

> 本页会随勘误就地更新。规范全文以 `main` 为准。

## 一句话

**同一套「库 + 记忆 + 判据」底座，两种运行形态**：

- **形态 A · 托盘/启动器**：独立常驻托盘进程，负责拉起客户端、自检、节点网络、状态小窗；
- **形态 B · 单一插件**：DSH 插件 `@syspersona/persona-core` 内部把接口全接住，节点服务也在插件里。
  **它是寄生形态**——活着靠 DSH：跑在 DSH 进程里，用 DSH 的 loader、patch 层、`ctx.agents`、`ctx.compaction`。

两套都在仓库里保留，都是**可部署的**。2026-11-16 本机从 A 切到 B，理由见「为什么并存、为什么切换」。

## 对照

| | 形态 A · 托盘/启动器 | 形态 B · 单一插件 |
|---|---|---|
| 存在方式 | **独立**：自己的进程，进程外拉起/探测客户端 | **寄生**：DSH 进程内的插件，靠它的 loader 与接口 |
| 组成 | `tools/tray/` 三个托盘脚本 + `persona-inbox` 插件 + lanbus | 一个插件 `persona-core`（A–H 段）+ `脚本/persona_deploy.py` |
| 谁在常驻 | 独立的 `pythonw` 托盘进程 | DSH 客户端进程本身 |
| 拉起客户端 | 托盘 watchdog | 计划任务 `SysPersona-Client`（每 5 分钟，DSH 单实例） |
| 节点服务 | `persona_lanbus.py`（8737） | 插件内置 HTTP（8738），host/member 两种角色 |
| 设置在哪 | 托盘菜单 + `ops/tray-clients.json` | 插件设置页 + `ops/<机器>/persona.conf.json` |
| 优点 | 不依赖客户端；客户端崩了托盘还在 | 一处配置、一处日志、一处部署；跨机同构 |
| 代价 | 两套代码要同时维护；托盘图标随 explorer 重启掉；跨机配置容易不一致 | 客户端不跑，节点就离线；插件随 DSH 版本走 |
| 现状（2026-11-16） | **保留、可部署**；本机不再跑托盘（改用插件），但代码与规格已同步到最新实现 | **本机正在用** |

**怎么选**：用 DSH 的，直接用插件（形态 B）；要「不依赖客户端、独立常驻」的，用托盘（形态 A，现行实现见 [`TRAY-CURRENT.md`](TRAY-CURRENT.md)）。


## 寄生形态与独立形态（本质区别，不是优缺点）

| | 托盘（独立） | 插件（寄生） |
|---|---|---|
| 靠什么活 | 自己是一个进程；用进程探测、文件、HTTP 跟别人打交道 | 宿主（DSH）给的 loader、patch 层、`ctx.agents`、`ctx.compaction` |
| 宿主换版 | 影响小（它不认识 DSH 的内部） | **可能直接断**：接口改名/收紧/标 experimental，插件就静默失效 |
| 能碰到什么 | 只能从外面看（进程在不在、窗口在不在、文件写没写） | 能**直接**投递进会话、触发压缩、读会话列表——托盘做不到 |
| 失效表现 | 图标不在 / watchdog 不拉 | 插件不加载（`core-tick` 无 `plugin_start`）、或加载了但接口调用抛 `inactive context` |
| 该防什么 | explorer 重启、多实例、rev 落后 | **DSH 升级**：每次升级都要重验；官方把某个接口标 experimental，就得留后手 |

所以取舍不是"哪个更好"，而是**能力 vs 耐久**：寄生形态能直接改会话，代价是把命交给宿主版本；
独立形态耐久，代价是只能从外面看。**要"AI 真的能自己动"，就得寄生；要"客户端崩了还能自愈"，就得独立。**
这也是为什么两套都留着：不是过渡，是两种必要的存在方式。

**由此沿出的规矩**：
1. 插件里凡是依赖 DSH 内部接口的地方（`ctx.compaction`、`ctx.agents`、`ctx.on('dispose')`），
   **必须能在失效时自己报出来**（如 `compact_busy` / `inactive context` 落进 `core-tick.jsonl`），
   不能静默——今天已经踩过两次"报成功没产物"；
2. DSH 每次升级后要重验一遍插件（最小判据：`plugin_start` 有、`/health` 通、`/clients` 200、压缩能压一次）；
3. 托盘那份**不要跟着 DSH 改动去改**，它本来就该对 DSH 无感。

## 共同底座（两套形态都依赖，不随形态变）

| 层 | 位置 | 作用 |
|---|---|---|
| 库 | `D:\<机器>-文件\AI会话检索记录`（`C:\SysPersona` 联接） | 脚本、配置、记忆、会话、续接 |
| 索引 | `index/L1.jsonl` + `fts.db` | 会话/文档可检索；`sessions/` 也在扫描范围内 |
| 自检 | `脚本/persona_selfcheck.py` | 30 项，三态 `ok / skip / fail` |
| 归档 | `脚本/persona_archive.py`、`tools/js/dsh_export.js` | 会话落盘（压前必归档） |
| 交接 | `脚本/persona_handoff.py`、`ops/<机器>/handoff.jsonl` | 做了什么、还差什么 |
| 版本 | `配置/版本.json`（全局只读）+ `ops/<机器>/version-local.json` | 谁在用的那台才 `publish` |

**判据只有一条：看产物，不看日志说 OK。**
例：自检真跑 = `ops/<机器>/core-selfcheck.lock` 的 mtime；任务真跑 = `core-tasks-state.json` 的 mtime；
压缩真压 = 水位掉且有 `compact_ok`；成员真报到 = 主机 `node-members.json` 里出现它。

## 能力落在哪

| 能力 | 形态 A | 形态 B |
|---|---|---|
| 往会话投递消息 | `persona-inbox` 插件（已并入 B） | 插件 A 段 |
| 定时自检 | 托盘「程序自检」 | 插件 B 段 |
| 空闲唤醒（自发开口） | 托盘「空闲唤醒」 | 插件 C 段（若装了 `dsh-iter-memory`，则关掉 C 段，见下） |
| 定时任务 | 托盘任务表 | 插件 D 段（`persona_daemon.py --once <任务>`） |
| 压缩后回填 | 无 | 插件 E 段 |
| 归档 / 压前归档 | 托盘 + `persona_archive` | 插件 F/G 段 |
| 压缩闸门（先归档再压） | 无 | 插件 G 段（`ctx.compaction.compactNow`） |
| 节点服务 | `persona_lanbus`（8737） | 插件 H 段（8738） |

## 节点（形态 B 里长什么样）

同一份插件，两种角色，只差一个设置：

| 角色 | 设置 | 行为 |
|---|---|---|
| host | `nodeHub` 留空 | 库/归档全写本地；对外提供查库、跑任务、收档 |
| member | `nodeHub: http://<host>:8738` | 不跑索引、查主机；归档**传到主机 → 校验 sha256 一致 → 删本地** |

接口：`/health`、`/lib/tree`、`/lib/file`、`/lib/query`、`/lib/session`、`/lib/put`、`/lib/received`、
`/clients`、`/job/run`、`/job/list`、`/msg`、`/node/heartbeat`。

**保底**：成员上传失败 → 进 `ops/<机器>/node-outbox.jsonl` → 主机一通就自动补传；
**没有回执就不删本地**（红线：不许因为传输问题丢会话）。

**跨机投递 `/msg` 是机对机用的**：直连对方 IP + 会话 id，不做「主机代发 / 选目标」的转发（人 2026-11-16 定）。

## 部署

```powershell
# 体检（不写任何东西）
python 脚本\persona_deploy.py --check

# 装成节点（host）
python 脚本\persona_deploy.py

# 装成普通机器（member），并从主机取插件代码
python 脚本\persona_deploy.py --role member --hub http://192.168.1.14:8738 --from <主机那份插件目录>
```

它做四件事，全部幂等：环境变量（`SYSPERSONA_ROOT`/`PERSONA_PY`/`PERSONA_NODE`）、`C:\SysPersona` 联接、
插件安装（node_modules 联接 + `package.json` bundles + 本机 `ops/<机器>/persona.conf.json` 的节点键）、
计划任务（`SysPersona-Daemon` 5 分钟 `--ensure`、`SysPersona-Client` 5 分钟拉起客户端）。
防火墙只检测不擅自提权，没规则就打印该跑的那一条。

## 为什么并存、为什么切换

- **留着 A**：它是「客户端崩了还能自愈」的形态，在只跑客户端不可靠的场景仍成立；文档与代码都在，随时可切回。
- **切到 B 的理由**（2026-11-16 人定，原话「把节点也做成插件吧，本来就需要一个客户端维护」）：
  1. 两套代码要同时维护，改一处要同步两处，实际结果是**两边不一致**；
  2. 托盘图标随 explorer 重启掉，重启后要人再点一次；
  3. 跨机配置容易各写各的（同一件事在两台机器上两套值）；
  4. 维护本来就要一个客户端，节点跟着插件走反而少一层。
- **B 的已知代价**（写在这里，不藏）：客户端不跑，节点就不在线；插件随 DSH 版本走，DSH 升级要跟着验。


## 插件版本

插件自己是**独立版本**，不和库版本（`配置/版本.json`）混：库版本管「脚本/配置/记忆」的整体，
插件版本只管 `persona-core` 这一个包。

| 项 | 规则 |
|---|---|
| 版本号 | `YYYY.MM.DD-HHMM`（时间戳式）。原因：这些天一天改十几版，"语义版本"没有意义；时间戳能直接对上"哪天的哪一版" |
| 权威位置 | 插件包里的 `package.json` 的 `version`（库内 `配置/客户端/plugins/persona-core/package.json`） |
| 装载判据 | 客户端启动后 `ops/<机器>/core-tick.jsonl` 里有一条 `kind:"plugin_start"`，带 `version` / `pid` / `root` / `node` / `py` —— **没这条就是没装上**，别信"已安装" |
| 升级方式 | 改库内插件包 → 目标机的插件目录同步（`persona_deploy.py --from`）→ **重启客户端**才装载 |
| 当前版本 | `2026.11.16-2023` |

**为什么不用语义版本**：插件还在一日多改的阶段，`v0.3.1` 这种号既不能说明"改了什么"，也不便对上现场；
时间戳 + `plugin_start` 留痕能让"这台机器跑的到底是哪一版"一眼可查。等接口稳定了再切语义版本，
切换时在 `CHANGELOG.md` 写一条换算（时间戳 → vX.Y.Z），历史版本号不改。

**和托盘版本的关系**：两套形态各有自己的版本（托盘看 `applied-rev.json` 与 `配置/版本.json`，
插件看 `package.json` + `plugin_start`）；**同一台机器只该有一套在跑**，否则版本会各说各话。

## 文档导航

**形态 A（托盘/启动器）**：**现行实现先读 [`docs/TRAY-CURRENT.md`](TRAY-CURRENT.md)**（2026-11-16 与代码同步）；
设计来源与历史：`docs/LAUNCHER_SPEC.md`、`docs/MIDDLEWARE_LAUNCHER.md`、`docs/TRAY_SPEC.md`、`docs/SERVICE.md`、`docs/CLIENT_ADAPTER.md`。

**形态 B（插件 + 节点）**：本页；实现见 `配置/客户端/plugins/persona-core/README.md`（库内）。

**两套共用**：`docs/ARCHITECTURE-v0.1.md`（底座与哲学）、`docs/MEMORY_NET_SPEC.md`、
`docs/PERSONA_SPEC.md`、`docs/RELEASING.md`、`CHANGELOG.md`。

## 变更记录

- **2026-11-16**：新增本页。本机由形态 A 切到形态 B；A 的规格与代码保留、不删。
  节点服务从 `persona_lanbus`（8737，只搬白名单产物）改为插件内置（8738，可查库、可跑任务、可收档），
  起因是人指出「前面做的节点只是链接了，但访问库还没有实现」。
