# 来源与许可证台账（PROVENANCE）

> 本页记录**我们吸收过哪些外部项目的什么部分**，以及各自的许可证与义务。
> 目的有两个：后期能追溯；发布前能一眼判断"能不能发"。
> 规则：**吸收前先看许可证**。没有明确许可、或禁止衍生的，一律不吸收。

## 一、现行插件 `@syspersona/persona-core`

| 来源 | 版本 | 许可证 | 吸收了什么 | 落在 |
|---|---|---|---|---|
| [dsh-iter-memory](https://github.com/MingHuanYue/dsh-iter-memory) | 1.2.0 | MIT · (c) 2026 dsh-tide-memory contributors | 潮位状态机（半衰期回落）、裁决链、四段处境注入、默认参数 | 插件 C 段 |
| [dsh-tide-memory](https://github.com/MingHuanYue/dsh-tide-memory) | 0.2.0 | MIT · (c) 2026 dsh-tide-memory contributors | 存储格式（`stores/*.jsonl`、`cold/`、行字段）、四个工具名与入参、证据分/鲜活度、去重与取代留链 | 插件 I 段 |

**许可义务**：MIT 要求**随包保留版权声明与许可文本**。已落实：
- 插件包内 `licenses/MIT-dsh-iter-memory.txt`、`licenses/MIT-dsh-tide-memory.txt`；
- 插件包内 `THIRD-PARTY-NOTICES.md`（列明吸收范围与我们的改动）；
- 插件 `index.js` 的 C 段、I 段段头注明来源；
- `package.json` 增加 `license: UNLICENSED`（本插件自身不对外授权）与 `thirdParty` 清单。

## 二、只参考思路、未复制代码

| 项目 | 参考了什么 | 说明 |
|---|---|---|
| `dsh-tide-whisper`（潮声） | 空闲唤醒的"随机沉默"节拍 | 我们 C 段最初照它做；现已被 C 段的潮位版取代 |
| `dsh-session-kit` | 会话导出/归档的组织方式 | 只参考结构 |
| `dsh-keep-going` | 断线续跑的思路 | 只参考结构 |

详见库内 `文档\插件-能力来源.md`。

## 三、发布判断（写死）

| 情况 | 能不能发 |
|---|---|
| 吸收的是 MIT / Apache-2.0 / BSD 这类许可 | **能发**，条件是把版权声明与许可文本随包带上（本页 §一 的做法） |
| 吸收的是 GPL / AGPL | **要谨慎**：会把整个插件拖进传染性许可，发布前必须单独评估 |
| 吸收的是"源码可见但禁止修改/商用/衍生" | **不能发**（只能自用） |
| 吸收的是**没有许可证**的代码 | **不能吸收，也不能发**（默认保留所有权利） |

**发布前检查清单**：① `licenses/` 每个来源都有原文；② `THIRD-PARTY-NOTICES.md` 与实际吸收范围一致；③ 代码段头有来源；④ 新增吸收项已在本页登记。

## 四、已退役、退回到归档的插件

| 插件 | 退役时间 | 归档位置 | 原因 |
|---|---|---|---|
| `persona-inbox` | 2026-11-16 | 库 `归档\插件-已并入-persona-core\persona-inbox` | 能力并入 `persona-core` A 段 |
| `dsh-iter-memory` | 2026-11-16 | 库 `归档\插件-已退役\iter-memory-20261116-2103` | 吸收进 C 段；详见其 `为什么退役.md` |
| `dsh-tide-memory` | 2026-11-16 | 库 `归档\插件-已退役\tide-memory-20261116-2110` | 吸收进 I 段；同上 |

## 五、变更记录

- **2026-11-16**：建本页。同日吸收 `dsh-iter-memory`（C 段）与 `dsh-tide-memory`（I 段），
  两者均为 MIT；落实包内许可证副本与归属声明。
