# v6.5.0 — 从「寄生社区版」改成「接官方壳 + 接管接口」

> **历史快照**：以下是当时的记录，保留原貌，不随现状修改。现在的实现与菜单以 [`README.md`](../README.md) 和 [`docs/USAGE.md`](USAGE.md) 为准。


## 范围

交付形态又动了一次：**不再寄生社区版，改为接官方 Hermes Agent，并通过它的 JSON-RPC/WS 网关接管派发与压缩。**

上一版为什么废：社区版把网关阉割了。它只剩一个能点的界面，没有任何可调用的请求入口——外部程序接不进去，只能靠人在窗口里点，也就注定要"到处乱开会话"去凑上下文。

## 三件套（边界写死）

| 件 | 现在是谁 | 约定 |
|---|---|---|
| 壳 | **Hermes Agent 官方版**（Electron + Python，本地 SQLite + FTS5，自带 JSON-RPC/WS 网关） | 只读不托管；记忆仍是纯文件，两边随时可拆 |
| 脑 | **DeepSeek 官方** `deepseek-flash`（1M 窗口；约 ¥1.1 输入 / ¥4.4 输出 每百万 token） | 判断型任务不降级给免费小模型 |
| 检索 | 库内索引 + `zsearch`（本地） | 不出门 |

## 为什么这次能装成（前几次失败的三个原因）

| 卡点 | 现象 | 解法 |
|---|---|---|
| 安装脚本把 `HERMES_HOME` 写死在 `start-here.cmd`，安装目标只认 `%LOCALAPPDATA%` | 装到 D 盘静默失败 | 直接用 `offline-install.ps1 -HermesHome D:\ProgramData\Hermes` |
| 依赖要联网下 Python / uv / node | 国内下载卡死 | 全离线包自带 wheels；`UV_PYTHON_INSTALL_MIRROR` 走镜像后 39 KB/s → **6.44 s** |
| 更新渠道默认「每个提交（main）」 | 等于天天跑测试版 | 切「稳定版发布」 |

## 接口实测（官方版才有）

| 方法 | 用途 | 结果 |
|---|---|---|
| `session.context_breakdown` | 读上下文水位 | ✅ 15,811 / 1,048,576，底座开销 16.7K |
| `session.history` | 取整段原文 | ✅ 用于镜像 |
| `session.compress` | 压缩 | ✅ 通，但见下面那条坑 |
| `session.active_list` | 会话与空闲状态 | ✅ 用于"人在聊就不许压" |
| `prompt.submit` / `hermes -z` | 派发 | ✅ 12–15 秒往返，纯文本回 |

## 压缩的规矩（本版最要紧的一条）

**先落盘，再压缩；而且只认实测降幅。**

顺序固定为：水位到 **300K**（`persona_limits.MIRROR`）→ 导出整段原文 + sha256 回读校验 → 调 `session.compress` → 再用 `context_breakdown` 比对前后。

**坑：`session.compress` 会假报成功。** 实测一条 2 条消息的会话，返回 `status: "compressed"`，但 `removed: 0`、`before_tokens == after_tokens`、水位一点没降。按 `status` 判成败就会在该压的时候误报"压好了"。所以判定只看数字：**没降就记 `unconfirmed`，绝不记成功**。

自动压缩仍然开着，但只当保底（阈值拉到 0.8 ≈ 838K），只在我们的主动压缩失效时才兜底。

## 设定文件：库是源，SOUL.md 是产物

`配置/内核/kernel.json` 是设定的唯一源；`脚本/persona_soul.py` 把它渲染成 `HERMES_HOME\SOUL.md`。

- 头部写源文件 sha256，**幂等**：设定没变时重复运行不改文件
- 覆盖前自动备份 `SOUL.md.bak-<时间戳>`
- 以后只改库里的内核，Hermes 那边零重读成本

## 本机事实（不改也会被反复问）

| 项 | 值 | 结论 |
|---|---|---|
| 显卡 / 内存 | GTX 960 **2 GB** / 15.9 GB | 本地模型不可行：列表里最小的 27B 要 16.2 GB；能跑的只有 1–4B，扛不住读文件跑命令 |
| 内存占用 | Hermes 全套 ≈ 0.68 GB，旧壳 ≈ 2.07 GB | 换壳顺带省 1.4 GB |
| 托盘 | 默认关闭，需在设置里开「最小化到托盘」 | 开了才会"关窗口 = 收起"，否则就是退出 |

## 未完成

- 微信 / QQ 通道（走腾讯 iLink Bot 与 QQ 开放平台，均为官方 API，已确认不需要公网 IP）
- 余额显示进托盘（DeepSeek 官方接口可查；硅基那个接口 410 已下线）
