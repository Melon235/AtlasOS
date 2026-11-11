# Stage G2b-0 — Multi-Worker Token / Context 效率诊断

只读诊断。未修改 `src/atlasos/`、AnyCode、测试、Fixture、Oracle、评估配置、G1 Handoff、G2a Budget Guard；未调用任何模型；未启动 Sandbox；未执行 Git 写操作。

数据来源：`experiments/results/stage-e1/e1-20261109T102030Z`（正式轮，36 配对）、`stage-e1/e1-20261109T101141Z`、`stage-g1/g1-20261110T103003Z`、`stage-g2a/g2a-20261110T112744Z`。
分析脚本：`experiments/evaluation/stage_g2b_diagnosis.py`；派生表：`experiments/results/stage-g2b-diagnosis/`。

---

## 1. Data Availability / 数据缺口

### 1.1 每个 run 目录里实际有什么

| 文件 | 内容 | E1 正式轮覆盖 |
|---|---|---|
| `result.json` | 运行级 OBSERVED 指标、`worker_results`（含 `stop_reason`、`token_usage`、`tool_calls` 计数）、完整 `tool_calls`（`input` 参数 + `output` 截断至 4000 字符）、oracle 判定 | 71/72 |
| `run.json` | 环境、配置摘要、`tool_runtime_metrics`（原生工具名、dispatch 计数） | 71/71 |
| `tool_conversations.json` | 每个 task 的**turn-boundary 会话快照**（真实消息：text / tool_use / tool_result） | 71/71 |
| `trajectory/<run_id>/transcript.jsonl` | M7 轨迹：`worker_lifecycle`、`sandbox_lifecycle`、`action_proposed`、`action_decision`、`tool_observation`、`execution_outcome` | 71/71 |
| `workers.json` | Worker 实例记录（profile、assignment） | 71/71 |
| `oracle.log` | 隐藏测试输出 | 71/71 |

### 1.2 关键结论：没有逐 Turn 的 Provider Usage

- `transcript.jsonl` 只有 6 类事件，**没有 `model_call` 事件，也没有 usage 字段**；`action_proposed` 只记录 `input_digest / input_shape / input_size`，不保留工具入参原文。
- Runner 里的 `DeepSeekAdapter.on_call` 观测（含 `prompt_cache_hit_tokens`）只被 `observations.append` 聚合成运行级总数（`_aggregate_observations`），**不落盘逐次记录**。
- `worker_results[*].token_usage` 是 OBSERVED，但 AnyCode 的 `merge_usage` 只累加 `input_tokens/output_tokens` 并丢弃 cache 字段：`input_tokens` 实际是 **cache-miss** 部分（a1 三个 worker 之和 ≈ 579,581，而运行级 `prompt_tokens` 是 4,992,381）。**因此无法从 worker_results 还原单个 worker 的真实 prompt 总量。**

### 1.3 各指标的可信度

| 指标 | 类型 | 说明 |
|---|---|---|
| 运行级 prompt / completion / cache hit / cache miss / model_turns / tool_calls / cost | `OBSERVED` | provider usage 聚合 |
| worker `stop_reason`、`success` | `OBSERVED` | AnyCode `AgentRunResult` |
| oracle 判定 | `OBSERVED` | 隐藏测试退出码 |
| 工具入参（path / command / pattern）与输出 | `OBSERVED` | `result.json.tool_calls`，输出截断 4000 字符 |
| 每轮实际发送的消息内容 | `RECONSTRUCTED` | `tool_conversations.json`；快照在 `before_turn` 保存，即"最后一次调用的完整 prompt"，更早的 prompt 是其前缀（append-only，未启用压缩） |
| 每轮 prompt token 数 | `ESTIMATED` | tiktoken(cl100k) 重建，并按运行级 OBSERVED 总量锚定 |
| 固定前缀（system prompt + 工具 schema） | `ESTIMATED` | 直接对渲染后的 profile 与原生 8 个工具定义计数 |
| 逐 Turn provider usage（含逐 Turn cache 命中） | `UNAVAILABLE` | 无落盘记录 |
| 跨 worker 重复探索的 token 影响 | `UNAVAILABLE` | 只能统计调用次数，无法把某次调用对应到某个 tool result 的重复增量 |

