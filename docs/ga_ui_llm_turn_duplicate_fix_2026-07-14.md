# GA UI：LLM Running Turn 重复 + 尾部截断 — 根因与修复

**日期：** 2026-07-14  
**复现会话：** `temp/sessions/session_b3aeef24a3a24a1495609ec298cb91b9.jsonl`  
**症状：**

1. 同一轮里 `LLM Running (Turn 1) ...` 及 tool 轨迹出现两次（先完整展开，再折叠版再来一遍）。
2. 最终回答偶发被截成 `:28**` 一类残片。

## 日志核实

Session `assistant_text` **本身只有一份**完整轨迹：

- Turn 1 → tool → Turn 2 → 最终 `现在是 **2026年7月14日 15:28**。`
- 后端没有写两份 Turn 1。

因此重复来自 **Ink UI 状态/提交路径**，不是模型又答了一遍。

## 根因

### A. `App.tsx` 把 pending delta 拼进 `assistant_done`（主因）

流式路径：

1. bridge 增量发 `assistant_delta`（`inc_out=True` 时为真正增量）。
2. App 侧 80ms 节流：delta 先攒在 `pendingDeltas`。
3. stream-commit 把溢出行写入 `a-{taskId}-c*` Static 段，live 只留 tail。
4. bridge 再发 **`assistant_done`，text = 整轮 full_resp**。

旧逻辑：

```ts
dispatch({ type: 'assistant_done', text: cachedDelta + event.text })
```

`event.text` 已是全文，再拼未 flush 的 `cachedDelta` → done 文本变成「尾部 + 全文」或至少与已 commit 前缀对不齐。  
`remainingAssistantTextAfterCommits` 前缀匹配失败 → **整轮全文再次落入 Static**，于是 Turn 1 / tool 再画一遍（第二次常已走 `formatAssistantText` 折叠样式，看起来像「展开版 + 折叠版」）。

### B. done-only 的 `</summary>` 改写（次因 / 放大器）

`agentmain.run` 曾在 **仅 `done` 时** 执行：

```python
full_resp = full_resp.replace('</summary>', '</summary>\n\n')
```

流式 `next` 已发出无双换行的版本；done 与 committed prefix 差空白 → 前缀 strip 更易失败。

### C. 截断 `:28**`

双重提交 + 四连反引号 fence（tool result `````）经 markdown 渲染时，fence/加粗边界错位，尾部只剩 `**` 前的碎片。去重后 + fence 规范化可消掉。

## 修复

| 位置 | 改动 |
|---|---|
| `frontends/ink-ui/src/App.tsx` | `assistant_done` 先 `flushDeltas()`，再用 **纯 `event.text`** 派发；禁止 `cachedDelta + done` |
| `frontends/ink-ui/src/streamCommit.ts` | `remainingAssistantTextAfterCommits` 增加空白/非空行弹性前缀匹配，兼容历史 done 改写 |
| `agentmain.py` | 流式路径 **不再** 在 done 上单独改写 markup；`normalize_display_assistant_text` 留给非流式/测试 |
| `messageFormat.ts` | 清理空 fence，降低 markdown 截断风险 |

## 测试

- `streamCommit.test.ts`：summary 换行注入 / 弹性 strip
- `state.test.ts`：长 turn + done 改写 → Turn 1/2 各仅一次
- `messageFormat.test.ts`：完整 tool 轨迹后最终加粗句保留
- `tests/test_agentmain_streaming.py`：normalize 幂等
- `npm test`：全量 pass

## 验证命令

```bash
cd frontends/ink-ui && npm test
cd ../.. && python -m unittest tests.test_agentmain_streaming -v
```
