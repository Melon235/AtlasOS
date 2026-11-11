# Stage G3-0 — Multi-Worker Failure Critical-Path Audit

只读审计。未修改 `src/atlasos/`、AnyCode、Scheduler/TaskQueue/Checkpoint、M1–M7、G1 Handoff、G2a Budget、G2b-1 Compaction、Benchmark/Fixture/Oracle；未调用模型、未启动 Sandbox、未执行任何 Git 写操作。

样本：`experiments/results/stage-e1/e1-20261109T102030Z/`（71 个 run 有 `result.json`），派生数据：`experiments/results/stage-g3-audit/`，脚本：`experiments/evaluation/stage_g3_failure_audit.py`。

---

## 1. Executive Summary

1. **`side_effect_unknown` 不是"结果未知"，而是"副作用工具返回了错误"。** AtlasOS 的 `bash` 把非零退出码映射成 `is_error=True` 且不设 `retry_safe`；AnyCode 的 `_normalize_result` 对"side-effecting 工具的错误结果"一律推断 `retry_safe=False`，`AgentRunner` 随即用 `side_effect_unknown` 终止该 worker。审计的 3 个样本里，M4 轨迹全部记录为 `completion_kind=EXECUTED_ERROR`（命令已执行完、有输出），却被当作不可恢复。
2. 最干净的一条因果链：`e1-12-conflict-a1`（D-2）中 worker `a` 第 1 轮执行 `ls -la /workspace/project`（退出码 2，仅 `ls: cannot access`），worker 当场被判定 `side_effect_unknown` 终止，`state.json` 少了 `{"a":1}`，最终 oracle 失败。**该 run 的失败完全由这一处分类映射造成。**
3. **`max_turns` 的主因不是"任务太大"，而是 worker 把轮次花在搜索评分产物上。** 最贵 run `eval-62dae7e46b5f`（e1-02-report-a1，364,589 prompt tokens）中，36 个 tool turn 里有 **16 个（44%）** 在 `/tmp` 下 `cat *.stdout`、`git bundle verify`、`git log --all`、`find /`，注入 **104,737 字符**探针输出；`stats` worker 的最后 5 轮全部在读取测试 stdout，从未写自己的 `report/stats.py`（该文件被 `render` worker 在第 12 轮代写）。
4. **"所有 worker 成功但 oracle 失败"是可复现的集成契约缺口**：`e1-07-export` 两个样本里 `formatter.py`/`exporter.py` 都无 `NotImplementedError` 残留，但两者对 `' | '` 分隔与行尾 `\n` 的约定不一致（`'a | b '` / `'a b'` vs 期望 `'a  | b \n'`），没有任何 worker 验证过这一对函数。
5. **大 Tool Result 入口卸载的潜在收益显著但需谨慎解读**：1,025 条 tool result 中 64 条（6.2%）超过 4,000 字符，占唯一 tool-result token 的 244,545；按"首次进入即落盘 + 200/200 预览"估算，可消除约 **994,024** 个重发 token，相当于 A1 总 prompt 的 **19.9%**（反事实、未计 cache 与重读）。

已确认的 Runtime 缺陷只有一处（G3-I01），其余是任务契约与集成验证问题；这一点对下一阶段的优先级影响很大。

---

## 2. Data Availability

| 数据 | 可用性 | 说明 |
|---|---|---|
| `result.json`（run 级指标、`worker_results`、`tool_calls`） | 可用（71/72 run） | 1 个 run（`e1-04-cli-a0` rep0）崩溃无 `result.json`，已发布总量同样不含它 |
| `tool_conversations.json`（逐 worker 逐轮消息） | 可用（71/71） | 用于重建关键路径与 tool result 字符数 |
| `trajectory/*/transcript.jsonl`（M4 执行结果） | 可用（71/71） | 只有 `execution_outcome`（state / completion_kind / retry_safe / result_size），**没有逐轮 token usage** |
| `oracle.log`、`workspace/` | 可用 | 用于判读集成失败与残留 stub |
| 逐 Turn provider usage | **不可用** | G2b-0 已确认；本轮不推测逐轮 token |
| 模型内部推理 | **不可用** | 所有"为什么"只基于可观测动作与输出 |

数据缺口集中在一处：`e1-04-cli-a0` rep0 无指标，不影响本轮选样（该任务未被选中）。

---

## 3. Selected Cases（8 个 run，全部可在磁盘定位）

