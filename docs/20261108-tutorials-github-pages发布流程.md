# 用 GitHub Actions 发布 tutorials 资料的流程

> 状态：已上线并验证（2026-11-08）
> 适用：往 `tutorials/` 加教程、图解、说明文档，自动发布成网页

---

## 0. 一句话流程

把文件放进 `tutorials/`，`git push` 到 `main`，等 Actions 跑完，网页就有了。**不需要任何手动部署动作。**

```
写 tutorials/xxx.md  →  git push  →  Actions 自动构建  →  网页更新
```

线上地址：**https://valkryhx.github.io/GenericAgent/**

---

## 1. 三个组成部分

| 文件 | 作用 | 什么时候需要改 |
| --- | --- | --- |
| `tutorials/` | 内容源文件（md / 自包含 html） | 每次加新内容 |
| `tutorials/index.html` | 入口页，手动维护的卡片列表 | 加新页面时**必须**同步更新 |
| `.github/scripts/build-tutorials.py` | 构建脚本，md → 带样式的 html | 一般不用动 |
| `.github/workflows/pages-tutorials.yml` | 触发与部署 | 一般不用动 |

**注意**：`tutorials/` 下所有文件会被发布成站点的**根目录**。所以 URL 是
`https://valkryhx.github.io/GenericAgent/`，**没有** `/tutorials/` 这一段。

---

## 2. 加一篇 Markdown 文档

### 步骤

1. 在 `tutorials/` 下新建 `我的主题.md`（**必须是平铺在 tutorials/ 根目录，不能放子目录**）
2. 第一行写一级标题 `# 标题`——它会成为网页的 `<title>`
3. 编辑 `tutorials/index.html`，在 `</div>` 前加一张卡片：

```html
<a class="card" href="./我的主题.html">
  <h2>卡片上显示的标题</h2>
  <p>一两句话说明这篇讲什么。</p>
  <span class="tag">文档</span>
</a>
```

4. 提交推送：

```bash
git add tutorials/
git commit -m "docs(tutorials): 新增 XXX"
git push origin main
```

5. 等 1–2 分钟，访问 https://valkryhx.github.io/GenericAgent/ 确认

### 支持的 Markdown 语法

构建脚本用的是 Python `markdown` 库，只开了 `tables` 和 `fenced_code` 两个扩展。实测可用：

