# 回归：阶段 3 ANSI 与 Ink 抢 stdout 导致花屏

**日期：** 2026-07-14  
**证据：** `截图/新bug.png`  
**状态：** **已回滚 App 侧 ANSI 写入**

## 现象

用户提问后 UI 出现：

- 多段 `GenericAgent running/idle` 标题叠在一起  
- 多组输入框横线堆叠  
- 中间大片空白、布局错乱  

## 根因

阶段 3 在 `App.tsx` 的 `useLayoutEffect` 里对 **Ink 仍占用的 stdout** 写入了 `insertHistorySequence` 生成的 ANSI：

- `CSI … r`（DECSTBM scroll region）  
- `\x1bM`（reverse index）  
- 额外 `\r\n`  

Ink 每帧也会移动光标/重绘 live dock。两套控制流并发 → 终端滚动区与帧缓冲错位 → 花屏叠层。

Codex 能写这些序列，是因为它 **自管整个 terminal backend**，不与 Ink 并存。

## 修复

1. **删除** App 中调用 `stdout.write(insertHistorySequence(...))` 的 effect 与相关 ref。  
2. **保留** `insertHistory.ts` 纯几何 + 序列构建（单测仍绿），文档标明 **禁止在 Ink 路径写序列**。  
3. 阶段 1（P0-A）与阶段 2（stream commit）**保留**，它们不碰 scroll region。

## 验证

```bash
cd frontends/ink-ui
npm test
npx tsx src/grok_selfcheck_full_visibility.ts
```

人工：提问后应只有 **一组** GenericAgent 标题 + **一组** 输入框，不应再出现截图中的叠层。
