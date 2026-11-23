# GA Response Speed Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make GA's interactive token output feel continuous like Pi by removing avoidable display waits first, then reducing repeated schema/history/prompt work without changing agent behavior.

**Architecture:** Keep provider streaming and the JSONL protocol intact. Move batching to an explicit low-latency policy: first delta immediate, later deltas coalesced by a bounded frame window, then preserve GA's existing stream-commit and Static-prefix invariants. After the display path is measured, add fingerprinted schema/tool-prompt caching and conservative history projection with explicit invalidation on context changes.

**Tech Stack:** Python 3.10-3.13, `unittest`, React 18 + Ink 5, TypeScript + Node test runner + `tsx`, existing JSONL bridge, existing `zhanlu ds` profile for paid A/B runs only.

**Spec:** `docs/20261122-ga-response-speed-optimization-design.md`

**Execution status:** The [implementation record](../../20261122-ga-response-speed-optimization-implementation.md) tracks verified results and deviations from this original plan. The Python gate remains active and the new paths are opt-in. The full acceptance criteria are still open.

## Global Constraints

- Use the repository's TDD rule: write a failing focused test before production code, then run the test red, implement the smallest change, and run it green.
- Keep diagnostic scripts in `tests/probes/python/` or `frontends/ink/ui/probes/`; every probe must state what it measures, the root cause coordinate, why it is deterministic, and why it must be kept.
- Do not delete existing diagnostics or revert unrelated user changes.
- Do not call a real API during local baseline work; paid measurements use only the configured `zhanlu ds` profile and compare baseline/candidate on the same interleaved task set.
- Preserve `assistant_delta` as incremental text and `assistant_done` as the final full display text.
- Preserve Static-prefix ordering, stream-commit fence safety, thinking/prose ordering, resize, selection, cursor, MCP, permission, workflow, compaction, resume, and model-switch behavior.
- Do not commit changes unless the user explicitly asks for a commit.

## Review Focus

- A one-character first chunk must become visible without waiting for a 16-character buffer; test in the Python stream policy and the Ink event scheduler.
- A slow stream with 1-5 character chunks must not create duplicate, missing, or reordered text; test with a deterministic delayed fake stream and `assistant_done` reconciliation.
- Thinking-to-prose and prose-to-tool boundaries must flush immediately and preserve ordering; test in the UI reducer and bridge timeline.
- Long history must not re-render the unchanged Static prefix on every delta; test partition identity/render counters before and after the active tail changes.
- Schema/history cache invalidation must occur after MCP, permission, model, compaction, resume, rewind, and provider-mode changes; test each invalidation class before enabling the optimization.

---

### Task 1: Establish deterministic latency baseline and trace contracts

**Files:**
- Create: `tests/probes/python/stream_latency_baseline.py`
- Create: `frontends/ink/ui/probes/stream_latency_timeline.ts`
- Create: `tests/test_stream_latency_probe.py`
- Modify: `genericagent/core/app.py` only if a trace hook is needed; keep it disabled unless `GA_LATENCY_TRACE=1`
- Modify: `frontends/ink/bridge.py` only if a trace hook is needed; keep protocol payload unchanged

**Interfaces:**
- Produces a deterministic synthetic stream with events for `first_chunk`, `short_chunks`, `thinking_boundary`, `tool_boundary`, `done`, and `slow_gap`.
- Produces timing fields named `provider_first_chunk`, `python_display_enqueue`, `bridge_emit`, `frontend_dispatch`, `render_frame`, and `assistant_done` when tracing is enabled.
- The probes must print a control/candidate verdict and write no production state.

- [ ] **Step 1: Write the failing Python probe assertions**

  Add tests that execute the probe against the legacy policy and assert it records the expected legacy behavior: a 1-15 character first stream does not flush through the legacy character gate, while a 16-character stream does. Assert that missing output or a timeout raises, never silently passes.

