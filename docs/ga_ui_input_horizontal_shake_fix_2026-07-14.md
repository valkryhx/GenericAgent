# GA UI：输入时左右晃动 — 根因与修复

**日期：** 2026-07-14
**症状：** 在 terminal 中打开 ink-ui，打字时输入框/光标区域左右轻微抖动。

## 根因

`InputView` 旧实现：

1. 外层 / 行容器使用 `width="100%"`（相对父级百分比）。
2. 每行拆成 **两个** `<Text>`：`gutter` + `content`（含 inverse 光标）。
3. 内容长度与 inverse caret 位置每键变化时，Yoga 对百分比子树重新测量，边框/行盒宽度在帧间抖动。
4. Windows Terminal 上表现为 composer **左右晃动**（不是纵向跳动）。

内容本身已有 `fixedInputLine` 右补空格，但 **布局宽度** 仍是弹性百分比，所以视觉仍会抖。

## 修复

| 位置 | 改动 |
|---|---|
| `promptChrome.ts` | 新增 `inputLineBoxWidth`、`fixedInputRow`：gutter+content+rightReserve 合成固定显示宽字符串 |
| `App.tsx` `InputView` | 外层 `width={columns}`；行 `width={lineWidth}`；单行单 `Text` 渲染 `fixedInputRow`，光标列算在 gutter 之后 |
| 测试 | `fixedInputRow` 定宽；App 边框宽跨键不变、内容宽不回缩 |

## 验证

```bash
cd frontends/ink-ui
npm test   # 291/291
```

重启 `ga ink` 后连续输入中英文，composer 边框与输入行不应再左右跳。