| case | run_id | sample | rep | 关注点 | oracle |
|---|---|---|---|---|---|
| A-a1-max-turns | `eval-62dae7e46b5f` | e1-02-report-a1 | 2 | prompt 最高的 A1，parser/stats 触顶 14 轮 | False |
| A-a0-control | `eval-1f142e399740` | e1-02-report-a0 | 2 | 同 repetition 单 worker 对照 | True |
| B-1-bash-exit1 | `eval-22c631eed70d` | e1-01-config-a1 | 0 | bash 退出 1（自身代码 Traceback） | True |
| B-2-bash-exit128 | `eval-bd3223dfea83` | e1-01-config-a1 | 1 | bash 退出 128（hexdump 输出） | False |
| B-3-bash-exit2 | `eval-fb1c99956928` | e1-12-conflict-a0 | 0 | bash 退出 2（`ls` 权限/路径） | False |
| C-1-all-success | `eval-723a87236df7` | e1-07-export-a1 | 0 | 两 worker 都 success，oracle 失败 | False |
| C-2-all-success | `eval-4b4a6c56b3e3` | e1-07-export-a1 | 1 | 同上，另一种格式错误 | False |
| D-1-success | `eval-a5c202418ad6` | e1-12-conflict-a1 | 0 | 三 worker 协作成功 | True |
| D-2-failure | `eval-5cdd3640d826` | e1-12-conflict-a1 | 1 | worker `a` 第 1 轮被 side_effect 终止 | False |

> 任务要求"至少两个 bash + 一个 list_files"。实际样本中 **M4 记录里没有 `list_files` 触发的 `side_effect_unknown`**：早先按 `result.tool_calls` 末尾调用看像是 `list_files`，但 M4 判定记录指向 `bash`（同一轮里 `ls` 先失败）。按真实证据只保留 3 个 bash 案例，不臆造 `list_files` 案例。

---

## 4. max_turns Critical Paths（A 组）

### 4.1 对照

| 指标 | A1（`eval-62dae7e46b5f`） | A0（`eval-1f142e399740`） |
|---|---:|---:|
| prompt tokens | 364,589 | 20,605 |
| model turns | 39 | 7 |
| tool turns | 36 | 6（8 次调用） |
| 首次 `file_write` | turn 7（parser） | **turn 4** |
| REPAIR 轮 | 0 | 0 |
| oracle | False | True |

### 4.2 轮次构成（A1，按 turn 归并）

| Worker | turns | EXPLORE | IMPLEMENT | VERIFY | OTHER | oracle-probe turns | 首次写入 |
|---|---:|---:|---:|---:|---:|---:|---:|
| parser | 10 | 6 | 1 | 1 | 2 | 3 | t7 |
| render | 13 | 9 | 2 | 1 | 1 | 4 | t12 |
| stats | 13 | 11 | 0 | 0 | 2 | **9** | 从未写入 |
| 合计 | 36 | 26 (72%) | 3 (8%) | 2 (6%) | 5 (14%) | **16 (44%)** | — |

分类规则（确定性、可复现）：`file_read/grep/list_files`→EXPLORE；`file_write`→IMPLEMENT；`file_edit`（此前有失败 VERIFY）→REPAIR；`bash` 命中 `pytest/py_compile/assert`→VERIFY，命中 `cat/head/ls/find/git log|show|reflog|cat-file|bundle/xxd`→EXPLORE，其余→OTHER。`oracle_probe` 是叠加标记（独立列），检测 `find /`、`/tmp`、`*.stdout`、`git bundle`、`git log --all`、`git cat-file`、`git reflog`、`hidden test` 等。

### 4.3 关键事件（真实轨迹）

```text
parser  t3  bash  git show --stat HEAD && git log --all --oneline ...   [probe]
parser  t4  bash  ls -la /tmp/project.bundle; find / -path /proc ...    [probe]
parser  t5  bash  cd /tmp && git bundle list-heads project.bundle ...   [probe]
parser  t7  file_write report/parser.py                                  [IMPLEMENT]
render  t4  bash  ls -la /tmp /workspace; ls -la /tmp/project.bundle    [probe]
render  t5  bash  cd /tmp && for f in *.stdout *.stderr; ... (15,423 chars)[probe]
render  t12 file_write report/stats.py                                   [IMPLEMENT — 非本 worker 归属]
render  t13 file_write report/render.py                                  [IMPLEMENT]
stats   t9  bash  cd /tmp && for f in *.stdout; head ... (8,365 chars)   [probe]
stats   t10 bash  cd /tmp && for f in *.stdout; ... (20,091 chars)       [probe]
stats   t11 bash  cd /tmp && for f in 19f978af... (32,182 chars)         [probe]
stats   t12 bash  cd /tmp && cat c756cfd3....stdout                      [probe]
stats   t13 bash  cd /tmp && cat 0b143cc8....stdout                      [probe]
```