### 1.4 重建可信度校验

| 校验项 | 结果 |
|---|---|
| 重建轮数 vs OBSERVED `model_turns` | a0 257/257，a1 987/987，**0 处不一致** |
| 每个 run 的会话数 vs `worker_results` 数 | 71/71 一致 |
| 锚定残差（每轮固定前缀）稳定性 | a0 1,741–2,022（p50 1,933）；a1 1,797–2,119（p50 1,968） |
| 直接测量的固定前缀 | coding_worker system 263 + 8 个工具 schema 1,132 = **1,395** token |
| 残差 − 直接测量 | ≈ 550 token/次（provider chat template、`call_id` / `tool_use_id`、tiktoken 与 DeepSeek 分词器差异） |

残差在各 run / 各 task 之间稳定在 1.7k–2.1k，说明"前缀重建 + 单点锚定"这一方法本身是自洽的；但逐轮的**绝对值仍是估算**，本报告只用它做结构占比与增长曲线，绝对总量一律引用 OBSERVED。

### 1.5 已知数据缺口

1. `e1-04-cli-a0` 的 rep0 崩溃（`returncode=1`，无 `result.json`），因此只有 71/72 个 run 有指标。已发布总量同样未包含它（见 §2 校验通过）。
2. `tool_calls[*].output` 截断到 4000 字符：不影响 token 统计（token 统计来自会话快照），但会影响"这次 bash 输出到底多大"的极值观察（最大 28,422 字符仍完整保留在会话快照里）。
3. E1 发生在 G1 之前，因此本报告的 handoff 数字是**旧版 500 字符截断**的量级，不代表 G1 双通道实现。

---

## 2. E1 成本放大分解（复现校验）

| 指标 | A0 单 Worker | A1 多 Worker | 放大 |
|---|---:|---:|---:|
| 运行数 | 35（含 1 次崩溃无指标） | 36 | |
| 成功 | 28 | 3 | |
| Prompt Tokens | 757,828 | 4,992,381 | **6.588×** |
| Model Turns | 257 | 987 | **3.840×** |
| 每轮 Prompt | 2,948.7 | 5,058.1 | **1.715×** |
| Completion Tokens | 61,598 | 171,656 | 2.787× |
| Tool Calls | 310 | 1,075 | 3.468× |
| Cache Hit / Miss | 689,152 / 68,676 | 4,412,800 / 579,581 | 6.403× / 8.439× |
| Cache Hit 占比 | 90.9% | 88.4% | |
| 估算成本 | $0.0987 | $0.4063 | **4.119×** |

- 恒等式校验：`3.840 × 1.715 = 6.585 ≈ 6.588`（逐 run 求和为 6.588）。
- 脚本内置校验 `expected_check.all_passed = true`：prompt 757,828 / 4,992,381、turns 257 / 987、success 28 / 3 全部与原报告一致。
- **重要口径提醒**：prompt token 放大 6.59×，但**成本只放大 4.12×**——A1 的 prompt token 里 88.4% 是 cache hit（单价约为 miss 的 1/10）。任何"6.59× 成本"的说法都不成立；miss token 放大是 8.44×。

### 2.1 按 Task 拆分（A1，每任务 3 次运行）

