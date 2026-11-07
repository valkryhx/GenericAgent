"""重写文档尾部：保留前 120 行，接上新的结论段。"""
path = r"D:\git_repos\GenericAgent\docs\20261108-GA-Ink抖动与滚动优化设计-对标CC319.md"
with open(path, encoding="utf-8", newline="") as f:
    lines = f.readlines()

keep = lines[:120]
while keep and keep[-1].strip() == "":
    keep.pop()

tail = """
---

## 3. 下一次动手前必须做的三件事

### 3.1 先确认 delta 粒度（最重要）

根因是「帧高跳变 ≈ delta 行数」。所以第一条要确认的是本机 relay 的 delta 到底多大。

```bash
# 在 bridge 侧或 reducer 里打点：每次 assistant_delta 的 text 含几个换行
# 如果多数 delta 只有 0-1 行（无换行），帧高就不会跳，问题在别处
# 如果多数 delta 有 2-3 行，就确认了本文的根因
```

### 3.2 验证方案 C（固定槽位 + tail 对齐）

模拟已经显示它能让帧高恒为 13（`.tmp_strategy_compare.ts`），但**还没上真机**。

关键点：**槽位高度必须等于 `DEFAULT_STREAM_LIVE_TAIL_LINES`**。
方案 A 失败是因为只钉了槽位而没对齐 tail，内容在 tail 内仍会把帧高从 7 顶到 13。

改动点：
- `src/messageViewportPlan.ts`：live 高度返回常量（= tail）
- `src/streamCommit.ts`：`DEFAULT_STREAM_LIVE_TAIL_LINES` 与上者取同一常量

**验收必须用 `.tmp_ga_ab_moves.py`**（真机每轮移动次数），不能只用单测。
方案 A 的教训就是单测全绿但真机零改善。

### 3.3 如果方案 C 也不行

那就承认：**inline 模式下「内容变多 → 画面要长高」是物理必然**，
除非把溢出行提交给终端 scrollback（CC 的做法，需要重写渲染层）。

务实的选择是接受它，或者改用 `GA_INK_MOUSE=full`（备用屏，实测帧高恒为 39）。

---

## 4. 不建议再试的方向

| 方向 | 为什么不行 |
| --- | --- |
| 批量提交 Static（攒 8 行再提交） | 提交次数 53→5，但**单次跳变从 1 行放大到 8 行**，更糟 |
| 只钉 live 槽位不动 tail | 实测零改善（方案 A） |
| 同步输出 `CSI ?2026h` | 本机 ConPTY **丢弃并重排**该序列（3 对全为空对），改完无法验证 |
| 合并多次 write | 实测空窗 5.13%→4.70%，ConPTY 已缓冲，感知不到 |
| 改 MCP 加载 | 首轮等待上限 2s 且后台刷新，不是抖动源 |

---

## 5. 方法学教训

1. **优化「频率」还是「幅度」，先想清楚。** 方案 B 把提交次数降了 90%，
   却让单次跳变放大 8 倍。用户感知的是幅度，不是频率。

2. **端到端指标优先。** 方案 A 单测断言「槽位高度恒定」全绿，
   但用户指标「输入框移动次数」纹丝不动。**测试断言了错误的量。**

3. **先做 A/B 基线，再写代码。** 方案 A 写完才发现无效；
   如果先花 10 分钟测基线，本可以在动手前否掉。

4. **区分「哪一层在变」。** 总帧高 = 底栏 + 槽位 + 内容。三者必须分别测量。

5. **测量工具的 debug 模式会污染结果。** Ink `debug: true` 时每帧写全量 Static
   （`ink.js:108`），行数测出来是 6→53（9 种）；`debug: false` 才是真实的 6→14。

6. **不要用「看起来合理」代替「测过」。** 本轮先后判断错三次
   （live 区 / 提交频率 / 屏幕刷新率），每次都靠实测纠正。
"""

keep.extend(tail.splitlines(keepends=True))
with open(path, "w", encoding="utf-8", newline="\n") as f:
    f.writelines(keep)

print("wrote", len(keep), "lines")
