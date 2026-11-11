# Stage G1 — 双通道 Worker Handoff（H0 Legacy vs H1 Dual）

状态：V1 延伸实验，实现 + 12 次真实运行完成。未提交 Git，改动留在工作区。

## 1. 结论摘要

| 问题 | 结论 |
|---|---|
| 长 Result 能否完整落盘并读回？ | 能。真实运行中出现 1 次落盘（1521 字符），文件 SHA-256 校验通过，消费方用 1 次 `handoff.read` 取回剩余全部内容。 |
| 下游是否真的使用了完整内容？ | 是（唯一可观测样例）。消费方严格按 Prompt 指示从 `offset=300` 续读，300 字符预览 + 1221 字符返回 = 1521 字符全文，无内容丢失。 |
| Handoff 修复是否改善成功率？ | 本轮样本内没有。H0 2/6、H1 1/6；H1 相对 H0 唯一丢失的成功样本（`e1-12-conflict` rep1）是 producer 自身失败，与 Handoff 无关。 |
| 增加了多少 Token / Tool Call / 延迟？ | 配对均值：prompt −982 tokens，completion +1015 tokens，tool calls −0.5，延迟 +7.6 s，成本 +$0.00119/次。差异主要来自模型轨迹方差，不是 Handoff 本身。 |
| 剩余失败中预算问题占比？ | 9 次失败中 1 次为 `BUDGET_EXCEEDED`（11.1%）；主要失败来源是 producer 自身失败（5/9）。 |

关键限制：**该样本的依赖结果普遍不长**。12 次运行中仅 2 次 legacy 依赖结果超过 500 字符上限（1818、1548），dual 仅触发 1 次落盘。因此本轮不足以支撑“Handoff 是否提升成功率”的统计结论，只能证明机制正确性与可观测性。

## 2. 实现

### 2.1 `src/atlasos/handoff.py`（新增，唯一新运行时模块）

- **`HandoffStore`**：`<run_dir>/handoffs/` 下内容寻址存储。UTF-8、SHA-256、`tmp + os.replace` 原子写入；同一内容重复写入得到同一 `ref_id`（幂等）；路径只由“可信 run 目录 + 内容摘要”生成，模型只看到 `ho-<24 hex>` 形式的不透明引用，引用先过严格正则再做任何文件访问；读取时重算摘要并校验长度与 run 身份。
- **`HandoffProjector`**：只决定“已选定的依赖结果如何进入 Prompt”。
  - `legacy`：逐字节复现 `AnyCode._build_task_prompt`（复用上游 `DEPENDENCY_CONTEXT_MAX_LENGTH`），并有等价性单元测试锁定。
  - `dual`：单条 ≤1200 字符且总预算允许 → 完整 Inline；所有依赖 Inline 总量 ≤3000 字符；超阈值 → 完整落盘，Prompt 只放 ≤300 字符预览 + 引用。不调用任何 LLM 摘要，不丢弃任何完整结果。
- **`HandoffReader` / `handoff.read`**：复用 `AtlasToolRuntime(external_tools=...)` 注册为可检索工具，经 `tool.search` → `dispatch_action` → M1 Registry → M3 Action Safety Gate → `ToolExecutor` 执行，未新增任何执行语义。单页 ≤4000 字符（默认 3000），返回 `next_offset`；消费方身份只从可信 `ExecutionContext.attributes["task_id"]` 读取，必须命中本 run 的任务图，且引用的 producer 必须是该任务的**直接依赖**。
- **`HandoffMetrics`**：记录 result 原始长度、inline/offloaded 字符数、ref 数量、read 调用/成功/拒绝次数，以及“已获取内容覆盖率”（Prompt 预览 + 各页返回的并集）。

### 2.2 接线（最小改动）

