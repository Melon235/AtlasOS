# Stage G2b-1 — Recoverable Tool Result Compaction（C0 off vs C1 rolling_2）

实现完成、机制可用、12 次真实对照完成。结论是**中性偏负**：压缩确实生效且可无损恢复，但在本样本中没有降低总 Prompt Token，反而因为 cache 前缀失效与执行轮次增加而提高了成本。报告按实测如实记录，不把"少跑几轮"当成压缩的收益。

未执行任何 Git 写操作；未修改 AnyCode、M1–M7、G1 Handoff、G2a Budget 的既有语义。

---

## 1. 调用链核对（G2b-1.1）

按本地实际代码（`.venv` 中 AnyCode 0.9.0 + 当前工作区）逐条确认：

| 问题 | 结论（证据） |
|---|---|
| `before_turn` 执行顺序 | `HookRunner.run_before_turn` 按注册顺序串行，每个 hook 的返回值作为下一个的输入（`guardrails/hooks.py:21-26`） |
| G2a Budget Hook 位置 | 原先注册在最后（`AtlasContextEngine.build_agent`）。本轮调序后为 **compaction → snapshot → budget** |
| M6 Conversation Snapshot 位置 | `ConversationSnapshotHook.before_turn`（`atlasos/tools/conversation.py:125-134`），在本轮调序后位于 compaction 之后 |
| `before_turn` 返回值是否成为后续状态 | **是**。`core/runner.py:322`：`conversation = await self._hook_runner.run_before_turn(conversation, ctx_info)`，返回值替换会话本身，后续 assistant/tool 消息追加在其后 |
| 是否启用 AnyCode ContextManager | **否**。`AgentConfig.context_policy` 默认 `None` → `AgentRunner._context_manager is None`（`core/runner.py:137-140`）→ `prepared_messages = conversation`（`:359-361`），即 hook 输出**就是**发往 provider 的内容 |
| Tool Result 的 ContentBlock 类型 | `ToolResultBlock(tool_use_id, content: str, is_error: bool|None)`，`LLMMessage` 与所有 block 均为 frozen pydantic 模型（`types.py:32-37`, `:80-83`） |
| Resume 时历史与 Task ID 如何恢复 | `AtlasToolAgent.run()` 用 `CURRENT_TASK_ID` 从 `ConversationStore` 取回快照并用 `LLMMessage.model_validate` 重放（`atlasos/tools/conversation.py:473-480`）；Task ID 来自 `set_current_task_id`（由 orchestrator 的 `task_start` 事件驱动），不是模型输入 |

三个直接结论：

1. 压缩可以只靠 `before_turn` 完成，且**会持久化**到会话（不会每轮重复归档）。
2. 因为 `context_policy is None`，**预算 hook 看到的 prompt 就是真正发出的 prompt**，不存在"hook 之后还被改写"的偏差（这一点在报告里可以明确声明，而不是保留估计边界）。
3. 快照必须放在 compaction **之后**，否则 resume 会重放未压缩历史，等于压缩白做。

---

## 2. 实现（G2b-1.2 ~ G2b-1.5）

### 2.1 新增模块 `src/atlasos/history_compaction.py`

- **`HistoryStore`**：`<run_dir>/history/`，内容寻址（`ref_id = hc-<24 hex>`，种子为 `run_id + task_id + tool_use_id + sha256(content)`）。
  - UTF-8 原文 + 元数据（`run_id / task_id / tool_use_id / sha256 / char_count / byte_count / created_at`）；
  - 原子写入（`tmp` + `os.replace`）；先写 payload 再写 metadata；
  - 同内容重复压缩幂等（同一 `ref_id`，不产生第二个文件）；
  - 读取时校验长度与 SHA-256，并校验 `run_id`、`task_id`；
  - 权限绑定写入磁盘元数据，**不依赖内存字典**，进程重启后仍可验证。
