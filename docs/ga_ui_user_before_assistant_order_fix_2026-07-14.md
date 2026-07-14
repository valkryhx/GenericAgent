# 回归：助手输出跑到用户问题前面 / 用户输入不可见

**日期：** 2026-07-14  
**证据：** 用户粘贴的修复后终端日志（时间查询 + 梅西搜索）  
**状态：** **已修复**

## 现象

1. 界面几乎不显示本轮用户输入（`> 现在几点了` 等）  
2. 助手工具块 / 最终回答出现在 scrollback 里，**顺序像「先答后问」**  
3. 多段 `LLM Running (Turn x)` 与工具细节交错，观感混乱  

## 根因（阶段 2 commit + P0-A 叠加）

| 步骤 | 错误行为 |
|---|---|
| 1 | P0-A：user 先 `done:false` 只在 live（正确防盖写） |
| 2 | 阶段 2：`assistant_delta` 过长时 **立刻** 把 assistant 前缀 `a-*-c*` **done→Static** |
| 3 | 此时 user 仍 `!done` → Static 里 **只有助手、没有用户** |
| 4 | live 窗口 `liveTranscriptViewportLines` 只截 **末尾 N 行** → 长助手尾把 **user 行裁掉** |
| 5 | 肉眼：看不到问题；Static 里助手在前 → **答在问前** |

## 修复

1. **`state.ts`**：在 `assistant_delta` 路径上，**先** `finalizeOpenUserMessages(taskId)`，**再** `commitStreamingAssistantMessages`。  
   → Static 顺序恒为：`user → a-*-c0 → a-*-c1 → …`  
2. **`messageWindow.ts` `liveTranscriptViewportLines`**：截尾时 **固定保留最后一个 `u-*` user 块**，再拼 assistant 尾。  
   → 即便 user 仍短暂在 live，也不会被长流挤没。  

## 验证

```text
npm test → 277/277
grok_selfcheck_user_duplicate → PASS（pre-delta live-only；post-delta Static 有 user）
grok_selfcheck_stream_commit → PASS（user 在 commits 之前）
grok_selfcheck_full_visibility → PASS
```

## 人工期望

- 提问后立刻（running、尚无 delta）live 可见 `> 问题`  
- 开始流式后 Static 出现问题，再出现工具/回答  
- 最终 scrollback：**问题在上，回答在下**  
