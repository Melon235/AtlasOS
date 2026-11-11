# Stage G2a — Run-scoped Token Budget 审计与最小修复

状态：实现 + 8 次真实运行完成。改动留在工作区，未执行任何 Git 写操作。

## 1. 结论摘要

| 问题 | 结论 |
|---|---|
| 修复前有无运行时预算准入？ | **没有**。120k 只是跑完之后的事后判定阈值；AnyCode 自带 `BudgetTracker` 在 AtlasOS 路径上 `guardrail_config=None`，`is_exhausted()` 恒为 False。 |
| 修复后是否阻止了预算不足的新请求？ | 是。guarded 4 次运行共 95 次请求，3 次在发送前被拒绝，`requests_started == requests_completed == 95`，无重复预留、无泄漏。 |
| 实际消耗是否仍超 120k？ | guarded **0/4 超限**（最高 114,640）；legacy **2/4 超限**（208,649 / 198,777，为上限的 1.74× / 1.66×）。 |
| 超额来源？ | guarded 无超额（`budget_overrun_tokens = 0`）。legacy 的超额来自"根本没有准入"，不是估算误差。 |
| 对成功率的影响？ | 两种模式都是 0/4。本轮不存在"被预算提前停掉的成功样本"，因此不能声称损失或提升成功率。 |
| 代价？ | guarded 总 token −24%（565,061 → 429,621），tool calls −39%（125 → 76），p50 延迟 −19%，成本 −19%。但该差异主要来自"更早停止"，不是推理效率提升。 |

控制边界（必须诚实声明）：预算按**请求前估算**预留，用仓库已有的 tokenizer 选择（tiktoken，装不上则退化为 chars/4 启发式）+ 1.15 安全系数 + 同 run 历史请求的实测比例校正；completion 按 `max_tokens` 预留。因此本实现是**并发安全的准入控制**，不是"绝对零超额"的数学保证。

## 2. G2a.1 现有预算调用链审计

审计对象：本地 `.venv` 中的 AnyCode 0.9.0 与当前工作区代码。

### 调用链

```text
experiments/evaluation/stage_e_cli.py         ← --budget-mode / --handoff-mode
  → experiments/evaluation/runner.py run_sample()
      ├─ RunnerSettings.token_budget = 120_000        （仅作为设置字段）
      ├─ ScopedWorkerFactory.create() → AgentConfig   （max_tokens=4096, max_turns=14）
      └─ AtlasContextEngine(config, tool_runtime=…, prompt_builder=…)
           → AnyCode.create_team → build_agent → AtlasToolAgent
                → Agent._get_runner()  (anycode/core/agent.py:87)
                     → create_adapter(provider)  → ResilientAdapter(max_attempts=1)
                     → AgentRunner(adapter, …)   (anycode/core/runner.py:107)
                          ├─ self._budget = BudgetTracker(guardrail_config)  (runner.py:132)
                          └─ while turn < limit (runner.py:306)
                               ├─ is_exhausted() 检查      (runner.py:307)   ← 唯一的请求前检查点
                               ├─ adapter.stream/chat → DeepSeek API
                               ├─ record_usage(response.usage) (runner.py:462) ← 响应后结算
                               └─ run_after_turn(response)     (runner.py:464)
```

### 逐条回答

1. **120k 在哪里定义？**
   `experiments/evaluation/runner.py` 的 `RunnerSettings.token_budget: int = 120_000`。它进入 `settings.digest()` 与 `environment["token_budget"]`，但**不进入**任何 `AgentConfig` 或 guardrail 配置。

2. **是实时限制还是评估后判定？**
   事后判定。`run_sample()` 的 `finally` 段比较 `metrics.prompt_tokens + metrics.completion_tokens > settings.token_budget`，超了就写 `failure_class = "BUDGET_EXCEEDED"`。判定发生在 `engine.run_tasks()` 返回之后。

3. **是否存在 LLM 请求前的预算检查？**
   代码里有一个（`AgentRunner.stream` 每轮开始调用 `self._budget.is_exhausted()`），但它在 AtlasOS 路径上**永远返回 False**：`self._budget = BudgetTracker(guardrail_config, …)`，而 `guardrail_config` 只在 `AnyCode.configure(guardrails=…)` 时被设置；AtlasOS 从未调用它。`BudgetTracker.is_exhausted()`（`anycode/guardrails/budget.py:71-82`）第一行就是 `if self._config is None: return False`。

4. **Token Usage 在请求前还是响应后更新？**
   响应后。`core/runner.py:461-462`：`cumulative_usage = merge_usage(cumulative_usage, response.usage)` 紧跟 `self._budget.record_usage(response.usage)`。