| Task | 成功 | Prompt | Turns | 每轮 Prompt | Tool Calls |
|---|---:|---:|---:|---:|---:|
| e1-02-report | 0/3 | 888,019 | 114 | **7,790** | 129 |
| e1-10-search | 0/3 | 731,908 | 112 | 6,535 | 123 |
| e1-04-cli | 0/3 | 617,576 | 108 | 5,718 | 133 |
| e1-03-rbac | 0/3 | 488,209 | 105 | 4,650 | 111 |
| e1-09-etl | 0/3 | 409,189 | 91 | 4,497 | 96 |
| e1-11-fallback | 0/3 | 391,387 | 73 | 5,361 | 80 |
| e1-08-stats | 0/3 | 373,020 | 99 | 3,768 | 102 |
| e1-01-config | 1/3 | 333,309 | 77 | 4,329 | 84 |
| e1-06-kv | 0/3 | 248,569 | 58 | 4,286 | 64 |
| e1-05-pairs | 0/3 | 219,912 | 57 | 3,858 | 64 |
| e1-07-export | 0/3 | 217,314 | 59 | 3,683 | 59 |
| e1-12-conflict | 2/3 | **73,969** | 34 | **2,176** | 30 |

成本与成功率呈明显反向关系：最省的 `e1-12-conflict`（2,176 token/轮）成功率 2/3，最贵的 `e1-02-report`（7,790 token/轮）0/3。

### 2.2 长尾（保留异常高成本案例）

| Sample | Prompt | Turns | 每轮 | Workers | 失败类型 |
|---|---:|---:|---:|---:|---|
| e1-02-report-a1 | 364,589 | 39 | 9,348 | 3 | BUDGET_EXCEEDED |
| e1-02-report-a1 | 283,564 | 34 | 8,340 | 3 | BUDGET_EXCEEDED |
| e1-04-cli-a1 | 274,843 | 42 | 6,544 | 3 | BUDGET_EXCEEDED |
| e1-10-search-a1 | 267,255 | 42 | 6,363 | 3 | BUDGET_EXCEEDED |
| e1-10-search-a1 | 251,612 | 34 | 7,400 | 3 | BUDGET_EXCEEDED |

A1 平均每 run 2.67 个 worker（96/36）；单个 run 的最贵一次是 120k 预算的 3.0 倍。

---

## 3. 重点任务逐 Worker / Turn 分析（G2b-0.3）

每轮的 prompt token 为 `ESTIMATED`（tiktoken 重建 + 运行级锚定），轮数与 OBSERVED 完全一致。

### 3.1 `e1-02-report`：单 Worker 与最贵 Worker 的对照

| Worker | Turns | 首轮 | 末轮 | 峰值 | 增长 |
|---|---:|---:|---:|---:|---:|
| A0 solo（rep0） | 7 | 2,038 | 4,073 | 4,073 | **2.00×** |
| A1 parser | 11 | 2,138 | 6,657 | 6,657 | 3.11× |
| A1 render | 14 | 2,142 | 16,292 | 16,292 | **7.61×** |
| A1 stats | 14 | 2,138 | **35,507** | 35,507 | **16.61×** |

同一 run（`eval-62dae7e46b5f`，该 run 总 prompt 364,589）内三个 worker 的逐轮曲线：

```text
turn            1     2     3     4     5     6     7     8     9    10    11    12    13    14
stats        2,138 2,319 2,540 2,792 3,091 3,509 3,725 6,975 10,369 13,797 21,759 34,246 34,830 35,507
  └ tool结果    0   133   265   456   685 1,007 1,167 4,376 7,723 11,079 18,911 31,258 31,700 32,260
render       2,142 2,342 2,568 3,006 5,716 11,974 12,392 13,208 13,430 13,645 14,356 14,734 15,410 16,292
  └ tool结果    0   134   265   556 3,047 9,206 9,496 10,083 10,179 10,279 10,915 11,019 11,038 11,057
parser       2,138 2,332 2,581 3,216 3,707 3,827 4,268 5,109 5,911 6,041 6,657   —     —     —
A0 solo      2,038 2,222 2,426 2,731 3,365 3,983 4,073   —     —     —     —     —     —     —
```

`stats` worker 的 prompt 从 2.1k 涨到 35.5k，其中 32.3k 是累积的 tool result；单次最大 bash 输出 **12,526 token（28,422 字符，697 行测试 stdout）**，另一次 7,832 token。这就是"单 Worker 上下文膨胀"的极端样本：echo 测试日志进上下文，然后在后续 12 轮里反复重发。

