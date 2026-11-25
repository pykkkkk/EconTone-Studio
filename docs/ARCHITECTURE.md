# 架构说明

EconTone Studio 分成三层：宿主（Node）、客户端（浏览器）、引擎桥（Python）。

```
┌────────────────────────── DSH 进程 ──────────────────────────┐
│  index.js（宿主）                                             │
│   ├─ HTTP 接口：/run /run/refresh /history /state /skills ... │
│   ├─ 运行调度：spawn python engine/studio_bridge.py           │
│   └─ 状态文件：<workspace>/.econtone/{state,binding}.json      │
└───────────────┬──────────────────────────────────────────────┘
                │ spawn（子进程，JSON over stdout）
┌───────────────▼──────────────┐        ┌────────────────────────┐
│ engine/studio_bridge.py      │        │ ~/.dsh/skills/*        │
│  调用 econtone-score 等脚本   │───────▶│  score/polish/prosody  │
└───────────────┬──────────────┘        │  ai-trace/equilibrium  │
                │ 运行产物               └────────────────────────┘
┌───────────────▼──────────────┐
│ <workspace>/.econtone/runs/  │  meta.json · run.json · input.md
└──────────────────────────────┘
                ▲
┌───────────────┴──────────────┐
│ lib/client.js（面板 UI）          │  fetch /econtone-studio/api/*
└──────────────────────────────┘
```

## 一次运行的流程

1. `POST /run` 创建运行目录 `runs/<runId>/`，写入 `input.md` 与 `meta.json`；
2. 宿主按参数选择引擎：
   - 博弈开启 → `equilibrium` 编排（需要一个博弈模型，如 `stack` / `nash`）；
   - 博弈关闭 → 单技能（`polish` / `ai-trace` / `prosody` 或任意本地 skill）；
3. 改写文本由当前 DSH 会话里的子 Agent 产出，宿主把它与引擎的测量结果合并；
4. 结果写入 `run.json`（评分卡、逐处改动、证书、润色稿）；
5. 客户端轮询 `GET /run?id=` 渲染评分卡、改动与证书。

## 数据契约

- `contracts/edit-unit.schema.json` 定义 `EditUnit` / `RunResult`；
- 运行结果核心字段：`score.rows[]`（四维评分）、`edits[]`（逐处改动）、`certificate`（博弈证书）、
  `finalText` / `baseText`（前后文本）、`draftId`（润色稿编号）。

## 评分模型

每张卡 0–100 显示，内部按 `score / max` 归一化后加权：

```
total = Σ(normalized_i × weight_i) / Σ weight_i
```

默认权重 `40 / 40 / 10 / 10`（对应四个维度的原始满分）。权重随运行参数保存，可在卡片下方调整。
结果落盘前会做一致性核对：P1 满分但列出语域问题时按条扣分，R1 按 RED/WARN 数量扣分。

## 接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 引擎健康检查 |
| GET | `/build` | 插件版本 |
| POST | `/run` | 发起评分/改写 |
| GET | `/run?id=` | 运行状态与结果 |
| GET | `/run/refresh?task=&draft=` | 按任务或润色稿编号取回最近结果 |
| GET | `/history` | 历史运行列表（含原文哈希与预览） |
| GET/POST | `/state` | 跨窗口同步的任务与参数 |
| GET | `/skills` | 本地 skill 列表（写作类过滤） |
| GET | `/page` | 独立页面 |
| POST | `/open-page` | 打开独立窗口 |

## 状态与持久化

- 任务与参数：`.econtone/state.json`，由客户端定期推送，宿主原样保存；
- 运行记录：`.econtone/runs/<runId>/`，`meta.json` 记录任务、编号与时间，`run.json` 记录完整结果；
- 客户端 localStorage 仅作缓存，冲突时以宿主状态为准。
