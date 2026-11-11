#!/usr/bin/env node
/**
 * GA Ink UI 帧追踪预加载模块 —— 零侵入探针。
 *
 * 目的
 * ----
 * 有些 Ink UI 缺陷只以「瞬时帧」形式存在：面板画一帧就消失，人眼读不完、截图来不及。
 * 本模块挂在 Node 的 `--import` 上，在 UI 入口（main.tsx）执行之前接管 `process.stdout`，
 * 把每一次 write 的原始字节同步追加到文件。于是「一闪而过」变成磁盘上可反复 grep 的记录。
 *
 * 为什么是预加载，而不是改 UI 源码
 * ------------------------------
 * `ga` 命令跑的是 **安装版**（site-packages 里的 frontends/ink/ui），不是当前源码目录。
 * 改源码对用户实际看到的界面无效，而重装 wheel 代价高、还会打断正在进行的工作。
 * `--import` 预加载与 UI 版本完全解耦：任意版本、任意目录都能用，装一次文件都不用动。
 *
 * 用法
 * ----
 *     GA_UI_TRACE=<输出目录> NODE_OPTIONS='--import "<repo>/scripts/ga_frame_trace.mjs"' ga
 *
 * 通常不必手写这些，直接跑：
 *
 *     python scripts/probe_ink_frames.py --attach      # 打印可直接粘贴的命令
 *
 * 环境变量
 * --------
 *   GA_UI_TRACE=<dir>   必填。输出目录；设为 1 时落到 <cwd>/temp/ink_trace/<stamp>。
 *   GA_UI_TRACE_MAX     可选。写入上限字节数，默认 64 MiB，超出后停止记录（UI 不受影响）。
 *
 * 产物
 * ----
 *   <dir>/frames-<pid>.log
 *
 *   带 pid 是因为 NODE_OPTIONS 会传给 ga 派生的**所有** node 进程（例如 workflow
 *   脚本校验子进程）。各自写各自的文件，互不覆盖。
 *
 *   记录格式是**字节长度定帧**，而不是靠空行/标记切分：
 *
 *       \n=== GA_FRAME <iso> #<n> cols=<w> bytes=<b> ===\n
 *       <恰好 b 个字节的原始 chunk>
 *
 *   因为 chunk 里本来就含大量换行与 ANSI 序列，"按空行切"会把一帧切碎；标记切分则会
 *   被 chunk 里偶然出现的同样文字破坏。长度前缀是唯一无条件精确的切分方式，解析方
 *   （scripts/probe_ink_frames.py 的 read_trace_file）依赖这一点。
 *
 * 不变量
 * ------
 * 记录器对 UI **完全透明**：仍是同一个 Proxy 语义，只多一次 appendFileSync；写失败被吞掉，
 * 绝不让调试手段改变被调试系统的行为。未设置 GA_UI_TRACE 时本模块不做任何事。
 */
import fs from 'node:fs'
import path from 'node:path'

const MAX_BYTES = Number(process.env.GA_UI_TRACE_MAX || 64 * 1024 * 1024)

/** The UI process is the one whose argv contains the Ink entry point. */
function looksLikeInkUi() {
  // tsx/dist/cli.mjs re-executes node against main.tsx, so BOTH processes see
  // --import. The wrapper only emits its own banner; skip it so one run yields one file.
  const script = process.argv[1] ?? ''
  if (/tsx[\\/]dist[\\/]/.test(script)) return false
  return process.argv.slice(1).some((arg) => /main\.tsx$/.test(arg))
}

function resolveOutDir(raw) {
  if (raw === '1') {
    const stamp = new Date().toISOString().replace(/[:.]/g, '-')
    return path.join(process.cwd(), 'temp', 'ink_trace', stamp)
  }
  return path.resolve(raw)
}

function install() {
  const raw = process.env.GA_UI_TRACE
  if (!raw || raw === '0') return

  // NODE_OPTIONS is inherited by EVERY node process the UI spawns -- MCP servers
  // (`npx .../server-memory`), workflow-script validators, npm helpers. Without this
  // filter each of them writes its own trace file and the real UI stream is buried in
  // noise. Default: only record the process whose argv names the Ink entry point.
  if (process.env.GA_UI_TRACE_ALL !== '1' && !looksLikeInkUi()) return

  let outDir
  try {
    outDir = resolveOutDir(raw)
    fs.mkdirSync(outDir, { recursive: true })
  } catch (err) {
    process.stderr.write(`[ga-trace] 无法创建追踪目录: ${err && err.message}\n`)
    return
  }

  const framesPath = path.join(outDir, `frames-${process.pid}.log`)
  const real = process.stdout
  const realWrite = real.write.bind(real)
  let seq = 0
  let written = 0

  const banner =
    `=== GA_TRACE ${new Date().toISOString()} pid=${process.pid} ` +
    `cwd=${process.cwd()} out=${outDir} ===\n` +
    `argv=${JSON.stringify(process.argv.slice(1))}\n`
  try {
    fs.writeFileSync(framesPath, banner)
  } catch (err) {
    process.stderr.write(`[ga-trace] 无法写入 ${framesPath}: ${err && err.message}\n`)
    return
  }

  function record(chunk) {
    if (written >= MAX_BYTES) return
    if (chunk == null) return
    if (typeof chunk !== 'string' && typeof chunk.length !== 'number') return
    try {
      const buf = typeof chunk === 'string' ? Buffer.from(chunk, 'utf8') : Buffer.from(chunk)
      seq += 1
      const head = Buffer.from(
        `\n=== GA_FRAME ${new Date().toISOString()} #${seq} ` +
          `cols=${real.columns ?? 0} rows=${real.rows ?? 0} bytes=${buf.length} ===\n`,
        'utf8',
      )
      // 单次 append：头 + 原始字节，避免两次写之间被其它进程/信号打断造成错位。
      fs.appendFileSync(framesPath, Buffer.concat([head, buf]))
      written += head.length + buf.length
    } catch {
      /* 记录失败绝不能影响 UI */
    }
  }

  // 用 Proxy 而不是替换成普通对象：ink 还会读 isTTY / columns / on('resize') / getColorDepth，
  // 这些必须原样透传，否则「调试手段改变了被调试系统」—— 非 TTY 渲染路径与 TTY 完全不同。
  const tee = new Proxy(real, {
    get(target, prop) {
      if (prop === 'write') {
        return (chunk, ...rest) => {
          record(chunk)
          return realWrite(chunk, ...rest)
        }
      }
      const value = Reflect.get(target, prop, target)
      return typeof value === 'function' ? value.bind(target) : value
    },
  })

  Object.defineProperty(process, 'stdout', { configurable: true, enumerable: true, get: () => tee })

  try {
    process.stderr.write(`[ga-trace] 记录 stdout -> ${framesPath}\n`)
  } catch {
    /* ignore */
  }
}

install()