5. **多 Worker 是否共享 Usage 状态？**
   不共享。`Agent._get_runner()` 为每个 agent 单独构造 `AgentRunner`，每个 runner 自带一个 `BudgetTracker`（`anycode/core/agent.py:87-98` → `runner.py:132`）。`GuardrailConfig` 里虽然有 `max_tokens_per_team` 字段（`anycode/types.py:981`），但**整个 anycode 包内没有任何读取点**，是死字段。

6. **并发请求是否可能同时通过预算检查？**
   会。既没有预留（reservation）也没有锁：即便配置了 guardrails，每个 worker 只看自己那份计数器，两个 worker 可以同时判定"还有额度"，然后一起发出请求。

7. **Provider Retry 是否产生额外未计入的 Token？**
   机制上会：`create_adapter` 默认把适配器包进 `ResilientAdapter`（`anycode/providers/adapter.py:55-61`），重试在适配器内部发生，只有最终响应的 `usage` 回到上层，失败尝试的用量不可见。**但本轮运行不受影响**：评估路径的 `evaluation_resilience()` 明确设置 `max_attempts=1`（`runner.py:196-209`），且 OpenAI 客户端 `max_retries=0`，即每次模型调用只有一次真实请求。

8. **Prompt / Completion / Cache Hit / Cache Miss 如何计数？**
   - `DeepSeekAdapter.parse_usage()`（`src/atlasos/integrations/deepseek.py:49-94`）原样保留 provider 的 `prompt_tokens` / `completion_tokens` / `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`，两者不一致时不"修正"。
   - `_to_token_usage()`（同文件 `:132-148`）映射到 AnyCode 的 `TokenUsage`：`input_tokens = cache_miss`、`cache_read_input_tokens = cache_hit`。**所以 `TokenUsage.input_tokens` 不含 cache hit**。
   - `merge_usage()`（`anycode/helpers/usage_tracker.py:10-14`）只累加 `input_tokens`/`output_tokens`，会丢掉 cache 字段。
   - 评估指标走 AtlasOS 自己的 observation 通道（`_aggregate_observations`）：`prompt_tokens` 是 provider 报告的**含 cache hit** 的总量，与 120k 判定口径一致。

### 审计结论

缺少执行前准入，且并发 Worker 之间不共享任何预算状态；同时存在两个"看起来存在但实际不生效"的机制（`BudgetTracker` 未被配置、`max_tokens_per_team` 未实现）。因此本轮**需要**新增准入，且不应重复实现第二套通用资源调度器。

## 3. G2a.2–G2a.3 最小实现

### 3.1 `src/atlasos/budget.py`（新增，唯一新运行时模块）

- **`RunBudgetState`**：一个 run 一份，字段为 `limit_tokens / used_prompt_tokens / used_completion_tokens / reserved_tokens`，外加 `peak_reserved_tokens`、`requests_started/completed/written_off`、`admission_rejections`、`budget_overrun_tokens`、估算误差与逐 worker 计数。
  - `asyncio.Lock` 保护 `admit` / `settle` / `abandon` / `write_off`；**锁内不调用 LLM**。
  - `admit()`：`requested = ceil(raw_estimate × 1.15 × learned_ratio) + completion_reserve`，与 `remaining = limit − used − reserved` 原子比较；不够就抛 `RunBudgetExhausted`，够了就登记 reservation（含 request_id）。
  - `settle()`：释放该 reservation，按 provider 真实 usage 记账；实际 > 预留时累加 `budget_overrun_tokens`。
  - `write_off()` / `finalize()`：结果未知的请求按**预留额**保守记账（不假定为 0 释放）。
- **`RunBudgetHook`（AnyCode `TurnHook` 实现）**：每个 agent 一个实例，共享同一个 `RunBudgetState`。
  - `before_turn(messages, ctx)` 在 `AgentRunner` 发出请求前执行（`core/runner.py:322`），估算 prompt = tiktoken(messages) + system prompt + 原生工具 schema 文本，然后 `state.admit(...)`。
  - `after_turn(response, ctx)` 在响应后执行（`core/runner.py:464`），用 `response.usage` 结算。`prompt_tokens_from_usage()` 把 `input_tokens + cache_read + cache_creation` 加回，保证 cache hit **只计一次**。
- **估算来源**：`anycode.context.tokenizer.select_tokenizer("tiktoken", …)`（仓库既有能力，tiktoken 已安装，`confidence="tokenizer"`）；不把字符数当 token 数使用。

### 3.2 接入点