- 标题、列表、表格、引用块
- 围栏代码块 ` ``` `
- 行内代码、链接、加粗、斜体
- 根目录图片（见 §5）

**不支持**：脚注、目录自动生成、任务列表勾选框、数学公式。

---

## 3. 加一张交互式 HTML 图

直接把**自包含**的 `.html` 放进 `tutorials/` 即可，脚本会原样复制，不做任何处理。

「自包含」指：CSS 和 JS 都内联，没有 `<script src="...">` 指向本地文件、没有外部 CDN 依赖。这样离线双击也能打开。

然后同样在 `index.html` 加一张卡片指向 `./你的图.html`。

> 为什么不做处理和 Jekyll：自包含 HTML 里若含 `{{ }}`，Jekyll 会当模板解析并改写。当前方案完全绕开这个问题。

---

## 4. ⚠️ 五个实测过的坑

这些是我逐条跑构建脚本验证出来的真实行为，不是猜测。

### 4.1 子目录会被完全忽略

```
tutorials/sub/nested.md   → 不会发布
tutorials/imgs/b.png      → 不会发布
```

脚本只处理 `tutorials/` **根目录下**的文件（`glob("*.md")` + `iterdir()` 且 `is_file()`）。

**后果**：图片请放 `tutorials/` 根目录，不要建 `imgs/` 子目录。

**更隐蔽的问题**：workflow 的触发条件是 `tutorials/**`（含子目录）。所以你在 `tutorials/sub/x.md` 上提交，**workflow 会跑，但内容不会出现在网站里**。看起来成功了，实际没发布。

### 4.2 `_` 开头**不能**用来隐藏草稿

```
tutorials/_draft.md  → 会渲染成 _draft.html 并发布
```

复制环节会跳过 `_` / `.` 开头的文件，但**渲染环节没有这个过滤**。所以 `_draft.md` 依然会变成网页被别人看到。

想藏草稿，就别放进 `tutorials/`。

### 4.3 同名 `.md` 和 `.html` 会互相覆盖

```
tutorials/c.md   → 渲染成 c.html
tutorials/c.html → 复制时覆盖上去
```

实际结果是**手写的 HTML 赢**（渲染先跑，复制后跑）。如果两个都要，请改名字区分。

### 4.4 `index.html` 不会被自动生成

新增页面后不更新 `index.html`，网页上的入口页不会出现新卡片，但**直接访问 URL 仍然能打开**（比如 `/我的主题.html`）。

### 4.5 没有一级标题时，标题会变成文件名

若 md 里没有 `# 标题`，`<title>` 会退化成文件名（如 `d.html` 的标题就是 `d`）。建议每篇都写一级标题。

---

## 5. 图片怎么放

**可以**（根目录，脚本会复制）：

```markdown
![示意图](./pic.png)
```

**不行**（子目录不会被复制，会 404）：

```markdown
![图](./imgs/pic.png)
```

用相对路径 `./`，不要用绝对路径或 `../`。

---

## 6. 本地预览（推送前先看效果）

### 只跑构建，产物在本地

```bash
python -m pip install markdown
python .github/scripts/build-tutorials.py tutorials _site
```

然后直接双击 `_site/*.html` 打开。这是**最接近线上**的预览方式。

### 快速语法检查

```bash
# 确认仓库要求的 LF 行尾（期望输出 0）
grep -c $'\r' tutorials/我的主题.md
```

---

## 7. 发布失败怎么排查

去 **Actions** 标签页 → 点最近的 `Deploy tutorials to GitHub Pages` 运行记录。

| 失败的 job | 常见原因 | 处理 |
| --- | --- | --- |
| `build` | `markdown` 装不上 / 脚本报错 | 本地跑一遍 §6 的构建命令复现 |
| `deploy` | Pages 未启用或源不是 Actions | Settings → Pages → Source 选 **GitHub Actions** |

`deploy` 失败但 `build` 成功，通常是 Pages 配置问题，不是内容问题。

### 手动触发一次

不想改内容也能重跑：Actions → 左侧选 `Deploy tutorials to GitHub Pages` → 右上 **Run workflow** → 选 `main` → Run。

---

## 8. 当前部署配置（供排查参考）

- **Pages 源**：GitHub Actions（不是 Branch）
- **触发条件**：push 到 `main` 且改动命中
  - `tutorials/**`
  - `.github/scripts/build-tutorials.py`
  - `.github/workflows/pages-tutorials.yml`
- **不在上述路径的改动不会触发**（比如只改 `genericagent/` 不会重新发布）
- **并发**：同一时间只跑一个，新的排队不打断
- **构建环境**：ubuntu-latest + Python 3.12 + `markdown`

---

## 9. 想扩展构建能力

改 `.github/scripts/build-tutorials.py`：

- 加 Markdown 扩展 → 改 `extensions=[...]` 那一行
- 支持子目录 → 把 `glob("*.md")` 换成 `rglob("*.md")`，并在复制环节递归。
  注意：`rglob` 会让 `sub/b.md` 仍输出成根目录的 `b.html`（**不保留子目录层级**），
  不同子目录下的同名文件会互相覆盖。真要支持子目录，得同时改用相对路径输出。
- 换样式 → 改脚本顶部的 `CSS` 常量

改完推送到 `main` 会自动触发（该文件在触发路径里），不需要额外操作。

**改完务必本地先跑一遍**（§6），确认没把已有的页面弄坏。