### 3.2 `e1-07-export`（集成失败案例）

| Worker | Turns | 首轮 | 末轮 | 增长 |
|---|---:|---:|---:|---:|
| A0 solo（3 次运行） | 8 | ≈2,002 | ≈3,265 | 1.62–1.64× |
| A1 exporter（3 次） | 9 | 2,085–2,148 | 3,742–4,466 | 1.79–2.14× |
| A1 formatter（3 次） | 9–14 | 1,949–2,012 | 3,610–8,350 | 1.85–**4.15×** |

该任务的 worker 上下文都比较克制（≤8.4k），失败原因是实现/集成，而不是 token 规模。

### 3.3 `e1-12-conflict`（正向收益案例）

| Worker | Turns | 首轮 | 末轮 | 增长 |
|---|---:|---:|---:|---:|
| A0 solo（3 次） | 1 | 1,807 | 1,807 | 1.00× |
| A1 a | 3–5 | 1,845–1,855 | 2,115–2,146 | 1.15–1.16× |
| A1 b | 2–3 | 1,845–1,855 | 1,890–1,965 | 1.02–1.07× |
| A1 merge | 4–6 | 1,936–2,080 | 2,999–3,281 | 1.44–1.69× |

这是唯一"多 Worker 便宜"的形态：每个 worker 只跑 1–6 轮、上下文基本不膨胀、harness 总量 73,969 反而低于 A0 的单 worker 平均成本结构，成功率也是最高的 2/3。

### 3.4 全局增长统计

- A0：末轮/首轮 p50 = **1.99×**（max 2.40×，n=35）。
- A1：末轮/首轮 p50 = **2.71×**（max **16.61×**，n=96）。
- 单 worker 峰值 prompt 前 5：35,507（e1-02-report stats）、20,169（parser）、19,969（e1-10-search rank）、19,019（e1-01-config loader）、18,533（e1-11-fallback consumer）。

> 未把不同 worker 的轮次拼接为同一会话；每个 worker 的曲线独立来自其自己的快照。

---

## 4. Prompt 内容组成（G2b-0.4）

以运行级 OBSERVED 总量为锚，按**实际发送内容**分类（`ESTIMATED`，单位 token）：

| 类别 | A0 | 占比 | A1 | 占比 |
|---|---:|---:|---:|---:|
| 固定前缀（System + Tool Schema + 模板） | 498,158 | **65.7%** | 1,944,487 | **38.9%** |
| Tool Results（历史累积） | 108,323 | 14.3% | 2,423,443 | **48.5%** |
| Previous Assistant Messages | 131,363 | 17.3% | 563,608 | 11.3% |
| Task Description | 19,986 | 2.6% | 37,736 | 0.8% |
| Handoff Context | 0 | 0.0% | 23,109 | 0.5% |
| Other | 0 | 0.0% | 0 | 0.0% |

回答提出的四个问题：

1. **固定 System / Tool Schema 占比过高吗？** 绝对值不高（实测 1,395 token/次，其中工具 schema 1,132），但**乘以调用次数后**是 A1 的 38.9%、A0 的 65.7%。A0 之所以更高，是因为它调用少、上下文浅，固定前缀无处摊薄。
2. **Tool Result 历史是否持续累积？** 是，而且是 A1 的最大单项（48.5%）。唯一 tool result 内容合计 433,398 token，实际计费 2,423,443 token，**重发倍数 5.59×**（A0 为 4.36×）。逐轮曲线（§3.1）显示它单调增长。
3. **Handoff Context 占比大吗？** 不大，A1 仅 0.5%（23,109 token）。E1 使用的是旧版 500 字符截断 handoff；G1 的双通道实现只会让这部分更小。**Handoff 不是成本问题，是正确性问题。**
4. **增长主要由哪类内容驱动？** tool result。每轮平均内容从 A0 的 1,028 token 涨到 A1 的 3,104 token（3.02×），而固定前缀几乎不变（1,921 → 1,954）。