- **`ToolResultCompactionHook`**（AnyCode `TurnHook`）：
  - 轮次从消息列表**推导**（`assistant` 消息计数即轮次），不依赖 hook 本地计数器 → resume 后判断一致；
  - 轮次定义与 G2b-0 反事实模拟一致：当前即将发送第 K 轮时，压缩 `t ≤ K-2-1` 的 tool result，保留最近 2 轮；
  - 仅处理 `ToolResultBlock`，且原文 > 800 字符、替换后更短、未含 `[HISTORY_RESULT_OFFLOADED]` 标记；
  - **先落盘并校验成功，才替换消息**；落盘失败保留原文并计数 `archive_failures`；
  - copy-on-write（`model_copy`），不改共享对象；保留 role、`tool_use_id`、`is_error`、tool_use 块与消息顺序；
  - 错误结果保留 `is_error=True`，占位符内附 ≤220 字符的错误摘要，不把错误伪装成成功。
- **`history.read`**：`ref_id / offset / limit`（`limit ≤ 4000`），单页 ≤4000 字符，返回 `content / total_chars / next_offset / sha256`；身份取自 `ExecutionContext.attributes` 的 `run_id` + `task_id`；跨 run、跨 worker、非法引用、Digest 失败全部拒绝；经 `AtlasToolRuntime(external_tools=...)` 注册，走 `tool.search → dispatch_action → M1 → M3 → ToolExecutor`，无第二个执行器。
- **`CompactionMetrics`**：`compaction_events / results_archived / original_chars_archived / placeholder_chars / history_bytes_stored / archive_failures / read_calls / read_success / read_denied` + 逐 worker 计数 + 样本。

### 2.2 接线

- `AtlasContextEngine(config, ..., budget_state=…, compaction=…)`：hook 顺序 `[*engine_hooks, compaction, conversation_snapshot, budget]`。`compaction` 需要 `tool_runtime`，否则构造即报错（避免静默失效）。
- `experiments/evaluation/runner.py`：`RunnerSettings.compaction_mode`（进入 digest/environment）；每个 run 创建 `HistoryCompactionRuntime`；`external_tools = handoff.external_tools() + compaction.external_tools()` → **两个实验臂工具注册表与 Schema 完全一致**；`metrics.extra["compaction"]` 落盘。
- `stage_e_cli.py`：`--compaction-mode off|rolling_2`。
- `stage_e_rounds.py`：新增 `g2b1` suite（C0/C1 交替顺序）。
- `stage_g2b1_analysis.py`：复用 Stage E1 聚合、G1 归因、G2b-0 的逐轮重建。

未改动：AnyCode、TaskQueue/Scheduler/Checkpoint、G1 `HandoffStore/Projector/handoff.read`、G2a `RunBudgetState` 的预留与结算、Worker 数量/Profile、Fixture/Oracle、120k/14 turn/60 tool call。

### 2.3 触发时机与恢复路径（交付要求）

- **触发时机**：每轮 `before_turn` 时，若已完成轮次 `K ≥ 3`（即存在 ≥3 个 tool round），则第 `K-2` 轮及更早、长度 > 800 字符的 tool result 被归档并替换。压缩发生在**请求之前**，因此 budget 估算看到的是压缩后的上下文。
- **恢复**：`history.read` 从磁盘按 `ref_id` 读回；权限同时校验 `run_id` 与 `task_id`；Digest 不符直接拒绝。
- **M6 Resume**：快照保存的是**压缩后**的会话；恢复后仍是同一批引用，`history.read` 仍可读取（单测 `test_round_boundary_is_derived_from_messages_not_a_counter` 与集成测试的快照断言覆盖；真实运行中 resume 未被触发，故只有确定性证据）。

---

## 3. 测试与回归

新增 `tests/unit/test_history_compaction.py`（16 项）：off 模式零改动；最近两轮保留；第三轮起压缩；同轮多结果一起压缩；短结果不压缩；结构与 `tool_use_id`/`is_error` 保持；分页无损还原；Digest 失败拒绝；跨 worker/跨 run/匿名拒绝；重复压缩不产生新文件；**写盘失败保留原文**；恢复后轮次判断一致；两模式工具 Schema 一致；非法模式拒绝；`after_turn` 直通。