- `AtlasContextEngine(config, *, prompt_builder, tool_runtime, budget_state=None)`：仅新增一个可选参数。`build_agent()` 在原有 hooks 之后追加 `RunBudgetHook`（放最后，确保估算看到 runner 真正会发送的 prompt）。`budget_state` 与 `tool_runtime` 缺一会直接报错，避免静默失效。
- `AtlasToolAgent`：新增 `budget_hook` 引用与一个后置条件。**这是必需的**：`AgentRunner.stream` 把 turn 循环内的任何异常转成 `error` 事件后直接 `return`（`core/runner.py:769-784`），`Agent.run` 拿不到 `done` 就退回空 `RunResult(stop_reason=None)`，在 `Agent._execute_run` 里被判为 `success=True`。若不处理，预算拒绝会变成"空输出但成功"的任务——正是本轮明令禁止的伪装。因此 `_execute_run()` 在 `take_rejection()` 非空时把结果改写为 `success=False, output="budget_exhausted: …", stop_reason=StopReason(code="budget_exceeded")`。这是 AtlasOS 自己子类里的 5 行后置条件，没有 fork AnyCode。
- `experiments/evaluation/runner.py`：`RunnerSettings.budget_mode`（进入 digest/environment）、每个 run 创建 `RunBudgetState`、`finally` 里 `finalize()` 并写 `metrics.extra["budget"]`、拒绝次数 > 0 时把 `failure_class` 归为 `BUDGET_EXCEEDED`（事后观测判定仍作为兜底保留）。
- CLI/轮次：`--budget-mode legacy|guarded`；`stage_e_rounds.py` 新增 `g2a` suite（两臂都跑 A1 + dual handoff，唯一变量是 `--budget-mode`）。

未修改：AnyCode 源码、DAG Scheduler、TaskQueue、Checkpoint、Worker 数量/Profile、14 turn / 60 tool call / 120k 预算、G1 的 HandoffStore/Projector/`handoff.read`、M3–M6 语义、V1 Context Plane、CLI/Web、包名。

### 3.3 预算耗尽行为

- 拒绝发生在 **provider 调用之前**（`before_turn`），不会产生费用。
- 该 worker 的 `AgentRunResult` 被改写为 `stop_reason.code="budget_exceeded"`、`output="budget_exhausted: …"`；AnyCode 走既有 `queue.fail(task.id, result.output)` 路径，下游按既有依赖语义失败。
- 不重试、不自动加预算、不截断模型输出；已经进入 provider 的在途请求正常结算（不强制终止）。

## 4. 测试与回归

新增 `tests/unit/test_budget.py`（15 项）：单 worker 累计；两个 worker 共享同一 state；并发 700/700 只能成功一个；拒绝发生在准入之前（`requests_started == 0`）；预留多、实际少时释放；实际超预留时记录 overrun 且比例封顶；provider 报错时保留预留并在 `finalize` 按预留额核销；取消时不被错误释放；确认未发送才允许 `abandon`；不同 run 互不影响；prompt+completion 计数且 cache 不重复；后续请求看到的是全 run 剩余（而非本 worker 用量）；估算比例随实测收敛；拒绝后 `stop_reason` 可读；非法 limit 拒绝构造。

扩展 `tests/integration/test_anycode_dag.py`（+2 项）：低预算下两个 worker 的请求**全部在 provider 之前被拒**（`calls == []`，`run.success is False`，两个 worker 输出均以 `budget_exhausted` 开头），并通过 `RunBudgetState` 断言 `requests_started == 0`、`peak_reserved_tokens == 0`；预算充足时 2 次请求全部结算，`used_prompt_tokens/used_completion_tokens` 与 fake usage 完全一致。G1 的 dual handoff 集成测试同文件继续通过。

```text
pytest -q          : 496 collected (1 live deselected), all passed
ruff check .       : All checks passed
ruff format --check: 307 files already formatted
pyright            : 0 errors, 0 warnings, 0 informations
```

## 5. G2a.5 真实对照（8 次）

设置：`e1-02-report`、`e1-07-export`（Stage E1 中 token 消耗最高的两个任务）× `legacy|guarded` × 2 次。两臂都固定 `--handoff-mode dual`；模型 `deepseek-chat`、temperature 0.0、120k budget、14 turn、60 tool call、OpenSandbox 配置均不变。结果目录：`experiments/results/stage-g2a/g2a-20261110T112744Z/`。

另有一次接线冒烟（`e1-07-export-a1` rep99，发现估算漏算工具 schema）与一次修正后冒烟（rep98），**不计入** 8 次对照。

### 5.1 逐次结果