工具结果规模分布（全量，a0+a1）：

| Tool | 次数 | 平均 token | p90 | 最大 |
|---|---:|---:|---:|---:|
| bash | 1,045 | 426 | 784 | 12,526 |
| file_read | 58 | 156 | 477 | 926 |
| file_write | 140 | 19 | 20 | 20 |
| list_files | 78 | 21 | 29 | 29 |
| file_edit | 4 | 14 | 14 | 14 |

`grep` 在 E1 中一次都没有被调用（工具可见但未被使用）。

> 口径说明：以上分类是把重建消息切成文本块后逐块计数（tool_use 计入 assistant，tool_result 计入 tool results），不含 provider 模板开销；模板开销落在"固定前缀"这一列，因此该列是"实测 1,395 + ≈550 模板/ID/分词器差异"的合计。没有为了凑占比调整任何一类。

---

## 5. 重复探索分析（G2b-0.5）

只看既有 `tool_calls`，不执行任何工具。路径做归一化（去掉 `/workspace/project` 前缀与 `./`），bash 只做**完全相同的命令**比对；跨 worker 的 bash 路径重叠仅作为启发式证据，不计入 token。

| 类型 | 用例数 | 额外调用 | 说明 |
|---|---:|---:|---|
| `EXACT_REPEAT`（同 worker 同参数） | 8 | **12** | file_write 4、file_edit 1、bash 3；其中 5 例此前有对该路径的写操作 |
| `CROSS_WORKER_REPEAT`（同 run 不同 worker 观察同一对象） | 49 | **58** | bash 25、list_files 15、file_write 7、file_read 2；8 例此前有写操作 |
| `BASH_PATH_OVERLAP`（不同 worker 的 bash 命令提到同一文件） | 71 个 run 存在 | — | 启发式，仅证明"调查对象重叠"，不折算 token |

- 额外调用合计 70 次，占全部 1,385 次 tool call 的 **5.1%**。
- 集中在 `e1-04-cli-a1`（exact 5 + cross 15）、`e1-03-rbac-a1`（cross 10）、`e1-09-etl-a1`（exact 2 + cross 5）。
- 典型 `EXACT_REPEAT`：`e1-04-cli-a1` 对 `cli/commands.py` 执行了 3 次完全相同的 `file_edit`；`e1-06-kv-a1` 3 次相同的 `git cat-file -p`。
- `file_read` 的跨 worker 重复只有 2 例——**多 worker 并没有大量重复读同一文件**。

结论：**重复探索不是主因**。它影响调用次数（5%），但跨 worker 重复的对象多是 `list_files`（每次 ~21 token）和描述性 bash，token 影响远小于上下文累积。按题目要求，不把调用次数直接当成 token 浪费；本报告也不给出"重复探索造成 N token"的数字，因为无法把某次重复调用映射到被重复计入的上下文字符数（`UNAVAILABLE`）。

---

## 6. Stop Reason 与失败归因（G2b-0.6）

### 6.1 Worker 停止原因

| Stop Reason | A0 | A1 |
|---|---:|---:|
| `success` | 29 | 48 |
| `max_turns` | 0 | **28** |
| `side_effect_unknown` | 5 | 18 |
| `doom_loop` | 1 | 2 |
| 合计 worker | 35 | 96 |

A1 的 28 次 `max_turns` 占总 worker 数 29%，A0 一次都没有。

### 6.2 各停止原因消耗的 prompt token（A1）

| Stop | Turns | Prompt Tokens | 占比 | 每轮 |
|---|---:|---:|---:|---:|
| `max_turns` | 392 | 2,590,168 | **51.9%** | **6,608** |
| `success` | 430 | 1,722,154 | 34.5% | 4,005 |
| `side_effect_unknown` | 142 | 574,070 | 11.5% | 4,043 |
| `doom_loop` | 23 | 105,991 | 2.1% | 4,608 |

**65.5% 的 A1 prompt token 由最终异常结束的 worker 消耗，56.4% 的轮次发生在这些 worker 上。**

