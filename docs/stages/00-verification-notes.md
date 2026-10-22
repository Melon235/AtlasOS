# Stage 0 Verification Notes

Evidence for the questions in `docs/stages/00-01-bootstrap-and-baseline.md` §0.2, recorded before
any Stage 1 work. Environment: Windows, Python 3.12.15 (uv-managed), uv 0.13.5.

## Pins

| Dependency | Pin | Evidence |
|---|---|---|
| AnyCode | `anycode-py[openai]==0.9.0` | PyPI 0.9.0, MIT, requires Python >=3.12 |
| ContextPilot | `contextpilot==0.5.0` (optional extra) | PyPI 0.5.0, Apache-2.0 |
| DeepSeek | OpenAI-compatible HTTP API | provider reports cache tokens |

AnyCode's GitHub `main` has moved to 0.10.0 with a new facade (`Agent`, `Crew`, `@tool`). AtlasOS
pins 0.9.0 and must not follow that README until an upgrade is deliberate.

## Answers

**1. AnyCode version.** 0.9.0, tag `v0.9.0`, commit `ffdc7d28e8d5ceefedf3e490ccbe37499eb6eec8`.

**2. Extension points.**
- LLM adapter: `anycode.types.LLMAdapter` (`name`, `chat`, `stream`), registered with
  `anycode.plugins.registry.register_provider_factory`; `AgentConfig.provider` selects it.
- Scheduler: `anycode.core.scheduler.Scheduler` is a concrete class with strategies
  (`dependency-first` default, `round-robin`, `least-busy`, `capability-match`). It is not a
  Protocol and `OrchestratorConfig` has no policy field, so a custom policy must subclass and be
  wired by the caller (Stage 4 decision).
- Context/prompt hook: prompts are assembled internally by `AnyCode._build_task_prompt`
  (task title + description + prerequisite outputs) and the runner. The only documented
  interception point is `TurnHook.before_turn`; section ordering is not configurable.
- Checkpoint metadata: `CheckpointConfig` + `CheckpointManager` (filesystem/sqlite), schema v2.

**3. ContextPilot as a normal dependency.** Yes, as an optional extra
(`uv sync --extra contextpilot`). It pulls a heavy stack (`transformers`, `datasets`,
`elasticsearch==8.18.1`, `fastapi`, `uvicorn`) and pins `elasticsearch` hard. No vLLM, SGLang,
GPU, or server is required for the in-process path.

**4. `optimize()` without self-hosted serving.** Yes. Verified:
`ContextPilot(use_gpu=False).optimize(["alpha doc", "beta doc", "gamma doc"], "Which doc is
first?")` returns exactly two OpenAI chat dicts — one `system` message containing all documents
and the importance ranking, one `user` message with the query. It performs no network I/O and
emits no `cache_control` markers.

**5. DeepSeek through the provider boundary.** Yes, but not through AnyCode's built-in
`OpenAIAdapter`: that adapter has no `base_url` parameter and its `parse_token_usage` ignores
`prompt_cache_hit_tokens`/`prompt_cache_miss_tokens`. AtlasOS therefore registers
`atlasos.integrations.deepseek.DeepSeekAdapter` (own `AsyncOpenAI` client + own usage
normalization) through the provider factory.

**6. Cache fields.** DeepSeek documents `prompt_cache_hit_tokens` and
`prompt_cache_miss_tokens` in the chat-completion `usage` object. The parser accepts both, plus
an OpenAI-style `prompt_tokens_details.cached_tokens` fallback. Live verification remains
unrun until a `DEEPSEEK_API_KEY` is provided; the opt-in test never asserts a positive hit count.

**7. Offline default.** Yes. `pytest` deselects the `live` marker via `addopts`, and the live test
also skips without `ATLASOS_RUN_LIVE_TESTS=1` and a key.

## Upstream behavior AtlasOS must account for

- `TeamRunResult.agent_results` is keyed by **agent name**, so two tasks assigned to the same
  agent collapse to one entry. Baselines that need per-task outputs should use one agent per task
  or read `team.get_tasks()`.
- `TaskSpec.depends_on` matches task **titles**, and the baseline prompt includes prerequisite
  results, truncated to `DEPENDENCY_CONTEXT_MAX_LENGTH`. That is the Stage 1
  `context_policy = "upstream_default"` behavior and must be documented as such, not described as
  "full history".
- AnyCode's `TokenUsage` cannot express "provider did not report cache". The AtlasOS
  `InferenceUsage` keeps `None`; AnyCode accounting treats an unknown split as fully fresh.

## Windows environment notes

- Python 3.12 must be installed separately because the system Python is 3.11 and AnyCode requires
  3.12+. `uv python install 3.12` keeps it out of the system interpreter.
- ContextPilot's installer writes `contextpilot_hook.pth` to `sys.prefix`; in a uv venv that path
  is processed before `Lib/site-packages` is on `sys.path`, so every interpreter start prints a
  `ModuleNotFoundError` warning. Moving the file to `.venv/Lib/site-packages/` fixes startup. The
  hooks only matter for self-hosted serving, which is out of V1 scope.
- ContextPilot prints progress with non-ASCII glyphs; on a GBK console set `PYTHONUTF8=1` (or run
  Python with `-X utf8`) to avoid `UnicodeEncodeError`.

## Commands used

```bash
uv sync --extra dev
uv run ruff check .
uv run pyright
uv run pytest
uv run python examples/baseline_team.py
uv sync --extra contextpilot
uv run pytest tests/integration/test_contextpilot_smoke.py
```