- [ ] **Step 2: Write the failing TypeScript probe assertions**

  Add a probe harness that feeds `assistant_delta` events into the App event path and records dispatch/frame times. Assert that the probe can distinguish `event received`, `dispatch`, and `frame rendered`, and that it reports a non-empty final frame.

- [ ] **Step 3: Run the new probes red**

  Run:

  ```powershell
  python -m unittest tests.test_stream_latency_probe -v
  cd frontends/ink/ui
  npx tsx probes/stream_latency_timeline.ts
  ```

  Expected: the new contract assertions fail because the trace hooks and candidate scheduler do not exist yet; the legacy control measurements must still print.

- [ ] **Step 4: Add opt-in trace points only**

  Add a small internal trace helper or equivalent monotonic timestamp calls. Do not include user text, prompts, API keys, or authorization headers. Emit no extra JSONL protocol events; write trace data to a test-owned sink or a file only when `GA_LATENCY_TRACE=1`.

- [ ] **Step 5: Run baseline and record the control output**

  Run the Python probe repeatedly with the legacy path and the TypeScript probe with the legacy 80ms scheduler. Record p50/p90 event-to-dispatch and render counts in the implementation notes. Do not use these as a product win; they are the baseline.

- [ ] **Step 6: Verify the baseline artifacts**

  Run:

  ```powershell
  python -m unittest tests.test_stream_latency_probe -v
  cd frontends/ink/ui
  npm run typecheck
  npx tsx probes/stream_latency_timeline.ts
  ```

  Expected: all probe contracts pass, legacy baseline is reproducible across at least three runs, and no API request is made.

### Task 2: Replace the Python 16-character gate with a bounded stream flush policy

**Files:**
- Create: `genericagent/core/streaming.py`
- Modify: `genericagent/core/app.py:97,373-374,1868-1895`
- Modify: `tests/test_agentmain_streaming.py`
- Create: `tests/test_streaming_policy.py`

**Interfaces:**
- `StreamFlushPolicy(mode: str = "frame", interval_ms: int = 24, max_chars: int = 64)`.
- `StreamFlushPolicy.push(text: str, now: float) -> str | None` returns only newly flushed text.
- `StreamFlushPolicy.flush(now: float, force: bool = False) -> str | None` returns the pending tail when the interval, size, or force condition applies.
- `StreamFlushPolicy.reset() -> None` clears state between turns.
- `genericagent.core.app.should_flush_display_delta(...)` remains as a compatibility helper for existing callers/tests, but the turn loop uses `StreamFlushPolicy` under `GA_STREAM_FLUSH_MODE=frame`.

- [ ] **Step 1: Write failing policy tests**

  Add tests for:

  - `legacy` behavior: 15 characters do not flush, 16 characters flush;
  - `frame` behavior: first non-empty chunk flushes immediately;
  - subsequent chunks flush when `now - last_flush >= 24ms` or pending chars reach 64;
  - a forced flush returns the complete pending tail exactly once;
  - empty chunks do not create events;
  - `reset()` prevents text from one turn leaking into the next.

- [ ] **Step 2: Run the policy tests red**

  Run:

  ```powershell
  python -m unittest tests.test_streaming_policy -v
  ```

  Expected: FAIL because `genericagent/core/streaming.py` does not exist.

- [ ] **Step 3: Implement `StreamFlushPolicy`**

  Keep the policy pure and monotonic-clock driven. The initial candidate values are `interval_ms=24` and `max_chars=64`; expose environment overrides only through the app integration, not through the pure class. The first non-empty chunk must be immediate so the policy cannot recreate the old first-token wait.

- [ ] **Step 4: Integrate the policy into `_drain_agent_stream()`**

  In `GenericAgent._drain_agent_stream`, instantiate one policy per stream, call `push()` for each chunk, enqueue only returned deltas, force-flush before classifying the outcome, and preserve the existing `inc_out` full-response behavior. Keep the legacy path selectable with `GA_STREAM_FLUSH_MODE=legacy` for rollback.

