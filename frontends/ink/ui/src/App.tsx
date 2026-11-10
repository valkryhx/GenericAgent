import React, { useEffect, useLayoutEffect, useMemo, useReducer, useRef, useState } from 'react'
import { Box, Static, Text, useApp, useStdin, useStdout } from 'ink'
import { getInkTheme, INK_THEME_NAMES, type InkTheme, type InkThemeName } from './theme.js'
import { moveThemeSelection, themeDescription, themePanelRows } from './themePanel.js'
import { startBridge, type BridgeClient } from './bridgeClient.js'
import { applyBridgeEvent, initialState } from './state.js'
import { createPasteStore } from './paste.js'
import { createImageAttachmentStore } from './imageAttachments.js'
import type { SkillStatus } from './protocol.js'
import { handleInput, applyClipboardImagePaste } from './inputController.js'
import { readClipboardText } from './clipboardText.js'
import {
  WORKFLOW_PANEL_MAX_ROWS,
  workflowListPanelFromRuns,
  workflowPanelCapturesKey,
  workflowPanelCommandForKey,
  workflowPanelFromDetail,
  workflowPanelRows,
  workflowPanelWithLive,
  workflowPanelWithRunUpdate,
  type WorkflowPanelState,
} from './workflowPanel.js'
import {
  HOOKS_PANEL_MAX_ROWS,
  hooksPanelCapturesKey,
  hooksPanelCommandForKey,
  hooksPanelFromStatus,
  hooksPanelLines,
  type HooksPanelState,
} from './hooksPanel.js'
import {
  TEAM_PANEL_MAX_ROWS,
  teamPanelCapturesKey,
  teamPanelCommandForKey,
  teamPanelFromRoster,
  teamPanelLines,
  teamPanelViewport,
  type TeamPanelState,
} from './teamPanel.js'
import { workflowStatusBarCommandForKey, workflowStatusBarFromState, workflowStatusBarRows } from './workflowStatusBar.js'
import { goalCardLines, goalCompactLine, goalTerminalLines } from './goalCard.js'
import { createInputHistory, nextInput, previousInput, recordInput } from './inputHistory.js'
import {
  liveTranscriptViewportLines,
  transcriptLines,
  transcriptLinesWithChannel,
  type TranscriptLine,
  type TranscriptPart,
  visibleTranscriptLines,
  wrapTranscriptLines,
} from './messageWindow.js'
import {
  handleSelectorInput,
  newestResumeSessions,
  rewindOptions,
  selectorSize,
  visibleSelectorRows,
  type SelectorState,
} from './selectors.js'
import type { BridgeEvent, PermissionMode, ResumeSession, TokenUsage } from './protocol.js'
import { contextUsageHint } from './contextHint.js'
import {
  loadingMcpPanel,
  mcpPanelRows,
  mcpStartupStatusRows,
  mcpStatusIcon,
  mcpToolsForServer,
  moveMcpSelection,
  panelFromMcpStatus,
  visibleMcpServerRows,
  type McpPanelState,
} from './mcpPanel.js'
import {
  modelPanelRows,
  moveModelSelection,
  moveReasoningSelection,
  openReasoningPanel,
  panelFromModelStatus,
  shouldApplyModelStatus,
  shouldOpenReasoningPanel,
  type ModelPanelState,
} from './modelPanel.js'
import {
  movePermissionSelection,
  panelFromPermissionStatus,
  permissionPanelOnEnter,
  permissionPanelOnEscape,
  permissionPanelRows,
  shouldApplyPermissionStatus,
  type PermissionPanelState,
} from './permissionPanel.js'
import {
  APPROVAL_OPTIONS,
  approvalFromPermissionRequest,
  approvalPanelOnEnter,
  approvalPanelOnEscape,
  approvalPanelOnSettled,
  approvalPanelRows,
  enqueueApprovalRequest,
  moveApprovalSelection,
  type ApprovalPanelState,
} from './approvalPanel.js'
import {
  GOAL_PROPOSAL_OPTIONS,
  enqueueGoalProposal,
  goalProposalFromEvent,
  goalProposalOnEnter,
  goalProposalOnEscape,
  goalProposalOnSettled,
  goalProposalRows,
  moveGoalProposalSelection,
  type GoalProposalPanelState,
} from './goalProposalPanel.js'
import {
  formatSlashSuggestionLine,
  moveSlashSelection,
  shouldCompleteSlashCommand,
  slashSelectionAction,
  slashSuggestions,
  visibleSlashSuggestions,
  type SlashCommand,
} from './slashCommands.js'
import {
  fixedInputLine,
  inputContentColumns,
  inputFrameBorderStyle,
  inputGutterColumns,
  inputLeftPaddingColumns,
  inputViewport,
  renderInputLine,
  type InputLinePart,
  type InputViewport,
} from './promptChrome.js'
import { completedStatusSegments, pickRunningVerb, runningStatusSegments, shouldShowActivityStatus } from './activityStatus.js'
import { inputChromeSections, type InputChromeSection } from './inputLayout.js'
import { agentNoticeFromRecord, agentNoticeLine, type AgentNotice } from './agentNotice.js'
import { footerPanelRows, modelSwitchPanelText, skillsPanelText, statusPanelText, type FooterPanel } from './footerPanel.js'
import {
  clearLocalCommandOutput,
  commandTextForLocalDecision,
  dismissedLocalCommandOutput,
} from './localCommandTranscript.js'
import {
  nextStopEchoGate,
  pendingLocalCommandAfterBridgeEvent,
  stopEchoGateAfterStatus,
} from './localCommandFlow.js'
import {
  computeLayoutMetrics,
  terminalCanvasColumns,
  transcriptContentColumns,
  transcriptScrollbarColumns,
} from './layoutMetrics.js'
import {
  cleanupTerminalForExit,
  clearInlineLiveViewportSequence,
  enterMainScreenTerminalSequenceForMode,
  reassertMouseTracking,
  resetViewportSequence,
} from './terminalCleanup.js'
import { inputCursorPosition } from './terminalCursor.js'
import type { CursorParkController } from './stdoutCursorPark.js'
import type { InputKey } from './inputController.js'
import { parseTerminalInput } from './terminalInput.js'
import { parseMouseEvent, parseMouseWheel, resolveFullscreenMode, resolveMouseCaptureMode } from './mouseWheel.js'
import {
  displayColumnEnd,
  partitionByDisplayColumns,
  type SelectionSpan,
} from './transcriptSelection.js'
import {
  buildFramePlan,
} from './framePlan.js'
import {
  framePointFromMouse,
  frameSelectionSpans,
  isEmptyFrameSelection,
  normalizeFrameSelection,
  selectedFrameText,
  type FramePoint,
  type FrameRow,
  type FrameSelectionRange,
} from './frameSelection.js'
import { writeClipboardTextAsync } from './clipboardText.js'
import { decideInterrupt, interruptConfirmHint } from './interruptConfirm.js'
import { countNewMessages, hitTestJumpToBottom, jumpToBottomPlacement, jumpToBottomText, type PillPlacement } from './jumpToBottomPill.js'
import {
  preserveTranscriptScrollOnContentChange,
  scrollOffsetForHistoryReplacement,
  scrollTranscriptBy,
  transcriptScrollStep,
  transcriptWheelStep,
} from './transcriptScroll.js'
import { splitStaticAndActiveMessages } from './messagePartition.js'
import { planMessageViewport } from './messageViewportPlan.js'

type Props = {
  python: string
  bridgeScript: string
  startBridgeClient?: typeof startBridge
  cursorPark?: CursorParkController
}

const STDIN_RESUME_GAP_MS = 5000

/**
 * Columns reserved on the left of the transcript content area.
 *
 * Must match the `paddingLeft` on the viewport box, otherwise the highlight is
 * shifted by however many columns differ — a one-column offset reads as the
 * cursor sitting between two characters.
 */
const TRANSCRIPT_LEFT_GUTTER = 1

function isResumeSuccessNotice(text: string): boolean {
  return /已恢复\s+\d+\s+轮/.test(text)
}

function resumeSuccessStatusText(text: string): string {
  const normalized = text.replace(/^[✅✓]\s*/, '').split('\n', 1)[0] ?? text
  const match = /已恢复\s+(\d+)\s+轮(?:结构化会话|完整对话|对话)?(?:（([^）]+)）)?/.exec(normalized)
  if (!match) return normalized
  const rounds = match[1]
  const source = match[2]
  return source ? `恢复完成：${rounds} 轮历史 · ${source}` : `恢复完成：${rounds} 轮历史`
}

/**
 * Paint one transcript row, optionally with a selection highlight.
 *
 * The highlight has to be applied *inside* the existing styled parts, not by
 * slicing `line.text`: rows carry per-segment colour (a tool result is grey, a
 * diff line is tinted), and rebuilding the row from plain text would drop all of
 * it. So walk the parts, carry a running display column, and split a part at the
 * highlight edges when the span cuts through it.
 */
function TranscriptLineView({ line, highlight }: { line: TranscriptLine; highlight?: SelectionSpan }) {
  const wrapProps = {
    color: line.color,
    backgroundColor: line.backgroundColor,
    wrap: 'truncate-end' as const,
  }
  const segments: Array<{ text: string; part?: TranscriptPart; inverse?: boolean }> = line.parts?.length
    ? line.parts.map(part => ({ text: part.text, part }))
    : [{ text: line.text }]
  if (!highlight) {
    return (
      <Text {...wrapProps}>
        {segments.map((segment, index) => (
          <StyledSegment key={index} segment={segment} />
        ))}
      </Text>
    )
  }

  let column = 0
  const painted: Array<{ text: string; part?: TranscriptPart; inverse?: boolean }> = []
  for (const segment of segments) {
    const segmentWidth = displayColumnEnd(segment.text)
    const segmentStart = column
    const segmentEnd = column + segmentWidth
    column = segmentEnd
    if (segmentEnd <= highlight.from || segmentStart >= highlight.to) {
      painted.push(segment)
      continue
    }
    // Split this segment into [before][selected][after] in ONE pass. Three
    // separate slices would duplicate any CJK character straddling a boundary,
    // widening the row as the cursor moved and shifting the truncation point —
    // the "text jitters left/right while dragging" report.
    const localFrom = Math.max(0, highlight.from - segmentStart)
    const localTo = Math.min(segmentWidth, highlight.to - segmentStart)
    const { before, selected, after } = partitionByDisplayColumns(segment.text, localFrom, localTo)
    if (before) painted.push({ ...segment, text: before })
    if (selected) painted.push({ ...segment, text: selected, inverse: true })
    if (after) painted.push({ ...segment, text: after })
  }
  return (
    <Text {...wrapProps}>
      {painted.map((segment, index) => (
        <StyledSegment key={index} segment={segment} />
      ))}
    </Text>
  )
}

function StyledSegment({ segment }: { segment: { text: string; part?: TranscriptPart; inverse?: boolean } }) {
  const part = segment.part
  return (
    <Text
      color={part?.color}
      backgroundColor={part?.backgroundColor}
      bold={part?.bold}
      italic={part?.italic}
      underline={part?.underline}
      dimColor={part?.dimColor}
      inverse={segment.inverse}
    >
      {segment.text}
    </Text>
  )
}
function formatResumeSession(session: ResumeSession): string {
  const minutes = Math.max(0, Math.round((Date.now() - session.mtime * 1000) / 60000))
  const age = minutes < 60 ? `${minutes}m ago` : minutes < 1440 ? `${Math.floor(minutes / 60)}h ago` : `${Math.floor(minutes / 1440)}d ago`
  const when = new Date(session.mtime * 1000)
  const stamp = `${String(when.getMonth() + 1).padStart(2, '0')}-${String(when.getDate()).padStart(2, '0')} ${String(when.getHours()).padStart(2, '0')}:${String(when.getMinutes()).padStart(2, '0')}`
  const preview = session.preview.replace(/\s+/g, ' ').slice(0, 80) || '(no preview)'
  const source = session.source === 'legacy' ? '[legacy] ' : ''
  return `${source}${stamp} - ${age} - ${session.rounds} turns - ${preview}`
}
function SelectorView({ selector, theme }: { selector: SelectorState; theme: InkTheme }) {
  const rows = visibleSelectorRows(selector, 8)
  const title = selector.mode === 'resume' ? 'Resume Conversation' : 'Rewind Conversation'
  const empty = selector.mode === 'resume' && selector.loading ? 'Loading conversations...' : selector.mode === 'resume' ? 'No resumable sessions found.' : 'Nothing to rewind to yet.'
  return (
    <Box flexDirection="column" paddingX={1}>
      <Text bold>{title}</Text>
      {rows.length === 0 ? <Text color={theme.muted}>{empty}</Text> : rows.map(row => {
        const text = selector.mode === 'resume'
          ? formatResumeSession(selector.sessions[row.index]!)
          : selector.options[row.index]!.text.replace(/\s+/g, ' ').slice(0, 90) || '(empty)'
        return (
        <Text key={`${selector.mode}-${row.index}`} color={row.selected ? theme.accent : undefined} inverse={row.selected}>
          {row.selected ? '> ' : '  '}{text}
        </Text>
        )
      })}
      <Text color={theme.muted}>Enter select - Up/Down move - Esc cancel</Text>
    </Box>
  )
}

function SlashSuggestionsView({ suggestions, selected, theme }: { suggestions: SlashCommand[]; selected: number; theme: InkTheme }) {
  const visible = visibleSlashSuggestions(suggestions, selected)
  return (
    <Box flexDirection="column" paddingX={1}>
      {visible.items.map((command, offset) => {
        const index = visible.startIndex + offset
        const active = index === selected
        return (
          <Text key={command.name} color={active ? theme.accent : undefined}>
            {'  '}{formatSlashSuggestionLine(command)}
          </Text>
        )
      })}
      <Text color={theme.muted}>Tab/Enter complete - Up/Down move - Esc cancel</Text>
    </Box>
  )
}