- `experiments/evaluation/runner.py`：`RunnerSettings` 新增 `handoff_mode`（进入 config digest）；每个 run 构建一个 `HandoffRuntime`（任务图来自 `task.workers` 的 `task_id/title/depends_on`，consumer 身份来自 worker 运行时 task id）；`external_tools=handoff.external_tools()`；`AtlasContextEngine(..., prompt_builder=handoff.build_prompt)`；运行结束把 `metrics.extra["handoff"]` 写入 `result.json` 与 `stage_e_evidence.json`。
- `experiments/evaluation/stage_e_cli.py`：新增 `--handoff-mode legacy|dual`，并在 stdout JSON 中附带 handoff 摘要。
- `experiments/evaluation/stage_e_rounds.py`：新增 `g1` suite（同一 A1 变体跑 H0/H1，交替顺序）与 `--handoff-mode` 透传。
- `experiments/evaluation/stage_g1_analysis.py`（新增）：复用 Stage E1 的 `_metrics`、`_run_result` 与 `collect_evidence`，只增加 handoff 汇总与失败归因。
- `.gitignore`：加入 `experiments/results/stage-g1/`（与 stage-e1/e2 一致）。

未修改：AnyCode 源码、TaskQueue、Scheduler、Checkpoint、M2–M7 语义、CLI/Web、V1 Context Plane、既有任务/Fixture/Oracle/预算配置。Legacy 与 Dual 的工具注册表完全一致（同一 `handoff.read` schema），避免工具面变化干扰对照。

### 2.3 并发身份

消费方身份不使用进程全局变量：`handoff.read` 只信任 `ToolUseContext.execution_context.attributes["task_id"]`，该值由 `ScopedWorkerFactory` 在创建 worker 时写入，并在 `AtlasDispatchExecutor` → `ToolExecutor` 路径中原样传递。并发在同 wave 的两个 consumer 各自读取自己依赖的结果，互不干扰（集成测试覆盖）。

## 3. 测试与回归

新增/扩展：

- `tests/unit/test_handoff.py`（新增 16 项）：短结果完整 Inline；长结果落盘 + 引用；分页读取无损还原；预览 + 分页视为完整获取；多依赖总 Inline 预算；存储幂等/内容寻址；引用格式拒绝 Host 路径；Digest 篡改拒绝；跨 run 引用拒绝；非依赖/无可信身份访问拒绝；legacy 与上游 `AnyCode._build_task_prompt` 逐字符等价；dual Prompt 含正确引用；两种模式工具 schema 一致；指标口径。
- `tests/integration/test_anycode_dag.py`（扩展 1 项）：真实 `AtlasContextEngine` + 共享 `AtlasToolRuntime`（带 M3 策略）跑 5 节点 DAG，两个 consumer 同 wave 并发：各自读自己的依赖、c1 越权读 c2 的 producer 被 `handoff_denied` 拒绝，`tool.search` 2 次、`handoff.read` dispatch 5 次（1 次失败），无内容串线。

回归结果：

```text
pytest -q          : 478 collected (1 live deselected), all passed
ruff check .       : All checks passed
ruff format --check: 303 files already formatted
pyright            : 0 errors, 0 warnings, 0 informations
```

## 4. 真实对照实验设置

复用 Stage E1 三个任务与全部配置，仅改变 handoff 模式：

- 任务：`e1-05-pairs`、`e1-07-export`、`e1-12-conflict`（A1 多 worker 变体）
- 模型 `deepseek-chat`，temperature 0.0，模型调用不重试
- 静态 DAG 与 Worker Profile 不变；fixture digest 在同一 pair 内完全一致
- 120k token budget / 14 turn limit / 60 tool call budget / OpenSandbox 配置不变
- 每任务每模式 2 次，共 12 次（顺序交替：rep0 = H0→H1，rep1 = H1→H0）
- 结果目录：`experiments/results/stage-g1/g1-20261110T103003Z/`
- 另有一次接线冒烟运行（`e1-05-pairs-a1` / dual / rep9，目录 `experiments/runs/stage-g1-smoke`，`.gitignore` 已排除），**不计入** 12 次对照样本

## 5. 逐次结果