- `stats` worker 的 **5 个末轮全部在读 `/tmp/*.stdout`**（同一次 oracle 运行的输出文件），从未写 `report/stats.py`；该文件由 `render` 在 t12 越界写入。
- `parser` 与 `render` 各用 4 轮做 git 历史/打包文件侦查（`git bundle verify`、`git reflog`、`git cat-file --batch-all-objects`）。

### 4.4 First Failure Point

- **A-a1-max-turns**：`FIRST_FAILURE_POINT = turn 3`（parser 开始从"读代码"转向"找隐藏测试"）。此前没有任何工具错误提示；这是**目标偏离**而非错误处理失败。到第 14 轮结束时缺的是：`stats` worker 根本没有产出 `report/stats.py`（由他人代写），`parser` 只有 10 轮就到上限、`render` 直到 t12–t13 才写入两个文件，没有任何轮次用于修复验证失败。
- **A-a0-control**：无偏离，t4 一次性写完三个文件，t5 自建 smoke check 通过。

### 4.5 结论（对应 G3-0.3 的五问）

- **主要消耗在探索阶段？** 是，EXPLORE 26/36（72%），其中 16 轮是"找评分产物"。
- **是否存在测试循环？** 没有。VERIFY 仅 2 轮，REPAIR 0 轮——**根本没进入"改—测—修"循环**。
- **任务复杂度超过能力？** 不成立：同任务 A0 用 7 轮完成。
- **提高 Turn Limit 会怎样？** 只会按当前行为继续增加探针轮次与日志重发（A0 已证明 7 轮足够），属于无效扩容。
- **能否通过更明确任务契约避免？** 能，且这是最小改动：明确"只在 `/workspace/project` 内工作、实现本 worker 拥有的文件、不做仓库历史/打包文件/`/tmp` 侦查"。

---

## 5. `side_effect_unknown` Root Cause（B 组）

### 5.1 代码路径（本地实测）

| 位置 | 行为 |
|---|---|
| `src/atlasos/sandbox/tools.py:386-396` | `bash` 返回 `ToolResult(is_error=code != 0)`，**不设置 `retry_safe`** |
| `anycode/tools/executor.py:151-155` `_normalize_result` | `retry_safe is None` 时：`retry_safe = not (tool.side_effecting and is_error)`；`bash_tool` 的 `side_effecting=True`（`anycode/tools/bash.py:161-171`）⇒ 任何错误 → `retry_safe=False` |
| `anycode/core/runner.py:639-641` | 本批结果中第一个 `retry_safe=False` 的记录（**不是最后一个工具调用**）直接触发 `stop_side_effect_unknown(record.output)`，worker 立即结束 |
| `src/atlasos/actions/gate.py:812` | `_failure(..., retry_safe: bool = False)`：M3 拒绝类结果默认也不可重试 |

### 5.2 三个案例

| case | worker / turn | 工具与入参 | M4 记录 | 停止消息要点 | 归类 |
|---|---|---|---|---|---|
| B-1 `eval-22c631eed70d` | loader / t4 | `bash`：`python3 --version; import tomllib; import yaml; tail -1` | `UNCERTAIN / EXECUTED_ERROR / retry_safe=False`，result 1,294 chars | 输出含 `config/loader.py:80 load_config ... parser(text)` 的 **自身代码 Traceback**，exit 1 | **A 已执行但有错误** |
| B-2 `eval-bd3223dfea83` | loader / t13 | `bash`：`import yaml; import tomllib; python3 --version; pip list` | `UNCERTAIN / EXECUTED_ERROR / False`，790 chars | 输出是 `xxd` 十六进制片段 + `NotImplemented`，exit 128 | **A 已执行但有错误** |
| B-3 `eval-fb1c99956928` | solo / t1 | `bash`：`ls -la /workspace/project 2>&1 \| head -50` | `UNCERTAIN / EXECUTED_ERROR / False`，277 chars | `ls: cannot access '/workspace/proje...`，exit 2 | **A 已执行但有错误** |