| 任务 | rep | 臂 | 成功 | prompt | completion | 合计 | 峰值预留 | 拒绝 | overrun | tools | 延迟 s | 归因 |
|---|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| e1-02-report | 0 | b0 legacy | 否 | 201,358 | 7,291 | **208,649** | 0 | 0 | 0 | 39 | 81.0 | BUDGET_OVERRUN |
| e1-02-report | 0 | b1 guarded | 否 | 94,429 | 4,894 | 99,323 | 36,523 | 1 | 0 | 21 | 54.8 | BUDGET_STOP |
| e1-07-export | 0 | b0 legacy | 否 | 86,688 | 4,410 | 91,098 | 0 | 0 | 0 | 25 | 65.8 | ORACLE_FAILURE |
| e1-07-export | 0 | b1 guarded | 否 | 102,423 | 4,588 | 107,011 | 15,204 | 0 | 0 | 21 | 62.6 | ORACLE_FAILURE |
| e1-02-report | 1 | b0 legacy | 否 | 190,677 | 8,100 | **198,777** | 0 | 0 | 0 | 43 | 80.7 | BUDGET_OVERRUN |
| e1-02-report | 1 | b1 guarded | 否 | 110,094 | 4,546 | 114,640 | 26,132 | 2 | 0 | 10 | 55.7 | BUDGET_STOP |
| e1-07-export | 1 | b0 legacy | 否 | 63,070 | 3,467 | 66,537 | 0 | 0 | 0 | 18 | 57.7 | PRODUCER_FAILURE |
| e1-07-export | 1 | b1 guarded | 否 | 105,720 | 2,927 | 108,647 | 15,690 | 0 | 0 | 24 | 75.4 | PRODUCER_FAILURE |

### 5.2 汇总

| 臂 | 运行 | 成功 | prompt | completion | 合计 | 超 120k | 峰值预留 | 请求 开始/完成/核销 | 拒绝 | overrun | 估算误差 | 学习比例 |
|---|---:|---:|---:|---:|---:|---:|---:|---|---:|---:|---:|---:|
| b0 legacy | 4 | 0/4 | 541,793 | 23,268 | 565,061 | 2/4 | 0 | 0/0/0 | 0 | 0 | 0 | 1.00 |
| b1 guarded | 4 | 0/4 | 412,666 | 16,955 | 429,621 | **0/4** | 36,523 | 95/95/0 | 3 | 0 | 32,990 | 1.08 |

配对差（guarded − legacy，n=4）：prompt −32,282、completion −1,578、tool calls −12.25、延迟 −9,150 ms、成本 −$0.00243。

### 5.3 拒绝详情（guarded）

```text
e1-02-report rep0 → stats 需要 ~18,093 tokens（已用 88,865，已预留 19,264），仅剩 11,871 / 120,000
e1-02-report rep1 → render 需要 ~16,714 tokens（已用 109,568，已预留 9,368），仅剩 1,064 / 120,000
```

两次拒绝都发生在 prompt 已经膨胀到 1.6万–1.8万 token 的请求上；若放行，该 run 必然突破 120k。

被拒 worker 的落地记录（`e1-02-report` rep0 b1 的 `stats`）：

```text
success=False  stop_reason=budget_exceeded  output="budget_exhausted: worker …stats needs ~18093 tokens …"
```

即：明确的停止原因、保留已完成执行记录、不伪装为模型成功、不自动重试。

## 6. G2a.6 指标分析

### 6.1 预算控制是否有效？

- **阻止了预算不足的新请求**：是，3 次拒绝全部发生在 provider 调用之前；`e1-02-report` 两个 guarded run 分别在 99,323 / 114,640 处停下，没有一次越过 120k。
- **是否出现并发重复预留**：没有。`requests_started == requests_completed == 95`、`requests_written_off == 0`、`reserved_tokens` 结算后回到 0，说明预留-结算严格配对，没有两个 worker 共用同一份剩余额度。
- **实际消耗是否仍超 120k**：guarded 0/4；legacy 2/4（208,649、198,777）。
- **超限原因**：guarded 无超限，`budget_overrun_tokens == 0`，说明没有出现"实际用量大于预留"的请求。legacy 的超限不是估算误差，而是**完全没有准入**。
- **Provider Usage 是否缺失**：没有。95 次请求全部拿到 usage 并完成结算（`requests_completed == requests_started`），预算账本与观测账本逐 run 完全一致（如 rep0 b1：`used_prompt_tokens 94,429 == metrics.prompt_tokens 94,429`）。
- **估算质量**：raw 估算的绝对误差合计 32,990 tokens / 95 次请求 ≈ 347 tokens/次；收敛后的修正比例 1.08。峰值预留最高 36,523（约 30% 预算），保守但可控。