扩展 `tests/integration/test_anycode_dag.py`（+3 项）：6 轮工具调用的真实 `AtlasContextEngine` 运行 → 最终 prompt 中旧结果已成引用、最近两轮原文保留、`history.read` 取回旧结果并出现在下一轮 prompt；off 模式全程无引用；hook 顺序断言为 `ToolResultCompactionHook → ConversationSnapshotHook → RunBudgetHook`，且 budget 结算次数等于请求数。

```text
pytest -q          : 514 passed, 0 failed, 0 errors, 0 skipped (1 live deselected; junit 复核)
ruff check .       : All checks passed
ruff format --check: 313 files already formatted
pyright            : 0 errors, 0 warnings, 0 informations
```

G1 / G2a 原有测试全部继续通过（集成文件 10 项全绿）。

---

## 4. 真实对照（12 次）

设置：`e1-02-report`、`e1-10-search`、`e1-12-conflict`；两臂均为 `handoff=dual`、`budget=guarded`，唯一变量是 `compaction=off|rolling_2`；每任务每臂 2 次，顺序交替（rep0：C0→C1；rep1：C1→C0）。模型 `deepseek-chat`、temperature 0.0、120k budget、14 turn、60 tool call、OpenSandbox 全部不变。结果目录：`experiments/results/stage-g2b1/g2b1-20261110T164723Z/`。

另有一次冒烟（`e1-02-report-a1` rep99，C1）用于验证消息结构与归档，**不计入** 12 次对照；该次归档 9 条、30,953 字符、0 失败、0 读取。

### 4.1 逐次结果

| 任务 | rep | 臂 | 成功 | Prompt | Turns | tok/turn | 归档数 | history.read | 预算拒绝 | 归因 |
|---|---:|---|---|---:|---:|---:|---:|---:|---:|---|
| e1-02-report | 0 | C0 | 否 | 106,372 | 27 | 3,940 | 0 | 0 | 2 | BUDGET_STOP |
| e1-02-report | 0 | C1 | 否 | 107,381 | 26 | 4,130 | 7 | 0 | 3 | BUDGET_STOP |
| e1-10-search | 0 | C0 | 否 | 109,032 | 28 | 3,894 | 0 | 0 | 1 | BUDGET_STOP |
| e1-10-search | 0 | C1 | 否 | 112,299 | 33 | 3,403 | 4 | 0 | 0 | PRODUCER_FAILURE |
| e1-12-conflict | 0 | C0 | 否 | 15,800 | 8 | 1,975 | 0 | 0 | 0 | PRODUCER_FAILURE |
| e1-12-conflict | 0 | C1 | **是** | 28,851 | 13 | 2,219 | 0 | 0 | 0 | TASK_SUCCESS |
| e1-02-report | 1 | C0 | 否 | 90,675 | 24 | 3,778 | 0 | 0 | 0 | ORACLE_FAILURE |
| e1-02-report | 1 | C1 | 否 | 101,836 | 27 | 3,772 | 5 | 0 | 1 | BUDGET_STOP |
| e1-10-search | 1 | C0 | 否 | 112,557 | 26 | 4,329 | 0 | 0 | 3 | BUDGET_STOP |
| e1-10-search | 1 | C1 | 否 | 112,907 | 29 | 3,893 | 9 | 0 | 2 | BUDGET_STOP |
| e1-12-conflict | 1 | C0 | **是** | 16,531 | 8 | 2,066 | 0 | 0 | 0 | TASK_SUCCESS |
| e1-12-conflict | 1 | C1 | **是** | 29,640 | 13 | 2,280 | 0 | 0 | 0 | TASK_SUCCESS |

### 4.2 汇总

