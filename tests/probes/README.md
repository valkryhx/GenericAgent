# 探针脚本（probes）

这里放的是一次性**测量工具**——用来验证 GA 的 TUI 行为（帧高、光标锚点、OSC 输出、
鼠标/滚轮链路、表格提交边界……）的脚本。它们不是产品代码，也不是 CI 测试，
所以不参与 `npm test`，需要时手动用 `npx tsx probes/xxx.ts` 跑。

## 目录

| 路径 | 内容 |
| --- | --- |
| `frontends/ink/ui/probes/` | Ink UI 侧探针（TypeScript，可 import `../src/*`） |
| `tests/probes/python/` | PTY / 抓屏 / pyte 回放类探针（Python） |
| `tests/probes/captures/` | 探针产出的原始抓包（日志、二进制字节流），随时可删 |

## 为什么要收进来

这些脚本原先以 `.tmp_*` 的形式散落在仓库根目录和 `frontends/ink/ui/` 根目录，
共 124 个文件，会盖住真正的入口文件。它们是结论的可复现证据（多份 `docs/`
文档直接引用了文件名），所以**不删除，只归类**。

## 约定

- 新写探针请直接放到对应目录，不要再在仓库根目录建 `.tmp_*`。
- 探针产出的临时文件写进 `tests/probes/captures/`（该目录已 gitignore）。
- 如果某个探针的结论进了文档，请在该文档里写**新路径**，便于回查。