| 任务 | rep | 模式 | 成功 | prompt | completion | tools | turns | cost USD | 延迟 s | 归因 |
|---|---|---:|---|---:|---:|---:|---:|---:|---:|---|
| e1-05-pairs | 0 | H0 | 否 | 54,842 | 1,781 | 18 | 16 | 0.00373 | 53.9 | PRODUCER_FAILURE |
| e1-05-pairs | 0 | H1 | 否 | 60,502 | 4,114 | 20 | 17 | 0.00709 | 68.9 | PRODUCER_FAILURE |
| e1-07-export | 0 | H0 | 否 | 111,480 | 3,019 | 29 | 27 | 0.00786 | 98.8 | PRODUCER_FAILURE |
| e1-07-export | 0 | H1 | 否 | 122,379 | 6,633 | 25 | 23 | 0.01175 | 88.0 | BUDGET_EXCEEDED |
| e1-12-conflict | 0 | H0 | 是 | 26,684 | 1,589 | 10 | 12 | 0.00287 | 39.5 | TASK_SUCCESS |
| e1-12-conflict | 0 | H1 | 是 | 31,706 | 1,924 | 12 | 14 | 0.00335 | 84.6 | TASK_SUCCESS |
| e1-05-pairs | 1 | H0 | 否 | 91,648 | 4,746 | 24 | 22 | 0.00839 | 76.8 | ORACLE_FAILURE |
| e1-05-pairs | 1 | H1 | 否 | 66,919 | 4,036 | 20 | 19 | 0.00693 | 64.3 | CONSUMER_READ_STILL_FAILED |
| e1-07-export | 1 | H0 | 否 | 60,037 | 3,371 | 20 | 20 | 0.00572 | 65.7 | ORACLE_FAILURE |
| e1-07-export | 1 | H1 | 否 | 51,386 | 3,748 | 18 | 16 | 0.00622 | 62.5 | PRODUCER_FAILURE |
| e1-12-conflict | 1 | H0 | 是 | 23,402 | 1,476 | 9 | 11 | 0.00258 | 38.1 | TASK_SUCCESS |
| e1-12-conflict | 1 | H1 | 否 | 29,310 | 1,618 | 12 | 13 | 0.00298 | 50.3 | PRODUCER_FAILURE |

## 6. 汇总指标

| 变体 | 运行 | 成功 | prompt tokens | completion tokens | turns | tool calls | cache hit ratio | cost USD | p50 延迟 ms |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| H0 legacy | 6 | 2/6 | 368,093 | 15,982 | 108 | 110 | 0.9097 | 0.031161 | 59,782 |
| H1 dual | 6 | 1/6 | 362,202 | 22,073 | 102 | 107 | 0.9093 | 0.038321 | 66,611 |

配对差（H1 − H0，n=6）：prompt −982（min −24,729 / max +10,899），completion +1,015（−710 / +3,614），tool calls −0.5（−4 / +3），延迟 +7,624 ms（−12,548 / +45,019），成本 +$0.00119。

Handoff 通道：

| 变体 | result 原始字符 | Inline 字符 | Offloaded 字符 | ref 数 | read 调用 | read 成功 | read 失败 | 完整获取的 consumer |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| H0 | 4,368 | 2,002 | 0 | 0 | 0 | 0 | 0 | 0 |
| H1 | 3,176 | 1,655 | 1,521 | 1 | 1 | 1 | 0 | 1 |

H0 侧 4,368 字符的依赖结果中有 2,366 字符被 500 字符上限直接丢弃；H1 侧超阈值的 1,521 字符全部落盘，另有 1,655 字符按规则完整 Inline。

## 7. 关键个案

**`e1-05-pairs` rep1 / H1（唯一的落盘 + 读取样例）**

