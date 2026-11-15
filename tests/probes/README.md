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

## 诊断脚本不是一次性用品

调试时写在 `src/*.test.ts` 里的诊断文件，**结论确认后不是删掉，而是搬进这里**：
去掉 `node:test` 包装，改成一个可直接 `npx tsx probes/xxx.ts` 跑的脚本。

理由是诊断文件承载了**"怎么想到的"**：断言失败信息往往离根因很远，而诊断脚本
（比如"把捕获流里真实到达的每一帧打出来"）才是跨过那段距离的方法。删掉它，
同一个 bug 回来时只能从头再猜。

两个硬要求：

1. **能确定性复现**。按真实时序跑、偶尔才红的探针不能证明任何事 —— 绿的时候你
   不知道是修好了还是没触发。要显式构造能触发的条件，并给出对照组。
2. **脚本自己判定**并打印结论（`✔` / `✖`），不要求人肉解读输出。

详细说明与范例见 `docs/20261115-诊断文件与诊断经验留存.md`。

## 已收录的探针

| 探针 | 回答什么问题 |
| --- | --- |
| `tests/probes/python/_probe_spawn_arg_loss.py` | `spawn_agent` 的实参为什么只剩一个字段？回放 session JSONL + `model_responses` 日志，用三个判据（`_raw` 入参 / 拆分 id / 截断哨兵）区分「模型少发」与「传输丢失」，并列出每次调用的实参键集。 |
| `tests/probes/python/_probe_workflow_deadline_budget.py` | workflow 超时测试为什么时好时坏？分别测量「node worker 启动开销」与「测试声明的预算」，判断预算是否小于固定启动成本——预算不够时 deadline 会在脚本第一行运行之前触发，job 根本不存在，也就无从取消。 |
| `tests/probes/python/_probe_test_flakiness_under_load.py` | 并行 / 满负载跑 pytest 时哪些用例会偶发失败？空闲与 6 个 CPU hog 两种条件下各测 N 轮通过率，并打印最常出现的失败断言。判定表见 `docs/20261116-并行pytest下偶发失败的用例清单.md`。 |
| `tests/probes/python/_probe_ink_bridge_stage_budget.py` | ink bridge 的 workflow 阶段预算够不够？测量 approve → 第二个 `agent()` 启动的真实耗时（自然条件最坏 2.38 s，旧上限 3.0 s）。**同时记录了 `Event.wait(timeout=N)` 不能当秒表用**：CPU 密集的邻线程会让它过冲后返回 True，从而给出假绿结论。 |