### 6.2 对模型执行的影响

- **成功率**：两臂均 0/4，没有出现"因更早停止而损失成功样本"的情况，也就不能声称收益。
- **延迟**：guarded p50 59.1s vs legacy 73.2s（更早停止，不是更快推理）。
- **新的失败类型**：guarded 引入 `BUDGET_STOP`（明确、可读），替代了 legacy 的 `BUDGET_OVERRUN`（跑完才发现超了）。`e1-07-export` 两臂失败原因都是 ORACLE_FAILURE / PRODUCER_FAILURE，与预算无关。
- **Worker 能否正常停止**：能。每个被拒 worker 只被拒一次就结束（`rejected=1`），没有循环重试；未出现无法停止的 worker。
- **提前停止的代价**：`e1-02-report` rep0 停在 99,323，仍剩约 20k 未用——因为当时另一个 worker 的在途请求占着 19,264 预留，而该请求需要 18,093。这是在"不让 Run 超支"和"不误杀可用额度"之间的保守取舍。

### 6.3 与历史问题的关系

Budget Guard **只限制资源消耗**，不解决以下问题，本轮也没有触及：

- Worker 重复探索 workspace（`e1-02-report` legacy 用了 39–43 次 tool call，guarded 也仍有 10–24 次）；
- Worker 子任务过大导致单轮 prompt 膨胀（本次两次拒绝都源自 1.6万–1.8万 token 的单次 prompt）；
- Worker 提前报告成功；
- Handoff 内容完整但上游代码本身错误（G1 已确认）。

因此 **token 消耗下降不能解释为 Agent 推理效率提升**：本轮 guarded 更省，是因为它在预算边界上被强制停下。

## 7. 已知问题与下一阶段建议

1. **估算不是上界**：`tiktoken(cl100k)` 是 DeepSeek 分词器的近似；completion 按 `max_tokens` 预留而不是真实输出。若 provider 以重试方式重新计费（评估配置已用 `max_attempts=1` 规避），预算账本会低估。要获得更硬的保证需要在适配器层拿到请求级 token 上界，这属于后续阶段。
2. **提前停止的保守性**：在途预留会占用剩余额度（本次最高 36.5k），可能让 Run 在仍有 1–2 万可用额度时停下。是否收紧（例如对已知稳定 prompt 降低预留）需要在更多样本上评估，本轮未做。
3. **单进程语义**：`asyncio.Lock` 只保证同一 Runtime 进程内一致；跨进程/分布式一致性不在本轮范围内，也不在本报告中宣称。
4. **样本量**：8 次运行、2 个任务、4 个配对。成功率差异（0/4 vs 0/4）不构成统计结论，只能说明"预算控制在真实运行中生效且不伪装成功"。
5. **下一阶段候选**（按依赖顺序，本轮不实施）：Scoped Context Optimization（降低单轮 prompt 体积，从根上减少拒绝概率）→ Worker 重复探索治理 → Token Budget 与 Worker 调度联动。

## 8. 变更清单

`git diff --stat`（已跟踪文件）：

```text
 .gitignore                                 |   2 +
 experiments/evaluation/runner.py           |  53 +++-
 experiments/evaluation/stage_e_cli.py      |  54 ++++
 experiments/evaluation/stage_e_rounds.py   | 210 +++++++++++++++-
 src/atlasos/integrations/anycode_engine.py |  57 ++++
 src/atlasos/tools/conversation.py          |  33 +++
 tests/integration/test_anycode_dag.py      | 417 ++++++++++++++++++++++++++++++-
 7 files changed, 820 insertions(+), 6 deletions(-)
```

新增未跟踪文件（G2a 部分）：`src/atlasos/budget.py`、`experiments/evaluation/stage_g2a_analysis.py`、`tests/unit/test_budget.py`、`docs/stage-g2a-report.md`（另含 G1 遗留未提交文件：`src/atlasos/handoff.py`、`experiments/evaluation/stage_g1_analysis.py`、`tests/unit/test_handoff.py`、`docs/stage-g1-report.md`）。

原始数据（未提交，`.gitignore` 已排除）：

- `experiments/results/stage-g2a/g2a-20261110T112744Z/manifest.json`
- `experiments/results/stage-g2a/g2a-20261110T112744Z/metrics.json` / `metrics.md`
- `experiments/results/stage-g2a/g2a-20261110T112744Z/runs/...`（8 个 run 目录，每个含 `result.json`，其中 `metrics.budget` 为完整账本）

Git 写操作：**未执行**（无 add/commit/push/tag/reset/checkout/clean）。