- [ ] **Step 5: Update regression tests**

  Extend `tests/test_agentmain_streaming.py` to assert both modes and preserve the step-marker immediate flush and generic marker non-flush behavior. Add an exact-output test proving the concatenation of all emitted `next` values equals the source stream.

- [ ] **Step 6: Run focused verification**

  Run:

  ```powershell
  python -m unittest tests.test_streaming_policy tests.test_agentmain_streaming -v
  python -m unittest tests.test_turn_outcome -v
  ```

  Expected: PASS; legacy mode remains available and frame mode emits the first short chunk immediately without changing final text.

### Task 3: Replace the Ink 80ms delta timer with one frame scheduler

**Files:**
- Create: `frontends/ink/ui/src/streamScheduler.ts`
- Create: `frontends/ink/ui/src/streamScheduler.test.ts`
- Modify: `frontends/ink/ui/src/App.tsx:1562-1572,1804-1813`
- Modify: `frontends/ink/ui/src/state.test.ts`
- Modify: `frontends/ink/ui/src/App.test.ts` if event-to-frame coverage belongs there

**Interfaces:**
- `StreamSchedulerOptions` with `intervalMs: number`, `dispatch: (taskId: number, text: string) => void`, and `now?: () => number` for tests.
- `createStreamScheduler(options) -> { push(taskId, text): void; flush(): void; clear(): void }`.
- First `push()` for a task dispatches immediately; later pushes coalesce until the next scheduled frame.
- `flush()` dispatches all pending task buffers in task-id order and clears them.
- `clear()` cancels timers and drops pending buffers without dispatching.

- [ ] **Step 1: Write failing scheduler tests**

  Add deterministic tests for:

  - first short delta dispatches immediately;
  - multiple deltas inside 24ms dispatch once with concatenated text;
  - deltas for two task IDs remain separate and flush in numeric task order;
  - `flush()` sends pending text exactly once;
  - `clear()` cancels pending output;
  - an `assistant_done`-equivalent flush prevents a late timer from dispatching duplicate text.

- [ ] **Step 2: Run scheduler tests red**

  Run:

  ```powershell
  cd frontends/ink/ui
  npx tsx --test src/streamScheduler.test.ts
  ```

  Expected: FAIL because the scheduler module does not exist.

- [ ] **Step 3: Implement the scheduler**

  Use one timer for the whole scheduler, not one timer per task. Set the initial integration interval to 24ms. Keep the scheduler independent of React and Ink so tests can measure event-to-dispatch without a rendered terminal.

- [ ] **Step 4: Integrate App event handling**

  Replace the local `pendingDeltas` and 80ms timer with `createStreamScheduler`. On `assistant_delta`, call `push`; on history replacement, rewind, assistant done, unmount, or bridge shutdown, call `flush`/`clear` in the existing order. Keep `assistant_done` as the final full-text reconciliation path.

- [ ] **Step 5: Add protocol-level regression assertions**

  Extend `state.test.ts` so a sequence of short deltas followed by `assistant_done` produces exactly one final assistant body, with no duplicate committed prefix. Add a thinking boundary test to prove `assistant_thinking` is not delayed behind pending prose.

- [ ] **Step 6: Run focused UI verification**

  Run:

  ```powershell
  cd frontends/ink/ui
  npx tsx --test src/streamScheduler.test.ts src/state.test.ts src/streamCommit.test.ts src/messagePartition.test.ts
  npm run typecheck
  ```

  Expected: PASS with no duplicate/missing text and no type errors.

### Task 4: Preserve the Static prefix and avoid reformatting it on live deltas

**Files:**
- Modify: `frontends/ink/ui/src/messagePartition.ts`
- Modify: `frontends/ink/ui/src/messagePartition.test.ts`
- Modify: `frontends/ink/ui/src/App.tsx:2831-2893`
- Modify: `frontends/ink/ui/src/App.test.ts`
- Create: `frontends/ink/ui/src/liveTranscriptMetrics.ts` only if a pure render-counter helper is needed