### 6.3 回答四个问题

1. **`max_turns` 是否集中在高 Prompt 增长 Worker？** 是。`max_turns` worker 的每轮 prompt 是 6,608 token，明显高于 `success` worker 的 4,005；最贵的 5 个单轮 prompt（35.5k / 20.2k / 20.0k / 19.0k / 18.5k）全部来自 14 轮的 worker。
2. **预算拒绝前的 Worker 是否经历重复探索？** 无法用 G2a 预算 ledger 直接回答：G2a 只有 4 个 guarded run，且拒绝发生在 token 膨胀到 1.6–1.8 万之后的请求上；本次 E1 的重复探索计数只有 12+58 次，与"膨胀"量级不匹配。**证据不足，标 `UNDETERMINED`。**
3. **`side_effect_unknown` 是否有共同工具类型？** 有。A0+A1 共 23 次 `side_effect_unknown`，其中 **22 次 worker 的最后一次动作是 `bash`**（另 1 次是 `list_files`）；M4 记录为已执行但结果无法确认幂等。由于本轮只允许记录工具名/退出码/错误类型，不再往下推断可重试性。
4. **Worker 自报成功但 Oracle 失败时，是否有明显的集成或验证缺失？** 有：A1 中 `stop_reason=success` 的 48 个 worker 里，**38 个所在 run 的 oracle 失败**（只有 10 个 pass）。与之对照，A0 的 29 个 success worker 中 26 个在通过的 run 里。说明多 worker 场景下"单个 worker 报告完成"与"整个 run 通过隐藏测试"之间缺一层集成/验证约束（这不是 token 问题，但它是同一个设计缺陷的另一面）。

### 6.4 失败类型汇总（run 级）

| 失败类型 | A0 | A1 |
|---|---:|---:|
| TASK_SUCCESS | 28 | 3 |
| ORACLE_FAILURE | 7 | 13 |
| BUDGET_EXCEEDED | 0 | **20** |

A1 的 20 次预算超限是"上下文累积"的直接后果，不是独立原因。

---

## 7. 根因排序（附证据、量级与可信度）

按"可归因的 token 量级"排序；证据不足的项标 `UNDETERMINED`，不为凑排序编造数字。

### B. 同一 Worker 的历史上下文膨胀（tool result 反复重发）— 主因

- 证据：A1 `tool_result` 累计 2,423,443 token = **48.5%** 的 prompt；唯一内容仅 433,398 token，**重发 5.59×**；逐轮曲线单调上升（§3.1）。
- 位置：`e1-02-report-a1`（stats 35.5k / render 16.3k）、`e1-10-search-a1`（rank 20.0k）、`e1-01-config-a1`（loader 19.0k）。
- 量级：2.42M token（占 A1 总量近一半）；末轮/首轮 p50 2.71×，最大 16.61×。
- 可信度：token 总量与轮数 `OBSERVED` 且完全对齐；分类拆分为 `ESTIMATED`（tiktoken + 锚定），残差稳定。
- 未确认：模型是否"真的读取"了这些重发的历史（无法观测注意力）；重发是否部分为保持一致性所必需。

### F. Worker 异常停止与无效轮次 — 次主因（按结果归因）

- 证据：`max_turns` 28/96、`side_effect_unknown` 18/96、`doom_loop` 2/96；这些 worker 合计消耗 **65.5%** 的 A1 prompt token；`max_turns` worker 的每轮成本最高（6,608）。
- 位置：`e1-02-report-a1`（`stats` 三次运行全部 max_turns；`render` 一次 max_turns、一次 side_effect_unknown；`parser` 一次 max_turns、两次 success）、`e1-10-search-a1`、`e1-04-cli-a1`。
- 量级：3.27M token（65.5%），392+142+23 = 557 轮（56.4%）。
- 可信度：停止原因 `OBSERVED`；每 worker 的 token 是重建值（`ESTIMATED`），但运行级合计 `OBSERVED` 对齐。
- 未确认：这些轮次里有多少是"必要探索"、多少是"打转"，缺少逐轮结果标签。