function McpPanelView({ panel, theme, maxServerRows }: { panel: McpPanelState; theme: InkTheme; maxServerRows: number }) {
  const selected = panel.servers[panel.selected]
  const selectedTools = selected ? mcpToolsForServer(panel, selected.name) : []
  const serverRows = visibleMcpServerRows(panel, maxServerRows)
  return (
    <Box flexDirection="column" paddingX={1}>
      <Text bold>MCP Servers</Text>
      {panel.configPath ? <Text color={theme.muted}>Config: {panel.configPath}</Text> : null}
      {panel.loading ? <Text color={theme.muted}>Loading MCP status...</Text> : null}
      {!panel.loading && panel.servers.length === 0 ? <Text color={theme.muted}>No MCP servers configured.</Text> : null}
      {serverRows.map(row => {
        const server = panel.servers[row.index]!
        return (
          <Text key={server.name} color={row.selected ? theme.accent : undefined}>
            {row.selected ? '> ' : '  '}
            <Text color={themeColorForMcpStatus(server.status, theme)}>{mcpStatusIcon(server.status)}</Text>
            {` ${server.name} - ${server.status} - ${server.transport} - ${server.tool_count} tools`}
          </Text>
        )
      })}
      {selected ? <Text color={theme.muted}>Actions: /mcp reconnect {selected.name} - /mcp {selected.disabled ? 'enable' : 'disable'} {selected.name}</Text> : null}
      {selected && panel.errors[selected.name] ? <Text color={theme.error}>{panel.errors[selected.name]}</Text> : null}
      {selectedTools.slice(0, 2).map(tool => (
        <Text key={tool.function.name} color={theme.muted}>  - {tool.function.name}</Text>
      ))}
      <Text color={theme.muted}>Up/Down move - Esc close</Text>
    </Box>
  )
}

function McpStartupStatusView({ rows, loading, theme, height }: { rows: string[]; loading: boolean; theme: InkTheme; height?: number }) {
  // `height` pins the band even when `rows` is empty: the row is reserved from the
  // first frame so the arrival of the MCP banner cannot push the composer up.
  return (
    <Box flexDirection="column" paddingX={1} height={height} flexShrink={0} overflow="hidden">
      {rows.map((row, index) => (
        <Text key={`${index}:${row}`} color={index === 0 && loading ? theme.warning : theme.muted} wrap="truncate-end">{row}</Text>
      ))}
    </Box>
  )
}

function ModelPanelView({ panel, theme }: { panel: ModelPanelState; theme: InkTheme }) {
  if (panel.reasoning) {
    const model = panel.reasoning.model
    const efforts = model.reasoningEfforts ?? []
    return (
      <Box flexDirection="column" paddingX={1} flexShrink={0}>
        <Text bold>{`Reasoning levels · ${model.name}`}</Text>
        {efforts.length === 0 ? <Text color={theme.muted}>No reasoning levels declared for this model.</Text> : null}
        {efforts.map((effort, index) => (
          <Text key={effort} color={index === panel.reasoning?.selected ? theme.accent : undefined}>
            {index === panel.reasoning?.selected ? '> ' : '  '}
            <Text color={model.reasoningEffort === effort ? theme.success : theme.muted}>{model.reasoningEffort === effort ? '✓' : ' '}</Text>
            {` ${effort}`}
            <Text color={theme.muted}>{model.defaultReasoningEffort === effort ? '  default' : ''}</Text>
          </Text>
        ))}
        <Text color={theme.muted}>Enter select - Up/Down move - Esc back</Text>
      </Box>
    )
  }
  return (
    <Box flexDirection="column" paddingX={1} flexShrink={0}>
      <Text bold>Models</Text>
      {panel.models.length === 0 ? <Text color={theme.muted}>No models configured.</Text> : null}
      {panel.models.map((model, index) => (
        <Text key={model.index} color={index === panel.selected ? theme.accent : undefined}>
          {index === panel.selected ? '> ' : '  '}
          <Text color={model.current ? theme.success : theme.muted}>{model.current ? '✓' : ' '}</Text>
          {` ${model.index}: ${model.name}`}
        </Text>
      ))}
      <Text color={theme.muted}>Enter select - Up/Down move - Esc cancel</Text>
    </Box>
  )
}

function PermissionPanelView({ panel, theme }: { panel: PermissionPanelState; theme: InkTheme }) {
  return (
    <Box flexDirection="column" paddingX={1} flexShrink={0}>
      <Text bold>Permission mode</Text>
      {panel.options.map((option, index) => {
        const isCurrent = option.mode === panel.current
        const isSelected = index === panel.selected
        return (
          <Text key={option.mode} color={isSelected ? theme.accent : undefined}>
            {isSelected ? '> ' : '  '}
            <Text color={isCurrent ? theme.success : theme.muted}>{isCurrent ? '✓' : ' '}</Text>
            {` ${option.title}`}
            <Text color={theme.muted}>{`  ${option.description}`}</Text>
          </Text>
        )
      })}
      {panel.confirming ? (
        <Text color={theme.warning}>
          {`⚠ 切到 Full Access：所有工具将直接执行、不再询问。Enter 确认 - Esc 取消`}
        </Text>
      ) : (
        <Text color={theme.muted}>Enter select - Up/Down move - Esc cancel</Text>
      )}
    </Box>
  )
}

function ApprovalPanelView({ panel, theme }: { panel: ApprovalPanelState; theme: InkTheme }) {
  const { current } = panel
  return (
    <Box flexDirection="column" paddingX={1} flexShrink={0}>
      <Text bold color={theme.warning}>需要批准</Text>
      <Text>{`工具: ${current.toolName}`}</Text>
      <Text color={theme.muted} wrap="truncate-end">{`参数: ${current.argsPreview || '(none)'}`}</Text>
      {current.reason ? <Text color={theme.muted} wrap="truncate-end">{`原因: ${current.reason}`}</Text> : null}
      {APPROVAL_OPTIONS.map((option, index) => {
        const isSelected = index === panel.selected
        return (
          <Text key={option.decision} color={isSelected ? theme.accent : undefined}>
            {isSelected ? '> ' : '  '}
            {option.title}
          </Text>
        )
      })}
      <Text color={theme.muted}>Enter 确认 · Esc 拒绝 · Up/Down 选择</Text>
    </Box>
  )
}

function GoalProposalPanelView({ panel, theme }: { panel: GoalProposalPanelState; theme: InkTheme }) {
  const { current } = panel
  return (
    <Box flexDirection="column" paddingX={1} flexShrink={0}>
      <Text bold color={theme.warning}>建议设定目标</Text>
      <Text wrap="wrap">{`${current.condition}`}</Text>
      {GOAL_PROPOSAL_OPTIONS.map((option, index) => {
        const isSelected = index === panel.selected
        return (
          <Text key={option.decision} color={isSelected ? theme.accent : undefined}>
            {isSelected ? '> ' : '  '}
            {option.title}
          </Text>
        )
      })}
      <Text color={theme.muted}>Enter 确认 · Esc 不设定 · Up/Down 选择</Text>
    </Box>
  )
}

function FooterPanelView({ panel, theme }: { panel: FooterPanel; theme: InkTheme }) {
  if (panel.type === 'status') {
    return (
      <Box paddingX={1}>
        <Text color={theme.muted} wrap="truncate-end">{panel.text}</Text>
      </Box>
    )
  }
  return (
    <Box flexDirection="column" paddingX={1}>
      {panel.text.split('\n').map((line, index) => (
        <Text key={index} color={index === 0 && panel.type === 'help' ? undefined : theme.muted}>{line}</Text>
      ))}
      <Text color={theme.muted}>Esc close</Text>
    </Box>
  )
}

function WorkflowPanelView({ lines, theme }: { lines: string[]; theme: InkTheme }) {
  return (
    <Box flexDirection="column" paddingX={1}>
      {lines.map((line, index, rows) => (
        <Text key={index} color={index === 0 ? theme.accent : index >= 4 && index < rows.length - 1 ? undefined : theme.muted} wrap="truncate-end">
          {line}
        </Text>
      ))}
    </Box>
  )
}

function MessageViewport({ height, columns, lines, ready, readyLabel = 'Ready.', totalRows, scrollOffset, selection: selectionSpansForRows, jumpToBottom, theme }: {
  height: number
  columns: number
  lines: TranscriptLine[]
  ready: boolean
  /** Text painted in the ready row. A single space keeps the row without the cue. */
  readyLabel?: string
  totalRows: number
  scrollOffset: number
  /** Per-row highlight spans from `selectionSpans`, aligned with `lines`. */
  selection?: Array<SelectionSpan | undefined>
  /**
   * The floating "Jump to bottom" pill. `null` while the view is already pinned to
   * the tail, which is the normal state — the pill only exists to get the user back
   * after they scrolled away.
   */
  jumpToBottom?: { placement: PillPlacement } | null
  theme: InkTheme
}) {
  void totalRows
  void scrollOffset
  // No scrollbar column. The thumb read as a dark stripe on the right edge and
  // needed a draggable target that only existed to work around not having wheel
  // paging. Wheel paging plus an in-app highlight cover both jobs now.
  //
  // The pill is painted by *replacing the last viewport row*, not by absolute
  // positioning: ink 5.2.1 has no `left/bottom` and no `onClick` on Box (Claude
  // Code ships a fork that does). Overlaying the bottom row gives the same visual
  // — a floating box that costs no extra line — and clicking is handled by the
  // app's own mouse path via `hitTestJumpToBottom`.
  const contentColumns = Math.max(1, columns - TRANSCRIPT_LEFT_GUTTER - 1)
  const pillRow = height - 1
  const pill = jumpToBottom?.placement ?? null
  const rows: React.ReactNode[] = []
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index]!
    if (pill && index === pillRow) {
      // The enclosing Box already supplies the left gutter via `paddingLeft`, so
      // `pill.left` is measured from the content edge and must NOT have the gutter
      // added again — doing so pushed the row one column over and made Ink
      // truncate it with a trailing ellipsis.
      const padLeft = ' '.repeat(pill.left)
      const padRight = ' '.repeat(Math.max(0, contentColumns - pill.left - pill.width))
      rows.push(
        <Text key={`pill-${line.id}`} wrap="truncate-end">
          {padLeft}
          <Text color={theme.userText} backgroundColor={theme.userBackground}>{` ${pill.text} `}</Text>
          {padRight}
        </Text>,
      )
      continue
    }
    rows.push(<TranscriptLineView key={line.id} line={line} highlight={selectionSpansForRows?.[index]} />)
  }
  return (
    <Box flexDirection="row" height={height} width={columns} overflow="hidden">
      <Box flexDirection="column" paddingLeft={TRANSCRIPT_LEFT_GUTTER} paddingRight={1} height={height} width={columns} overflow="hidden">
        {ready
          ? <Text color={theme.muted}>{readyLabel}</Text>
          : rows}
      </Box>
    </Box>
  )
}

/**
 * Split a plain string into highlighted / non-highlighted parts.
 *
 * One `partitionByDisplayColumns` pass, not three overlapping slices: a CJK character
 * straddling a boundary would otherwise be emitted by both neighbours, the row would grow,
 * and `wrap="truncate-end"` would slide the truncation point — the visible "text jitters
 * while dragging" bug.
 */
function splitForHighlight(text: string, span: SelectionSpan): InputLinePart[] {
  const { before, selected, after } = partitionByDisplayColumns(text, span.from, span.to)
  return [
    ...(before ? [{ text: before }] : []),
    ...(selected ? [{ text: selected, inverse: true }] : []),
    ...(after ? [{ text: after }] : []),
  ]
}

/** Narrow every part to `[from, to)` display columns, keeping each part's own styling. */
function applyHighlight(parts: InputLinePart[], span: SelectionSpan): InputLinePart[] {
  const out: InputLinePart[] = []
  let column = 0
  for (const part of parts) {
    const width = displayColumnEnd(part.text)
    const start = column
    column += width
    if (column <= span.from || start >= span.to) {
      out.push(part)
      continue
    }
    const localFrom = Math.max(0, span.from - start)
    const localTo = Math.min(width, span.to - start)
    const { before, selected, after } = partitionByDisplayColumns(part.text, localFrom, localTo)
    if (before) out.push({ ...part, text: before })
    // The caret's own inverse block stays inverse; the highlight simply keeps it dark.
    if (selected) out.push({ ...part, text: selected, inverse: true })
    if (after) out.push({ ...part, text: after })
  }
  return out
}

function BottomChrome({ children, columns, height }: { children: React.ReactNode; columns: number; height: number }) {  return (
    <Box flexDirection="column" flexShrink={0} width={columns} height={height} overflow="hidden">
      {children}
    </Box>
  )
}

function InputView({ viewport, showCursor, columns, theme, selection }: { viewport: InputViewport; showCursor: boolean; columns: number; theme: InkTheme; selection?: Array<SelectionSpan | undefined> }) {  const lines = viewport.lines
  const contentColumns = inputContentColumns(columns)
  return (
    <Box
      flexDirection="column"
      alignItems="flex-start"
      borderStyle={inputFrameBorderStyle}
      borderColor={theme.border}
      borderLeft={false}
      borderRight={false}
      borderTop
      borderBottom
      width="100%"
      height={viewport.lines.length + 2}
      flexShrink={0}
      paddingLeft={inputLeftPaddingColumns}
      overflow="hidden"
    >
      {lines.map((line, index) => {
        const fixed = fixedInputLine(line.text, contentColumns)
        // The selection span indexes the *painted* row, which is the gutter plus the
        // text (that is what the frame model stores, and what `selectedFrameText` reads
        // back). The row is painted as two separate <Text> nodes, so a span crossing the
        // boundary has to be cut into a gutter part and a text part -- and each part must
        // be one partition, never three overlapping slices, or a wide character would be
        // duplicated and the row would grow while dragging.
        const span = selection?.[index]
        const gutterWidth = displayColumnEnd(line.gutter)
        const gutterSpan = span
          ? { from: Math.max(0, Math.min(span.from, gutterWidth)), to: Math.max(0, Math.min(span.to, gutterWidth)) }
          : null
        const textSpan = span
          ? { from: Math.max(0, span.from - gutterWidth), to: Math.max(0, span.to - gutterWidth) }
          : null
        const gutterParts = gutterSpan && gutterSpan.to > gutterSpan.from
          ? splitForHighlight(line.gutter, gutterSpan)
          : [{ text: line.gutter }]
        const lineParts = renderInputLine(fixed, showCursor && line.cursorColumn !== undefined, line.cursorColumn === undefined ? undefined : line.cursorColumn + 1)
        const painted = textSpan && textSpan.to > textSpan.from
          ? applyHighlight(lineParts, textSpan)
          : lineParts
        return (
          <Box key={index} width="100%" overflow="hidden">
            <Text color={theme.accent} wrap="truncate-end">
              {gutterParts.map((part, partIndex) => (
                part.inverse
                  ? <Text key={partIndex} inverse>{part.text}</Text>
                  : <Text key={partIndex}>{part.text}</Text>
              ))}
            </Text>
            {/*
              文本默认 muted，仅 caret 的 inverse 块用 accent。
              旧实现父级 Text color=accent 会让 inverse 继承 cyan 前景/背景，
              在 spawnSync 卡帧或多次贴图重绘时看起来像「蓝绿条」。
            */}
            <Text color={theme.muted} wrap="truncate-end">
              {painted.map((part, partIndex) => (
                part.inverse
                  ? <Text key={partIndex} color={theme.accent} inverse>{part.text}</Text>
                  : <Text key={partIndex}>{part.text}</Text>
              ))}
            </Text>
          </Box>
        )
      })}
    </Box>
  )
}