**Interfaces:**
- Extend `splitStaticAndActiveMessages(messages, options, previous?)` so it may reuse `previous.staticMessages` when the static prefix is unchanged by object identity and length.
- The returned `MessagePartition` remains `{ staticMessages: ChatMessage[]; activeMessages: ChatMessage[] }`.
- No UI semantic change: the static array is always a strict prefix and active messages begin at the first open message.

- [ ] **Step 1: Write failing structural-sharing tests**

  Add a test that creates a completed history plus an open user/assistant tail, updates only the active assistant object, and asserts the next partition reuses the previous static prefix array while returning a new active suffix. Add a test that changing a completed message invalidates the reuse.

- [ ] **Step 2: Run the partition tests red**

  Run:

  ```powershell
  cd frontends/ink/ui
  npx tsx --test src/messagePartition.test.ts
  ```

  Expected: FAIL because the current function has no previous-partition reuse contract.

- [ ] **Step 3: Implement prefix reuse**

  Compare prefix length and object identity, then reuse the old static array only when every prefix entry is the same object. Keep the current ordering rules for done local-command rows after an open turn. Do not compare only IDs; text or role changes must invalidate reuse.

- [ ] **Step 4: Integrate App memoization**

  Keep a ref to the previous partition and pass it into the partition function. Ensure static transcript `useMemo` dependencies see a stable static array during active-only updates. Do not remove the full transcript path used by fullscreen selection until the existing frame/selection tests remain green.

- [ ] **Step 5: Add a render-counter regression test**

  Instrument the pure transcript projection or test helper, feed 100 active deltas after a 100-message completed history, and assert the completed-history projection is computed once per stable prefix rather than once per delta. Assert all assistant text still appears once after `assistant_done`.

- [ ] **Step 6: Run focused UI verification**

  Run:

  ```powershell
  cd frontends/ink/ui
  npx tsx --test src/messagePartition.test.ts src/state.test.ts src/App.test.ts src/streamCommit.test.ts
  npm run typecheck
  ```

  Expected: PASS; static prefix identity is stable during live updates and all existing transcript/selection behavior remains intact.

### Task 5: Cache schema snapshots and tool JSON with explicit invalidation

**Files:**
- Create: `genericagent/core/schema_cache.py`
- Create: `tests/test_schema_cache.py`
- Modify: `genericagent/core/app.py:478-543,2160-2209`
- Modify: `genericagent/llm/core.py:1408-1454`
- Modify: existing capability/MCP tests only where cache invalidation contracts are asserted

**Interfaces:**
- `SchemaSnapshot` dataclass containing `tools`, `fingerprint`, `source_epoch`, and `created_at`.
- `SchemaCache.get(key) -> SchemaSnapshot | None`.
- `SchemaCache.put(key, snapshot) -> None`.
- `SchemaCache.invalidate(reason: str) -> None`.
- `schema_cache_key(model_name, language_suffix, mcp_fingerprint, capability_fingerprint, permission_mode, context_epoch) -> tuple`.
- `ToolClient` receives a stable tools fingerprint and reuses serialized `tools_json` only when the fingerprint matches.

- [ ] **Step 1: Write failing cache tests**

  Add tests for:

  - same key returns the same immutable snapshot;
  - changing MCP tools, language suffix, capability profile, permission mode, or model changes the key;
  - explicit invalidation removes the snapshot;
  - schema list mutation by a caller cannot mutate the cached snapshot;
  - tool JSON is reused for equal fingerprint and rebuilt for changed fingerprint.

- [ ] **Step 2: Run cache tests red**

  Run:

  ```powershell
  python -m unittest tests.test_schema_cache -v
  ```

  Expected: FAIL because the cache module and fingerprint integration do not exist.