### E + A. 固定前缀 × 调用次数（System + Tool Schema + 模板）

- 证据：每轮固定开销 1,950（p50，范围 1.74k–2.12k）；直接测量 system 263 + 8 个工具 schema 1,132 = 1,395，余 ≈550 为模板/ID/分词器差异；A1 合计 1,944,487 token = 38.9%。
- 位置：全局，与任务无关（所有 worker 共用同一 profile 与工具集）。
- 量级：1.94M token；若调用数不变，这部分完全不可压缩，只能靠"减少调用"或"减少前缀"降低。
- 可信度：高（直接测量 + 残差稳定）。
- 未确认：DeepSeek 侧模板的具体 token 数（≈550 是残差推算）。

### D. 单 Worker 子任务范围过大

- 证据：`e1-02-report` 的 stats worker 单轮 35.5k，其中 32.3k 来自累积 tool result；单次 bash 输出最大 28,422 字符（697 行测试日志）；`e1-10-search` rank worker 单轮 20.0k。
- 位置：`e1-02-report-a1`、`e1-10-search-a1`、`e1-03-rbac-a1`。
- 量级：无法与 B 完全分离（D 是 B 的放大器）；可确认的是"少数 worker 贡献了不成比例的 token"。
- 可信度：中高（逐轮重建）；"子任务过大"本身是判断，不是观测。
- 未确认：任务文本是否真的要求这么宽的探索范围（未做任务契约语义分析）。

### C. 跨 Worker 重复探索

- 证据：额外调用 70 次（5.1%），其中跨 worker 58 次；`file_read` 跨 worker 重复仅 2 例；`list_files` 15 例（每次约 21 token）。
- 位置：`e1-04-cli-a1`（20 次）、`e1-03-rbac-a1`（10 次）。
- 量级：调用次数可见但**token 影响 `UNAVAILABLE`**；按单次 tool result 平均 426 token 粗估上限约 2.5 万 token（≈0.5%），不构成主因。
- 可信度：调用计数高；token 影响低置信。
- 未确认：bash 命令的路径级重叠（71 个 run 存在）是否真的产生了重复的大块内容——无逐调用文本对齐。

### 其他（不参与排序，但需澄清）

- **Handoff（G1）**：A1 仅 23,109 token（0.5%）。不是成本来源。
- **Cache**：A1 88.4% 的 prompt token 是 cache hit；因此"6.59× token → 6.59× 成本"不成立（成本 4.12×）。

---

## 8. 下一阶段唯一优先建议

### 做 Context Compaction（针对 tool result 的按轮次压缩），不要先做 Scoped Context 或改 Worker Task Contract

**理由（全部来自上面的量化）**

1. tool result 是唯一"既最大又可机械压缩"的块：48.5% 的 A1 prompt，且 5.59× 重发。
2. 反事实模拟（脚本 `compaction_simulation`，以真实逐轮重建为输入）：

| 策略（保留最近 N 轮 tool result 原文） | A1 Prompt | 占当前 | 放大倍数 |
|---|---:|---:|---:|
| N=1 | 3,002,338 | **60.1%** | 4.45× |
| N=2 | 3,410,752 | **68.3%** | 4.91× |
| N=3 | 3,783,723 | 75.8% | 5.31× |
| N=4 | 4,103,066 | 82.2% | 5.62× |
| 不压缩（自校验） | 4,992,383 | 100.0% | 6.588× |

   自校验行与 OBSERVED 总量几乎完全一致（差 2 token），说明模拟口径可信。取 N=2：**A1 prompt token 可降 31.7%，放大倍数从 6.59× 降到 4.91×**。