- parser 产出 1,521 字符 → 判定超阈值，完整写入 `handoffs/ho-3fd4dd08b3f4a01e14c5c45e.txt`（复算 SHA-256 与元数据一致，长度 1,521）。
- summary Prompt 收到 300 字符预览 + 引用 `ho-3fd4dd08b3f4a01e14c5c45e` + 续读指令。
- 模型实际调用 `handoff.read({"ref_id": "ho-3fd4dd08b3f4a01e14c5c45e", "offset": 300})`，返回 1,221 字符，`next_offset=null`；300 + 1,221 = 全文。
- 仍然失败：`pairs.py:37 raise ValueError("invalid pair: 'a:1'")` —— producer 自己写错了分号/冒号切分。同 pair 的 H0 也以同样原因失败。**这是 producer 实现缺陷，不能归因于截断**，Handoff 只保证内容可达。

**`e1-12-conflict`（对照组）**

三个 worker 的输出都在 135–517 字符之间，两种模式下均完整 Inline（legacy 也未触顶）。H1 rep1 的失败是 worker `b` 自身 `side_effect_unknown` 失败，与 Handoff 无关：这些样本上 dual 与 legacy 语义等价，差异纯粹是模型轨迹噪声。

## 8. 失败归因（9 次失败）

| 归因 | 次数 | 说明 |
|---|---:|---|
| PRODUCER_FAILURE | 5 | 上游 worker 自身失败（`doom_loop` / `max_turns` / `side_effect_unknown`），下游必然受限 |
| ORACLE_FAILURE | 2 | 所有 worker 成功，隐藏测试仍失败（实现不正确） |
| CONSUMER_READ_STILL_FAILED | 1 | 消费方已读完全文，仍失败（上游实现错误） |
| BUDGET_EXCEEDED | 1 | `e1-07-export` H1 rep0，129,012 > 120,000 tokens；该 run 同时有 formatter 触顶 `max_turns`，归因按记录层先判预算 |
| HANDOFF_FAILURE / CONSUMER_DID_NOT_READ / PRODUCER 归因于截断 | 0 | 未出现引用错误、Digest 错误、越权访问或未读引用 |

预算占剩余失败 1/9 = 11.1%；prompt token 预算仍是该任务族的主要风险（`e1-07-export` 两次运行分别落在 114k / 129k）。

## 9. 局限与后续（不在本轮范围）

1. **样本量**：n=6 配对，且只有 1 次落盘样例。成功率的任何差异都在噪声范围内，不做显著性主张。
2. **任务形态不匹配**：Stage E1 三任务的依赖结果多为 200–1,800 字符，仅偶发越界；`dual` 与 `legacy` 大量时间等价。要检验 Handoff 的价值需要“上游结果明显长于 500 字符且下游确实依赖其细节”的样本，本轮按要求未新增或修改任务。
3. **失败主要发生在 producer 侧**，与 Handoff 正交；Token budget 与 Worker 执行策略未处理（按要求留到后续阶段）。
4. **成本信号**：completion tokens 上升主要来自个别 run 的轨迹差异，不足以说明 dual 更贵；`handoff.read` 每次约多 1 次 `dispatch_action`。
5. 未实现跨进程恢复流程；但已落盘的 Result 可被重新读取并通过 Digest 校验（单测覆盖）。

## 10. 变更清单

`git diff --stat`（已跟踪文件）：

```text
 .gitignore                               |   1 +
 experiments/evaluation/runner.py         |  32 +++-
 experiments/evaluation/stage_e_cli.py    |  27 ++++
 experiments/evaluation/stage_e_rounds.py | 106 ++++++++++++-
 tests/integration/test_anycode_dag.py    | 264 ++++++++++++++++++++++++++++++-
 5 files changed, 424 insertions(+), 6 deletions(-)
```

新增文件（未跟踪）：`src/atlasos/handoff.py`（777 行）、`experiments/evaluation/stage_g1_analysis.py`（268 行）、`tests/unit/test_handoff.py`（363 行）、`docs/stage-g1-report.md`（本文件）。

原始数据（未提交，`.gitignore` 已排除）：

- `experiments/results/stage-g1/g1-20261110T103003Z/manifest.json`
- `experiments/results/stage-g1/g1-20261110T103003Z/metrics.json` / `metrics.md`
- `experiments/results/stage-g1/g1-20261110T103003Z/runs/...`（12 个 run 目录，含 `result.json` 与 `handoffs/`）