type CompletedTurnActivity = { durationSeconds: number; completedAt: number }


function ActivityView({ seconds, label, tokenUsage, completedTurn, theme }: { seconds: number; label: string; tokenUsage: TokenUsage | null; completedTurn: CompletedTurnActivity | null; theme: InkTheme }) {
  // Token counts are the numbers people actually read off this line, so they are
  // rendered bold while the surrounding prose stays normal weight. Bolding the
  // whole line (the easy version) makes "Worked for 2m 11s • Oct 5 at 13:41" and
  // the counts blur into one another.
  const segments = completedTurn
    ? completedStatusSegments(completedTurn.durationSeconds, new Date(completedTurn.completedAt), tokenUsage)
    : runningStatusSegments(seconds, label, tokenUsage)
  return (
    <Box paddingX={1}>
      <Text color={completedTurn ? theme.muted : theme.warning} wrap="truncate-end">
        {segments.map((segment, index) => (
          <Text key={index} bold={segment.emphasis} color={segment.emphasis ? theme.accent : undefined}>
            {segment.text}
          </Text>
        ))}
      </Text>
    </Box>
  )
}

function WorkflowStatusBarView({ rows, theme }: { rows: string[]; theme: InkTheme }) {
  return (
    <Box paddingX={1}>
      <Text color={theme.warning} wrap="truncate-end">{rows.join(' · ')}</Text>
    </Box>
  )
}

function GoalStatusCardView({ lines, terminal, theme }: { lines: string[]; terminal: string[]; theme: InkTheme }) {
  if (lines.length === 0 && terminal.length === 0) return null
  return (
    <Box flexDirection="column" paddingX={1} flexShrink={0}>
      {lines.map((line, index) => (
        <Text key={`g-${index}`} color={index === 0 ? theme.accent : theme.muted} wrap="truncate-end">{line}</Text>
      ))}
      {terminal.map((line, index) => (
        <Text key={`gt-${index}`} color={line.startsWith('✓') ? theme.success : theme.error} wrap="truncate-end">{line}</Text>
      ))}
    </Box>
  )
}

function themeColorForMcpStatus(status: string, theme: InkTheme): string {
  if (status === 'connected') return theme.success
  if (status === 'failed') return theme.error
  if (status === 'disabled') return theme.muted
  return theme.warning
}

function ThemePanelView({ selected, currentTheme, theme }: { selected: number; currentTheme: InkThemeName; theme: InkTheme }) {
  return (
    <Box flexDirection="column" paddingX={1} flexShrink={0}>
      <Text bold>Themes</Text>
      {INK_THEME_NAMES.map((themeName, index) => (
        <Text key={themeName} color={index === selected ? theme.accent : undefined}>
          {index === selected ? '> ' : '  '}
          <Text color={themeName === currentTheme ? theme.success : theme.muted}>{themeName === currentTheme ? '✓' : ' '}</Text>
          {` ${themeName.padEnd(9)} ${themeDescription(themeName)}`}
        </Text>
      ))}
      <Text color={theme.muted}>Enter select - Up/Down move - Esc cancel</Text>
    </Box>
  )
}

function ActivityPlaceholder() {
  return (
    <Box height={1}>
      <Text> </Text>
    </Box>
  )
}

export function helpText(): string {
  return [
    'Commands:',
    '/resume, /continue - pick a previous conversation',
    '/resume N, /continue N - resume by index',
    '/rewind, /checkpoint - restore conversation to before a user message',
    '/compact [instructions] - summarize and replace long conversation context',
    '/workflows - list workflow runs',
    '/skills - show skills and context cost',
    '/team, /agents - show team roster and shared task board',
    '/hooks - show hook configuration, coverage and recent executions',
    '/workflow on|off - opt this session into dynamic workflow orchestration (the agent then authors a JS workflow script)',
    '/workflow detail|resume|stop RUN_ID - inspect or control workflows',
    '/goal [condition] - set a stop-condition goal (auto-clears when met)',
    '/goal clear - remove the current goal; bare /goal shows it',
    '/clear - clear display only',
    '/status - show current frontend status',
    '/model, /llm - show and switch AI models',
    '/permissions - show and switch permission mode (read only / ask / full access)',
    '/stop - stop current backend task',
    '/exit, /quit - exit',
    '',
    'Image paste:',
    'Alt+V - paste clipboard image as [Image #N] (recommended on Windows)',
    'Ctrl+V - paste clipboard image when terminal does not intercept it',
    'Ctrl+Alt+V - alternate paste-image binding (Codex-style)',
    'Or paste a local image path (e.g. D:\\\\shots\\\\a.png)',
  ].join('\n')
}