3. 与既有机制同构：G1 已经建立了"完整落盘 + 引用 + 按需读取"的模式；把同一模式用在一个 worker 自己的历史 tool result 上，是复用而不是新增体系。
4. 为什么不选另外两个：
   - **Scoped Context**（按任务选择上下文）解决的是"跨 worker / 跨任务该看什么"，而数据显示跨 worker 重复只占 5%，收益面小；它还会同时改动选择与渲染两层，属于更大的架构变更。
   - **Worker Task Contract 调整**（限制子任务范围/禁止大范围 dump）针对 D，而 D 只是 B 的放大器；在 B 未处理前，改契约只能缓解个别 worker，且会改变任务定义，污染后续对照。

**建议的最小形态（下一阶段，不在本轮实施）**：在 worker 自己的会话内，只保留最近 2 轮 tool result 原文，更早的替换为 `[tool result elided: <tool> <size> chars, digest <sha>]` 一行引用（可选：允许 worker 通过既有工具按需重取）。必须在同一 run 内可开关，以便与本轮数据做对照。

**必须诚实声明的边界**

- 31.7% 是**反事实上界**，假设 worker 不会重新读取被压缩的内容；真实收益可能被"重新读取"部分抵消（成本从 prompt 转移到 tool call）。
- 压缩会改变 prompt 前缀，从而影响 DeepSeek 的 cache 命中结构；由于 88.4% 的 token 是 hit，实际成本下降幅度会小于 token 下降幅度，需要在下一阶段同时记录 hit/miss。
- 压缩不能解决 65.5% 的 token 花在异常结束 worker 上的问题；那是轮次效率问题，需要单独阶段处理（且不应与本阶段混在一起）。

---

## 9. 未能确定的问题

1. **逐 Turn provider usage**（每轮的 prompt / cache hit / cache miss）：`UNAVAILABLE`。若后续需要，最小改造是在 `DeepSeekAdapter.on_call` 处把观测逐条落盘（本轮禁止修改 Runtime，故未做）。
2. **重发的历史是否被模型实际使用**：无法观测；只能确认它被计费。
3. **跨 worker 重复探索的 token 代价**：只有调用次数，没有逐调用内容对齐，`UNAVAILABLE`。
4. **"必要"与"浪费"的边界**：worker 修改文件后再次读取、修好后重跑测试，属于必要；现有记录无法逐轮判定，仅能标记"此前是否有写操作"。
5. **`max_turns` worker 内部的有效/无效轮次比例**：缺少逐轮结果标签，`UNDETERMINED`。
6. **G1/G2a 运行的 handoff 成本**：两者执行条件与 E1 不同（G1 引入双通道 handoff、G2a 引入预算准入），本轮不把它们与 E1 混算；只在 inventory 中记录其文件结构。
7. **`e1-04-cli-a0` rep0 崩溃原因**：`returncode=1`、无 stderr、无 `result.json`，无法从既有记录定位。

---

## 10. 复现方式

```powershell
# 只读：扫描已有结果目录，不调用模型、不启动 Sandbox
.\.venv\Scripts\python.exe -m experiments.evaluation.stage_g2b_diagnosis
```

输出（`experiments/results/stage-g2b-diagnosis/`）：

| 文件 | 内容 |
|---|---|
| `summary.json` | inventory、放大分解、E1 校验、轮次对账、组成、重发、停止原因、固定前缀、压缩模拟 |
| `runs.csv` / `workers.csv` | 运行级 / worker 级 OBSERVED 指标 |
| `turns.csv` | 1,244 条逐 worker 逐轮记录（重建 token、分类 token、累计） |
| `turn_summaries.csv` | 每 worker 的首轮/末轮/峰值/增长率 |
| `composition.csv` / `resend.csv` | 组成占比、唯一内容 vs 计费内容 |
| `stop_tokens.csv` / `stop_oracle.csv` | 停止原因 × token / × oracle |
| `compaction.csv` | 压缩反事实模拟 |

脚本内置校验：`summary.json.expected_check.all_passed == true`（prompt 757,828 / 4,992,381、turns 257 / 987、success 28 / 3 与原报告一致）；`turn_reconciliation.turn_mismatches == []`（1,244/1,244 轮对齐）。
