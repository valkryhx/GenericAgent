/**
 * 决定性测量：流式文本在固定列宽下 wrap，渲染行数是否非线性。
 *
 * 现象线索：真机上一轮里输入框有一次 15 -> 14（-1），内容变多反而变矮。
 * 若成立，则「轻微上下位移」= 中文文本逼近列宽边界时触发的重排，
 * 与 delta 粒度无关，也与 Static 提交无关。
 *
 * 做法：模拟 120 列下，一段纯文本每来 16 字符，计算总行数（含 wrap）。
 * 中文按 2 列宽计算（wcwidth 近似）。
 */
const WIDTH = 120
const NL = '\n'

/** 粗粒度中文宽度：CJK / 全角标点按 2 列，其余按 1 列。 */
function charWidth(ch: string): number {
  const code = ch.codePointAt(0)!
  if (
    (code >= 0x1100 && code <= 0x115f)
    || (code >= 0x2e80 && code <= 0xa4cf)
    || (code >= 0xac00 && code <= 0xd7a3)
    || (code >= 0xf900 && code <= 0xfaff)
    || (code >= 0xfe30 && code <= 0xfe6f)
    || (code >= 0xff00 && code <= 0xff60)
    || (code >= 0xffe0 && code <= 0xffe6)
  ) return 2
  return 1
}

/** 计算一段文本在 width 列下占多少显示行（逐字软换行，同终端行为）。 */
function displayRows(text: string, width: number): number {
  let rows = 1
  let col = 0
  for (const ch of text) {
    if (ch === NL) {
      rows += 1
      col = 0
      continue
    }
    const w = charWidth(ch)
    if (col + w > width) {
      rows += 1
      col = 0
    }
    col += w
  }
  return rows
}

// 逐 16 字符喂入（= bridge 的 STREAM_FLUSH_CHARS），记录总显示行数
const body = (
  '终端渲染的核心是字符单元格：终端把屏幕抽象成一张由行列组成的网格，'
  + '每个格子存放一个字符以及它的前景色背景色和字符属性。'
  + '渲染器的工作就是把一个逻辑界面转换成这张网格上的字符与颜色的差异，'
  + '并尽可能少地写出转义序列。'
  + '因为终端是慢速设备，早期通过串口连接时带宽极其有限，'
  + '所以从那个时代留下来的设计哲学一直延续到今天。'
  + '现代的终端模拟器虽然早已不受带宽限制，但差量渲染依然是性能优化的核心手段。'
  + '一个流畅的界面，关键在于让每一帧的写入量最小，同时保证帧的原子可见性。'
).repeat(3)

console.log(`正文 ${body.length} 字符，按 16 字符/次 喂入，终端 ${WIDTH} 列\n`)
console.log('步长  累计字符  显示行数  变化')
console.log('-'.repeat(44))

let prev = displayRows('', WIDTH)
const heights: number[] = []
const jumps: number[] = []
let step = 0
for (let i = 16; i <= body.length; i += 16) {
  step += 1
  const prefix = body.slice(0, i)
  const h = displayRows(prefix, WIDTH)
  heights.push(h)
  const d = h - prev
  if (d !== 0) jumps.push(d)
  if (d !== 0 || step % 10 === 0) {
    console.log(
      `${String(step).padStart(4)} ${String(i).padStart(9)} ${String(h).padStart(9)} ${(d > 0 ? '+' : '') + (d === 0 ? '0' : d)}`,
    )
  }
  prev = h
}

console.log('')
console.log(`最终显示行数 ${heights[heights.length - 1]}`)
const downs = jumps.filter(j => j < 0)
console.log(`行数变化事件 ${jumps.length} 次，其中【变矮】 ${downs.length} 次`)
if (downs.length > 0) {
  console.log(`  => wrap reflow 会吐回行数（出现 ${downs.join(',')}），这就是「上下位移」的直接证据`)
} else {
  console.log('  => 本样本单调增长，未复现变矮')
}

// 对照：纯 ASCII 文本
const ascii = 'The terminal renders cells. '.repeat(60)
let p2 = 1
let asciiDown = 0
for (let i = 16; i <= ascii.length; i += 16) {
  const h = displayRows(ascii.slice(0, i), WIDTH)
  if (h < p2) asciiDown += 1
  p2 = h
}
console.log('')
console.log(`对照：纯 ASCII 同样长度，变矮 ${asciiDown} 次`)
