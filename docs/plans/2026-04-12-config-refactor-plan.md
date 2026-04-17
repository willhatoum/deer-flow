# Config Refactor Implementation Plan — Shipped

> **Status:** Shipped in [PR #2271](https://github.com/bytedance/deer-flow/pull/2271). All tasks complete. This document is an implementation log; for the shipped architecture see [design doc](./2026-04-12-config-refactor-design.md).
>
> **Goal:** Eliminate global mutable state in the configuration system — frozen `AppConfig`, pure `from_file()`, process-global + ContextVar-override lifecycle, `Runtime[DeerFlowContext]` propagation.
>
> **Tech Stack:** Pydantic v2 (`frozen=True`, `model_copy`), Python `contextvars.ContextVar` + `Token`, LangGraph `Runtime` / `ToolRuntime`.
>
> **Issues:** [#2151](https://github.com/bytedance/deer-flow/issues/2151) (implementation), [#1811](https://github.com/bytedance/deer-flow/issues/1811) (RFC)

## Post-mortem — divergences from the original plan

The implementation diverged from the original task-by-task plan in three places. The rationale lives in the design doc §7; here is the commit trail.

| Divergence | Original plan | Shipped | Triggering commit |
|------------|--------------|---------|-------------------|
| Lifecycle storage | Single `ContextVar` in new `context.py`, raises `ConfigNotInitializedError` | 3-tier: `AppConfig._global` (process singleton) + `_override: ContextVar` + auto-load-with-warning fallback | `7a11e925` ("use process-global + ContextVar override"), refined in `4df595b0` |
| Module / API shape | Top-level `get_app_config()` / `init_app_config()` in `context.py` | Classmethods on `AppConfig` (`current`, `init`, `set_override`, `reset_override`); `DeerFlowContext` + `resolve_context` in `deer_flow_context.py` | Same commits + `9040e49e` (call-site migration) |
| Middleware access | `resolve_context(runtime)` in every middleware and tool | Typed middleware reads `runtime.context.xxx` directly; `resolve_context()` only in dict-legacy callers; defensive `try/except` wrappers removed | `a934a822` ("simplify runtime context access") |

**Core insight:** ContextVar alone could not propagate config changes across Gateway request boundaries; process-global fixed that. The override ContextVar was kept for test/multi-client isolation. Hard-fail on uninitialized access (`ConfigNotInitializedError`) was dropped in favor of warning + auto-load to preserve backward compatibility, and tests use an autouse fixture in `backend/tests/conftest.py` to avoid the auto-load path.

---

## File Structure (shipped)

### New files

| File | Responsibility |
|------|---------------|
| `deerflow/config/deer_flow_context.py` | `DeerFlowContext` frozen dataclass + `resolve_context()` helper |

The originally-planned `deerflow/config/context.py` was never created. Lifecycle (`init`, `current`, `set_override`, `reset_override`) is on `AppConfig` itself in `app_config.py`.

### Modified files (config layer)

| File | Change |
|------|--------|
| `deerflow/config/app_config.py` | `frozen=True`, purify `from_file()`, delete mtime/reload/reset/push/pop; add classmethods `init`/`current`/`set_override`/`reset_override` with `_global` ClassVar and `_override` ContextVar |
| `deerflow/config/memory_config.py` | `frozen=True`, delete all globals and loader functions |
| `deerflow/config/title_config.py` | Same pattern |
| `deerflow/config/summarization_config.py` | Same pattern |
| `deerflow/config/subagents_config.py` | Same pattern |
| `deerflow/config/guardrails_config.py` | Same pattern (also delete `reset_guardrails_config`) |
| `deerflow/config/tool_search_config.py` | Same pattern |
| `deerflow/config/checkpointer_config.py` | Same pattern |
| `deerflow/config/stream_bridge_config.py` | Same pattern |
| `deerflow/config/acp_config.py` | Same pattern |
| `deerflow/config/extensions_config.py` | `frozen=True`, delete globals (`_extensions_config`, `reload_extensions_config`, `reset_extensions_config`, `set_extensions_config`) |
| `deerflow/config/database_config.py` | `frozen=True` (added in `4df595b0` review round) |
| `deerflow/config/run_events_config.py` | `frozen=True` (same) |
| `deerflow/config/tracing_config.py` | `frozen=True`, unchanged exports |
| `deerflow/config/__init__.py` | Removed deleted getter exports; no new re-exports needed since API is now on `AppConfig` |

### Modified files (production consumers)

| File | Change |
|------|--------|
| `deerflow/agents/lead_agent/agent.py` | `get_summarization_config()` → `AppConfig.current().summarization` |
| `deerflow/agents/lead_agent/prompt.py` | `get_memory_config()` → `AppConfig.current().memory`; ACP agents derived from `AppConfig.current()` |
| `deerflow/agents/middlewares/memory_middleware.py` | Reads `runtime.context.app_config.memory` directly (typed `Runtime[DeerFlowContext]`) |
| `deerflow/agents/middlewares/title_middleware.py` | `after_model` / `aafter_model` read `runtime.context.app_config.title`; helpers take `TitleConfig` as required parameter |
| `deerflow/agents/middlewares/tool_error_handling_middleware.py` | `get_guardrails_config()` → `AppConfig.current().guardrails` |
| `deerflow/agents/middlewares/loop_detection_middleware.py` | Reads `runtime.context.thread_id` directly |
| `deerflow/agents/middlewares/thread_data_middleware.py` | Reads `runtime.context.thread_id` directly |
| `deerflow/agents/middlewares/uploads_middleware.py` | Reads `runtime.context.thread_id` directly |
| `deerflow/agents/memory/updater.py` / `queue.py` / `storage.py` | `get_memory_config()` → `AppConfig.current().memory` |
| `deerflow/runtime/checkpointer/provider.py` / `async_provider.py` | `get_checkpointer_config()` → `AppConfig.current().checkpointer` |
| `deerflow/runtime/store/provider.py` / `async_provider.py` | Same pattern |
| `deerflow/runtime/stream_bridge/async_provider.py` | `get_stream_bridge_config()` → `AppConfig.current().stream_bridge` |
| `deerflow/runtime/runs/worker.py` | Constructs `DeerFlowContext(app_config=AppConfig.current(), thread_id=thread_id)` and passes via `agent.astream(context=...)` |
| `deerflow/subagents/registry.py` | `get_subagents_app_config()` → `AppConfig.current().subagents` |
| `deerflow/sandbox/middleware.py` | Reads `runtime.context.thread_id`; removed `runtime.context["sandbox_id"]` read path |
| `deerflow/sandbox/tools.py` | Removed 3× `runtime.context["sandbox_id"] = ...` writes; state now flows through `runtime.state["sandbox"]`; sandbox-config access via `resolve_context(runtime).app_config.sandbox` where dict-context fallback may still apply |
| `deerflow/sandbox/local/local_sandbox_provider.py` / `sandbox_provider.py` / `security.py` | `get_app_config()` → `AppConfig.current()` |
| `deerflow/community/*/tools.py` (tavily, jina_ai, firecrawl, exa, ddg_search, image_search, infoquest, aio_sandbox) | `get_app_config()` → `AppConfig.current()` |
| `deerflow/skills/loader.py` / `manager.py` / `security_scanner.py` | Same pattern |
| `deerflow/tools/builtins/*.py` | Typed tools read `runtime.context.xxx`; `task_tool.py` uses `resolve_context()` for bash-subagent guard |
| `deerflow/tools/tools.py` / `skill_manage_tool.py` | ACP agents derived from `AppConfig.current()`; skill manage reads `runtime.context.thread_id` |
| `deerflow/models/factory.py` | `get_app_config()` → `AppConfig.current()` |
| `deerflow/utils/file_conversion.py` | Same |
| `deerflow/client.py` | `AppConfig.init(AppConfig.from_file(config_path))`; constructs `DeerFlowContext` at invoke time. Earlier iterations used `set_override()`; removed in `a934a822` |
| `app/gateway/app.py` | `AppConfig.init(AppConfig.from_file())` at startup |
| `app/gateway/deps.py` / `auth/reset_admin.py` | `get_app_config()` → `AppConfig.current()` |
| `app/gateway/routers/mcp.py` / `skills.py` | Construct new config + `AppConfig.init()` instead of `reload_extensions_config()` |
| `app/gateway/routers/memory.py` / `models.py` | `get_memory_config()` → `AppConfig.current().memory`, etc. |
| `app/channels/service.py` | `get_app_config()` → `AppConfig.current()` |
| `backend/CLAUDE.md` | Config Lifecycle + `DeerFlowContext` sections updated |

### Modified files (tests)

~100 test locations updated. Patterns:

- `@patch("...get_memory_config")` → `@patch.object(AppConfig, "current", ...)` returning a frozen `AppConfig` with the desired sub-config
- Tests that mutated `AppConfig` instances now construct fresh ones or use `model_copy(update={...})`
- `backend/tests/conftest.py` gained an autouse `_auto_app_config` fixture that sets `AppConfig._global` to a minimal config for every test

New test files:
- `backend/tests/test_config_frozen.py` — verifies every config model rejects mutation
- `backend/tests/test_deer_flow_context.py` — verifies `DeerFlowContext` construction, defaults, and `resolve_context()` for all three input shapes
- `backend/tests/test_app_config_reload.py` — verifies lifecycle: `init()` visibility across contexts, `set_override()` + `reset_override()` with `Token`, auto-load warning

---

## Task log

All tasks complete. Checkboxes below reflect the shipped state. For detailed step-by-step TDD sequence, see the commit history on `refactor/config-deerflow-context`.

### Task 1: Freeze all sub-config models

- [x] Write `test_config_frozen.py` parameterized over every config model
- [x] Add `model_config = ConfigDict(frozen=True)` (or `extra="allow", frozen=True`) to every model
- [x] Add frozen=True to `DatabaseConfig`, `RunEventsConfig` in review round (`4df595b0`)
- [x] Fix tests that mutated config objects — use `model_copy(update={...})` or fresh instances

### Task 2: Freeze `AppConfig`

- [x] Extend `test_config_frozen.py` with `test_app_config_is_frozen`
- [x] Change `AppConfig.model_config` to `ConfigDict(extra="allow", frozen=True)`

### Task 3: Purify `from_file()`

- [x] Write test verifying no `load_*_from_dict()` calls happen during `from_file()`
- [x] Remove all 8 `load_*_from_dict()` calls and their imports from `app_config.py`

### Task 4: Replace `app_config.py` lifecycle

**Diverged from original plan.** See post-mortem for rationale.

- [x] ~~Create `deerflow/config/context.py`~~ → Lifecycle added directly to `AppConfig` as classmethods
- [x] Add `_global: ClassVar[AppConfig | None]` for process-global storage (atomic pointer swap under GIL, no lock)
- [x] Add `_override: ClassVar[ContextVar[AppConfig]]` for per-context override
- [x] Implement `init()`, `current()`, `set_override()` (returns `Token`), `reset_override()`
- [x] `current()` priority order: override → global → auto-load-with-warning
- [x] Delete old lifecycle: `get_app_config`, `reload_app_config`, `reset_app_config`, `set_app_config`, `peek_current_app_config`, `push_current_app_config`, `pop_current_app_config`, `_load_and_cache_app_config`, mtime globals
- [x] Write `test_app_config_reload.py` covering init/override/reset/auto-load paths

Commits: `7a11e925` (initial process-global + override), `4df595b0` (harden: `Token` return, auto-load warning, doc `_global` lock-free rationale).

### Task 5: Migrate call sites to `AppConfig.current()`

- [x] ~100 `get_app_config()` / `get_memory_config()` / `get_title_config()` / ... call sites migrated to `AppConfig.current().xxx`
- [x] Tests that patched module-level getters migrated to `patch.object(AppConfig, "current", ...)`
- [x] Update `deerflow/config/__init__.py` — removed deleted getter exports

Commits: `9040e49e` (bulk migration), `82fdabd7` (deps.py + reset_admin.py follow-up), `6c0c2ecf` (test mocks update), `faec3bf9` (runtime-path migration).

### Task 6: Delete sub-config module globals (memory / title / summarization)

- [x] Delete `_memory_config`, `get_memory_config`, `set_memory_config`, `load_memory_config_from_dict` from `memory_config.py`
- [x] Delete analogous globals from `title_config.py`, `summarization_config.py`
- [x] Migrate 6 production consumers of `get_memory_config`, 1 of `get_title_config`, 1 of `get_summarization_config`
- [x] Fix tests that patched the deleted getters

### Task 7: Delete remaining sub-config module globals

- [x] `subagents_config.py` — delete globals; migrate `subagents/registry.py`
- [x] `guardrails_config.py` — delete globals + `reset_guardrails_config`; migrate `tool_error_handling_middleware.py`
- [x] `tool_search_config.py` — delete globals (no production consumers)
- [x] `checkpointer_config.py` — delete globals; migrate 2 consumers in runtime/
- [x] `stream_bridge_config.py` — delete globals; migrate 1 consumer
- [x] `acp_config.py` — delete globals; migrate 2 consumers (`agents/lead_agent/prompt.py`, `tools/tools.py`)
- [x] `extensions_config.py` — delete globals + `reload_extensions_config`/`reset_extensions_config`/`set_extensions_config`; migrate 4 consumers (`sandbox/tools.py`, `client.py`, `gateway/routers/mcp.py`, `gateway/routers/skills.py`)

### Task 8: Update `__init__.py` exports

- [x] Remove deleted-getter exports; keep type exports (`AppConfig`, `ExtensionsConfig`, `MemoryConfig`, etc.)
- [x] `tracing_config` re-exports preserved (still function-based, no lifecycle change)

### Task 9: Gateway config update flow

- [x] `app/gateway/routers/mcp.py`: write extensions_config.json → `AppConfig.init(AppConfig.from_file())`
- [x] `app/gateway/routers/skills.py`: same pattern
- [x] `deerflow/client.py`: `update_mcp_config()` and `update_skill()` reuse the same pattern (now via `AppConfig.current().extensions` + `init(AppConfig.from_file())`)

### Task 10: Create `DeerFlowContext`

- [x] Create `deerflow/config/deer_flow_context.py` with `DeerFlowContext` frozen dataclass
- [x] Fields: `app_config: AppConfig`, `thread_id: str`, `agent_name: str | None = None`
- [x] Typed via `TYPE_CHECKING` import to avoid circular dependency
- [x] Wire into `create_agent(context_schema=DeerFlowContext)` in `lead_agent/agent.py`
- [x] Wire into `DeerFlowClient.stream(context=...)`

### Task 11: Add `resolve_context()` helper

- [x] Handle typed context (Gateway/Client path): return `runtime.context` directly
- [x] Handle dict context (legacy/tests): construct `DeerFlowContext` from dict keys; warn on empty `thread_id`
- [x] Handle missing context (LangGraph Server): fall back to `get_config().get("configurable", {})`; warn on empty `thread_id`
- [x] Write `test_deer_flow_context.py` covering all three paths

### Task 12: Remove `sandbox_id` from `runtime.context`

- [x] Delete 3× `runtime.context["sandbox_id"] = sandbox_id` writes in `sandbox/tools.py`
- [x] Delete context-based release path in `sandbox/middleware.py:after_agent`
- [x] Sandbox state flows exclusively through `runtime.state["sandbox"] = {"sandbox_id": ...}`

### Task 13: Wire `DeerFlowContext` into Gateway runtime and client

- [x] `deerflow/runtime/runs/worker.py`: construct `DeerFlowContext(app_config=AppConfig.current(), thread_id=thread_id)`, pass via `agent.astream(context=...)`; remove dict-context injection
- [x] `deerflow/client.py`: call `AppConfig.init(AppConfig.from_file(config_path))` in `__init__` / `_reload_config()`; construct `DeerFlowContext` at invoke time

### Task 14: Migrate middleware/tools from dict access to typed access

Originally planned as "replace with `resolve_context()`". Shipped as: typed middleware reads `runtime.context.xxx` directly; `resolve_context()` only where dict-context may still appear.

- [x] `thread_data_middleware`, `uploads_middleware`, `memory_middleware`, `loop_detection_middleware`: `runtime.context.thread_id` direct read
- [x] `sandbox/middleware.py`: same
- [x] `present_file_tool`, `setup_agent_tool`, `skill_manage_tool`: same pattern (typed `ToolRuntime`)
- [x] `task_tool.py`: keep `resolve_context()` for bash-subagent guard (uses `app_config`)
- [x] `sandbox/tools.py`: keep `resolve_context()` for sandbox config + thread_id in dict-legacy paths

Commit: `a934a822`.

### Task 15: Middleware reads config from Runtime

- [x] `memory_middleware`: `runtime.context.app_config.memory` — no wrapper, no `try/except`
- [x] `title_middleware`: `runtime.context.app_config.title` passed as required parameter to helpers; no `TitleConfig | None` fallback
- [x] `tool_error_handling_middleware`: reads from `AppConfig.current().guardrails` (lives outside per-invocation context)

Commit: `a934a822`.

### Task 16: Final cleanup and verification

- [x] Grep verified: no remaining `runtime.context.get(...)` / `runtime.context[...]` patterns in production code (the pattern exists in `app/channels/wechat.py` but is unrelated — it's a channel-token helper, not LangGraph runtime)
- [x] Grep verified: no remaining `get_memory_config` / `get_title_config` / `get_summarization_config` / `get_subagents_app_config` / `get_guardrails_config` / `get_tool_search_config` / `get_checkpointer_config` / `get_stream_bridge_config` / `get_acp_agents` / `reload_*` / `reset_*` / `set_extensions_config` / `push_current_app_config` / `pop_current_app_config` / `load_*_from_dict` references
- [x] Full test suite passes (`make test` — 2376 passed per PR description)
- [x] CI green (backend-unit-tests)
- [x] `backend/CLAUDE.md` updated with new Config Lifecycle and `DeerFlowContext` sections

---

## Follow-ups (not in Phase 1 PR)

- Consider re-exporting `DeerFlowContext` / `resolve_context` from `deerflow.config.__init__` for ergonomic imports.
- `app/channels/wechat.py` uses `_resolve_context_token` — unrelated naming collision with `resolve_context()`. No action required but worth noting for future readers.
- **Phase 2** (below) subsumes the auto-load-warning concern: `AppConfig.current()` goes away entirely rather than getting its warning promoted to error.

---

# Phase 2: Pure explicit parameter passing

> **Status:** Shipped. P2-1..P2-5 landed first with `AppConfig.current()` kept as a transition fallback; P2-6..P2-10 landed together in commit `84dccef2` to eliminate the fallback and delete the ambient-lookup surface entirely. `AppConfig` is now a pure Pydantic value object with no process-global state and no classmethod accessors.
>
> **Design:** [§8 of the design doc](./2026-04-12-config-refactor-design.md#8-phase-2-pure-explicit-parameter-passing)

## Shipped commits

| Commit | Task | Category | What changed |
|--------|------|----------|--------------|
| `c45157e0` | P2-1 | infrastructure | `get_config` FastAPI dependency, `app.state.config` populated at startup |
| `70323e05` | P2-2 | G (Gateway) | 6 routers migrated to `Depends(get_config)`; reload paths dual-write `app.state.config` + `AppConfig.init()` |
| `f8738d1e` | P2-3 | H (Client) | `DeerFlowClient.__init__(config=...)` captures config locally; multi-client isolation test pins invariant |
| `23b424e7` | P2-4 | B (Agent construction) | `make_lead_agent`, `_build_middlewares`, `_resolve_model_name`, `build_lead_runtime_middlewares` accept optional `app_config` |
| `74b7a7ef` | P2-5 (partial) | D (Runtime) | `RunContext` gains `app_config` field; Worker builds `DeerFlowContext` from it; Gateway `deps.get_run_context` populates it. Standalone providers (checkpointer/store/stream_bridge) already accept optional config from Phase 1 |
| `84dccef2` | P2-6..P2-10 | C+E+F+I + deletion | Memory closure-captures `MemoryConfig`; sandbox/skills/community/factories/tools thread `app_config` end-to-end; `resolve_context()` rejects non-typed runtime.context; `AppConfig.current()` removed; `get_sandbox_provider(app_config)` required; `make_lead_agent` LangGraph-Server bootstrap path loads via `AppConfig.from_file()`. All 2337 non-e2e tests pass. |

## Completed tasks (P2-6 through P2-10)

All landed in `84dccef2`.

### P2-6: Memory subsystem closure-captured config (Category C) — shipped
- [x] `MemoryConfig` captured at enqueue time so the Timer thread survives the ContextVar boundary.
- [x] `deerflow/agents/memory/{queue,updater,storage}.py` no longer read any process-global.

### P2-7: Sandbox / skills / factories / tools / community (Categories E+F) — shipped
- [x] `sandbox/tools.py` helpers take `app_config` explicitly; the `_cached` attribute trick is gone.
- [x] `sandbox/security.py`, `sandbox/sandbox_provider.py`, `sandbox/local/local_sandbox_provider.py`, `community/aio_sandbox/aio_sandbox_provider.py` all require `app_config`.
- [x] `skills/manager.py` + `skills/loader.py` + `agents/lead_agent/prompt.py` cache refresh thread `app_config` through the worker thread via closure.
- [x] Community tools (tavily, jina, firecrawl, exa, ddg, image_search, infoquest, aio_sandbox) read `resolve_context(runtime).app_config`.
- [x] `subagents/registry.py` (`get_subagent_config`, `list_subagents`, `get_available_subagent_names`) take `app_config`.
- [x] `models/factory.py::create_chat_model` and `tools/tools.py::get_available_tools` require `app_config`.

### P2-8: Test fixtures (Category I) — shipped
- [x] `conftest.py` autouse fixture no longer monkey-patches `AppConfig.current`; it only stubs `from_file()` so tests don't need a real `config.yaml`.
- [x] ~90 call sites migrated: `patch.object(AppConfig, "current", ...)` removed where production no longer calls it (≈56 sites), and for the remaining ~10 files whose tests called `AppConfig.current()` themselves, the tests now hold the config in a local variable and pass it explicitly.
- [x] `test_deer_flow_context.py` updated to assert that `resolve_context()` raises on dict/None contexts.
- [x] `grep -rn 'AppConfig\.current' backend/tests` is clean.

### P2-9: Simplify `resolve_context()` — shipped
- [x] `resolve_context(runtime)` returns `runtime.context` when it is a `DeerFlowContext`; any other shape raises `RuntimeError` pointing at the composition root that should have attached the typed context.
- [x] The dict-context and `get_config().configurable` fallbacks are deleted.

### P2-10: Delete `AppConfig` lifecycle — shipped
- [x] `AppConfig.current()` classmethod removed.
- [x] `_global` / `_override` / `init` / `set_override` / `reset_override` already gone as of Phase 1; nothing left to delete on the ambient side.
- [x] LangGraph Server bootstrap uses `AppConfig.from_file()` inside `make_lead_agent` — a pure load, not an ambient lookup.
- [x] `backend/CLAUDE.md` Config Lifecycle section rewritten to describe the explicit-parameter design.
- [x] `app/gateway/deps.py` docstrings no longer mention `AppConfig.current()`.
- [x] Production grep confirms zero `AppConfig.current()` call sites in `backend/packages` or `backend/app`.

## Rationale

Phase 1 fixed the **data side** (frozen ADT, no sub-module globals, pure `from_file`). Phase 2 fixes the **access side** (no ambient lookup). Together they make `AppConfig` referentially transparent: a function's result depends only on its inputs, nothing ambient.

## Scope

- ~97 production call sites: `AppConfig.current()` → parameter
- ~91 test mock sites: `patch.object(AppConfig, "current")` / `AppConfig._global = ...` → fixture injection
- ~30 FastAPI endpoints: add `config: AppConfig = Depends(get_config)`
- ~15 factory/helper functions: add `config: AppConfig` parameter
- Delete Phase 1 lifecycle from `app_config.py`

## Ordering rule

`AppConfig._global` can only be deleted **after** every caller is migrated. Tasks run in this order:

1. Introduce new primitives alongside the old ones (Task P2-1)
2. Migrate call sites category by category (Tasks P2-2 through P2-9)
3. Delete the old lifecycle (Task P2-10)

Each category task is independently mergeable. After a category is migrated, grep confirms the old callers in that category are gone but the old lifecycle still exists (other categories may still use it).

## File structure (Phase 2)

### Modified files

| File | Change |
|------|--------|
| `app/gateway/app.py` | Store config on `app.state.config` at startup; remove `AppConfig.init()` call |
| `app/gateway/deps.py` | Add `get_config(request: Request) -> AppConfig`; remove `AppConfig.current()` uses |
| `app/gateway/routers/*.py` | Add `config: AppConfig = Depends(get_config)` to each endpoint; remove `AppConfig.current()` |
| `app/gateway/auth/reset_admin.py` | Take `config: AppConfig` parameter |
| `app/channels/service.py` | Take `config: AppConfig` parameter |
| `deerflow/client.py` | Remove `AppConfig.init()` call; store `self._config = AppConfig.from_file(...)`; all methods read `self._config` |
| `deerflow/agents/lead_agent/agent.py` | `make_lead_agent(runtime_config, app_config)`, `_build_middlewares(app_config, ...)`, pass down through every helper |
| `deerflow/agents/lead_agent/prompt.py` | Every helper takes config (or the specific sub-config slice it needs) as a parameter |
| `deerflow/agents/middlewares/tool_error_handling_middleware.py` | Take guardrails config at construction |
| `deerflow/agents/memory/queue.py` | Capture `MemoryConfig` at enqueue; Timer closure reads from capture |
| `deerflow/agents/memory/updater.py` | Constructor takes `MemoryConfig`; store on `self` |
| `deerflow/agents/memory/storage.py` | Constructor takes `MemoryConfig`; store on `self` |
| `deerflow/runtime/runs/worker.py` | Receive `AppConfig` from `RunManager`; build `DeerFlowContext` from parameter |
| `deerflow/runtime/checkpointer/provider.py` / `async_provider.py` | Constructor takes `CheckpointerConfig \| None` |
| `deerflow/runtime/store/provider.py` / `async_provider.py` | Constructor takes relevant config |
| `deerflow/runtime/stream_bridge/async_provider.py` | Constructor takes `StreamBridgeConfig \| None` |
| `deerflow/sandbox/*.py`, `deerflow/skills/*.py` | Helpers take config parameter |
| `deerflow/community/*/tools.py` | Factory takes config parameter |
| `deerflow/models/factory.py` | `create_chat_model(name, config, thinking_enabled=False)` |
| `deerflow/tools/tools.py` | `get_available_tools(config, ...)` |
| `deerflow/subagents/registry.py` | Helper takes `SubagentsAppConfig` |
| `deerflow/config/deer_flow_context.py` | Simplify `resolve_context()`: typed-only; raise on non-DeerFlowContext |
| `deerflow/config/app_config.py` | **Delete** `_global`, `_override`, `init`, `current`, `set_override`, `reset_override` |
| `backend/tests/conftest.py` | Replace `_auto_app_config` autouse fixture with per-test `test_config` fixture returning `AppConfig` |
| `backend/tests/test_*.py` | Replace `patch.object(AppConfig, "current", ...)` with passing different `AppConfig` instances |
| `backend/CLAUDE.md` | Update Config Lifecycle section to describe pure-parameter design |

### New files

None. Phase 2 is a pure refactor — same file set.

---

## Task P2-1: Add FastAPI `Depends(get_config)` infrastructure

Introduce the new FastAPI DI primitive. Old `AppConfig.current()` still works; this task only adds the new path.

**Files:**
- Modify: `backend/app/gateway/app.py`
- Modify: `backend/app/gateway/deps.py`
- Test: `backend/tests/test_gateway_deps_config.py` (new)

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_gateway_deps_config.py
from fastapi import FastAPI, Depends
from fastapi.testclient import TestClient
from deerflow.config.app_config import AppConfig
from deerflow.config.sandbox_config import SandboxConfig
from app.gateway.deps import get_config


def test_get_config_returns_app_state_config():
    app = FastAPI()
    cfg = AppConfig(sandbox=SandboxConfig(use="test"))
    app.state.config = cfg

    @app.get("/probe")
    def probe(c: AppConfig = Depends(get_config)):
        return {"same": c is cfg}

    client = TestClient(app)
    assert client.get("/probe").json() == {"same": True}
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd backend && PYTHONPATH=. uv run pytest tests/test_gateway_deps_config.py -v
```
Expected: FAIL — `get_config` doesn't exist or returns the wrong thing.

- [ ] **Step 3: Add `get_config` to `deps.py`**

```python
# backend/app/gateway/deps.py
from fastapi import Request
from deerflow.config.app_config import AppConfig


def get_config(request: Request) -> AppConfig:
    """FastAPI dependency that returns the app-scoped AppConfig."""
    return request.app.state.config
```

- [ ] **Step 4: Wire startup in `app.py`**

In `backend/app/gateway/app.py`, at startup (existing `AppConfig.init` call site), add:

```python
app.state.config = AppConfig.from_file()
# Keep AppConfig.init() for now — other callers still use AppConfig.current()
AppConfig.init(app.state.config)
```

- [ ] **Step 5: Run test to verify it passes**

```bash
cd backend && PYTHONPATH=. uv run pytest tests/test_gateway_deps_config.py -v
```
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/app/gateway/deps.py backend/app/gateway/app.py backend/tests/test_gateway_deps_config.py
git commit -m "feat(config): add FastAPI get_config dependency reading from app.state"
```

---

## Task P2-2 (Category G): Migrate FastAPI routers to `Depends(get_config)`

**Files:**
- Modify: `backend/app/gateway/routers/models.py` (2 calls)
- Modify: `backend/app/gateway/routers/mcp.py` (3 calls)
- Modify: `backend/app/gateway/routers/memory.py` (2 calls)
- Modify: `backend/app/gateway/routers/skills.py` (1 call)
- Modify: `backend/app/gateway/auth/reset_admin.py` (1 call)
- Modify: `backend/app/channels/service.py` (1 call)

**Pattern for each endpoint:**

```python
# Before
from deerflow.config.app_config import AppConfig

@router.get("/models")
def list_models():
    models = AppConfig.current().models
    ...

# After
from fastapi import Depends
from app.gateway.deps import get_config

@router.get("/models")
def list_models(config: AppConfig = Depends(get_config)):
    models = config.models
    ...
```

**For `mcp.py` / `skills.py` runtime config reload:**

```python
# Before
AppConfig.init(AppConfig.from_file())

# After
request.app.state.config = AppConfig.from_file()
# Keep the AppConfig.init() call alongside for now — other consumers still need it
AppConfig.init(request.app.state.config)
```

- [ ] **Step 1: Migrate `models.py`**

Replace 2 `AppConfig.current()` reads with `config: AppConfig = Depends(get_config)` parameter.

- [ ] **Step 2: Migrate `mcp.py`** — 3 reads + 1 reload write

- [ ] **Step 3: Migrate `memory.py`** — 2 reads

- [ ] **Step 4: Migrate `skills.py`** — 1 read + 1 reload write

- [ ] **Step 5: Migrate `auth/reset_admin.py`**

`reset_admin.py` is a CLI-like entry. Signature changes to `reset_admin(config: AppConfig)`. Caller in `cli.py` (or wherever it's invoked) constructs config at top.

- [ ] **Step 6: Migrate `app/channels/service.py`**

Constructor or `start_channel_service(config: AppConfig)` — pass config from `app.py` where it's called.

- [ ] **Step 7: Run full gateway test suite**

```bash
cd backend && PYTHONPATH=. uv run pytest tests/test_gateway_*.py tests/test_channels_*.py -v
```

- [ ] **Step 8: Grep verify Category G complete**

```bash
cd backend && grep -rn "AppConfig\.current()" app/gateway/ app/channels/
```
Expected: no matches.

- [ ] **Step 9: Commit**

```bash
git add backend/app/gateway/ backend/app/channels/ backend/tests/
git commit -m "refactor(config): migrate gateway routers and channels to Depends(get_config)"
```

---

## Task P2-3 (Category H): `DeerFlowClient` constructor-captured config

**Files:**
- Modify: `backend/packages/harness/deerflow/client.py` (7 `current()` + 2 `init()` calls)
- Modify: `backend/tests/test_client.py`, `backend/tests/test_client_e2e.py`

**Pattern:**

```python
# Before
class DeerFlowClient:
    def __init__(self, config_path: str | None = None):
        if config_path is not None:
            AppConfig.init(AppConfig.from_file(config_path))
        self._app_config = AppConfig.current()

    def some_method(self):
        ext = AppConfig.current().extensions
        ...

# After
class DeerFlowClient:
    def __init__(
        self,
        config_path: str | None = None,
        config: AppConfig | None = None,
    ):
        self._config = config or AppConfig.from_file(config_path)

    def some_method(self):
        ext = self._config.extensions
        ...

    def _reload_config(self):
        # Mutate self._config with model_copy or rebuild from file
        self._config = AppConfig.from_file(...)
```

- [ ] **Step 1: Update constructor signature**

Add `config: AppConfig | None = None` parameter. Construct `self._config` locally, not via `AppConfig.init() + current()`.

- [ ] **Step 2: Replace all 7 `AppConfig.current()` calls with `self._config`**

- [ ] **Step 3: Update `_reload_config()` to rebuild `self._config`**

- [ ] **Step 4: Write test for multi-client isolation**

```python
# backend/tests/test_client_multi_isolation.py
from deerflow.client import DeerFlowClient
from deerflow.config.app_config import AppConfig
from deerflow.config.sandbox_config import SandboxConfig
from deerflow.config.memory_config import MemoryConfig


def test_two_clients_different_configs_do_not_contend():
    cfg_a = AppConfig(sandbox=SandboxConfig(use="test"), memory=MemoryConfig(enabled=True))
    cfg_b = AppConfig(sandbox=SandboxConfig(use="test"), memory=MemoryConfig(enabled=False))

    client_a = DeerFlowClient(config=cfg_a)
    client_b = DeerFlowClient(config=cfg_b)

    assert client_a._config.memory.enabled is True
    assert client_b._config.memory.enabled is False
    # Verify mutation of one client's config does not affect the other
    # (impossible because frozen, but verify via identity too)
    assert client_a._config is cfg_a
    assert client_b._config is cfg_b
```

- [ ] **Step 5: Run test to verify multi-client works**

```bash
cd backend && PYTHONPATH=. uv run pytest tests/test_client_multi_isolation.py -v
```

- [ ] **Step 6: Update existing client tests**

Replace `AppConfig.init(MagicMock(...))` patterns in `test_client.py` with constructing `AppConfig` instances and passing via `DeerFlowClient(config=cfg)`.

- [ ] **Step 7: Run full client test suite**

```bash
cd backend && PYTHONPATH=. uv run pytest tests/test_client*.py -v
```

- [ ] **Step 8: Grep verify Category H complete**

```bash
cd backend && grep -n "AppConfig\.current()\|AppConfig\.init(" packages/harness/deerflow/client.py
```
Expected: no matches.

- [ ] **Step 9: Commit**

```bash
git add backend/packages/harness/deerflow/client.py backend/tests/
git commit -m "refactor(config): DeerFlowClient captures config in constructor"
```

---

## Task P2-4 (Category B): Agent construction — thread `AppConfig` from `make_lead_agent`

**Files:**
- Modify: `backend/packages/harness/deerflow/agents/lead_agent/agent.py` (5 calls)
- Modify: `backend/packages/harness/deerflow/agents/lead_agent/prompt.py` (5 calls)
- Modify: `backend/packages/harness/deerflow/agents/middlewares/tool_error_handling_middleware.py` (1 call)

**Pattern:**

```python
# Before
def make_lead_agent(config: RunnableConfig) -> CompiledStateGraph:
    app_config = AppConfig.current()
    model_name = _resolve_runtime_model_name(config)
    ...

def _build_middlewares(config, runtime_config):
    if AppConfig.current().token_usage.enabled:
        ...

# After
def make_lead_agent(config: RunnableConfig, app_config: AppConfig) -> CompiledStateGraph:
    model_name = _resolve_runtime_model_name(config, app_config)
    ...

def _build_middlewares(app_config: AppConfig, runtime_config: RunnableConfig):
    if app_config.token_usage.enabled:
        ...
```

- [ ] **Step 1: Update `make_lead_agent` signature and internal calls**

Add `app_config: AppConfig` parameter. Replace all 5 `AppConfig.current()` calls with `app_config.xxx`.

- [ ] **Step 2: Update `_build_middlewares`, `_create_*_middleware` helpers**

Thread `app_config` through each helper that previously called `AppConfig.current()`.

- [ ] **Step 3: Update `prompt.py` helpers**

Every function that previously called `AppConfig.current()` now takes the relevant config slice as a parameter. Caller (either `apply_prompt_template` or `make_lead_agent`) provides it.

- [ ] **Step 4: Update `tool_error_handling_middleware.py`**

Guardrail config is needed at middleware construction. Pass `GuardrailsConfig` to the middleware's `__init__`.

- [ ] **Step 5: Update the two call sites of `make_lead_agent`**

- `backend/langgraph.json` (or wherever LangGraph Server registers the agent) — the registration function wraps `make_lead_agent` and must supply `app_config`. If LangGraph Server doesn't support injecting extra args, wrap:

  ```python
  def _lead_agent_for_langgraph(config: RunnableConfig):
      return make_lead_agent(config, AppConfig.from_file())
  ```

  (LangGraph Server still reads config from file — there's no central config broker in that process yet.)

- `backend/packages/harness/deerflow/client.py` — already has `self._config`, pass it: `make_lead_agent(config, self._config)`.

- [ ] **Step 6: Run agent tests**

```bash
cd backend && PYTHONPATH=. uv run pytest tests/test_lead_agent*.py -v
```

- [ ] **Step 7: Grep verify Category B complete**

```bash
cd backend && grep -n "AppConfig\.current()" packages/harness/deerflow/agents/lead_agent/ packages/harness/deerflow/agents/middlewares/
```
Expected: no matches.

- [ ] **Step 8: Commit**

```bash
git add backend/packages/harness/deerflow/agents/ backend/langgraph.json backend/packages/harness/deerflow/client.py backend/tests/
git commit -m "refactor(config): thread AppConfig through lead agent construction"
```

---

## Task P2-5 (Category D): Runtime infrastructure takes config at construction

**Files:**
- Modify: `deerflow/runtime/checkpointer/provider.py` (2 calls), `async_provider.py` (1 call)
- Modify: `deerflow/runtime/store/provider.py` (2 calls), `async_provider.py` (1 call)
- Modify: `deerflow/runtime/stream_bridge/async_provider.py` (1 call)
- Modify: `deerflow/runtime/runs/worker.py` (1 call)

**Pattern:**

```python
# Before
class CheckpointerProvider:
    def get(self):
        config = AppConfig.current().checkpointer
        ...

# After
class CheckpointerProvider:
    def __init__(self, config: CheckpointerConfig | None):
        self._config = config

    def get(self):
        config = self._config
        ...
```

Callers construct these providers at startup (from `app/gateway/app.py` or `DeerFlowClient.__init__`) with the relevant config slice.

- [ ] **Step 1: Update `CheckpointerProvider` constructor + `get_checkpointer_provider()` factory**

The factory may need to go from a module-level singleton getter to one that accepts config. Alternatively, the factory stays but takes config as parameter.

- [ ] **Step 2: Update `StoreProvider` analogously**

- [ ] **Step 3: Update `StreamBridgeProvider` analogously**

- [ ] **Step 4: Update `worker.py`**

`Worker` already receives a `RunManager`; `RunManager` receives config at construction time (from Gateway `app.py`) and forwards to `Worker`. Replace `AppConfig.current()` in worker with the injected config.

- [ ] **Step 5: Update `RunManager` construction in `app/gateway/app.py`**

Pass `app.state.config` into `RunManager(..., config=app.state.config)`.

- [ ] **Step 6: Run runtime tests**

```bash
cd backend && PYTHONPATH=. uv run pytest tests/test_checkpointer*.py tests/test_store*.py tests/test_stream_bridge*.py tests/test_worker*.py -v
```

- [ ] **Step 7: Grep verify Category D complete**

```bash
cd backend && grep -rn "AppConfig\.current()" packages/harness/deerflow/runtime/
```
Expected: no matches.

- [ ] **Step 8: Commit**

```bash
git add backend/packages/harness/deerflow/runtime/ backend/app/gateway/app.py backend/tests/
git commit -m "refactor(config): runtime providers take config at construction"
```

---

## Task P2-6 (Category C): Memory subsystem — closure-captured config

**Files:**
- Modify: `deerflow/agents/memory/queue.py` (2 calls)
- Modify: `deerflow/agents/memory/updater.py` (3 calls)
- Modify: `deerflow/agents/memory/storage.py` (3 calls)

This category is the trickiest because the Timer callback runs on a thread without Runtime. Config must be captured at enqueue time into the closure.

**Pattern:**

```python
# Before — config read from ambient state on Timer thread
class MemoryQueue:
    def add(self, conversation, user_id):
        config = AppConfig.current().memory  # may not exist on Timer thread
        if not config.enabled:
            return
        # schedule Timer ...

# After — config captured at enqueue time
class MemoryQueue:
    def __init__(self, updater: MemoryUpdater, config: MemoryConfig):
        self._updater = updater
        self._config = config

    def add(self, conversation, user_id):
        config = self._config  # captured at construction
        if not config.enabled:
            return
        # Timer callback closes over `config` and `conversation`
        def _flush():
            self._updater.update(conversation, user_id, config)
        self._timer = Timer(config.debounce_seconds, _flush)
        self._timer.start()
```

- [ ] **Step 1: Add `MemoryConfig` parameter to `MemoryStorage.__init__`**

Replace all 3 `AppConfig.current().memory` reads with `self._config.memory` field accesses.

- [ ] **Step 2: Add `MemoryConfig` parameter to `MemoryUpdater.__init__`**

Same pattern.

- [ ] **Step 3: Add `MemoryConfig` parameter to `MemoryQueue.__init__`**

Same pattern. Timer callbacks close over `self._config`.

- [ ] **Step 4: Update the factory / caller path**

`MemoryMiddleware` (the consumer) currently constructs `MemoryQueue` lazily. Now it must get `MemoryConfig` from `runtime.context.app_config.memory` in `before_model`, and construct the queue with that config. Cache construction by config identity if re-construction on every invocation is too expensive.

Alternatively: `MemoryMiddleware.__init__(config: MemoryConfig)` and the config is supplied at middleware-chain construction time (from `make_lead_agent` → `_build_middlewares`).

- [ ] **Step 5: Write regression test for Timer thread**

```python
# backend/tests/test_memory_queue_timer_captures_config.py
def test_timer_callback_uses_captured_config():
    """Verify Timer callback reads config from closure, not ambient state."""
    cfg = MemoryConfig(enabled=True, debounce_seconds=0.01, ...)
    updater = MagicMock()
    queue = MemoryQueue(updater=updater, config=cfg)

    queue.add(conversation=..., user_id="u1")
    time.sleep(0.05)

    # Verify updater was called with the captured cfg, not a re-read from AppConfig
    assert updater.update.called
```

- [ ] **Step 6: Run memory tests**

```bash
cd backend && PYTHONPATH=. uv run pytest tests/test_memory*.py -v
```

- [ ] **Step 7: Grep verify Category C complete**

```bash
cd backend && grep -rn "AppConfig\.current()" packages/harness/deerflow/agents/memory/
```
Expected: no matches.

- [ ] **Step 8: Commit**

```bash
git add backend/packages/harness/deerflow/agents/memory/ backend/tests/
git commit -m "refactor(config): memory subsystem captures config at construction/enqueue"
```

---

## Task P2-7 (Category E+F): Sandbox / skills / factories / tools / community — parameter threading

This is the largest mechanical task by file count. All follow the same pattern: add `config: AppConfig` (or a sub-config slice) to the function signature, replace `AppConfig.current()` with the parameter.

**Files:**
- `deerflow/sandbox/local/local_sandbox_provider.py` (1), `sandbox_provider.py` (1), `security.py` (2)
- `deerflow/sandbox/tools.py` (5 — these already use `resolve_context()`; no change)
- `deerflow/skills/loader.py` (1), `manager.py` (1), `security_scanner.py` (1)
- `deerflow/models/factory.py` (1)
- `deerflow/tools/tools.py` (2)
- `deerflow/subagents/registry.py` (1)
- `deerflow/utils/file_conversion.py` (1)
- `deerflow/community/aio_sandbox/aio_sandbox_provider.py` (2)
- `deerflow/community/tavily/tools.py` (2)
- `deerflow/community/jina_ai/tools.py` (1)
- `deerflow/community/infoquest/tools.py` (3)
- `deerflow/community/image_search/tools.py` (1)
- `deerflow/community/firecrawl/tools.py` (2)
- `deerflow/community/exa/tools.py` (2)
- `deerflow/community/ddg_search/tools.py` (1)

**Pattern:**

```python
# Before
def get_available_tools(groups, include_mcp=True, model_name=None, subagent_enabled=False):
    config = AppConfig.current()
    ...

# After
def get_available_tools(
    app_config: AppConfig,
    groups=None,
    include_mcp=True,
    model_name=None,
    subagent_enabled=False,
):
    config = app_config
    ...
```

**Caller responsibility:** whoever calls `get_available_tools()` must have `AppConfig` in scope. For agent construction that's `make_lead_agent(config, app_config)` from Task P2-4. For factory tools registered via `use:` strings in config, the `tools.py` resolution pass threads `app_config` through.

- [ ] **Step 1: Update `deerflow/models/factory.py`**

`create_chat_model(name, thinking_enabled=False)` → `create_chat_model(name, app_config, thinking_enabled=False)`. Every caller (agent.py, client.py memory-updater internal model setup) passes `app_config`.

- [ ] **Step 2: Update `deerflow/tools/tools.py`**

`get_available_tools(...)` signature gains `app_config: AppConfig`. Community tool resolution inside it also threads config.

- [ ] **Step 3: Update `deerflow/subagents/registry.py`**

- [ ] **Step 4: Update `deerflow/sandbox/*.py` (non-tools)**

Provider construction takes config. `security.py` helpers take config parameter.

- [ ] **Step 5: Update `deerflow/skills/*.py`**

Loader / manager / scanner take config parameter.

- [ ] **Step 6: Update `deerflow/utils/file_conversion.py`**

- [ ] **Step 7: Update community tool factories**

Each `community/<name>/tools.py` factory now accepts `app_config`. The `tools.py` resolution pass (Step 2) supplies it when instantiating.

- [ ] **Step 8: Run affected test files**

```bash
cd backend && PYTHONPATH=. uv run pytest tests/test_tool*.py tests/test_skill*.py tests/test_sandbox*.py tests/test_community*.py tests/test_*tool*.py -v
```

- [ ] **Step 9: Grep verify Category E+F complete**

```bash
cd backend && grep -rn "AppConfig\.current()" packages/harness/deerflow/{sandbox,skills,models,tools,subagents,utils,community}/
```
Expected: no matches (except `sandbox/tools.py` may retain `resolve_context()` calls for dict-legacy paths — those are fine).

- [ ] **Step 10: Commit**

```bash
git add backend/packages/harness/deerflow/ backend/tests/
git commit -m "refactor(config): thread AppConfig through sandbox/skills/factories/tools"
```

---

## Task P2-8 (Category I): Test fixtures

**Files:**
- Modify: `backend/tests/conftest.py`
- Modify: ~18 test files using `patch.object(AppConfig, "current")` or `AppConfig._global = ...`

**Pattern:**

```python
# Before — conftest.py autouse fixture
@pytest.fixture(autouse=True)
def _auto_app_config():
    previous_global = AppConfig._global
    AppConfig._global = AppConfig(sandbox=SandboxConfig(use="test"))
    try:
        yield
    finally:
        AppConfig._global = previous_global


# Before — test using it
def test_something():
    with patch.object(AppConfig, "current", return_value=AppConfig(...)):
        result = function_under_test()

# After — conftest.py fixture returns config
@pytest.fixture
def test_config() -> AppConfig:
    """Minimal AppConfig for tests that need one."""
    return AppConfig(sandbox=SandboxConfig(use="test"))


# After — test passes config explicitly
def test_something(test_config):
    overridden = test_config.model_copy(update={"memory": MemoryConfig(enabled=False)})
    result = function_under_test(config=overridden)
```

- [ ] **Step 1: Update `conftest.py`**

Replace `_auto_app_config` autouse fixture with a non-autouse `test_config` fixture. The autouse is no longer needed because `AppConfig.current()` no longer exists after P2-10.

**Note:** Do not remove autouse yet. Tests that still call `AppConfig.current()` (pre-migration) would break. Instead:
- Add the new `test_config` fixture
- Keep autouse for now so old tests still work
- Remove autouse only in Task P2-10 alongside deletion of `current()`

- [ ] **Step 2: Migrate tests by module, starting with most isolated**

For each test file using `patch.object(AppConfig, "current", ...)`:
- Replace with fixture injection: `def test_xxx(test_config)` and pass `test_config` (or a `model_copy(update=...)` variant) into the function under test.

Per-file migration order (smallest blast radius first):
1. `test_memory_updater.py` (14 occurrences) — Memory subsystem already took config parameter in P2-6
2. `test_client.py` (20 occurrences) — Client already took config in P2-3
3. `test_checkpointer.py` (11 occurrences) — Providers took config in P2-5
4. `test_memory_storage.py` (10 occurrences)
5. Remaining files

- [ ] **Step 3: Verify all tests pass after each file migration**

```bash
cd backend && PYTHONPATH=. uv run pytest tests/<migrated_file>.py -v
```

- [ ] **Step 4: Commit after each file (keeps diffs reviewable)**

```bash
git commit -m "refactor(tests): migrate <file> to explicit config fixture"
```

- [ ] **Step 5: Final grep verify**

```bash
cd backend && grep -rn "patch\.object(AppConfig, \"current\"" tests/
cd backend && grep -rn "AppConfig\._global" tests/
```
Expected: no matches.

---

## Task P2-9: Simplify `resolve_context()`

**Files:**
- Modify: `backend/packages/harness/deerflow/config/deer_flow_context.py`
- Test: `backend/tests/test_deer_flow_context.py`

After P2-2 through P2-8, every caller that invokes `resolve_context()` either passes a typed `DeerFlowContext` or a dict. The dict path's `AppConfig.current()` fallback is no longer reachable if all construction sites are explicit.

- [ ] **Step 1: Update `test_deer_flow_context.py` to expect hard failure on non-DeerFlowContext**

```python
def test_resolve_context_raises_on_missing_context():
    runtime = MagicMock()
    runtime.context = None
    with pytest.raises(RuntimeError, match="not a DeerFlowContext"):
        resolve_context(runtime)

def test_resolve_context_raises_on_dict_context():
    runtime = MagicMock()
    runtime.context = {"thread_id": "t1"}
    with pytest.raises(RuntimeError, match="not a DeerFlowContext"):
        resolve_context(runtime)
```

- [ ] **Step 2: Simplify `resolve_context()`**

```python
def resolve_context(runtime: Any) -> DeerFlowContext:
    ctx = getattr(runtime, "context", None)
    if isinstance(ctx, DeerFlowContext):
        return ctx
    raise RuntimeError(
        "runtime.context is not a DeerFlowContext. Every caller must "
        "construct and inject one explicitly; there is no global fallback."
    )
```

- [ ] **Step 3: Run `test_deer_flow_context.py`**

```bash
cd backend && PYTHONPATH=. uv run pytest tests/test_deer_flow_context.py -v
```

- [ ] **Step 4: Run full test suite to catch any missed dict-context callers**

```bash
cd backend && PYTHONPATH=. uv run pytest -v
```

If failures surface, they indicate a caller that was still relying on dict-context fallback. Fix by constructing proper `DeerFlowContext`.

- [ ] **Step 5: Commit**

```bash
git add backend/packages/harness/deerflow/config/deer_flow_context.py backend/tests/test_deer_flow_context.py
git commit -m "refactor(config): resolve_context requires typed DeerFlowContext"
```

---

## Task P2-10: Delete `AppConfig` lifecycle

**Files:**
- Modify: `backend/packages/harness/deerflow/config/app_config.py`
- Modify: `backend/tests/conftest.py` (remove `_auto_app_config` autouse fixture)
- Modify: `backend/tests/test_app_config_reload.py` (delete or rewrite as pure `from_file()` test)
- Modify: `backend/CLAUDE.md` (update Config Lifecycle section)

Final deletion. Grep must show no callers of `AppConfig.current()`, `AppConfig.init()`, `AppConfig.set_override()`, `AppConfig.reset_override()` in production or tests.

- [ ] **Step 1: Final grep — verify no callers remain**

```bash
cd backend && grep -rn "AppConfig\.\(current\|init\|set_override\|reset_override\)" packages/ app/ tests/
```
Expected: no matches (except the `app_config.py` definitions themselves).

If any match, return to the relevant Category task and finish the migration.

- [ ] **Step 2: Delete from `app_config.py`**

Remove:
- `_global: ClassVar[AppConfig | None]`
- `_override: ClassVar[ContextVar[AppConfig]]`
- `init()`, `set_override()`, `reset_override()`, `current()`
- The comment block `"# -- Lifecycle (process-global + per-context override) --"`
- Unused imports: `ContextVar`, `Token`, `ClassVar`

The class reduces to: Pydantic fields + `from_file()`, `resolve_config_path()`, `resolve_env_variables()`, `_check_config_version()`, `get_model_config()`, `get_tool_config()`, `get_tool_group_config()`.

- [ ] **Step 3: Remove `_auto_app_config` autouse fixture from `conftest.py`**

Keep only the explicit `test_config` fixture (non-autouse).

- [ ] **Step 4: Delete or rewrite `test_app_config_reload.py`**

The tests covered `init` / `set_override` / auto-load, all of which are gone. Rewrite as a single test:

```python
def test_from_file_is_pure(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("config_version: 6\nsandbox:\n  use: test\n")

    result1 = AppConfig.from_file(str(config_file))
    result2 = AppConfig.from_file(str(config_file))

    # Different objects (Pydantic doesn't intern)
    assert result1 is not result2
    # But equal values
    assert result1 == result2
    # Frozen — cannot mutate
    with pytest.raises(ValidationError):
        result1.log_level = "debug"
```

- [ ] **Step 5: Update `backend/CLAUDE.md`**

Rewrite the "Config Lifecycle" section:

```markdown
**Config Lifecycle**: All config models are `frozen=True` (immutable after construction). `AppConfig.from_file()` is a pure function — no side effects. There is no process-global or ContextVar — every consumer receives `AppConfig` as an explicit parameter.

- `app/gateway/app.py` loads config at startup and stores on `app.state.config`; routers access via `Depends(get_config)`
- `DeerFlowClient.__init__(config_path=..., config=...)` captures config as `self._config`
- Agent execution path: `DeerFlowContext(app_config=..., thread_id=...)` injected via LangGraph `Runtime[DeerFlowContext]`
- Background threads (memory debounce Timer): config captured at enqueue time in closure
- Tests: use the `test_config` fixture or construct `AppConfig` directly
```

- [ ] **Step 6: Run full test suite**

```bash
cd backend && PYTHONPATH=. uv run pytest -v
```
Expected: all pass.

- [ ] **Step 7: Run linter**

```bash
cd backend && make lint
```

- [ ] **Step 8: Commit**

```bash
git add backend/packages/harness/deerflow/config/app_config.py backend/tests/conftest.py backend/tests/test_app_config_reload.py backend/CLAUDE.md
git commit -m "refactor(config): delete AppConfig process-global and ContextVar lifecycle"
```

---

## Verification — Phase 2 complete

- [ ] **No global lookup remains**

```bash
cd backend && grep -rn "AppConfig\.current()\|AppConfig\._global\|AppConfig\._override\|AppConfig\.init(\|AppConfig\.set_override(\|AppConfig\.reset_override(" packages/ app/ tests/
```
Expected: no matches.

- [ ] **`AppConfig` is a pure value object**

Read `backend/packages/harness/deerflow/config/app_config.py`. It should contain: Pydantic fields, `from_file()`, `resolve_config_path()`, `resolve_env_variables()`, `_check_config_version()`, `get_model_config()`, `get_tool_config()`, `get_tool_group_config()`. Nothing else.

- [ ] **Multi-client isolation works**

`tests/test_client_multi_isolation.py` passes — two clients with different configs coexist.

- [ ] **Full test suite green**

```bash
cd backend && PYTHONPATH=. uv run pytest -v && make lint
```

- [ ] **Commit log tells the story**

```bash
git log --oneline refactor/explicit-config-p2
```
Shows ~10 commits, each scoped to one Category.
