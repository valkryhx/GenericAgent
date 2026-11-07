import { computeLayoutMetrics } from '../src/layoutMetrics.js'
import {
  INLINE_FIXED_SLOT_MIN_MESSAGE_ROWS,
  inlineLiveViewportRows,
  planFixedMessageViewport,
} from '../src/messageViewportPlan.js'

for (const rows of [24, 30, 40]) {
  const m = computeLayoutMetrics({
    rows,
    columns: 120,
    hasActivity: true,
    hasError: false,
    hasPanel: false,
    hasSlashSuggestions: false,
    inputRows: 1,
    headerRows: 0,
  })
  const fixed = planFixedMessageViewport({
    hasStaticMessages: true,
    liveLineCount: 5,
    messageRows: m.messageRows,
  })
  console.log(
    `rows=${rows} messageRows=${m.messageRows} threshold=${INLINE_FIXED_SLOT_MIN_MESSAGE_ROWS}`
    + `  slot=${inlineLiveViewportRows({ messageRows: m.messageRows })}`
    + `  fixed=${JSON.stringify(fixed)}`,
  )
}
