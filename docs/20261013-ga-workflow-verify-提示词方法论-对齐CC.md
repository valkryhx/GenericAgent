# GA workflow：verify 的「提示词方法论」对齐 CC

> 目标读者：后续实施者。本文件先落盘事实与差距，再据此改提示词。
> 上游证据仓库：`D:\git_codes\claudecode283_hack`（Claude Code 2.1.283）。

---

## 1. 结论（一句话）

CC 的 verify **不是宿主强制的「真实运行」契约**，而是 **prompt 层方法论**（对抗验证 / judge panel / completeness critic 等）。
GA 走同一条路线：**不改宿主判据，只把提示词补到与 CC 同款**。

---

## 2. CC 的事实与确切配方

### 2.1 没有独立验收器（`docs/20261012-复刻dynamic-workflow指南.md` §10.2）

- 「是否有独立于 LLM 的验收器？**没有**。脚本 `return` 什么，最终结果就是什么。」
- 「错误结果是否可能顺利通过执行器并成为最终输出？**是**……唯一对抗这种风险的是 **prompt 层的方法论**
  （adversarial verify / judge panel / completeness critic）——这是**概率性**缓解，不是验证器。」

### 2.2 质量模式配方（`claudecode283/extracted/workflow-authoring-reference.md:112-126`）

原文要点（可直接照抄的**具体文本**）：

- **Adversarial verify**（对抗验证）：
  ```js
  const votes = await parallel(Array.from({length: 3}, () => () =>
    agent(`Try to refute: ${claim}. Default to refuted=true if uncertain.`, {schema: VERDICT})))
  const survives = votes.filter(Boolean).filter(v => !v.refuted).length >= 2
  ```
  N 个**独立** skeptic，每个被要求 **REFUTE**；`Default to refuted=true if uncertain`（不确定即判「被驳倒」）；≥多数 refute 则 kill。
- **Perspective-diverse verify**：每个 verifier 给一个**不同 lens**（correctness / security / perf / does-it-reproduce），
  多样性比 N 个同款 refuter 更能抓到失败模式。
- **Judge panel**：N 个不同角度的独立尝试 → 并行 judge 打分 → 从赢家综合并移植亚军的好点子。
- **Loop-until-dry**：未知规模的发现，一直生成 finder 直到 **K 轮连续无新发现**；
  **关键陷阱**：`dedup vs seen, NOT confirmed` —— 否则被 judge 否掉的 finding 每轮重现，永不收敛。
- **Multi-modal sweep**：并行用不同搜索角度（by-container / by-content / by-entity / by-time）。
- **Completeness critic**：最后一个 agent 问「**还缺什么**——没跑过的模态、没验证的 claim、没读的源」，其发现成为下一轮工作。
- **No silent caps**：有界覆盖（top-N、无重试、采样）必须 `log()` 出被丢弃的内容；静默截断读起来像「全覆盖」。

### 2.3 结构选择：「DEFAULT TO pipeline()」

- 明确「**DEFAULT TO pipeline()**」；只有 **stage N 需要 stage N-1 的全体**时才用 barrier（parallel）。
- Barrier 的正当理由：全量去重/合并、总数为零早退、stage N 的 prompt 要引用「其他 finding」。
- Barrier 的**不正当**理由：「我只是想先 flatten/map/filter」「概念上分阶段」「代码更整洁」——这些把 transform 放进 pipeline stage 即可。
- Smell test：`await parallel(...)` → 纯 transform → `await parallel(...)` 中间那个 transform 不需要 barrier。

---

## 3. GA 现状

`skills/workflow-authoring/SKILL.md` §5「Patterns that improve complex-task quality」**已覆盖**：
adversarial verify / perspective-diverse verify / judge panel / loop until dry / multi-modal sweep /
completeness critic / no silent caps / pass-values-don't-share-files。

子代理 role 提示（`workflow_child_agent.ROLE_INSTRUCTIONS`）里：
- `verification`：证据式 + fail-closed（「missing evidence means verificationPassed=false」）。
- `review`：独立按 rubric 报告 finding。

**已基本对齐**——但有几处差距（见 §4）。

---

## 4. 差距与改动（本次）

