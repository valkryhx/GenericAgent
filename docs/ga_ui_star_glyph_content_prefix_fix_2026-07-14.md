# 回归：✻ 错误出现在助手正文首字

**日期：** 2026-07-14  
**状态：** **已修复**

## 现象

终端输出里多次出现 `✻` 打头的正文/工具块，例如：

```text
✻ 现在是 2026-07-14 ...
✻ [Action] Calling MCP tool...
✻ LLM Running (Turn 2) ...
```

用户预期：`✻` **只**用于底部活动条，如  
`✻ Tinkering (19s · ↑42.2k ↓329 Σ42.6k)`。

## 根因

`messageWindow.ts` `appendMessageLines` 对 **每条** assistant 消息的首行写死：

```ts
const prefix = index === 0 ? '✻ ' : '  '
```

阶段 2 stream commit 后，一轮回答会拆成多个 `a-{task}-c*` + live tail，**每个分段都再盖一个 ✻**，所以正文里 ✻ 会刷很多次。

活动条本身的 `formatRunningStatus` 也会用 `✻`，语义正确；问题是正文误复用了同一符号。

## 修复

- 助手正文 **不再** 加 `✻` 前缀（首行直接正文，续行仍缩进两空格）。  
- `formatRunningStatus` **保留** `✻`（活动条专用）。  
- 单测：`transcriptLines does not prefix assistant body with activity glyph ✻`；更新旧断言。

## 验证

```bash
cd frontends/ink-ui
npx tsx --test src/messageWindow.test.ts src/activityStatus.test.ts
npm test
```

人工：提问后正文/工具块不以 `✻` 开头；底部 Running 行仍可出现 `✻ Thinking…`。
