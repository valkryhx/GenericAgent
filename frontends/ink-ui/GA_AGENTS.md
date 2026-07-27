# GA Ink UI 层项目说明

本文件适用于 `frontends/ink-ui/` 下的 React + Ink 终端 UI。它比 `frontends/GA_AGENTS.md` 更具体，因此与上层说明冲突时优先遵守本文件。

## Ink UI 专属约束

- npm 命令必须在 `frontends/ink-ui` 目录执行。
- 终端 UI bug 优先写程序化回归测试，不优先依赖截图或肉眼观察。
- CJK、emoji、全角字符宽度必须用 `string-width` 语义处理，不能用 JavaScript `.length` 推断列宽。
- 涉及光标、IME、布局、重复渲染、流式输出时，应优先把问题降维成 stdout 字节序列、虚拟终端状态、帧几何或纯函数断言。

## 光标与 IME

- Windows Terminal 的 IME 候选框锚定在可见原生光标上。
- cursor park writer 必须在光标停到输入 caret 后 SHOW 原生光标，并在下一帧写入前 HIDE。
- 不要回退到“始终隐藏原生光标，只显示反显块”的旧方案。

## 推荐验证入口

- `npm run test`
- `npm run typecheck`
- 针对单个测试文件可用 `npx tsx --test src/<name>.test.ts`