三种表现（自身代码抛错、工具命令畸形退出、路径不存在）都被映射为同一个 `side_effect_unknown`。

### 5.3 为什么是误判

- M4 自己把 `completion_kind` 记为 `EXECUTED_ERROR`（**已执行且已知失败**），只有 `state` 留在 `UNCERTAIN`。
- 这三条都有明确的 stdout/stderr 与非零退出码，属于"业务失败"（类别 A），不属于"是否已产生副作用未知"（类别 C）。
- 由此产生的二次伤害：**worker 被立即终止，模型没有机会看到失败输出并修复**——这正是 A 组"REPAIR 0 轮"的另一面。

### 5.4 最小修复建议（不实施）

```text
File:       src/atlasos/sandbox/tools.py
Functions:  SandboxWorkspaceTools.bash / grep / list_files / file_write / file_edit
Current:    ToolResult(..., is_error=code != 0)                      # retry_safe 未设置
Expected:   ToolResult(..., is_error=code != 0, retry_safe=True)     # 已拿到退出码=执行已完成
            # 仅 SandboxLostError / 超时 / transport 失败保留 retry_safe=False
Evidence:   B-1/B-2/B-3 的 execution_outcome 均为 COMPLETED+EXECUTED_ERROR
```

同类问题（代码层面，E1 样本中未观测到实例）：`gate.py::_failure` 默认 `retry_safe=False`，会让"执行前被拒绝"（类别 B）也终止 worker。建议与 G3-I01 一并改为"拒绝=可重试"，但需先有覆盖测试。

---

## 6. Worker Success vs Oracle Failure（C 组，`e1-07-export`）

### 6.1 契约（来自 `stage_e_tasks.py:357-398`）

- `formatter.format_row(row, widths)`：空格填充列、以 `' | '` 连接；隐藏测试期望 `format_row(['a','bb'],[2,3]) == 'a  | bb '`。
- `exporter.export_rows(rows, widths)`：行以换行连接 **并带行尾换行**；期望 `export_rows([['a','b'],[2,2]]) == 'a  | b \n'`。
- DAG：`formatter` → `exporter`（exporter 依赖 formatter）。E1 使用的是**旧版 500 字符 handoff**，exporter 只拿到 formatter 结果的前 500 字符。

### 6.2 三层判定

| 层 | C-1 `eval-723a87236df7` | C-2 `eval-4b4a6c56b3e3` |
|---|---|---|
| Worker 执行 | 两个 worker 均 `success`，无 `NotImplementedError` 残留 | 同左 |
| 依赖契约 | exporter 未复现 formatter 的填充规则：实际 `'a | b '`（`'a'` 未按 width=2 填充） | formatter 自身错：`'a | bb '`（未填充、分隔符少空格） |
| 集成 | `export_rows` 返回缺行尾 `\n`：`'a | b '` vs `'a  | b \n'` | `'a b'` vs `'a  | b \n'`（分隔符丢失 + 无换行） |

- 结论：`WORKER_EXECUTION_SUCCESS = True`，`WORKER_DELIVERABLE_CORRECT` 在**单个文件层面**可能成立（无可验证断言），`DAG_INTEGRATION_SUCCESS = False`。
- **没有任何 worker 被要求验证"两个文件放在一起"的行为**：formatter 不跑 exporter，exporter 只按提示"Read formatter.py then implement"照抄了一次格式，且 handoff 只有 500 字符，看不到完整实现。
- 隐藏测试失败点固定在同一处（`tests/test_hidden_export.py:12`），说明契约是明确可判定的——缺的是**执行该判定的人**。

### 6.3 最小修复建议

在 `exporter` 的 worker 文本里加一条可验证交付条件：实现后用 `format_row + export_rows` 跑一个 2 行样例并粘贴实际输出（无需新增 worker、无需改 DAG）。这会同时覆盖"依赖契约"和"集成"两层。

---

## 7. Positive Collaboration Pattern（D 组，`e1-12-conflict`）