| 臂 | 成功 | Prompt | Completion | Turns | Tool Calls | Cache Hit | Cache Miss | Hit 率 | 成本 | p50 延迟 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| C0 off | 1/6 | 450,967 | 17,695 | 121 | 78 | 391,296 | 59,671 | 86.8% | $0.0415 | 59.3 s |
| C1 rolling_2 | 2/6 | 492,914 | 19,547 | 141 | 98 | 380,800 | 112,114 | 77.3% | $0.0594 | 62.0 s |

配对差（C1 − C0，n=6）：prompt **+6,991**（min +350，max +13,109），completion +309，tool calls +3.3，延迟 +7.6 s，成本 **+$0.0030**。

### 4.3 压缩本身

| 臂 | compaction_events | 归档结果 | 归档字符 | 占位字符 | 落盘字节 | 失败 | read 调用/成功/拒绝 |
|---|---:|---:|---:|---:|---:|---:|---|
| C0 | 0 | 0 | 0 | 0 | 0 | 0 | 0/0/0 |
| C1 | 25 | 25 | 82,153 | 7,022 | 82,153 | **0** | **0/0/0** |

- 归档/占位字符比 **11.7 : 1**（82,153 → 7,022，-91.5%）；
- `archive_failures = 0`，说明落盘-校验-替换的顺序在真实运行中没有出现"归档失败仍替换"的情况；
- **模型一次都没有调用 `history.read`**（12 次运行 0 次），所以"worker 因压缩而增加读取"在本样本中不成立，同时"模型实际用到了恢复路径"也**没有被真实运行验证**，只由确定性测试覆盖。

---

## 5. 指标分析（G2b-1.6 的十个问题）

1. **实际 Prompt Token 是否减少？** **没有**。C1 总量比 C0 高 9.3%（492,914 vs 450,967）。
2. **Cache Hit / Miss 是否变化？** 明显变化，且是负向的：miss 从 59,671 涨到 112,114（**+88%**），hit 率 86.8% → 77.3%。被替换的旧结果破坏了稳定前缀，之后每一次请求都少命中一段缓存。
3. **实际 Cost 是否下降？** 没有，上涨 43%（C1/C0 = 1.43）。成本上涨几乎完全来自 cache-miss token。
4. **历史原文是否可无损恢复？** 机制上可以：单测覆盖分页逐字节还原，冒烟运行中 9 个归档文件 SHA-256 全部校验通过。**真实对照中没有一次被读取**，所以"实际使用中的无损恢复"缺少运行期证据。
5. **Worker 是否因压缩而增加 `history.read`？** 没有（0 次）。
6. **是否出现压缩后重复执行工具或重新探索？** 没有出现"重读被压缩文件"的行为（`file_read`/`bash` 调用分布与 C0 同量级），但 C1 的 tool call 总数更高（98 vs 78），差异主要来自多跑的轮次。
7. **Budget Guard 是否因上下文缩小而减少拒绝？** 没有（6 vs 6）。C1 因为多跑轮次把省下的空间又用掉了。
8. **Worker `max_turns` 是否减少？** 没有。C1 出现 2 次 `max_turns`，C0 出现 2 次；C0 更多以 `budget_exceeded` 结束（6 次），C1 为 6 次。
9. **Oracle Success 是否改善或退化？** 1/6 → 2/6，属于 n=6 的噪声范围，**不构成改善证据**。
10. **是否引入新的失败类型？** 没有。C1 的失败类型仍是 `BUDGET_STOP` / `ORACLE_FAILURE` / `PRODUCER_FAILURE`，且未出现消息结构、权限或 Digest 相关错误。

### 5.1 必须区分的两件事

- **每轮 prompt 确实小了，但只在部分 run 中**：
  - `e1-10-search`（两次）每轮 prompt 从 3,894 → 3,403（-12.6%）与 4,329 → 3,893（-10.1%），这是压缩生效的直接证据；
  - `e1-02-report` 每轮 3,940 → 4,130（+4.8%）与 3,778 → 3,772（-0.2%），几乎没有变化，因为该任务的膨胀来自少数几次超大 bash 输出，而那几次输出在 C1 中也被"最近两轮"保护着；
  - `e1-12-conflict` **两臂完全等同**（`archived = 0`，所有 tool result < 800 字符）。