- [ ] **Step 3: Implement the bounded snapshot cache**

  Keep it process-local and bounded to the current active profile/context. Use deep copies or immutable projections at the boundary. Do not persist schema cache to disk; MCP runtime remains responsible for its own durable discovery cache.

- [ ] **Step 4: Integrate `load_tool_schema()`**

  Split static asset loading from dynamic MCP append/filter. Build the fingerprint after MCP snapshot and capability filtering are known. Reuse a snapshot on a matching key; atomically replace it when background MCP discovery changes the fingerprint.

- [ ] **Step 5: Integrate `ToolClient` tool JSON reuse**

  Avoid repeated `json.dumps(tools)` for the same fingerprint. Preserve `file_write` description augmentation and ensure the augmented schema participates in the fingerprint. Keep legacy `last_tools` behavior as a fallback for profiles without a fingerprint.

- [ ] **Step 6: Add invalidation coverage**

  Exercise model switch, permission switch, MCP enable/disable/reconnect, capability profile change, and language-specific schema selection. Assert the next turn sees the new tool set and no stale tool remains exposed.

- [ ] **Step 7: Run focused verification**

  Run:

  ```powershell
  python -m unittest tests.test_schema_cache tests.test_agentmain_streaming tests.test_mcp_runtime -v
  ```

  Expected: PASS; repeated same-schema turns avoid reconstruction while all schema-changing operations force a new snapshot.

### Task 6: Add conservative history projection and prompt timing metrics

**Files:**
- Create: `genericagent/llm/history_projection.py`
- Create: `tests/test_history_projection.py`
- Modify: `genericagent/llm/core.py:1174-1234,1317-1335`
- Modify: `genericagent/core/app.py` to increment/pass `context_epoch` at turn and context-reset boundaries

**Interfaces:**
- `HistoryProjectionAnchor` dataclass: `history_len`, `tail_fingerprint`, `context_epoch`, `model`, `api_mode`.
- `HistoryProjection.project(history, anchor, previous_projection) -> tuple[projected_history, next_anchor]`.
- `HistoryProjection.invalidate() -> None`.
- `full_projection` remains the fallback for an anchor mismatch or unsupported message shape.

- [ ] **Step 1: Write failing projection tests**

  Cover:

  - unchanged prefix plus one appended message reuses the prior projected prefix;
  - changed tail appends only the new projection;
  - tool_use/tool_result pairing forces full projection when the anchor is unsafe;
  - compaction, rewind, resume, model switch, provider mode switch, and context epoch change force full projection;
  - projected history is deep-copied at the provider boundary so later backend mutation cannot corrupt the cache.

- [ ] **Step 2: Run projection tests red**

  Run:

  ```powershell
  python -m unittest tests.test_history_projection -v
  ```

  Expected: FAIL because the projection module does not exist.

- [ ] **Step 3: Implement conservative incremental projection**

  Reuse only a stable prefix whose object identity/fingerprint and context epoch match. If any message contains unsupported blocks, malformed tool pairing, images requiring re-encoding, or an uncertain history mutation, return the existing full conversion path rather than guessing.

- [ ] **Step 4: Integrate `BaseSession.ask()` and native sessions**

  Keep the current lock, retry rollback, trim, compaction, and provider-specific normalization semantics. The projection is a construction optimization; it must not change the backend `history` truth or the outgoing provider message shape.

- [ ] **Step 5: Add timing counters**

  Under `GA_LATENCY_TRACE=1`, record history copy, history projection, schema lookup, tool JSON serialization, and provider request start separately. Keep logs sanitized and bounded.

- [ ] **Step 6: Run focused verification**

  Run:

  ```powershell
  python -m unittest tests.test_history_projection tests.test_llm_thinking tests.test_agentmain_streaming -v
  python -m unittest tests.test_ink_bridge -v
  ```

  Expected: PASS; provider payload contracts and thinking/tool history behavior remain unchanged.

