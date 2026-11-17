/**
 * Named glyph set, aligned with Claude Code v2.1.283's UI constants.
 *
 * CC keeps every icon in one named table (source: `m0052_0xbfaae5d.js`, exe
 * @201043800) and references them by semantic name in the render layer. We do
 * the same here so the transcript stops scattering emoji and ASCII markers
 * across modules.
 *
 * Evidence + offsets: docs/20261016-CC-UI-对标调研.md (GA) and
 * claudecode283_hack/docs/20261016-CC-UI渲染对标调研.md (CC).
 */
export const GLYPHS = {
  /** Tool-use bullet. CC: macOS `⏺`, other platforms `●` (source `Fr`). */
  bullet: process.platform === 'darwin' ? '\u23FA' : '\u25CF',
  /** Tool-result line prefix. CC constant `ZHo` = `\u23BF`. */
  resultPrefix: '\u23BF',
  /** Spinner / thinking star. CC constant `yC` = `\u273B`. */
  star: '\u273B',
  /** Thinking-animation frames. CC `Dmn = [∴, ∷, ∵, ∷]`. */
  thinkingFrames: ['\u2234', '\u2237', '\u2235', '\u2237'] as const,
  /** Spinner frames. CC `lt` = `· ✢ * ✶ ✻ ✽` (see CC doc §5.1). */
  spinnerFrames: ['\u00B7', '\u2722', '*', '\u2736', '\u273B', '\u273D'] as const,
  /** Collapsed / expanded disclosure triangles. CC `v$` = `\u25B8`. */
  triangleRight: '\u25B8',
  triangleDown: '\u25BE',
  /** Status marks. */
  check: '\u2713',
  cross: '\u2717',
  pending: '\u25CC',
  warn: '\u26A0',
  /** Selection / breadcrumb pointer. CC `pointerSmall` = `\u203A`. */
  pointer: '\u203A',
  /** Arrows (CC `arrowUp` / `arrowDown` / `arrowRight`). */
  arrowUp: '\u2191',
  arrowDown: '\u2193',
  arrowRight: '\u2192',
  /** Tree drawing (CC `o_`: branch / last / pipe). */
  treeBranch: '\u251C',
  treeLast: '\u2514',
  treePipe: '\u2502',
  /** Rounded box (CC `wb`). */
  box: { topLeft: '\u256D', topRight: '\u256E', bottomLeft: '\u2570', bottomRight: '\u256F' },
  /** Rule / divider. CC `Bx` = `\u2500`. */
  rule: '\u2500',
  /** Markdown table frame (light box drawing). */
  table: {
    topLeft: '\u250C',
    topRight: '\u2510',
    bottomLeft: '\u2514',
    bottomRight: '\u2518',
    horizontal: '\u2500',
    vertical: '\u2502',
    cross: '\u253C',
    teeDown: '\u252C',
    teeUp: '\u2534',
    teeRight: '\u251C',
    teeLeft: '\u2524',
  },
} as const

export type Glyphs = typeof GLYPHS

/**
 * Tool-result prefix, matching CC's `"  ⎿  "` (2 spaces + `⎿` + space + nbsp).
 * `dim` should be applied by the caller; CC renders the whole prefix dimmed.
 */
export const RESULT_PREFIX = `  ${GLYPHS.resultPrefix}  `

/** The `(ctrl+o to expand)` hint CC renders dimmed next to folded blocks. */
export const EXPAND_HINT = '(ctrl+o to expand)'
