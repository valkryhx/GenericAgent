# GA UI：中间 LLM Turn 显示为 `**LLM Running ...**` 字面量

**日期：** 2026-07-14
**截图：** `截图/llm_turn_显示问题.png`
**会话：** `temp/sessions/session_8cc72593a75640fda4d4aab73217594c.jsonl`

## 现象

- Turn 1 正常加粗
- **Turn 2 原样显示** `**LLM Running (Turn 2) ...**`（markdown 未解析）
- Turn 3 又正常

不是「中间 turn 输入格式不同」——session 里三处都是同一写法：`**LLM Running (Turn N) ...**`。

## 根因

1. 流式路径用 `streamCommit` 把超长 live 助手文本切成 `a-{taskId}-c*` Static 段 + 短 live tail。
2. 旧切点是纯行数：`overflow = lines.length - tailLines`。
3. GA tool 状态被包在 **`````** fence 里。某次切点落在：
   - commit 段以未闭合的 ````` 结束
   - live/下一 commit 段以 Turn 2 开头，但前面还带着未配对 fence
4. 该段经 `formatAssistantText` 把 ````` → ``` 后交给 `marked`，**Turn 2 落在 code fence 内**，`**` 变成字面量。
5. Turn 1/3 恰好落在 fence 闭合的段里，所以正常。

## 修复

| 层 | 改动 |
|---|---|
| `streamCommit.ts` | `findFenceSafeOverflowCount`：禁止在未配对 backtick fence 中间切开；切不稳则本 tick 不 commit |
| `messageFormat.ts` | 解开 GA tool status 的 4–5 反引号 fence（保留 Action/Status 正文）；规范化 `\r\n` |

## 测试

- `streamCommit.test.ts`：open-fence 切点回归
- `messageFormat.test.ts`：status fence 剥离后 Turn 2 仍可 bold
- `markdownRender.test.ts`：Turn 标记 bold
- `npm test` 全绿

## 验证

```bash
cd frontends/ink-ui && npm test
```