| 维度 | D-1 成功 `eval-a5c202418ad6` | D-2 失败 `eval-5cdd3640d826` |
|---|---|---|
| 分工 | a→`{"a":1}`、b→`{"b":2}`、merge 合并两键 | 同左 |
| Worker 停止 | 三者皆 `success` | **a = `side_effect_unknown`（第 1 轮 `ls` 退出 2）**，b/merge 正常 |
| 冲突写 | a、b 都写 `state.json`，merge 以两者依赖结果重建 | a 未写成功，`state.json` 只剩 `{"b":2}` |
| merge 行为 | t1 读文件、t2 用 python 校验两键、t3 复核 `final.json` | t1–t5 反复列目录、`git log/show`、python 校验 → 缺 `a` 键，oracle 失败 |
| 结果 | 3 turns 内完成、oracle True | 5 turns、oracle False |

**Successful Collaboration Pattern（仅基于这 2 个样本，不外推）**

```text
Task Structure      : 2 个互不依赖的写者 + 1 个显式依赖两者的合并者
Worker Ownership    : a 只负责 state.json 的 a 键；b 只负责 b 键；merge 独占 final.json
Dependency Contract : merge 必须从 two dependency outputs（或最终 state.json）重建并集
Expected Deliverable: state.json = final.json = {"a":1,"b":2}
Merge Requirements  : 读取、重建、写回、自校验（D-1 用 python 断言两键存在）
Failure Risks       : 任一写者被 side_effect 提前终止 ⇒ 并集缺键（D-2 的真实成因）
```

可复用的特点：**合并者拥有唯一的最终写权限**，且依赖结果很短（无需 handoff 分页）。风险点不在协作设计，而在单个 worker 的提前终止。

---

## 8. Large Tool Result Offloading Opportunity（离线估算）

策略假设：`>4000` 字符的 tool result 首次进入会话时落盘，只保留头 200 + 尾 200 字符 + 引用。

| 指标 | 值 |
|---|---:|
| tool result 总数（A1 全部 36 run） | 1,025 |
| 超过 4,000 字符的条数 | **64（6.2%）** |
| 这些结果的原始字符 | 602,124 |
| 唯一 token（tiktoken cl100k） | 244,545 |
| 这些结果的累计重发 token（当前） | **1,038,292** |
| 替换为 400 字符预览后的重发 token | 44,268 |
| 估算节省 | **994,024** |
| 占上述重发量 | 95.7% |
| 占 A1 总 prompt（4,992,381） | **19.9%** |
| 触发工具分布 | `bash` 64/64（`cat`/测试日志/`xxd`） |

必须注意的边界：

- 这是**反事实**：假设 worker 不重读归档内容；一旦重读，成本从 prompt 转移到 tool call。
- 未计 DeepSeek 前缀缓存：G2b-1 已证明"替换历史内容"会打掉 cache 命中（hit 86.8%→77.3%），因此 **prompt token 节省 ≠ 成本节省**。
- 入口卸载与 G2b-1 的滚动压缩不同：卸载发生在**首次写入**时，前缀此后保持稳定，不会每轮改写历史——这正好绕开 G2b-1 的缓存问题。
- 不得据此推断 oracle 成功率。

---

## 9. Prioritized Fix List

只列 CONFIRMED 与证据充分的 LIKELY。