### Task 7: Run staged `zhanlu ds` A/B comparisons and tune one variable at a time

**Files:**
- Create: `tests/probes/python/zhanlu_ds_latency_ab.py`
- Modify: `docs/20261122-ga-response-speed-optimization-design.md` with measured results and decisions
- Create: `docs/20261122-ga-response-speed-optimization-implementation.md`

**Interfaces:**
- Probe accepts `--profile zhanlu-ds` or the exact configured profile name discovered from local config, `--mode legacy|candidate`, `--rounds N`, and a fixed task-set file.
- Probe refuses to run if the selected profile is not the project `zhanlu ds` configuration or if credentials/base URL cannot be identified from the project config.
- Probe outputs JSONL timing rows plus a summary with p50/p90, output token counts, cache fields, render counters, and quality invariant results.

- [ ] **Step 1: Write the probe contract and no-network tests**

  Test argument validation, profile selection, task-set determinism, redaction, and summary calculation with fake rows. The test must prove that an unknown profile is rejected before any subprocess/API call.

- [ ] **Step 2: Run probe tests red**

  Run:

  ```powershell
  python -m unittest tests.test_zhanlu_ds_latency_ab -v
  ```

  Expected: FAIL because the probe does not exist.

- [ ] **Step 3: Implement the probe without running it**

  Reuse the existing real Ink self-test conventions. Use the project’s own config loader to resolve the profile; never read Claude Code’s environment as a fallback. Interleave baseline and candidate tasks, keep task prompts fixed, and record enough evidence to compare display delay separately from provider delay.

- [ ] **Step 4: Run local verification only**

  Run the probe in fake/offline mode and run all focused unit tests. Do not invoke the real model in this step.

- [ ] **Step 5: Run Phase 1 A/B only after explicit measurement start**

  Use `zhanlu ds` with the fixed task set and at least three interleaved rounds per task. Compare legacy versus frame scheduler, then keep the candidate only if first visible output improves without missing/duplicate text or quality regression.

- [ ] **Step 6: Run Phase 2 A/B separately**

  Starting from the accepted display candidate, compare schema cache alone, then history projection alone, then the combined candidate. Record cache hit/miss and request construction timings; do not attribute a provider speed change to local code without the phase timestamps.

- [ ] **Step 7: Write the implementation report**

  Record baseline, candidate, task set, profile/config identity, p50/p90 metrics, output/cache usage, quality invariants, failed candidates, and the chosen default/rollback flag. Label any result not checked against a real API as “quality not checked”.

### Task 8: Full regression and completion review

**Files:**
- Modify: `docs/20261122-ga-response-speed-optimization-implementation.md`
- Modify: feature flags/config docs only if the chosen candidate becomes the default

- [ ] **Step 1: Run Python focused tests**

  ```powershell
  python -m unittest tests.test_streaming_policy tests.test_agentmain_streaming tests.test_schema_cache tests.test_history_projection tests.test_ink_bridge -v
  ```

- [ ] **Step 2: Run Ink focused tests and typecheck**

  ```powershell
  cd frontends/ink/ui
  npm test
  npm run typecheck
  ```

- [ ] **Step 3: Run the repository test suite**

  ```powershell
  python -m unittest discover -s tests -t .
  ```

- [ ] **Step 4: Run deterministic probes repeatedly**

  Repeat the stream and timeline probes enough to establish stable control/candidate behavior. Any stochastic or non-monotonic result must be reported as unresolved rather than converted into a success claim.

- [ ] **Step 5: Review the final diff against the design invariants**

  Check protocol compatibility, feature flag rollback, no secret leakage in traces, MCP/schema invalidation, provider history compatibility, and all Review Focus cases.

- [ ] **Step 6: Report comparison results**

  State exactly which phases were implemented, which were measured with `zhanlu ds`, which were not run, and whether the evidence supports changing the default behavior. Do not claim completion or speed improvement without the corresponding test and timing output.