| # | 差距 | 改哪 | 改成 |
| --- | --- | --- | --- |
| 1 | 缺「DEFAULT TO pipeline()」+ barrier smell test | `SKILL.md` §4 pipeline | 加一段：默认 pipeline；仅当 stage N 需要 stage N-1 全体才用 barrier；附 smell test |
| 2 | loop-until-dry 缺 dedup-vs-`seen` 陷阱 | `SKILL.md` §5 | 在 loop-until-dry 补「dedup vs `seen`，不是 `confirmed`，否则不收敛」 |
| 3 | 缺 adversarial-refuter 的具体配方 | `SKILL.md` §5 | 补上 CC 的 refuter prompt 文本 + 多数阈值 + fail-closed 默认 |
| 4 | `verification` role 是「验证」口吻 | `workflow_child_agent.ROLE_INSTRUCTIONS` | 改为「**对抗式**验证：主动反驳，不只确认」+ 不确定/无证据即 `verificationPassed=false` |
| 5 | `review` role 缺「先反驳每个 finding」 | 同上 | 加「报告前先对抗式反驳每个 finding，站不住的丢掉」 |

> 不改宿主判据：**不做**「真实运行」强制契约（见 `docs/20261013-ga-workflow-受控subprocess-实施记录-阶段4.md` §6）——
> 与 CC 一致，verify 的质量由 prompt 方法论保证。

---

## 5. 实施状态（2026-10-13）

**已实施**：上表 1-5 全部落盘到 `skills/workflow-authoring/SKILL.md` 与 `workflow_child_agent.py`。
测试：新增 `tests/test_workflow_child_agent.py::LiveJobTelemetryTest::test_verification_and_review_roles_are_adversarial`；
role 文本变更不破坏既有 drift guard（它只断言 role 键与部分 role 的子串）。全量 discover **1378 OK (skipped=3)**。

---

## 6. 补充：`review-changes` canonical 示例（2026-10-13）

### 6.1 它是什么 / 用在哪（CC）

`review-changes` **不是一个能跑的内置 workflow**，而是嵌在 **Workflow 工具描述**里的**教学模板**——
模型每次拿到 Workflow 工具都会读到，并照抄这个形状。证据：

- 全仓 grep：`review-changes` **只出现在 `claudecode283/modules/m1502_0xdb4a986.js`**（= Workflow 工具描述）。
- 落点：`claudecode283/extracted/workflow-tool-description.md:16-40`（标题为 "The canonical multi-stage pattern"）、
  `workflow-authoring-reference.md:10`（把它当作 **Review** 类任务的范式）。
- CC 只会从 `.claude/workflows/` 加载**用户保存**的 workflow（`m0868_0xd1234ad.js` `loadWorkflowsDir`），
  `/workflows` 列表里可保存 / 再跑；`review-changes` 本身不是内置命令。

### 6.2 canonical 形态（CC 原文要点）

```js
export const meta = { name: 'review-changes', description: '...', phases: [{ title: 'Review' }, { title: 'Verify' }] }
const DIMENSIONS = [{ key: 'bugs', prompt: '...' }, { key: 'perf', prompt: '...' }]
const results = await pipeline(
  DIMENSIONS,
  d => agent(d.prompt, { label: `review:${d.key}`, phase: 'Review', schema: FINDINGS_SCHEMA }),
  review => parallel(review.findings.map(f => () =>
    agent(`Adversarially verify: ${f.title}`, { label: `verify:${f.file}`, phase: 'Verify', schema: VERDICT_SCHEMA })
      .then(v => ({ ...f, verdict: v }))
  )),
)
const confirmed = results.flat().filter(Boolean).filter(f => f.verdict?.isReal)
return { confirmed }
// Dimension 'bugs' findings verify while dimension 'perf' is still reviewing. No wasted wall-clock.
```

它把三件事焊在一起：**pipeline 默认**（无 barrier）+ **对抗式 verify**（`Adversarially verify`）+ **schema 结构化判定**（`verdict.isReal` 过滤）。

### 6.3 GA 的改动

GA **已有**等价物但偏弱：`skills/workflow-authoring/SKILL.md` §3 的 `meta` 示例就叫 `review-changes`，
§4 的 pipeline 示例是**简化版**（缺 schema / 对抗式 verify / verdict 过滤 / wall-clock 说明）。

**本次**：把 §4 的 pipeline 示例升级为上面 CC 的 canonical 完整形态，并注明
`FINDINGS_SCHEMA` / `VERDICT_SCHEMA` 由**作者定义**（GA 不内置这两个 schema；`VERDICT` 需含 `isReal`）。

> 不改宿主：仍然只是提示词层（§2 的路线）。GA 的 `/workflows` 支持保存/再跑，与 CC 的 `.claude/workflows/` 对应。