| Issue ID | Category | Evidence | Root Cause | Confidence | Impact | Minimal Fix | Target Module | Verification |
|---|---|---|---|---|---|---|---|---|
| **G3-I01** | side_effect | D-2 `eval-5cdd3640d826`：a 第 1 轮 `ls` exit 2 → `side_effect_unknown` → 缺 `{"a":1}` → oracle False；B-1 exit 1（自身 Traceback）、B-2 exit 128、B-3 exit 2，M4 均为 `COMPLETED/EXECUTED_ERROR` | `bash` 未设 `retry_safe`；`_normalize_result` 对 side-effecting 工具的错误一律 `retry_safe=False`；runner 首个不安全记录即终止 | **CONFIRMED** | 18/96 A1 worker；至少 1 次 run 直接因此失败 | 退出码已确定时 `retry_safe=True`；仅 timeout/transport/`SandboxLostError` 保持 False | `src/atlasos/sandbox/tools.py` | Fake sandbox：bash 返回 exit 1，worker 必须继续并看到错误输出 |
| **G3-I03** | max_turns | A 组 `eval-62dae7e46b5f`：16/36 turn（44%）为 oracle-probe，注入 104,737 字符；`stats` 末 5 轮全在读 `/tmp/*.stdout`，从未写自己的文件 | worker prompt 没有交付边界，模型用轮次搜索评分产物 | **CONFIRMED** | e1-02-report 的全部 max_turns worker | worker 文本加"只在 `/workspace/project` 内、实现你拥有的文件、不做 git 历史/打包/`/tmp` 侦查" | `experiments/evaluation/stage_e_tasks.py` | 重跑 e1-02-report：probe turn = 0，首次写入 ≤ turn 4 |
| **G3-I04** | integration | C-1/C-2：两 worker 皆 success、无 stub，`export_rows` 输出 `'a | b '` / `'a b'` vs 期望 `'a  | b \n'` | 没有任何 worker 验证 `formatter+exporter` 的组合；handoff 仅 500 字符 | **LIKELY** | e1-07-export 全部"全成功但 oracle 失败" run | exporter 交付条件加"用 2 行样例跑 `format_row+export_rows` 并粘贴输出" | `experiments/evaluation/stage_e_tasks.py` | 重跑 e1-07-export：集成断言失败时 exporter 不能报 success |
| **G3-I05** | contract | A 组 EXPLORE 26/36（72%），IMPLEMENT 仅 3 轮；`render` 越界写 `stats.py`（t12），`stats` 全程未写 | 探索无上界，且 ownership 未被强制 | **LIKELY** | 与 G3-I03 同源的 3 个 worker | 同上一条的任务文本；明确"只写自己拥有的文件" | `experiments/evaluation/stage_e_tasks.py` | 越界写入率、EXPLORE 占比下降 |
| G3-I02 | side_effect | 代码路径 `gate.py::_failure(retry_safe=False)`；**E1 样本未观测到实例** | 拒绝类结果默认不可重试 | LIKELY（代码）/ 未观测 | 潜在：一次策略拒绝即终止 worker | 拒绝类结果改 `retry_safe=True` + 独立 code | `src/atlasos/actions/gate.py` | 单测：拒绝后 worker 继续 |

优先级与理由：

1. **G3-I01 最优先**——唯一被证明直接改变任务结果的 Runtime 缺陷（D-2 整条因果链），且修复面极小（一个 `retry_safe` 赋值），还能顺带恢复"看到错误→修复"的循环（A 组 REPAIR=0 的一半原因）。
2. **G3-I03 第二**——44% 的轮次被烧在搜索评分产物上，直接解释了 `max_turns` 与 6.59× 成本；修复只改任务文本，不动 Runtime。
3. **G3-I04 第三**——13/36 的 oracle 失败属"全成功但集成错"，它是唯一能用"一条可验证交付条件"消除的整类失败。

不列入修复清单：`UNDETERMINED` 项（见下），以及任何需要改 Scheduler/DAG/预算的改动。

---

## 10. Remaining Uncertainties

1. **模型为何去 `/tmp` 找 `*.stdout`**：只能确认行为（命令与输出），无法确认动机（是否看到提示、是否在找隐藏测试）。标记 `UNDETERMINED`。
2. **B-2 的 exit 128 具体由哪条子命令产生**：输出是 `xxd` 片段，无法逐条对齐退出码；只能确认"已执行且有输出"。该案例归为 A 类仍成立，但归因精度有限。
3. **`side_effect_unknown` 的 18 个 worker 是否全部为 A 类**：本轮只审计 3 个；如果要全量分类，需要把同样的 M4 轨迹扫描推广到全部 18 个 instance。
4. **G3-I04 是否也影响其他任务**：只抽查了 e1-07-export；`e1-05-pairs`/`e1-06-kv` 等也存在跨模块契约，但未审计。
5. **入口卸载的真实收益**：19.9% 是 prompt token 反事实，未计 cache 命中损失与 worker 重读；需要 G3-1 真实对照（且必须与 G2b-1 一样记录 hit/miss）。
6. **`e1-04-cli-a0` rep0** 无 `result.json`，其崩溃原因无法定位（与本次选样无关）。

---

## 11. 复现

```powershell
.\.venv\Scripts\python.exe -m experiments.evaluation.stage_g3_failure_audit
```

输出（`experiments/results/stage-g3-audit/`）：`selected_cases.json`、`critical_paths.csv`（全部选中 run 的逐调用事件）、`side_effect_cases.csv`、`integration_failures.csv`、`collaboration.csv`、`worker_contracts.csv`、`tool_offload_estimates.csv`、`offload_summary.json`、`issues.json`。

脚本只读：不调用模型、不启动 Sandbox、不写任何 run 目录；`runs_scanned = 71`，与 Stage E1 正式轮一致。