- **总 token 不能归因于压缩**：C1 多跑了 20 个 turn。而 `e1-12-conflict` 两臂功能上完全一致却出现 8 → 13 turn 的固定差异，说明轮次差异主要来自"run 身份不同导致的轨迹不同"（任务 prompt 里包含 `task_id`，其中含 run_id），**不是压缩造成的**。所以既不能说 C1 更啰嗦，也不能把 C0 的低 token 当作压缩失败。

G2b-0 的 31.7% 反事实上界在本轮**没有兑现**；实测每轮下降最多约 13%，且在总账上被 cache-miss 与轮次噪声抵消。

---

## 6. 已知限制与下一阶段建议

### 6.1 限制

1. **样本量**：n=6 配对，且 `e1-12-conflict` 的两次配对完全没触发压缩（等于两个天然对照组）。任何成功率/轮次结论都不具统计意义。
2. **零读取**：模型从未调用 `history.read`。原因无法从记录判定（可能是不需要，也可能是"最近两轮还在，够用"）。恢复路径的真实可用性只有确定性测试 + 冒烟归档校验支撑。
3. **cache 前缀破坏是结构性的**：只要替换历史内容，就会让该点之后的缓存失效。对 DeepSeek 这种"按前缀命中"的缓存，压缩与缓存复用天然冲突，本轮实测的 9% token 增益远小于 88% 的 miss 增长。
4. **`e1-12` 的轮次差异**（8 vs 13）不可归因于压缩，提示后续对照必须控制 `task_id`/run_id 对 prompt 的影响，或至少在同一 run 内做 A/B。
5. **800 字符阈值与"最近两轮"是工程取值**，本轮未做敏感性扫描。

### 6.2 下一阶段唯一优先建议

**先解决"压缩 vs 缓存"的冲突，再谈压缩收益**：把压缩目标从"替换任意历史 tool result"改为**只在缓存已无复用价值的边界上压缩**（例如仅在上下文接近窗口/预算压力时触发一次性重写，而不是每轮增量替换），或者把被压缩内容放到**稳定前缀之外**（尾部追加一个引用清单）以保持前缀稳定。

换句话说：G2b-1 证明了"可恢复压缩"在工程上是安全的（0 归档失败、0 协议破坏、0 权限越界），但**它不是一个划算的默认策略**——在 120k 预算、14 轮上限、DeepSeek 前缀缓存的组合下，增量压缩的收益被缓存失效吃掉。下一阶段应做的是"**预算压力触发的单次重写**"，而不是继续增加压缩频率。

不建议现在做：LLM 摘要压缩、Assistant Message 压缩、Scoped Context、动态预算分配——都需要先解决缓存前缀问题，否则只是把同样的成本换个地方付。

---

## 7. 变更清单

见交付说明中的 `git diff --stat`。新增文件：

- `src/atlasos/history_compaction.py`（HistoryStore / ToolResultCompactionHook / history.read / CompactionRuntime）
- `tests/unit/test_history_compaction.py`
- `experiments/evaluation/stage_g2b1_analysis.py`
- `docs/stage-g2b1-report.md`

修改文件：

- `src/atlasos/integrations/anycode_engine.py`（compaction 注入 + hook 调序）
- `experiments/evaluation/runner.py`（compaction 模式、工具注册、指标）
- `experiments/evaluation/stage_e_cli.py`（`--compaction-mode`）
- `experiments/evaluation/stage_e_rounds.py`（`g2b1` suite）
- `tests/integration/test_anycode_dag.py`（3 项集成测试）

原始数据：`experiments/results/stage-g2b1/g2b1-20261110T164723Z/`（manifest、metrics、12 个 run 目录，每个含 `result.json` 与 `history/`）。