export function App({ python, bridgeScript, startBridgeClient = startBridge, cursorPark }: Props) {
  const { exit } = useApp()
  const { stdout } = useStdout()
  const { setRawMode, isRawModeSupported, internal_eventEmitter } = useStdin()
  const [terminalSize, setTerminalSize] = useState(() => ({
    columns: Math.max(1, stdout.columns || 80),
    rows: Math.max(1, stdout.rows || 24),
  }))
  /** 与 terminalSize 同步的即时快照：resize 监听里要判断「宽/高是否真的变了」。 */
  const terminalSizeRef = useRef(terminalSize)
  const [state, dispatch] = useReducer(applyBridgeEvent, initialState)
  const [input, setInput] = useState('')
  const [cursorOffset, setCursorOffset] = useState(0)
  const [inputHistory, setInputHistory] = useState(() => createInputHistory())
  const [skills, setSkills] = useState<SkillStatus[]>([])
  const [selector, setSelector] = useState<SelectorState | null>(null)
  const [mcpPanel, setMcpPanel] = useState<McpPanelState | null>(null)
  const [mcpStartupStatus, setMcpStartupStatus] = useState<Extract<BridgeEvent, { type: 'mcp_progress' }> | null>(null)
  const [modelPanel, setModelPanel] = useState<ModelPanelState | null>(null)
  const [permissionPanel, setPermissionPanel] = useState<PermissionPanelState | null>(null)
  const [approvalPanel, setApprovalPanel] = useState<ApprovalPanelState | null>(null)
  const [goalProposalPanel, setGoalProposalPanel] = useState<GoalProposalPanelState | null>(null)
  const [themePanelSelected, setThemePanelSelected] = useState<number | null>(null)
  const [themeName, setThemeName] = useState<InkThemeName>('default')
  const theme = useMemo(() => getInkTheme(themeName), [themeName])
  const mouseMode = useMemo(() => resolveMouseCaptureMode(), [])
  // Layout vs mouse capture are separate decisions.
  // - `fullscreen`: alternate screen + pinned root height. What keeps the composer
  //   from sliding down as output grows.
  // - `mouseMode`: how much pointer traffic GA claims. Any capture mode takes
  //   native click-drag selection away from the terminal, so the default is `off`.
  const fullscreen = useMemo(() => resolveFullscreenMode(), [])
  const [footerPanel, setFooterPanel] = useState<FooterPanel | null>(null)
  const [contextHint, setContextHint] = useState<string | null>(null)
  // Subagent lifecycle announcements. The bridge already streams every child's status
  // (`agent_snapshot`); before this, the UI stored it and rendered nothing, so a child
  // finishing was invisible unless you read state.json off disk. Keyed by execution id so
  // a re-emitted snapshot cannot announce the same child twice, and kept in arrival order
  // so several children finishing together accumulate instead of replacing each other.
  const [agentNotices, setAgentNotices] = useState<AgentNotice[]>([])
  /**
   * The notices as of the last render, for the bridge event callback.
   *
   * The callback is installed once (its effect depends only on the bridge config), so it
   * closes over the first `agentNotices` value. Reading the ref is what lets a later
   * snapshot see -- and correct -- what was already shown.
   */
  const agentNoticesRef = useRef<AgentNotice[]>([])
  agentNoticesRef.current = agentNotices
  // Tracks whether the session was actually running, so "notices are stale" can be told
  // apart from "the app has never run a turn yet". Without it the clear-on-idle path also
  // fires on the initial `connecting -> idle` transition and eats the first snapshot.
  const wasBusyRef = useRef(false)
  // Prompts the user typed while a turn was running. They are folded into that turn
  // at the next tool boundary (the bridge is the one that actually delivers them);
  // this copy exists so the composer can say "accepted, waiting its turn" instead of
  // leaving the user guessing. Cleared when the turn they belong to ends.
  const [queuedPrompts, setQueuedPrompts] = useState<string[]>([])
  const [workflowPanel, setWorkflowPanel] = useState<WorkflowPanelState | null>(null)
  const [teamPanel, setTeamPanel] = useState<TeamPanelState | null>(null)
  const [hooksPanel, setHooksPanel] = useState<HooksPanelState | null>(null)
  const [slashSelected, setSlashSelected] = useState(0)
  const [expandedTools, setExpandedTools] = useState(false)
  const [transcriptScrollOffset, setTranscriptScrollOffset] = useState(0)
  const [staticTranscriptGeneration, setStaticTranscriptGeneration] = useState(0)
  const [terminalReady, setTerminalReady] = useState(false)
  const [runningStartedAt, setRunningStartedAt] = useState<number | null>(null)
  const [completedTurnActivity, setCompletedTurnActivity] = useState<CompletedTurnActivity | null>(null)
  const [lastActivitySeconds, setLastActivitySeconds] = useState(0)
  const [runningLabel, setRunningLabel] = useState(() => pickRunningVerb())
  const [notice, setNotice] = useState<string | null>(null)
  const [now, setNow] = useState(() => Date.now())
  /**
   * In-app selection, in *frame* coordinates: `row` is an absolute screen row and
   * `col` an absolute canvas column, so one drag can run from the transcript into the
   * composer (Claude Code's model: `onSelectionStart`/`onSelectionDrag` receive screen
   * coordinates and the text comes from the rendered frame). `anchor` is where the press
   * landed, `focus` follows the drag; keeping both unnormalized lets a drag back past
   * the anchor reverse direction.
   */
  const [selection, setSelection] = useState<{ anchor: FramePoint; focus: FramePoint } | null>(null)
  const selectionRef = useRef<{ anchor: FramePoint; focus: FramePoint } | null>(null)
  selectionRef.current = selection
  /**
   * 最新一帧的行模型。拖拽松开与 Ctrl+C 都在事件回调里读它——回调闭包会过期，
   * ref 不会。
   */
  const frameRowsRef = useRef<FrameRow[]>([])
  /** 第一次 Ctrl+C 的时间戳；窗口内再来一次才真退出。 */
  const interruptArmedAtRef = useRef<number | null>(null)
  const bridgeRef = useRef<BridgeClient | null>(null)
  const activeTurnStartedAtRef = useRef<number | null>(null)
  const terminalInputHandlerRef = useRef<((rawInput: string, key: InputKey) => void) | null>(null)
  const transcriptRowsRef = useRef({ totalRows: 0, viewportRows: 1 })
  const liveViewportGeometryRef = useRef({ rows: 1, cursorRow: 0 })
  const terminalCleanedRef = useRef(false)
  const resumePendingRef = useRef(false)
  const mcpPanelOpenRef = useRef(false)
  const modelPanelPendingRef = useRef(false)
  const modelPanelOpenRef = useRef(false)
  const permissionPanelPendingRef = useRef(false)
  const permissionPanelOpenRef = useRef(false)
  const pendingLocalCommandRef = useRef<string | null>(null)
  /** 同一 stop 周期内 /stop 回显只写一次（防双 Enter / 重入导致 Static 双份）。 */
  const stopTranscriptEchoedRef = useRef(false)
  const pendingHistoryReplacementScrollRef = useRef(false)
  const lastStdinAtRef = useRef(Date.now())
  const pasteStore = useMemo(() => createPasteStore(), [])
  const imageStore = useMemo(() => createImageAttachmentStore(), [])
  const imagePasteInFlightRef = useRef(false)
  const imagePasteSeqRef = useRef(0)
  const inputSnapshotRef = useRef({ value: '', cursorOffset: 0 })
  /** 剪贴板文本回退：记录本次插入的文本与时刻，用于 150ms 内丢弃终端重复投递的同一段 paste。 */
  const clipboardFallbackRef = useRef<{ text: string; at: number } | null>(null)
  const noticeTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const slashItems = useMemo(() => selector || mcpPanel || modelPanel || permissionPanel || approvalPanel || goalProposalPanel || themePanelSelected !== null || footerPanel || workflowPanel || teamPanel || hooksPanel ? [] : slashSuggestions(input, skills), [input, selector, mcpPanel, modelPanel, permissionPanel, approvalPanel, themePanelSelected, footerPanel, workflowPanel, teamPanel, hooksPanel, skills])
  const skillNames = useMemo(() => new Set(skills.map(skill => skill.name)), [skills])

  useEffect(() => {
    inputSnapshotRef.current = { value: input, cursorOffset }
  }, [input, cursorOffset])

  const appendLocalCommandInput = (commandText: string) => {
    dispatch({ type: 'local_command_input', text: commandText })
    pendingLocalCommandRef.current = commandText
  }

  const appendLocalCommandOutput = (text: string) => {
    dispatch({ type: 'local_command_output', text })
    pendingLocalCommandRef.current = null
  }

  const resetStaticTranscriptOutput = () => {
    setStaticTranscriptGeneration(value => value + 1)
  }

  // 整屏重置（清屏 + 清 scrollback + 光标归位）。<Static> 只能追加、无法撤销已写出的行，
  // 所以「历史被替换/裁剪后重印」必须先重置，否则旧行与新行叠加成重复。
  const resetTranscriptViewport = () => {
    if (cursorPark) cursorPark.resetViewport()
    else stdout.write(resetViewportSequence())
  }

  const cleanupTerminalOnce = () => {
    if (terminalCleanedRef.current) return
    terminalCleanedRef.current = true
    // park 由 cursorPark 控制器管理（相对移动，无 save/restore）。清理前先 unpark：
    // 把光标从 caret 相对移回帧底，让下面的 clear 几何从确定的帧底出发。
    cursorPark?.unpark()
    cursorPark?.dispose()
    const liveViewportGeometry = liveViewportGeometryRef.current
    // unpark 后光标恒在帧底（= rows 行、cursorRow 取 rows），与旧「未 park」分支一致。
    const inlineCleanupGeometry = { rows: liveViewportGeometry.rows, cursorRow: liveViewportGeometry.rows }
    if (!fullscreen) {
      stdout.write(clearInlineLiveViewportSequence(inlineCleanupGeometry))
    }
    cleanupTerminalForExit(stdout, mouseMode, fullscreen)
  }

  const exitCleanly = () => {
    cleanupTerminalOnce()
    exit()
  }

  const dismissPendingLocalCommand = () => {
    const commandText = pendingLocalCommandRef.current
    if (!commandText) return
    appendLocalCommandOutput(dismissedLocalCommandOutput(commandText))
  }

  const showNotice = (message: string) => {
    setNotice(message)
    if (noticeTimerRef.current) clearTimeout(noticeTimerRef.current)
    noticeTimerRef.current = setTimeout(() => {
      setNotice(null)
      noticeTimerRef.current = null
    }, 2000)
  }

  const applyInputDecision = (decision: ReturnType<typeof handleInput>) => {
    if (decision.notice) showNotice(decision.notice)
    if (decision.pendingImagePaste) {
      // 异步剪贴板：立刻返回，不阻塞 stdin；完成后把 [Image #N] 插到当前光标处。
      // 多次连按：in-flight 时忽略，避免并行 PowerShell 抢剪贴板。
      if (imagePasteInFlightRef.current) return
      imagePasteInFlightRef.current = true
      const seq = ++imagePasteSeqRef.current
      const baseValue = inputSnapshotRef.current.value
      const baseOffset = inputSnapshotRef.current.cursorOffset
      const policy = decision.imagePastePolicy ?? 'image-only'
      void applyClipboardImagePaste(baseValue, baseOffset, imageStore)
        .then(async (pasted) => {
          if (seq !== imagePasteSeqRef.current) return
          if (!pasted.ok) {
            // 剪贴板无图：Ctrl+V 语义允许回退成文本；Alt+V 只贴图，给一行提示。
            if (pasted.reason === 'no-image') {
              if (policy === 'image-then-text') {
                const text = readClipboardText()
                if (text) {
                  const current = inputSnapshotRef.current
                  const at = current.cursorOffset
                  const next = current.value.slice(0, at) + text + current.value.slice(at)
                  setInput(next)
                  setCursorOffset(at + text.length)
                  inputSnapshotRef.current = { value: next, cursorOffset: at + text.length }
                  clipboardFallbackRef.current = { text, at: Date.now() }
                }
              }
              return
            }
            if (pasted.reason === 'too-small') {
              showNotice('[image] 剪贴板里的图片太小，未插入')
              return
            }
            return
          }
          const current = inputSnapshotRef.current
          // await 期间输入未变：直接用捕获结果
          if (current.value === baseValue) {
            setInput(pasted.value)
            setCursorOffset(pasted.cursorOffset)
            return
          }
          // 用户已继续编辑：只把新 placeholder 插到「完成瞬间」的光标处
          const last = [...imageStore.byId.values()].at(-1)
          if (!last || current.value.includes(last.placeholder)) return
          const insertAt = current.cursorOffset
          const spacer = insertAt > 0 && !/\s$/.test(current.value.slice(0, insertAt)) ? ' ' : ''
          const withPh = current.value.slice(0, insertAt) + spacer + last.placeholder + current.value.slice(insertAt)
          setInput(withPh)
          setCursorOffset(insertAt + spacer.length + last.placeholder.length)
        })
        .finally(() => {
          if (seq === imagePasteSeqRef.current) imagePasteInFlightRef.current = false
        })
      return
    }
    const nextOffset = decision.cursorOffset ?? decision.value.length
    setInput(decision.value)
    setCursorOffset(nextOffset)
    // 立即同步快照：避免 React 重绘前二次 stdin 仍读到旧的 `/stop` 再提交一次。
    inputSnapshotRef.current = { value: decision.value, cursorOffset: nextOffset }
    const localCommandText = commandTextForLocalDecision(decision)
    if (decision.action?.type === 'clear') {
      setFooterPanel(null)
      setTranscriptScrollOffset(0)
      pendingLocalCommandRef.current = null
      dispatch({ type: 'clear' })
      resetStaticTranscriptOutput()
      dispatch({ type: 'local_command_input', text: localCommandText ?? '/clear' })
      dispatch({ type: 'local_command_output', text: clearLocalCommandOutput() })
      if (decision.exit) exitCleanly()
      return
    }
    // /stop 闸门必须在任何 dispatch 前回写：abort 常在同一 tick 内连发两次
    // （双 Enter / slash Enter 重入）；旧逻辑在 idle 重置闸门会让第二下再写一对 Static 行。
    const isStopCommand = decision.command?.type === 'stop'
    const stopGate = nextStopEchoGate({
      alreadyEchoed: stopTranscriptEchoedRef.current,
      isStopCommand,
    })
    if (isStopCommand) {
      stopTranscriptEchoedRef.current = stopGate.nextEchoed
    }
    if (localCommandText) {
      // /stop 重复进入时不再追加第二份 `/stop` 行（与 compact 重复显示同类问题）
      if (!isStopCommand || stopGate.echo) {
        appendLocalCommandInput(localCommandText)
      }
    }
    if (decision.command) {
      setFooterPanel(null)
      if (decision.command.type.startsWith('workflow_')) {
        setWorkflowPanel(null)
      }
      if (decision.command.type !== 'team_list') {
        setTeamPanel(null)
      }
      if (decision.command.type !== 'hooks_status') {
        setHooksPanel(null)
      }
      const command = decision.command
      if (command.type === 'submit') {
        setInputHistory(history => recordInput(history, command.text))
        setTranscriptScrollOffset(0)
      } else if (command.type === 'queue_prompt') {
        // The draft moves into the queue: it will be folded into the running turn at
        // the next tool boundary. Recording it in history here keeps up-arrow useful
        // for re-sending after the turn ends, which is what the user expects from a
        // draft they typed deliberately.
        setInputHistory(history => recordInput(history, command.text))
        setQueuedPrompts(prompts => [...prompts, command.text])
      } else if (command.type === 'skill_invoke') {
        setInputHistory(history => recordInput(history, `/${command.skill}${command.args ? ` ${command.args}` : ''}`))
        setTranscriptScrollOffset(0)
      }
      if (command.type === 'stop' && localCommandText && stopGate.echo) {
        appendLocalCommandOutput('Stop requested')
      }
      // abort 可多次调用；transcript 只回显一次
      bridgeRef.current?.send(command)
    }
    if (decision.action?.type === 'open_resume') {
      setFooterPanel(null)
      resumePendingRef.current = true
      const includeLegacy = Boolean(decision.action.includeLegacy)
      setSelector({ mode: 'resume', selected: 0, sessions: [], loading: true, includeLegacy })
      bridgeRef.current?.send({ type: 'list_resume_sessions', ...(includeLegacy ? { includeLegacy: true } : {}) })
    } else if (decision.action?.type === 'open_rewind') {
      setFooterPanel(null)
      const options = rewindOptions(state.messages)
      setSelector({ mode: 'rewind', selected: Math.max(0, options.length - 1), options })
    } else if (decision.action?.type === 'open_mcp') {
      setFooterPanel(null)
      mcpPanelOpenRef.current = true
      setMcpPanel(loadingMcpPanel())
      bridgeRef.current?.send({ type: 'mcp_status' })
    } else if (decision.action?.type === 'open_model') {
      setFooterPanel(null)
      modelPanelPendingRef.current = true
      bridgeRef.current?.send({ type: 'model_status' })
    } else if (decision.action?.type === 'open_permissions') {
      setFooterPanel(null)
      permissionPanelPendingRef.current = true
      bridgeRef.current?.send({ type: 'permission_status' })
    } else if (decision.action?.type === 'open_theme') {
      setFooterPanel(null)
      setThemePanelSelected(Math.max(0, INK_THEME_NAMES.indexOf(themeName)))
    } else if (decision.action?.type === 'help') {
      setFooterPanel({ type: 'help', text: helpText() })
    } else if (decision.action?.type === 'status') {
      setFooterPanel({ type: 'status', text: statusPanelText(state.status, state.messages.length) })
    } else if (decision.action?.type === 'skills') {
      setFooterPanel({ type: 'help', text: skillsPanelText(skills) })
    }
    if (decision.exit) {
      exitCleanly()
    }
  }

  useEffect(() => {
    setSlashSelected(0)
  }, [input])

  useEffect(() => {
    mcpPanelOpenRef.current = mcpPanel !== null
  }, [mcpPanel])

  useEffect(() => {
    modelPanelOpenRef.current = modelPanel !== null
  }, [modelPanel])

  useEffect(() => {
    permissionPanelOpenRef.current = permissionPanel !== null
  }, [permissionPanel])

  useEffect(() => () => {
    if (noticeTimerRef.current) clearTimeout(noticeTimerRef.current)
  }, [])

  useEffect(() => {
    stdout.write(enterMainScreenTerminalSequenceForMode(mouseMode, fullscreen))
    setTerminalReady(true)
    return () => {
      cleanupTerminalOnce()
    }
  }, [fullscreen, mouseMode, stdout])

  useEffect(() => {
    const syncTerminalSize = () => {
      // 退出清理之后（终端状态已交还）绝不能再写整屏重置，否则会清掉用户自己的屏幕。
      if (terminalCleanedRef.current) return
      const next = {
        columns: Math.max(1, stdout.columns || 80),
        rows: Math.max(1, stdout.rows || 24),
      }
      const previous = terminalSizeRef.current
      if (previous.columns === next.columns && previous.rows === next.rows) return
      terminalSizeRef.current = next
      // 先提交新尺寸：React（legacy root）会同步重画一帧，此后所有写入都按新宽度折行。
      setTerminalSize(next)
      if (previous.columns === next.columns) return
      // 宽度变化会让终端对**已经写出**的帧做 reflow（长行重新折行），上一帧实际占用的行数
      // 随之改变；而 ink 下一帧仍用改尺寸前的 previousLineCount 做相对擦除（log-update 的
      // eraseLines），于是擦不干净 → 旧帧顶行残留。真机表现就是放大/缩小终端后底部信息重复
      // 叠加（截图 2026-10-10 092005）。参考 Claude Code（log-update 的
      // fullResetSequence_CAUSES_FLICKER）与 Codex（draw_with_resize_reflow 的
      // clear_after_position + invalidate_viewport）：不猜 reflow 后的几何，直接整屏重置 +
      // 全量重绘 —— 重挂 <Static> 让 ink 重发历史，ink 随后按新宽度写出新帧，屏幕上只留一份。
      resetTranscriptViewport()
      resetStaticTranscriptOutput()
    }
    stdout.on('resize', syncTerminalSize)
    return () => {
      stdout.off('resize', syncTerminalSize)
    }
  }, [stdout, cursorPark])

  useEffect(() => {
    if (state.status === 'running' || state.status === 'stopping') {
      wasBusyRef.current = true
      setRunningStartedAt(value => {
        if (value !== null) return value
        setLastActivitySeconds(0)
        setRunningLabel(pickRunningVerb())
        return Date.now()
      })
      setNow(Date.now())
      const timer = setInterval(() => setNow(Date.now()), 1000)
      return () => clearInterval(timer)
    }
    // The turn these prompts belonged to is over, so the "queued" badge must not
    // survive into the next turn as a lie about a message that was already delivered.
    setQueuedPrompts(prompts => (prompts.length > 0 ? [] : prompts))
    // Same reason for the subagent announcements, but only on a real turn end.
    // Clearing on *any* non-running state also wiped the notices that arrive during
    // startup (`connecting` -> `idle`), which is when a snapshot can land before the
    // first turn ever runs -- that made the announcement appear at random.
    if (wasBusyRef.current) {
      wasBusyRef.current = false
      setAgentNotices(items => (items.length > 0 ? [] : items))
    }
    if (runningStartedAt !== null) {
      setLastActivitySeconds(Math.max(0, Math.floor((Date.now() - runningStartedAt) / 1000)))
    }
    setRunningStartedAt(null)
    setNow(Date.now())
    return undefined
  }, [runningStartedAt, state.status])

  useEffect(() => {
    let pendingDeltas: { [taskId: string]: string } = {}
    let throttleTimer: NodeJS.Timeout | null = null

    function flushDeltas() {
      if (Object.keys(pendingDeltas).length === 0) return
      for (const [taskId, text] of Object.entries(pendingDeltas)) {
        dispatch({ type: 'assistant_delta', taskId: Number(taskId), text })
      }
      pendingDeltas = {}
    }

    function onEvent(event: BridgeEvent) {
      if (event.type === 'ready') {
        bridgeRef.current?.send({ type: 'skill_status' })
        bridgeRef.current?.send({ type: 'mcp_watch_start' })
      }
      if (event.type === 'context_usage') {
        setContextHint(contextUsageHint(event))
      }
      if (event.type === 'status') {
        if (event.status === "running" && activeTurnStartedAtRef.current === null) {
          activeTurnStartedAtRef.current = Date.now()
          setCompletedTurnActivity(null)
        } else if (event.status === "idle" && activeTurnStartedAtRef.current !== null) {
          const completedAt = Date.now()
          setCompletedTurnActivity({
            durationSeconds: Math.max(0, Math.floor((completedAt - activeTurnStartedAtRef.current) / 1000)),
            completedAt,
          })
          activeTurnStartedAtRef.current = null
        }
        if (stopEchoGateAfterStatus(event.status) === 'reset') {
          stopTranscriptEchoedRef.current = false
        }
      }
      if (event.type === 'skill_status') {
        setSkills(event.skills)
        return
      }
      if (event.type === 'mcp_status') {
        setMcpStartupStatus({
          ...event,
          type: 'mcp_progress',
          loading: Boolean(event.loading),
          discovery_running: event.discovery_running ?? false,
          discovery_complete: event.discovery_complete ?? !event.loading,
        })
        if (!mcpPanelOpenRef.current) return
        setMcpPanel(panelFromMcpStatus(event))
        return
      }
      if (event.type === 'mcp_progress') {
        setMcpStartupStatus(event)
        if (mcpPanelOpenRef.current) {
          setMcpPanel(panelFromMcpStatus({
            type: 'mcp_status',
            config_path: event.config_path,
            servers: event.servers,
            tools: event.tools,
            errors: event.errors,
          }))
        }
        return
      }
      if (event.type === 'model_status') {
        if (pendingLocalCommandRef.current === '/model ?') {
          const rows = event.models.map(model => `${model.current ? '* ' : '  '}${model.index}. ${model.name}`)
          appendLocalCommandOutput(rows.length > 0 ? rows.join('\n') : 'No models configured')
          return
        }
        if (!shouldApplyModelStatus(modelPanelPendingRef.current, modelPanelOpenRef.current)) return
        modelPanelPendingRef.current = false
        setModelPanel(panelFromModelStatus(event))
        return
      }
      if (event.type === 'model_switch_result') {
        if (pendingLocalCommandRef.current) {
          appendLocalCommandOutput(event.message)
          return
        }
        setFooterPanel({ type: 'model', text: modelSwitchPanelText(event.message) })
        return
      }
      if (event.type === 'permission_status') {
        // /permissions ? —— 直接把当前档与可选档位打到 transcript，不开面板
        if (pendingLocalCommandRef.current === '/permissions ?') {
          const rows = event.modes.map(mode => `${mode === event.mode ? '* ' : '  '}${mode}`)
          appendLocalCommandOutput(
            `Current permission mode: ${event.mode}\n${rows.join('\n')}`,
          )
          return
        }
        if (!shouldApplyPermissionStatus(permissionPanelPendingRef.current, permissionPanelOpenRef.current)) return
        permissionPanelPendingRef.current = false
        setPermissionPanel(panelFromPermissionStatus(event))
        return
      }
      if (event.type === 'permission_request') {
        // 工具审批优先于切档面板
        setPermissionPanel(null)
        permissionPanelOpenRef.current = false
        permissionPanelPendingRef.current = false
        setApprovalPanel(panel => enqueueApprovalRequest(panel, approvalFromPermissionRequest(event)))
        return
      }
      if (event.type === 'permission_request_settled') {
        setApprovalPanel(panel => approvalPanelOnSettled(panel, event.requestId))
        return
      }
      if (event.type === 'goal_proposal_request') {
        setGoalProposalPanel(panel => enqueueGoalProposal(panel, goalProposalFromEvent(event)))
        return
      }
      if (event.type === 'goal_proposal_settled') {
        setGoalProposalPanel(panel => goalProposalOnSettled(panel, event.requestId))
        return
      }
      if (event.type === 'permission_switch_result') {
        if (pendingLocalCommandRef.current) {
          appendLocalCommandOutput(`Permission mode set to ${event.mode}`)
          return
        }
        dispatch({ type: 'system', text: `Permission mode set to ${event.mode}` })
        return
      }
      if (event.type === 'resume_sessions') {
        if (!resumePendingRef.current) return
        resumePendingRef.current = false
        setSelector(current => ({ mode: 'resume', selected: 0, sessions: newestResumeSessions(event.sessions), includeLegacy: Boolean(current?.mode === 'resume' && current.includeLegacy) }))
        return
      }
      if (event.type === 'history_replace') {
        const commandText = pendingLocalCommandRef.current
        if (event.resetSession) {
          setCompletedTurnActivity(null)
          activeTurnStartedAtRef.current = null
        }
        setSelector(null)
        setFooterPanel(null)
        resumePendingRef.current = false
        flushDeltas()
        // <Static> can only append. A whole-history replacement must clear the
        // old viewport and scrollback before the reducer remounts <Static>, or
        // old rows remain underneath the replayed transcript. Inline mode also
        // drops its old relative scroll offset; full mode keeps its existing
        // bottom-anchor effect below.
        resetTranscriptViewport()
        if (!fullscreen) setTranscriptScrollOffset(0)
        pendingHistoryReplacementScrollRef.current = true
        // 重挂 <Static> 的代数由 reducer 在同一个 action 里 +1，与 messages 落在同一次
        // commit；这里若再单独 bump，会先按「追加尾部」写一次、再全量重印 → 尾部重复
        // （resume 恢复的历史比当前会话长时可见，截图 resume_bug.png）。
        dispatch(event)
        // /compact 成功结果已在 history_replace 的 system 文案里；再 append 会把
        // 「/compact」插到结果后面，且 pending 若不清掉会影响后续 system 事件。
        // resume 等仍回显触发命令。
        if (commandText && !/^\/compact(?:\s|$)/i.test(commandText.trim())) {
          dispatch({ type: 'local_command_input', text: commandText })
        }
        pendingLocalCommandRef.current = null
        return
      }
      if (event.type === 'local_command_output') {
        pendingLocalCommandRef.current = pendingLocalCommandAfterBridgeEvent(pendingLocalCommandRef.current, event)
        dispatch(event)
        return
      }
      if (event.type === 'workflow_runs') {
        dispatch(event)
        setWorkflowPanel(workflowListPanelFromRuns(event.runs, event.savedWorkflows ?? []))
        return
      }
      if (event.type === 'team_roster') {
        dispatch(event)
        setTeamPanel(teamPanelFromRoster(event))
        if (pendingLocalCommandRef.current) pendingLocalCommandRef.current = null
        return
      }
      if (event.type === 'hooks_status') {
        setHooksPanel(hooksPanelFromStatus(event))
        if (pendingLocalCommandRef.current) pendingLocalCommandRef.current = null
        return
      }
      if (event.type === 'workflow_detail') {
        dispatch(event)
        setWorkflowPanel(workflowPanelFromDetail(event))
        if (pendingLocalCommandRef.current) pendingLocalCommandRef.current = null
        return
      }
      if (event.type === 'workflow_live') {
        dispatch(event)
        // Live counters are UI-only; fold them into the open panel so the
        // agent rows show the current turn/tool/token without a full refresh.
        setWorkflowPanel(panel => (panel ? workflowPanelWithLive(panel, event.runId, event.jobs) : panel))
        return
      }
      if (event.type === 'workflow_run') {
        dispatch(event)
        setWorkflowPanel(panel => panel ? workflowPanelWithRunUpdate(panel, event.run) : panel)
        if (pendingLocalCommandRef.current) pendingLocalCommandRef.current = null
        return
      }
      if (event.type === 'rewind_done') {
        const commandText = pendingLocalCommandRef.current
        setSelector(null)
        setInput(event.text)
        flushDeltas()
        // rewind 让历史变短，但 Static 不会撤销已写出的行；先整屏重置再重印，
        // 屏幕上只剩裁剪后的那一份（reducer 里 staticGeneration +1 触发重挂）。
        resetTranscriptViewport()
        dispatch(event)
        if (commandText) {
          dispatch({ type: 'local_command_input', text: commandText })
          appendLocalCommandOutput('Rewound to selected message')
        }
        return
      }
      if (event.type === 'system' && isResumeSuccessNotice(event.text)) {
        pendingLocalCommandRef.current = null
        setFooterPanel({ type: 'status', text: resumeSuccessStatusText(event.text) })
        return
      }
      if (event.type === 'system' && pendingLocalCommandRef.current) {
        appendLocalCommandOutput(event.text)
        return
      }
      if (event.type === 'assistant_delta') {
        const taskIdStr = String(event.taskId)
        pendingDeltas[taskIdStr] = (pendingDeltas[taskIdStr] || '') + event.text
        if (!throttleTimer) {
          throttleTimer = setTimeout(() => {
            throttleTimer = null
            flushDeltas()
          }, 80)
        }
        return
      }
      if (event.type === 'assistant_done') {
        // Bridge `done` is already the full assistant_text for the turn.
        // Flush any throttled deltas first, then finalize with the full text only —
        // never concatenate pending + done (that double-counts streamed content and
        // breaks stream-commit prefix stripping → "LLM Running Turn 1" appears twice).
        if (throttleTimer) {
          clearTimeout(throttleTimer)
          throttleTimer = null
        }
        flushDeltas()
        const taskIdStr = String(event.taskId)
        delete pendingDeltas[taskIdStr]
        dispatch({ type: 'assistant_done', taskId: event.taskId, text: event.text })
        return
      }
      if (event.type === 'agent_snapshot') {
        // Announce each child exactly once, keyed by the child -- NOT by its verdict.
        //
        // A child's status legitimately moves while it finishes: `running` -> `closed`
        // (process reclaimed, no work verdict yet) -> `succeeded`, and its metadata gains
        // `turnStatus: completed`. Keying on `${executionId}:${word}` treated each of
        // those as a brand new child, so one subagent left a `✗ failed` line and then a
        // `✓ completed` line beside it. The same name flipping between two verdicts is
        // what the user reported, and neither line can be trusted next to the other.
        //
        // The verdict that survives is the latest one. Announcing the *first* terminal
        // word instead would be wrong in the other direction, because the first word is
        // exactly the unreliable one -- it is taken before the turn's own verdict lands.
        let changed = false
        const byExecution = new Map<string, AgentNotice>()
        for (const notice of agentNoticesRef.current) byExecution.set(notice.executionId, notice)
        for (const record of event.snapshot.records || []) {
          const notice = agentNoticeFromRecord(record as Parameters<typeof agentNoticeFromRecord>[0])
          if (!notice) continue
          const previous = byExecution.get(notice.executionId)
          // Nothing to do when the verdict has not moved. Without this, the bridge's
          // re-emission of an unchanged snapshot would rewrite the chrome every frame.
          if (previous && previous.ok === notice.ok && previous.word === notice.word) continue
          byExecution.set(notice.executionId, notice)
          changed = true
        }
        if (changed) {
          // Oldest first, bounded: a long session must not grow the chrome forever, and
          // older announcements have already been read.
          const next = [...byExecution.values()]
          setAgentNotices(next.length > 6 ? next.slice(-6) : next)
        }
        dispatch(event)
        return
      }
      dispatch(event)
    }

    bridgeRef.current = startBridgeClient(python, bridgeScript, onEvent, code => {
      dispatch({ type: 'error', code: 'bridge_exit', message: `bridge exited: ${code ?? 'signal'}` })
    })
    return () => {
      bridgeRef.current?.stop()
      if (throttleTimer) clearTimeout(throttleTimer)
    }
  }, [bridgeScript, python, startBridgeClient])

  const clearSelection = () => {
    if (selectionRef.current) setSelection(null)
  }

  /**
   * Translate a mouse report into a selection point.
   *
   * SGR reports are 1-based and already absolute screen coordinates; nothing is
   * subtracted here. The header offset and the left gutter belong to the *rows*
   * (`framePlan` puts them there), because the transcript and the composer have
   * different gutters — folding one of them into the point is what used to make a drag
   * inside the composer resolve to the last transcript row.
   *
   * Clamping happens in `normalizeFrameSelection`, so an out-of-area drag sticks to the
   * nearest edge instead of throwing the selection away.
   */
  const selectionPointFromMouse = (x: number, y: number): FramePoint => framePointFromMouse(x, y)

  const handleMouseEvent = (event: NonNullable<ReturnType<typeof parseMouseEvent>>) => {
    if (event.kind === 'wheel') return
    const point = selectionPointFromMouse(event.x, event.y)

    if (event.kind === 'press') {
      // Middle/right click: never selects. Left button only (SGR button 0).
      if (event.button !== 0) {
        clearSelection()
        return
      }
      // "Jump to bottom" pill takes the click instead of starting a selection —
      // but only when the pill is actually on screen, which is the only time the
      // user could have aimed at it. The pill's geometry is in transcript-viewport
      // coordinates; `point` is a frame row, so subtract the rows painted above the
      // viewport (and the transcript's own gutter) before hit-testing.
      const pill = jumpToBottomRef.current
      const viewportRow = point.row - metrics.headerRows
      const viewportCol = point.col - TRANSCRIPT_LEFT_GUTTER
      if (pill && hitTestJumpToBottom(pill.placement, metrics.messageRows - 1, viewportRow, viewportCol)) {
        setTranscriptScrollOffset(0)
        clearSelection()
        return
      }
      setSelection({ anchor: point, focus: point })
      return
    }
    if (event.kind === 'drag') {
      const current = selectionRef.current
      if (!current) return
      // Motion reports for non-held buttons do not reach us (1002h only reports
      // motion while a button is down), so any drag here extends the selection.
      setSelection({ anchor: current.anchor, focus: point })
      return
    }
    // release
    const payload = copyCurrentSelection()
    if (payload === null) setSelection(null)
  }

  /**
   * 把当前选区写进剪贴板，返回复制到的文本（没有可用选区时返回 null）。
   *
   * 拖拽松开和 Ctrl+C 共用这一条路径：两处各自实现迟早会出现「拖拽复制的
   * 内容」和「Ctrl+C 复制的内容」不一致——比如一个按显示列裁、一个按字符数裁。
   *
   * 读取的是**整帧**行文本（transcript + 底栏 + composer + 提示），不是 transcript
   * 视口：Claude Code 的选区就是屏幕级坐标、从渲染后的帧里取字（`m0701`），所以
   * 一次拖拽可以横跨两个区域。
   */
  const copyCurrentSelection = (): string | null => {
    const current = selectionRef.current
    if (!current) return null
    const frame = frameRowsRef.current
    const range = normalizeFrameSelection(current.anchor, current.focus, frame)
    if (isEmptyFrameSelection(range)) return null
    const payload = selectedFrameText(range, frame)
    if (payload.length === 0) return null
    // Keep the highlight on screen so the user can see what was copied; it is
    // cleared by the next wheel notch, key press or new press.
    writeClipboardTextAsync(payload, stdout, ok => {
      if (ok) showNotice(`已复制 ${payload.split('\n').length} 行到剪贴板`)
      else showNotice('复制失败：剪贴板不可用')
    })
    return payload
  }

  const handleTerminalInput = (rawInput: string, key: InputKey) => {
    // Wheel works in every fullscreen mode; button/drag events only exist in `full`.
    if (fullscreen) {
      const mouseInput = key.sequence ?? rawInput
      const wheel = parseMouseWheel(mouseInput)
      if (wheel) {
        const { totalRows, viewportRows } = transcriptRowsRef.current
        const delta = transcriptWheelStep()
        setTranscriptScrollOffset(offset => scrollTranscriptBy(offset, wheel === 'up' ? delta : -delta, totalRows, viewportRows))
        // Scrolling moves the rows under a fixed screen position, so any existing
        // highlight would then point at different text. Drop it.
        clearSelection()
        return
      }
      const mouseEvent = parseMouseEvent(mouseInput)
      if (mouseEvent) {
        handleMouseEvent(mouseEvent)
        return
      }
    }
    if (fullscreen && key.pageUp) {
      const { totalRows, viewportRows } = transcriptRowsRef.current
      setTranscriptScrollOffset(offset => scrollTranscriptBy(offset, transcriptScrollStep(viewportRows), totalRows, viewportRows))
      return
    }
    if (fullscreen && key.pageDown) {
      const { totalRows, viewportRows } = transcriptRowsRef.current
      setTranscriptScrollOffset(offset => scrollTranscriptBy(offset, -transcriptScrollStep(viewportRows), totalRows, viewportRows))
      return
    }
    // Jump to bottom (Claude Code parity). The alternate screen has no terminal
    // scrollback, so after paging up there must be a one-key way back to the live
    // tail: bare End, Ctrl+End, or Shift+End (terminals spell these differently).
    if (fullscreen && key.end) {
      setTranscriptScrollOffset(0)
      return
    }
    if (key.ctrl && (rawInput === 'c' || rawInput === '\u0003')) {
      // Ctrl+C has two meanings, matching Claude Code 319:
      //   - with a selection → copy it (m2124:8688 swallows the event with
      //     `return !0`, so `app:interrupt` never fires);
      //   - without one      → interrupt, but ask for confirmation first (`m1072`
      //     pending state + "press ctrl+c again to exit").
      const decision = decideInterrupt({
        hasSelection: Boolean(selectionRef.current),
        armedAt: interruptArmedAtRef.current,
        now: Date.now(),
      })
      if (decision.kind === 'copy-selection') {
        copyCurrentSelection()
        return
      }
      if (decision.kind === 'arm') {
        interruptArmedAtRef.current = Date.now()
        showNotice(interruptConfirmHint())
        return
      }
      interruptArmedAtRef.current = null
      bridgeRef.current?.send({ type: 'shutdown' })
      exitCleanly()
      return
    }
    if (key.ctrl && (rawInput === 'o' || rawInput === '\u000f')) {
      // Expanding/collapsing tools changes rows already emitted through
      // <Static>. Static only appends, so an in-place state flip leaves old
      // rows in scrollback and skips/duplicates rows. Reset once and remount.
      resetTranscriptViewport()
      resetStaticTranscriptOutput()
      setExpandedTools(value => !value)
      return
    }
    // A passive goal nudge is dismissed with Esc when idle (never blocks stop).
    if (key.escape && state.goalNudge && (state.status === 'idle') && input.trim() === '') {
      dispatch({ type: 'goal_nudge', text: '' })
      return
    }
    // Any keypress drops a finished selection highlight. It is a copy result, not
    // a mode: holding it on screen while the user types would leave inverted text
    // scattered through the transcript. Escape with a selection present is
    // consumed here so it clears the highlight instead of also cancelling work.
    if (selectionRef.current && (key.escape || rawInput.length > 0 || key.return || key.backspace || key.delete)) {
      clearSelection()
      if (key.escape) return
    }
    if (footerPanel) {
      if (key.escape) {
        setFooterPanel(null)
        dismissPendingLocalCommand()
        return
      }
      if (rawInput || key.return || key.backspace || key.delete || key.upArrow || key.downArrow) {
        setFooterPanel(null)
        dismissPendingLocalCommand()
      }
    }
    if (mcpPanel) {
      if (key.escape) {
        mcpPanelOpenRef.current = false
        setMcpPanel(null)
        dismissPendingLocalCommand()
        return
      }
      if (key.upArrow) {
        setMcpPanel(panel => panel ? moveMcpSelection(panel, -1) : panel)
        return
      }
      if (key.downArrow) {
        setMcpPanel(panel => panel ? moveMcpSelection(panel, 1) : panel)
        return
      }
    }
    if (workflowPanel) {
      const decision = workflowPanelCommandForKey(workflowPanel, key, rawInput)
      if (decision?.panel) {
        setWorkflowPanel(decision.panel)
        return
      }
      if (decision?.command) {
        bridgeRef.current?.send(decision.command)
        return
      }
      if (key.escape) {
        setWorkflowPanel(null)
        dismissPendingLocalCommand()
        return
      }
      // The overlay owns its keys: an unhandled `r`/`x`/`j` must not be typed
      // into the composer, which would then disable the status-bar shortcut
      // (it only runs while the composer is empty).
      if (workflowPanelCapturesKey(workflowPanel, key, rawInput)) return
    }
    if (hooksPanel) {
      if (key.escape) {
        setHooksPanel(null)
        return
      }
      const decision = hooksPanelCommandForKey(hooksPanel, key, rawInput)
      if (decision?.panel) {
        setHooksPanel(decision.panel)
        return
      }
      if (decision?.command) {
        bridgeRef.current?.send(decision.command)
        return
      }
      if (hooksPanelCapturesKey(hooksPanel, key, rawInput)) return
    }
    if (teamPanel) {
      const decision = teamPanelCommandForKey(teamPanel, key, rawInput)
      if (decision?.panel) {
        setTeamPanel(decision.panel)
        return
      }
      if (decision?.command) {
        bridgeRef.current?.send(decision.command)
        return
      }
      if (key.escape) {
        setTeamPanel(null)
        dismissPendingLocalCommand()
        return
      }
      if (teamPanelCapturesKey(teamPanel, key, rawInput)) return
    }
    if (modelPanel) {
      if (key.escape) {
        if (modelPanel.reasoning) {
          setModelPanel(panel => panel ? { ...panel, reasoning: null } : panel)
        } else {
          modelPanelOpenRef.current = false
          setModelPanel(null)
        }
        dismissPendingLocalCommand()
        return
      }
      if (modelPanel.reasoning) {
        const efforts = modelPanel.reasoning.model.reasoningEfforts ?? []
        if (key.upArrow) {
          setModelPanel(panel => panel?.reasoning
            ? {
                ...panel,
                reasoning: {
                  ...panel.reasoning,
                  selected: moveReasoningSelection(panel.reasoning.selected, -1, efforts.length),
                },
              }
            : panel)
          return
        }
        if (key.downArrow) {
          setModelPanel(panel => panel?.reasoning
            ? {
                ...panel,
                reasoning: {
                  ...panel.reasoning,
                  selected: moveReasoningSelection(panel.reasoning.selected, 1, efforts.length),
                },
              }
            : panel)
          return
        }
        if (key.return) {
          const effort = efforts[modelPanel.reasoning.selected]
          if (effort) {
            bridgeRef.current?.send({
              type: 'model_switch',
              selector: String(modelPanel.reasoning.model.index),
              reasoningEffort: effort,
            })
          }
          modelPanelOpenRef.current = false
          modelPanelPendingRef.current = false
          setModelPanel(null)
          return
        }
        return
      }
      if (key.upArrow) {
        setModelPanel(panel => panel ? { ...panel, selected: moveModelSelection(panel.selected, -1, panel.models.length) } : panel)
        return
      }
      if (key.downArrow) {
        setModelPanel(panel => panel ? { ...panel, selected: moveModelSelection(panel.selected, 1, panel.models.length) } : panel)
        return
      }
      if (key.return) {
        const selected = modelPanel.models[modelPanel.selected]
        if (selected) {
          if (shouldOpenReasoningPanel(selected)) {
            setModelPanel(panel => panel ? openReasoningPanel(panel, selected) : panel)
            return
          }
          bridgeRef.current?.send({ type: 'model_switch', selector: String(selected.index) })
        }
        modelPanelOpenRef.current = false
        modelPanelPendingRef.current = false
        setModelPanel(null)
        return
      }
    }
    // 目标提案审批 overlay（与工具审批互斥；任一打开都抢焦点）
    if (goalProposalPanel) {
      if (key.escape) {
        const action = goalProposalOnEscape(goalProposalPanel)
        bridgeRef.current?.send({
          type: 'goal_proposal_response',
          requestId: action.requestId,
          decision: action.decision,
        })
        setGoalProposalPanel(null)
        return
      }
      if (key.upArrow) {
        setGoalProposalPanel(panel => panel ? { ...panel, selected: moveGoalProposalSelection(panel.selected, -1) } : panel)
        return
      }
      if (key.downArrow) {
        setGoalProposalPanel(panel => panel ? { ...panel, selected: moveGoalProposalSelection(panel.selected, 1) } : panel)
        return
      }
      if (key.return) {
        const action = goalProposalOnEnter(goalProposalPanel)
        bridgeRef.current?.send({
          type: 'goal_proposal_response',
          requestId: action.requestId,
          decision: action.decision,
        })
        setGoalProposalPanel(null)
        return
      }
    }
    // 工具审批 overlay 优先于切档面板（安全关键路径抢焦点）
    if (approvalPanel) {
      if (key.escape) {
        const action = approvalPanelOnEscape(approvalPanel)
        if (action.type === 'respond') {
          bridgeRef.current?.send({
            type: 'permission_response',
            requestId: action.requestId,
            decision: action.decision,
          })
          setApprovalPanel(action.next)
        }
        return
      }
      if (key.upArrow) {
        setApprovalPanel(panel => panel ? { ...panel, selected: moveApprovalSelection(panel.selected, -1) } : panel)
        return
      }
      if (key.downArrow) {
        setApprovalPanel(panel => panel ? { ...panel, selected: moveApprovalSelection(panel.selected, 1) } : panel)
        return
      }
      if (key.return) {
        const action = approvalPanelOnEnter(approvalPanel)
        if (action.type === 'respond') {
          bridgeRef.current?.send({
            type: 'permission_response',
            requestId: action.requestId,
            decision: action.decision,
          })
          setApprovalPanel(action.next)
        }
        return
      }
    }
    if (permissionPanel) {
      if (key.escape) {
        // 确认态：退回列表；列表态：关闭面板（permissionPanelOnEscape 返回 null）
        const next = permissionPanelOnEscape(permissionPanel)
        if (next === null) {
          permissionPanelOpenRef.current = false
          dismissPendingLocalCommand()
        }
        setPermissionPanel(next)
        return
      }
      if (key.upArrow) {
        // 确认态不移动光标（只有 Enter 确认 / Esc 退回）
        if (permissionPanel.confirming) return
        setPermissionPanel(panel => panel ? { ...panel, selected: movePermissionSelection(panel.selected, -1, panel.options.length) } : panel)
        return
      }
      if (key.downArrow) {
        if (permissionPanel.confirming) return
        setPermissionPanel(panel => panel ? { ...panel, selected: movePermissionSelection(panel.selected, 1, panel.options.length) } : panel)
        return
      }
      if (key.return) {
        const action = permissionPanelOnEnter(permissionPanel)
        if (action.type === 'confirm') {
          // 切到 Full Access：进入二次确认态，不关面板
          setPermissionPanel(panel => panel ? { ...panel, confirming: action.mode } : panel)
          return
        }
        if (action.type === 'apply') {
          bridgeRef.current?.send({ type: 'set_permission_mode', mode: action.mode as PermissionMode })
        }
        // apply / noop 都收起面板
        permissionPanelOpenRef.current = false
        permissionPanelPendingRef.current = false
        setPermissionPanel(null)
        return
      }
    }
    if (themePanelSelected !== null) {
      if (key.escape) {
        setThemePanelSelected(null)
        dismissPendingLocalCommand()
        return
      }
      if (key.upArrow) {
        setThemePanelSelected(selected => moveThemeSelection(selected ?? 0, -1))
        return
      }
      if (key.downArrow) {
        setThemePanelSelected(selected => moveThemeSelection(selected ?? 0, 1))
        return
      }
      if (key.return) {
        const selectedTheme = INK_THEME_NAMES[themePanelSelected] ?? 'default'
        setThemeName(selectedTheme)
        setThemePanelSelected(null)
        appendLocalCommandOutput(`Theme set to ${selectedTheme}`)
        return
      }
    }
    if (selector) {
      const decision = handleSelectorInput(selector, key)
      setSelector(decision.selector)
      if (!decision.selector && selector.mode === 'resume') {
        resumePendingRef.current = false
      }
      if (!decision.selector && !decision.command && decision.input === undefined) {
        dismissPendingLocalCommand()
      }
      if (decision.command) {
        bridgeRef.current?.send(decision.command)
      }
      if (decision.input !== undefined) {
        setInput(decision.input)
        setCursorOffset(decision.input.length)
      }
      return
    }
    if (slashItems.length > 0) {
      if (key.upArrow) {
        setSlashSelected(selected => moveSlashSelection(selected, -1, slashItems))
        return
      }
      if (key.downArrow) {
        setSlashSelected(selected => moveSlashSelection(selected, 1, slashItems))
        return
      }
      if ((key as { tab?: boolean }).tab || (key.return && !key.ctrl && !key.meta && !key.shift)) {
        const selectedCommand = slashItems[slashSelected] ?? slashItems[0]
        const trigger = (key as { tab?: boolean }).tab ? 'tab' : 'enter'
        const action = slashSelectionAction(input, selectedCommand, trigger)
        if (action.type === 'complete') {
          if (trigger === 'tab' || shouldCompleteSlashCommand(input, selectedCommand)) {
            setInput(action.value)
            setCursorOffset(action.value.length)
            return
          }
        } else {
          applyInputDecision(handleInput(action.value, '', { return: true }, state.status, pasteStore, skillNames, undefined, imageStore))
          return
        }
      }
      if (key.escape) {
        setInput('')
        setCursorOffset(0)
        return
      }
    }
    if (showWorkflowStatusBar && workflowStatusBar) {
      const command = workflowStatusBarCommandForKey(workflowStatusBar, key, rawInput)
      if (command) {
        // The status bar is a background-workflow entry point, so it must stay
        // reachable while the run is in flight:
        //  - empty composer: Enter opens the panel, `x` stops the run (as before);
        //  - composer has a draft AND a turn is running/stopping: Enter would be a
        //    no-op ("keeps draft"), so use it to open the panel instead of doing
        //    nothing;
        //  - composer has a draft and the session is idle: Enter submits the draft,
        //    and single-letter keys are ordinary text — never steal them.
        const statusBusy = state.status === 'running' || state.status === 'stopping'
        if (input.trim() === '' || (key.return && statusBusy)) {
          bridgeRef.current?.send(command)
          return
        }
      }
    }
    if (key.upArrow) {
      const result = previousInput(inputHistory, input)
      setInputHistory(result.history)
      setInput(result.value)
      setCursorOffset(result.value.length)
      return
    }
    if (key.downArrow) {
      const result = nextInput(inputHistory, input)
      setInputHistory(result.history)
      setInput(result.value)
      setCursorOffset(result.value.length)
      return
    }
    // 剪贴板回退去重：终端若同时把同一段文本以 bracketed paste 送进来，会在 150ms 内
    // 再插入一次。这里丢弃与「刚回退插入的文本」相同（或互为包含）的那次 paste。
    const fallback = clipboardFallbackRef.current
    if (fallback && rawInput && Date.now() - fallback.at <= 150) {
      const pasted = rawInput.replace(/\r\n/g, '\n')
      if (pasted === fallback.text || fallback.text.includes(pasted) || pasted.includes(fallback.text)) {
        clipboardFallbackRef.current = null
        return
      }
    }
    // 用同步快照而非 React state：同一 tick 内二次 stdin 不能再读到已提交的 `/stop`。
    const snapshot = inputSnapshotRef.current
    const decision = handleInput(
      snapshot.value,
      rawInput,
      key,
      state.status,
      pasteStore,
      skillNames,
      snapshot.cursorOffset,
      imageStore,
    )
    applyInputDecision(decision)
  }
  terminalInputHandlerRef.current = handleTerminalInput

  useEffect(() => {
    // Ink can render a final diagnostic frame in a non-interactive process
    // (pipes, CI, `timeout < ga`); only a real TTY may enter raw mode.
    if (!isRawModeSupported) return
    setRawMode(true)
    const handleData = (data: Buffer | string) => {
      const currentStdinAt = Date.now()
      if (currentStdinAt - lastStdinAtRef.current > STDIN_RESUME_GAP_MS) {
        reassertMouseTracking(stdout, mouseMode)
      }
      lastStdinAtRef.current = currentStdinAt
      const parsed = parseTerminalInput(String(data))
      terminalInputHandlerRef.current?.(parsed.rawInput, parsed.key)
    }
    internal_eventEmitter.on('input', handleData)
    return () => {
      internal_eventEmitter.removeListener('input', handleData)
      setRawMode(false)
    }
  }, [internal_eventEmitter, isRawModeSupported, mouseMode, setRawMode, stdout])

  const statusColor = state.status === 'running' ? 'yellow' : state.status === 'idle' ? 'green' : 'gray'
  const columns = terminalSize.columns
  const canvasColumns = terminalCanvasColumns(columns)
  const activePanel = mcpPanel || modelPanel || permissionPanel || approvalPanel || goalProposalPanel || themePanelSelected !== null || selector || footerPanel || workflowPanel || teamPanel || hooksPanel
  const mcpStartupRows = mcpStartupStatus ? mcpStartupStatusRows(mcpStartupStatus) : []
  const showMcpStartupStatus = mcpStartupRows.length > 0 && !activePanel && slashItems.length === 0
  // Reserve the MCP banner row from the very first frame, even before any status
  // arrives — but only in fullscreen mode. The banner shows up ~4s into startup
  // (background discovery); if it claimed its row only then, the bottom chrome would
  // grow by one row and push the composer up mid-session. In fullscreen the composer
  // must never move, so the row is permanently reserved.
  //
  // Inline mode keeps the old behaviour: there the composer already moves with
  // output, and a reserved blank row would just be a permanent empty line.
  const mcpBannerReserved = fullscreen && !activePanel && slashItems.length === 0
  const mcpBannerRows = mcpBannerReserved ? Math.max(1, mcpStartupRows.length) : mcpStartupRows.length
  const workflowStatusBar = useMemo(() => workflowStatusBarFromState(state), [state])
  const showWorkflowStatusBar = Boolean(workflowStatusBar) && !activePanel && slashItems.length === 0
  const workflowStatusRows = workflowStatusBar ? workflowStatusBarRows(workflowStatusBar) : []
  const goalCardLinesOut = goalCardLines(state.goalMetadata, now)
  const goalTerminalLinesOut = state.status === 'running' || state.status === 'stopping' ? [] : goalTerminalLines(state.goalTerminal)
  const showGoalCard = (goalCardLinesOut.length > 0 || goalTerminalLinesOut.length > 0) && !activePanel && slashItems.length === 0
  // When a panel (workflow/team/...) or the slash-completion popup owns the
  // bottom band, the full goal card is hidden. Collapse it to a single line so
  // a running OR just-reached goal is never lost behind another surface. See
  // goalCompactLine() for the CC /goal (dialog) comparison.
  const compactGoalLine = !showGoalCard
    ? goalCompactLine(state.goalMetadata, goalTerminalLinesOut.length > 0 ? state.goalTerminal : null, now)
    : null
  const goalRows = showGoalCard
    ? goalCardLinesOut.length + goalTerminalLinesOut.length
    : compactGoalLine ? 1 : 0
  const promptViewport = useMemo(() => inputViewport(input, {
    columns: inputContentColumns(canvasColumns),
    maxRows: 6,
    cursorOffset,
  }), [canvasColumns, cursorOffset, input])
  const inputRows = promptViewport.lines.length
  const errorRows = state.error ? 1 : 0
  const hasActivity = showWorkflowStatusBar || shouldShowActivityStatus(state.status, runningStartedAt !== null, state.tokenUsage, completedTurnActivity !== null)
  const workflowPanelLines = useMemo(
    () => (workflowPanel ? workflowPanelRows(workflowPanel).slice(0, WORKFLOW_PANEL_MAX_ROWS) : []),
    [workflowPanel],
  )
  const teamPanelRows = useMemo(
    () => (teamPanel ? teamPanelViewport(teamPanelLines(teamPanel), TEAM_PANEL_MAX_ROWS) : []),
    [teamPanel],
  )
  const hooksPanelRows = useMemo(
    () => (hooksPanel ? hooksPanelLines(hooksPanel).slice(0, HOOKS_PANEL_MAX_ROWS) : []),
    [hooksPanel],
  )
  const panelRows = goalProposalPanel
    ? goalProposalRows(goalProposalPanel)
    : approvalPanel
    ? approvalPanelRows(approvalPanel)
    : modelPanel
    ? modelPanelRows(modelPanel)
    : permissionPanel
      ? permissionPanelRows(permissionPanel)
    : mcpPanel
      ? mcpPanelRows(mcpPanel)
    : themePanelSelected !== null
      ? themePanelRows()
    : selector
      ? Math.min(selectorSize(selector), 8) + 2
    : workflowPanel
      ? workflowPanelLines.length
    : teamPanel
      ? teamPanelRows.length
    : hooksPanel
      ? hooksPanelRows.length
    : footerPanel
      ? footerPanelRows(footerPanel)
    : showMcpStartupStatus || mcpBannerReserved
      ? mcpBannerRows
    : slashItems.length > 0
      ? visibleSlashSuggestions(slashItems, slashSelected).items.length + 1
      : 0
  const metrics = computeLayoutMetrics({
    rows: terminalSize.rows,
    columns,
    hasActivity,
    hasError: Boolean(state.error),
    hasPanel: Boolean(activePanel) || showMcpStartupStatus || mcpBannerReserved,
    hasSlashSuggestions: slashItems.length > 0,
    inputRows,
    panelRows,
    goalRows,
    noticeRows: agentNotices.length,
    // Inline scrollback: no title/status header. A live "GenericAgent running"
    // band sits after <Static> and looks jammed into the middle of output.
    // Full mouse mode keeps a 1-row header (single viewport, no Static).
    headerRows: fullscreen ? 1 : 0,
  })
  const mcpServerRowCapacity = mcpPanel
    ? Math.max(1, Math.min(
      mcpPanel.servers.length,
      metrics.bottomRows - (3 + inputRows) - 1 - errorRows - mcpPanelRows(mcpPanel, 0),
    ))
    : 0
  const keepLatestTaskActive = state.status === 'running' || state.status === 'stopping'
  const messagePartition = useMemo(() => (
    splitStaticAndActiveMessages(state.messages, { keepLatestTaskActive })
  ), [keepLatestTaskActive, state.messages])

  const staticTranscript = useMemo(() => (
    fullscreen
      ? { rows: [], channel: null }
      : (() => {
          const rendered = transcriptLinesWithChannel(messagePartition.staticMessages, { expandedTools, theme })
          return {
            rows: wrapTranscriptLines(rendered.lines, transcriptContentColumns(metrics.canvasColumns)),
            channel: rendered.channel,
          }
        })()
  ), [expandedTools, messagePartition.staticMessages, metrics.canvasColumns, fullscreen, theme])
  const staticTranscriptRows = staticTranscript.rows
  const activeTranscriptRows = useMemo(() => (
    fullscreen
      ? []
      : wrapTranscriptLines(transcriptLinesWithChannel(messagePartition.activeMessages, {
          expandedTools,
          theme,
          initialChannel: staticTranscript.channel,
        }).lines, transcriptContentColumns(metrics.canvasColumns))
  ), [expandedTools, messagePartition.activeMessages, metrics.canvasColumns, fullscreen, staticTranscript.channel, theme])
  const inlineMessageViewportPlan = useMemo(() => (
    planMessageViewport({
      hasStaticMessages: staticTranscriptRows.length > 0,
      liveLineCount: activeTranscriptRows.length,
      messageRows: metrics.messageRows,
      // In the real UI the live band should use the terminal's available message
      // area. The 12-row default is only a headless/legacy fallback now; keeping
      // it here caused long tool args to be tail-clipped during the first turn.
      maxLiveRows: metrics.messageRows,
      keepLivePlaceholder: keepLatestTaskActive && activeTranscriptRows.length === 0,
    })
  ), [activeTranscriptRows.length, keepLatestTaskActive, metrics.messageRows, staticTranscriptRows.length])
  const inlineMessageRows = inlineMessageViewportPlan.kind === 'none'
    ? 0
    : inlineMessageViewportPlan.height
  const messageRowsForCursor = fullscreen ? metrics.messageRows : inlineMessageRows
  const liveTranscriptRows = useMemo(() => (
    inlineMessageViewportPlan.kind === 'live'
      ? liveTranscriptViewportLines(activeTranscriptRows, inlineMessageViewportPlan.height)
      : []
  ), [activeTranscriptRows, inlineMessageViewportPlan])

  const transcriptRows = useMemo(() => (
    wrapTranscriptLines(transcriptLines(state.messages, { expandedTools, theme }), transcriptContentColumns(metrics.canvasColumns))
  ), [state.messages, expandedTools, metrics.canvasColumns, theme])
  const previousTranscriptTotalRows = transcriptRowsRef.current.totalRows
  const historyReplacementScrollOffset = fullscreen && pendingHistoryReplacementScrollRef.current
    ? scrollOffsetForHistoryReplacement(transcriptRows.length, metrics.messageRows)
    : null
  const effectiveTranscriptScrollOffset = historyReplacementScrollOffset ?? preserveTranscriptScrollOnContentChange(
    transcriptScrollOffset,
    previousTranscriptTotalRows,
    transcriptRows.length,
    metrics.messageRows,
  )
  const visibleTranscript = useMemo(() => (
    visibleTranscriptLines(transcriptRows, { maxRows: metrics.messageRows, scrollOffset: effectiveTranscriptScrollOffset })
  ), [transcriptRows, metrics.messageRows, effectiveTranscriptScrollOffset])
  // "Jump to bottom" pill. Shown only once the view has left the tail: while
  // pinned, the newest row is already on screen and the pill would just be a
  // floating box over live output.
  //
  // The count is in *messages*, not wrapped rows. Counting `totalRows` deltas was
  // wrong by roughly 4x: one turn renders as 4+ rows (a 问答 pair, plus any tool
  // lines), so "3 new messages" showed up as "12". Claude Code counts assistant
  // *segments* (`m2064` `$C`: walk the messages, skip progress and empty
  // assistant parts, and increment only at the start of a run of assistant
  // messages), which is the unit a user means by "messages".
  const scrollPillAnchorRef = useRef<number | null>(null)
  if (visibleTranscript.scrollOffset <= 0) {
    scrollPillAnchorRef.current = null
  } else if (scrollPillAnchorRef.current === null) {
    scrollPillAnchorRef.current = state.messages.length
  }
  const newMessagesWhileScrolledUp = scrollPillAnchorRef.current === null
    ? 0
    : countNewMessages(state.messages, scrollPillAnchorRef.current)
  const jumpToBottom = useMemo(() => {
    if (!fullscreen || visibleTranscript.scrollOffset <= 0) return null
    const contentColumns = Math.max(1, metrics.canvasColumns - TRANSCRIPT_LEFT_GUTTER - 1)
    const text = jumpToBottomText({
      count: newMessagesWhileScrolledUp,
      availableColumns: Math.max(0, contentColumns - 2),
      clickable: mouseMode !== 'off',
    })
    return { text, placement: jumpToBottomPlacement(text, contentColumns) }
  }, [fullscreen, visibleTranscript.scrollOffset, newMessagesWhileScrolledUp, metrics.canvasColumns, mouseMode])
  const jumpToBottomRef = useRef(jumpToBottom)
  jumpToBottomRef.current = jumpToBottom
  // Plain text of each visible row, in the same order and count as the rendered
  // lines. The frame model is assembled from this, so the highlight can never
  // drift from what is actually on screen.
  const visibleTranscriptText = useMemo(
    () => visibleTranscript.lines.map(line => line.text),
    [visibleTranscript],
  )
  transcriptRowsRef.current = fullscreen
    ? { totalRows: visibleTranscript.totalRows, viewportRows: metrics.messageRows }
    : { totalRows: activeTranscriptRows.length, viewportRows: Math.max(1, inlineMessageRows) }
  // Slash/panel render *below* the input (Codex popup under composer), so they
  // must not push the IME caret row. panelRows stays 0 for cursor math; total
  // bottomRows / clear geometry still include panel height via metrics.
  const inputCursor = inputCursorPosition({
    headerRows: metrics.headerRows,
    messageRows: messageRowsForCursor,
    activityRows: 1 + goalRows,
    errorRows,
    panelRows: 0,
    hintRows: 0,
    inputBorderTopRows: 1,
    inputPaddingLeftColumns: inputLeftPaddingColumns,
    inputGutterColumns,
    inputCursorLine: promptViewport.cursorLine,
    inputCursorColumn: promptViewport.cursorColumn,
  })
  const liveViewportRows = fullscreen ? metrics.rows : metrics.headerRows + messageRowsForCursor + metrics.bottomRows
  liveViewportGeometryRef.current = {
    rows: liveViewportRows,
    cursorRow: Math.min(inputCursor.row, Math.max(0, liveViewportRows - 1)),
  }

  /**
   * 整帧行模型：选区的唯一依据。
   *
   * composer 的行号**不在这里重算**，而是取 `inputCursor` 同一份锚点——caret 每帧都由
   * 它摆放，也就是用户肉眼验过的那一个几何量。用 header/band 常量自己推一遍，会让本
   * 模块「按自己的算式正确」，哪怕绘制出来的布局已经不一致——那正是本仓库踩过的假通过
   * 形状（探针复算产品公式）。
   *
   * band（goal 卡 / 活动行 / 子代理通知）只填空白占位：它的**行数**由锚点决定
   * （transcript 底 → composer 顶之间的所有行），所以 band 里到底画了什么文字不影响
   * composer 的地址。transcript 的行号是精确的（它是紧接 header 的固定高度视口）。
   */
  const frameRows = useMemo(() => {
    const composerRow = Math.max(0, inputCursor.row - promptViewport.cursorLine)
    const composerLines = promptViewport.lines.map(line => ({ gutter: line.gutter, text: line.text }))
    return buildFramePlan({
      canvasColumns: metrics.canvasColumns,
      rows: liveViewportRows,
      headerRows: metrics.headerRows,
      header: metrics.headerRows > 0 ? `GenericAgent   ${state.status}` : null,
      transcriptRows: metrics.messageRows,
      transcriptLines: visibleTranscriptText,
      composerRow,
      composerLines,
    })
  }, [
    inputCursor.row,
    liveViewportRows,
    metrics.canvasColumns,
    metrics.headerRows,
    metrics.messageRows,
    promptViewport.cursorLine,
    promptViewport.lines,
    state.status,
    visibleTranscriptText,
  ])
  frameRowsRef.current = frameRows

  /**
   * 选区在整帧上的归一化范围。
   *
   * `frameSelectionSpans` 返回与**整帧**等长的稀疏数组，索引即绝对屏幕行号；
   * transcript 与 composer 各自按自己的偏移去取，两边看到的是同一份选区，
   * 不会出现「transcript 高亮了、输入框没高亮」。
   */
  const selectionRange = useMemo<FrameSelectionRange | null>(() => (
    selection ? normalizeFrameSelection(selection.anchor, selection.focus, frameRows) : null
  ), [selection, frameRows])
  const frameRowSpans = useMemo(
    () => frameSelectionSpans(selectionRange, frameRows),
    [selectionRange, frameRows],
  )
  // The transcript viewport starts under the header, and its rows are the first
  // `messageRows` entries of the frame.
  const selectionRowSpans = useMemo(
    () => frameRowSpans.slice(metrics.headerRows, metrics.headerRows + metrics.messageRows),
    [frameRowSpans, metrics.headerRows, metrics.messageRows],
  )
  // The composer's own slice: absolute rows -> the `InputView` line indexes it paints.
  const composerSelectionSpans = useMemo(() => {
    const start = Math.max(0, inputCursor.row - promptViewport.cursorLine)
    return frameRowSpans.slice(start, start + promptViewport.lines.length)
  }, [frameRowSpans, inputCursor.row, promptViewport.cursorLine, promptViewport.lines.length])

  // NOTE: Phase-3 raw ANSI insert_history sequences must NOT be written while Ink owns
  // stdout. Doing so (DECSTBM + RI) races Ink redraws and produces duplicated headers /
  // stacked composers (see 截图/新bug.png). Geometry helpers remain in insertHistory.ts
  // for unit tests and a future non-Ink or coordinated renderer path.

  // 路径 A：不再旁路 stdout.write(CUP)。改为每次渲染把 caret 相对帧底的位置声明给
  // cursorPark 包裹流（相当于 CC 的 cursorDeclarationRef），由包裹流在 ink 写完帧后
  // 用相对移动定位光标、写下一帧前相对复位回帧底。运行中 / 未就绪 → null（不 park，
  // 光标留在帧底，与 ink 默认一致）。geometry 计算沿用现有 inputCursor。
  useLayoutEffect(() => {
    if (!cursorPark) return
    // Selection panels live below the composer. Keep their native cursor
    // hidden: the input already draws its inverse caret, while parking the
    // terminal cursor can leave a second visible cursor over the panel title.
    //
    // A running turn is NOT a reason to hide it. CC keeps the native cursor on the
    // composer's caret for the whole turn, and that is what makes the composer read
    // as usable while the agent works. Hiding it during a run was reported as "the
    // app looks dead, I cannot tell whether I can type".
    const inactive = !terminalReady || Boolean(activePanel)
    if (inactive) {
      cursorPark.setPark(null)
      return
    }
    const up = Math.max(0, liveViewportRows - inputCursor.row)
    cursorPark.setPark({ up, col: inputCursor.column })
  }, [activePanel, cursorPark, inputCursor.column, inputCursor.row, liveViewportRows, state.status, terminalReady])

  useEffect(() => {
    if (!fullscreen) {
      pendingHistoryReplacementScrollRef.current = false
      return
    }
    if (!pendingHistoryReplacementScrollRef.current) return
    pendingHistoryReplacementScrollRef.current = false
    setTranscriptScrollOffset(historyReplacementScrollOffset ?? scrollOffsetForHistoryReplacement(visibleTranscript.totalRows, metrics.messageRows))
  }, [historyReplacementScrollOffset, metrics.messageRows, mouseMode, visibleTranscript.totalRows])

  useEffect(() => {
    if (!fullscreen) return
    if (transcriptScrollOffset !== visibleTranscript.scrollOffset) {
      setTranscriptScrollOffset(visibleTranscript.scrollOffset)
    }
  }, [mouseMode, transcriptScrollOffset, visibleTranscript.scrollOffset])

  const inputHint = queuedPrompts.length > 0
    ? `Queued ${queuedPrompts.length} message${queuedPrompts.length > 1 ? 's' : ''} · sends at the next tool boundary · Esc stop`
    : (state.status === 'running' || state.status === 'stopping')
      ? 'Running · type to queue · Enter queues · PgUp/PgDn · Ctrl+O tools · Esc stop'
      : 'Enter send · Alt+Enter newline · PgUp/PgDn · Ctrl+O tools · Ctrl+C exit'
  const runningSeconds = runningStartedAt === null ? 0 : Math.floor((now - runningStartedAt) / 1000)
  const activitySeconds = runningStartedAt === null ? lastActivitySeconds : runningSeconds
  const inputSections = inputChromeSections({
    hasError: Boolean(state.error),
    hasPanel: Boolean(activePanel),
    hasSlashSuggestions: slashItems.length > 0,
    hasMcpStatus: showMcpStartupStatus || mcpBannerReserved,
    hasAboveInputStatus: footerPanel?.type === 'status',
  })
  const renderInputSection = (section: InputChromeSection) => {
    if (section === 'error') return state.error ? <Box key={section} paddingX={1}><Text color={theme.error} wrap="truncate-end">{state.error}</Text></Box> : null
    // Subagent announcements sit above the hint so a finishing child is the last thing
    // the eye lands on before the prompt. Green + bold on purpose: a completion is the
    // one event worth catching at a glance while the agent is busy working.
    if (section === 'hint' && agentNotices.length > 0) {
      return (
        <Box key={section} flexDirection="column" paddingX={1}>
          {agentNotices.map(item => React.cloneElement(
            agentNoticeLine(item, theme.success) as React.ReactElement,
            { key: item.executionId },
          ))}
        </Box>
      )
    }
    if (section === 'hint') return <Box key={section} paddingX={1}><Text color={notice ? theme.warning : state.goalNudge ? theme.accent : theme.muted} wrap="truncate-end">{notice ?? (footerPanel?.type === 'status' ? footerPanel.text : (contextHint ?? (state.goalNudge ? `✦ ${state.goalNudge}` : inputHint)))}</Text></Box>
    // The caret is drawn whatever the turn status is: while a turn runs the composer
    // still accepts text (that text is queued), so hiding the caret removed the only
    // visual answer to "can I type right now?".
    if (section === 'input') return <InputView key={section} viewport={promptViewport} showCursor={true} columns={metrics.canvasColumns} theme={theme} selection={composerSelectionSpans} />
    if (section === 'mcpStatus') return <McpStartupStatusView key={section} rows={mcpStartupRows} loading={Boolean(mcpStartupStatus?.loading)} theme={theme} height={panelRows > 0 ? panelRows : undefined} />
    if (section === 'panel') {
      if (goalProposalPanel) return <GoalProposalPanelView key={section} panel={goalProposalPanel} theme={theme} />
      if (approvalPanel) return <ApprovalPanelView key={section} panel={approvalPanel} theme={theme} />
      if (mcpPanel) return <McpPanelView key={section} panel={mcpPanel} theme={theme} maxServerRows={mcpServerRowCapacity} />
      if (modelPanel) return <ModelPanelView key={section} panel={modelPanel} theme={theme} />
      if (permissionPanel) return <PermissionPanelView key={section} panel={permissionPanel} theme={theme} />
      if (themePanelSelected !== null) return <ThemePanelView key={section} selected={themePanelSelected} currentTheme={themeName} theme={theme} />
      if (selector) return <SelectorView key={section} selector={selector} theme={theme} />
      if (workflowPanel) return <WorkflowPanelView key={section} lines={workflowPanelLines} theme={theme} />
      if (teamPanel) return <WorkflowPanelView key={section} lines={teamPanelRows} theme={theme} />
      if (hooksPanel) return <WorkflowPanelView key={section} lines={hooksPanelRows} theme={theme} />
      if (footerPanel && footerPanel.type !== 'status') return <FooterPanelView key={section} panel={footerPanel} theme={theme} />
      return null
    }
    return slashItems.length > 0 ? <SlashSuggestionsView key={section} suggestions={slashItems} selected={slashSelected} theme={theme} /> : null
  }
  const renderMessageViewport = () => {
    if (fullscreen) {
      return (
        <MessageViewport
          height={metrics.messageRows}
          columns={metrics.canvasColumns}
          lines={visibleTranscript.lines}
          ready={false}
          readyLabel=" "
          totalRows={visibleTranscript.totalRows}
          scrollOffset={visibleTranscript.scrollOffset}
          selection={selectionRowSpans}
          jumpToBottom={jumpToBottom}
          theme={theme}
        />
      )
    }
    if (inlineMessageViewportPlan.kind === 'none') return null
    if (inlineMessageViewportPlan.kind === 'ready') {
      return (
        <MessageViewport
          height={inlineMessageViewportPlan.height}
          columns={metrics.canvasColumns}
          lines={[]}
          ready
          totalRows={0}
          scrollOffset={0}
          theme={theme}
        />
      )
    }
    return (
      <MessageViewport
        height={inlineMessageViewportPlan.height}
        columns={metrics.canvasColumns}
        lines={liveTranscriptRows}
        ready={liveTranscriptRows.length === 0}
        totalRows={activeTranscriptRows.length}
        scrollOffset={0}
        theme={theme}
      />
    )
  }
  if (!terminalReady) return null
  return (
    <>
      {fullscreen ? null : (
        <Static key={String(state.staticGeneration) + ':' + staticTranscriptGeneration} items={staticTranscriptRows}>
          {line => <TranscriptLineView key={line.id} line={line} />}
        </Static>
      )}
      <Box flexDirection="column" width={metrics.canvasColumns}>
        {metrics.headerRows > 0 ? (
          <Box justifyContent="space-between" paddingX={1} width={metrics.canvasColumns} height={metrics.headerRows} flexShrink={0} overflow="hidden">
            <Text bold wrap="truncate-end">GenericAgent</Text>
            <Text color={statusColor} wrap="truncate-end">{state.status}</Text>
          </Box>
        ) : null}
        {renderMessageViewport()}
        <BottomChrome columns={metrics.canvasColumns} height={metrics.bottomRows}>
          {showGoalCard ? <GoalStatusCardView lines={goalCardLinesOut} terminal={goalTerminalLinesOut} theme={theme} /> : null}
          {showWorkflowStatusBar ? <WorkflowStatusBarView rows={workflowStatusRows} theme={theme} /> : compactGoalLine ? <Box paddingX={1}><Text color={compactGoalLine.startsWith('✓') ? theme.success : compactGoalLine.startsWith('✗') ? theme.error : theme.accent} wrap="truncate-end">{compactGoalLine}</Text></Box> : hasActivity ? <ActivityView seconds={activitySeconds} label={state.activityLabel ?? runningLabel} tokenUsage={state.tokenUsage} completedTurn={completedTurnActivity} theme={theme} /> : <ActivityPlaceholder />}
          {inputSections.map(renderInputSection)}
        </BottomChrome>
      </Box>
    </>
  )
}
