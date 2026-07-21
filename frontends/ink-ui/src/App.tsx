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
import { workflowListPanelFromRuns, workflowPanelCommandForKey, workflowPanelFromDetail, workflowPanelRows, workflowPanelWithRunUpdate, type WorkflowPanelState } from './workflowPanel.js'
import { workflowStatusBarCommandForKey, workflowStatusBarFromState, workflowStatusBarRows } from './workflowStatusBar.js'
import { createInputHistory, nextInput, previousInput, recordInput } from './inputHistory.js'
import {
  liveTranscriptViewportLines,
  transcriptLines,
  type TranscriptLine,
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
import {
  loadingMcpPanel,
  mcpPanelRows,
  mcpStatusIcon,
  mcpToolsForServer,
  moveMcpSelection,
  panelFromMcpStatus,
  visibleMcpServerRows,
  type McpPanelState,
} from './mcpPanel.js'
import { modelPanelRows, moveModelSelection, panelFromModelStatus, shouldApplyModelStatus, type ModelPanelState } from './modelPanel.js'
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
  type InputViewport,
} from './promptChrome.js'
import { formatRunningStatus, pickRunningVerb, shouldShowActivityStatus } from './activityStatus.js'
import { inputChromeSections, type InputChromeSection } from './inputLayout.js'
import { footerPanelRows, modelSwitchPanelText, statusPanelText, type FooterPanel } from './footerPanel.js'
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
} from './terminalCleanup.js'
import { inputCursorPosition } from './terminalCursor.js'
import type { CursorParkController } from './stdoutCursorPark.js'
import type { InputKey } from './inputController.js'
import { parseTerminalInput } from './terminalInput.js'
import { parseMouseEvent, parseMouseWheel, resolveMouseCaptureMode } from './mouseWheel.js'
import {
  preserveTranscriptScrollOnContentChange,
  scrollOffsetForHistoryReplacement,
  scrollTranscriptBy,
  transcriptScrollStep,
  transcriptWheelStep,
} from './transcriptScroll.js'
import {
  scrollOffsetForScrollbarClick,
  shouldHandleScrollbarDrag,
  transcriptScrollbar,
  transcriptScrollbarCells,
} from './transcriptScrollbar.js'
import { splitStaticAndActiveMessages } from './messagePartition.js'
import { planMessageViewport } from './messageViewportPlan.js'

type Props = {
  python: string
  bridgeScript: string
  startBridgeClient?: typeof startBridge
  cursorPark?: CursorParkController
}

const STDIN_RESUME_GAP_MS = 5000

function isResumeSuccessNotice(text: string): boolean {
  return /已恢复\s+\d+\s+轮/.test(text)
}

function resumeSuccessStatusText(text: string): string {
  const normalized = text.replace(/^✅\s*/, '').split('\n', 1)[0] ?? text
  const match = /已恢复\s+(\d+)\s+轮(?:结构化会话|完整对话|对话)?(?:（([^）]+)）)?/.exec(normalized)
  if (!match) return normalized
  const rounds = match[1]
  const source = match[2]
  return source ? `恢复完成：${rounds} 轮历史 · ${source}` : `恢复完成：${rounds} 轮历史`
}

function TranscriptLineView({ line }: { line: TranscriptLine }) {
  if (line.parts?.length) {
    return (
      <Text color={line.color} backgroundColor={line.backgroundColor} wrap="truncate-end">
        {line.parts.map((part, index) => (
          <Text
            key={index}
            color={part.color ?? line.color}
            backgroundColor={part.backgroundColor ?? line.backgroundColor}
            bold={part.bold}
            italic={part.italic}
            underline={part.underline}
            dimColor={part.dimColor}
          >
            {part.text}
          </Text>
        ))}
      </Text>
    )
  }
  return (
    <Text color={line.color} backgroundColor={line.backgroundColor} wrap="truncate-end">
      {line.text}
    </Text>
  )
}
function formatResumeSession(session: ResumeSession): string {
  const minutes = Math.max(0, Math.round((Date.now() - session.mtime * 1000) / 60000))
  const age = minutes < 60 ? `${minutes}m ago` : minutes < 1440 ? `${Math.floor(minutes / 60)}h ago` : `${Math.floor(minutes / 1440)}d ago`
  const when = new Date(session.mtime * 1000)
  const stamp = `${String(when.getMonth() + 1).padStart(2, '0')}-${String(when.getDate()).padStart(2, '0')} ${String(when.getHours()).padStart(2, '0')}:${String(when.getMinutes()).padStart(2, '0')}`
  const preview = session.preview.replace(/\s+/g, ' ').slice(0, 80) || '(no preview)'
  return `${stamp} - ${age} - ${session.rounds} turns - ${preview}`
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
            {active ? '> ' : '  '}{formatSlashSuggestionLine(command)}
          </Text>
        )
      })}
      <Text color={theme.muted}>Tab/Enter complete - Up/Down move - Esc cancel</Text>
    </Box>
  )
}

function McpPanelView({ panel, theme }: { panel: McpPanelState; theme: InkTheme }) {
  const selected = panel.servers[panel.selected]
  const selectedTools = selected ? mcpToolsForServer(panel, selected.name) : []
  const serverRows = visibleMcpServerRows(panel, 5)
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

function ModelPanelView({ panel, theme }: { panel: ModelPanelState; theme: InkTheme }) {
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

function WorkflowPanelView({ panel, theme }: { panel: WorkflowPanelState; theme: InkTheme }) {
  return (
    <Box flexDirection="column" paddingX={1}>
      {workflowPanelRows(panel).map((line, index, rows) => (
        <Text key={index} color={index === 0 ? theme.accent : index >= 4 && index < rows.length - 1 ? undefined : theme.muted} wrap="truncate-end">
          {line}
        </Text>
      ))}
    </Box>
  )
}

function MessageViewport({ height, columns, lines, ready, totalRows, scrollOffset, theme, showScrollbar = true }: {
  height: number
  columns: number
  lines: TranscriptLine[]
  ready: boolean
  totalRows: number
  scrollOffset: number
  theme: InkTheme
  showScrollbar?: boolean
}) {
  const scrollbar = transcriptScrollbar({ totalRows, viewportRows: height, scrollOffset })
  const scrollbarColumns = showScrollbar ? transcriptScrollbarColumns(columns) : 0
  const messageColumns = Math.max(1, columns - scrollbarColumns)
  const scrollbarCells = transcriptScrollbarCells({ totalRows, viewportRows: height, scrollOffset })
  return (
    <Box flexDirection="row" height={height} width={columns} overflow="hidden">
      <Box flexDirection="column" paddingLeft={1} paddingRight={1} height={height} width={messageColumns} overflow="hidden">
        {ready ? <Text color={theme.muted}>Ready.</Text> : lines.map(line => <TranscriptLineView key={line.id} line={line} />)}
      </Box>
      {scrollbarColumns > 0 ? (
        <Box width={scrollbarColumns} height={height} overflow="hidden">
          <Text wrap="truncate-end">{scrollbarCells.map((cell, index) => (
            <Text key={index} color={cell.active ? theme.scrollbar : theme.muted}>{cell.text}{index < scrollbarCells.length - 1 ? '\n' : ''}</Text>
          ))}</Text>
        </Box>
      ) : null}
    </Box>
  )
}

function BottomChrome({ children, columns, height }: { children: React.ReactNode; columns: number; height: number }) {
  return (
    <Box flexDirection="column" flexShrink={0} width={columns} height={height} overflow="hidden">
      {children}
    </Box>
  )
}

function InputView({ viewport, showCursor, columns, theme }: { viewport: InputViewport; showCursor: boolean; columns: number; theme: InkTheme }) {
  const lines = viewport.lines
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
      {lines.map((line, index) => (
        <Box key={index} width="100%" overflow="hidden">
          <Text color={theme.accent} wrap="truncate-end">{line.gutter}</Text>
          {/*
            文本默认 muted，仅 caret 的 inverse 块用 accent。
            旧实现父级 Text color=accent 会让 inverse 继承 cyan 前景/背景，
            在 spawnSync 卡帧或多次贴图重绘时看起来像「蓝绿条」。
          */}
          <Text color={theme.muted} wrap="truncate-end">
            {renderInputLine(fixedInputLine(line.text, contentColumns), showCursor && line.cursorColumn !== undefined, line.cursorColumn === undefined ? undefined : line.cursorColumn + 1).map((part, partIndex) => (
              part.inverse
                ? <Text key={partIndex} color={theme.accent} inverse>{part.text}</Text>
                : <Text key={partIndex}>{part.text}</Text>
            ))}
          </Text>
        </Box>
      ))}
    </Box>
  )
}

function ActivityView({ seconds, label, tokenUsage, theme }: { seconds: number; label: string; tokenUsage: TokenUsage | null; theme: InkTheme }) {
  return (
    <Box paddingX={1}>
      <Text color={theme.warning}>{formatRunningStatus(seconds, label, tokenUsage)}</Text>
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
    '/workflows - inspect workflow runs and approval controls',
    '/workflow plan [--manual] [--timeout SECONDS] TASK - plan and run a dynamic workflow',
    '/workflow detail|approve|resume|deny|stop RUN_ID - control workflows',
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
  const { setRawMode, internal_eventEmitter } = useStdin()
  const [terminalSize, setTerminalSize] = useState(() => ({
    columns: Math.max(1, stdout.columns || 80),
    rows: Math.max(1, stdout.rows || 24),
  }))
  const [state, dispatch] = useReducer(applyBridgeEvent, initialState)
  const [input, setInput] = useState('')
  const [cursorOffset, setCursorOffset] = useState(0)
  const [inputHistory, setInputHistory] = useState(() => createInputHistory())
  const [skills, setSkills] = useState<SkillStatus[]>([])
  const [selector, setSelector] = useState<SelectorState | null>(null)
  const [mcpPanel, setMcpPanel] = useState<McpPanelState | null>(null)
  const [modelPanel, setModelPanel] = useState<ModelPanelState | null>(null)
  const [permissionPanel, setPermissionPanel] = useState<PermissionPanelState | null>(null)
  const [themePanelSelected, setThemePanelSelected] = useState<number | null>(null)
  const [themeName, setThemeName] = useState<InkThemeName>('default')
  const theme = useMemo(() => getInkTheme(themeName), [themeName])
  const mouseMode = useMemo(() => resolveMouseCaptureMode(), [])
  const [footerPanel, setFooterPanel] = useState<FooterPanel | null>(null)
  const [workflowPanel, setWorkflowPanel] = useState<WorkflowPanelState | null>(null)
  const [slashSelected, setSlashSelected] = useState(0)
  const [expandedTools, setExpandedTools] = useState(false)
  const [transcriptScrollOffset, setTranscriptScrollOffset] = useState(0)
  const [staticTranscriptGeneration, setStaticTranscriptGeneration] = useState(0)
  const [terminalReady, setTerminalReady] = useState(false)
  const [runningStartedAt, setRunningStartedAt] = useState<number | null>(null)
  const [lastActivitySeconds, setLastActivitySeconds] = useState(0)
  const [runningLabel, setRunningLabel] = useState(() => pickRunningVerb())
  const [now, setNow] = useState(() => Date.now())
  const bridgeRef = useRef<BridgeClient | null>(null)
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
  const scrollbarDragRef = useRef(false)
  const lastStdinAtRef = useRef(Date.now())
  const pasteStore = useMemo(() => createPasteStore(), [])
  const imageStore = useMemo(() => createImageAttachmentStore(), [])
  const imagePasteInFlightRef = useRef(false)
  const imagePasteSeqRef = useRef(0)
  const inputSnapshotRef = useRef({ value: '', cursorOffset: 0 })
  const slashItems = useMemo(() => selector || mcpPanel || modelPanel || permissionPanel || themePanelSelected !== null || footerPanel || workflowPanel ? [] : slashSuggestions(input, skills), [input, selector, mcpPanel, modelPanel, permissionPanel, themePanelSelected, footerPanel, workflowPanel, skills])
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
    if (mouseMode !== 'full') {
      stdout.write(clearInlineLiveViewportSequence(inlineCleanupGeometry))
    }
    cleanupTerminalForExit(stdout, mouseMode)
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

  const applyInputDecision = (decision: ReturnType<typeof handleInput>) => {
    if (decision.pendingImagePaste) {
      // 异步剪贴板：立刻返回，不阻塞 stdin；完成后把 [Image #N] 插到当前光标处。
      // 多次连按：in-flight 时忽略，避免并行 PowerShell 抢剪贴板。
      if (imagePasteInFlightRef.current) return
      imagePasteInFlightRef.current = true
      const seq = ++imagePasteSeqRef.current
      const baseValue = inputSnapshotRef.current.value
      const baseOffset = inputSnapshotRef.current.cursorOffset
      void applyClipboardImagePaste(baseValue, baseOffset, imageStore)
        .then((pasted) => {
          if (seq !== imagePasteSeqRef.current) return
          if (!pasted.ok) return
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
      const command = decision.command
      if (command.type === 'submit') {
        setInputHistory(history => recordInput(history, command.text))
        setTranscriptScrollOffset(0)
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
      setSelector({ mode: 'resume', selected: 0, sessions: [], loading: true })
      bridgeRef.current?.send({ type: 'list_resume_sessions' })
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

  useEffect(() => {
    stdout.write(enterMainScreenTerminalSequenceForMode(mouseMode))
    setTerminalReady(true)
    return () => {
      cleanupTerminalOnce()
    }
  }, [mouseMode, stdout])

  useEffect(() => {
    const syncTerminalSize = () => {
      const next = {
        columns: Math.max(1, stdout.columns || 80),
        rows: Math.max(1, stdout.rows || 24),
      }
      setTerminalSize(current => (
        current.columns === next.columns && current.rows === next.rows ? current : next
      ))
    }
    stdout.on('resize', syncTerminalSize)
    return () => {
      stdout.off('resize', syncTerminalSize)
    }
  }, [stdout])

  useEffect(() => {
    if (state.status === 'running' || state.status === 'stopping') {
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
      }
      if (event.type === 'status') {
        if (stopEchoGateAfterStatus(event.status) === 'reset') {
          stopTranscriptEchoedRef.current = false
        }
      }
      if (event.type === 'skill_status') {
        setSkills(event.skills)
        return
      }
      if (event.type === 'mcp_status') {
        if (!mcpPanelOpenRef.current) return
        setMcpPanel(panelFromMcpStatus(event))
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
        setSelector({ mode: 'resume', selected: 0, sessions: newestResumeSessions(event.sessions) })
        return
      }
      if (event.type === 'history_replace') {
        const commandText = pendingLocalCommandRef.current
        setSelector(null)
        setFooterPanel(null)
        resumePendingRef.current = false
        flushDeltas()
        pendingHistoryReplacementScrollRef.current = true
        dispatch(event)
        resetStaticTranscriptOutput()
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
        setWorkflowPanel(workflowListPanelFromRuns(event.runs))
        return
      }
      if (event.type === 'workflow_detail') {
        dispatch(event)
        setWorkflowPanel(workflowPanelFromDetail(event))
        if (pendingLocalCommandRef.current) pendingLocalCommandRef.current = null
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
        dispatch(event)
        resetStaticTranscriptOutput()
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

  const handleTerminalInput = (rawInput: string, key: InputKey) => {
    if (mouseMode === 'full') {
      const mouseInput = key.sequence ?? rawInput
      const wheel = parseMouseWheel(mouseInput)
      if (wheel) {
        const { totalRows, viewportRows } = transcriptRowsRef.current
        const delta = transcriptWheelStep()
        setTranscriptScrollOffset(offset => scrollTranscriptBy(offset, wheel === 'up' ? delta : -delta, totalRows, viewportRows))
        return
      }
      const mouseEvent = parseMouseEvent(mouseInput)
      if (mouseEvent) {
        if (mouseEvent.kind === 'release') {
          scrollbarDragRef.current = false
          return
        }
        if (mouseEvent.kind === 'wheel') {
          return
        }
        const { totalRows, viewportRows } = transcriptRowsRef.current
        if (shouldHandleScrollbarDrag({
          kind: mouseEvent.kind,
          x: mouseEvent.x,
          columns: terminalCanvasColumns(terminalSize.columns),
          dragging: scrollbarDragRef.current,
        })) {
          const offset = scrollOffsetForScrollbarClick({ totalRows, viewportRows, y: mouseEvent.y, viewportTop: 2 })
          if (offset !== null) {
            scrollbarDragRef.current = true
            setTranscriptScrollOffset(offset)
          }
        }
        return
      }
    }
    if (mouseMode === 'full' && key.pageUp) {
      const { totalRows, viewportRows } = transcriptRowsRef.current
      setTranscriptScrollOffset(offset => scrollTranscriptBy(offset, transcriptScrollStep(viewportRows), totalRows, viewportRows))
      return
    }
    if (mouseMode === 'full' && key.pageDown) {
      const { totalRows, viewportRows } = transcriptRowsRef.current
      setTranscriptScrollOffset(offset => scrollTranscriptBy(offset, -transcriptScrollStep(viewportRows), totalRows, viewportRows))
      return
    }
    if (key.ctrl && (rawInput === 'c' || rawInput === '\u0003')) {
      bridgeRef.current?.send({ type: 'shutdown' })
      exitCleanly()
      return
    }
    if (key.ctrl && (rawInput === 'o' || rawInput === '\u000f')) {
      setExpandedTools(value => !value)
      return
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
    }
    if (modelPanel) {
      if (key.escape) {
        modelPanelOpenRef.current = false
        setModelPanel(null)
        dismissPendingLocalCommand()
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
          bridgeRef.current?.send({ type: 'model_switch', selector: String(selected.index) })
        }
        modelPanelOpenRef.current = false
        modelPanelPendingRef.current = false
        setModelPanel(null)
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
    if (showWorkflowStatusBar && workflowStatusBar && input.trim() === '') {
      const command = workflowStatusBarCommandForKey(workflowStatusBar, key, rawInput)
      if (command) {
        bridgeRef.current?.send(command)
        return
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
  }, [internal_eventEmitter, mouseMode, setRawMode, stdout])

  const statusColor = state.status === 'running' ? 'yellow' : state.status === 'idle' ? 'green' : 'gray'
  const columns = terminalSize.columns
  const canvasColumns = terminalCanvasColumns(columns)
  const activePanel = mcpPanel || modelPanel || permissionPanel || themePanelSelected !== null || selector || footerPanel || workflowPanel
  const workflowStatusBar = useMemo(() => workflowStatusBarFromState(state), [state])
  const showWorkflowStatusBar = Boolean(workflowStatusBar) && !activePanel && slashItems.length === 0
  const workflowStatusRows = workflowStatusBar ? workflowStatusBarRows(workflowStatusBar) : []
  const promptViewport = useMemo(() => inputViewport(input, {
    columns: inputContentColumns(canvasColumns),
    maxRows: 6,
    cursorOffset,
  }), [canvasColumns, cursorOffset, input])
  const inputRows = promptViewport.lines.length
  const errorRows = state.error ? 1 : 0
  const hasActivity = showWorkflowStatusBar || shouldShowActivityStatus(state.status, runningStartedAt !== null, state.tokenUsage)
  const panelRows = modelPanel
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
      ? Math.min(workflowPanelRows(workflowPanel).length, 8)
    : footerPanel
      ? footerPanelRows(footerPanel)
    : slashItems.length > 0
      ? visibleSlashSuggestions(slashItems, slashSelected).items.length + 1
      : 0
  const metrics = computeLayoutMetrics({
    rows: terminalSize.rows,
    columns,
    hasActivity,
    hasError: Boolean(state.error),
    hasPanel: Boolean(activePanel),
    hasSlashSuggestions: slashItems.length > 0,
    inputRows,
    panelRows,
    // Inline scrollback: no title/status header. A live "GenericAgent running"
    // band sits after <Static> and looks jammed into the middle of output.
    // Full mouse mode keeps a 1-row header (single viewport, no Static).
    headerRows: mouseMode === 'full' ? 1 : 0,
  })
  const keepLatestTaskActive = state.status === 'running' || state.status === 'stopping'
  const messagePartition = useMemo(() => (
    splitStaticAndActiveMessages(state.messages, { keepLatestTaskActive })
  ), [keepLatestTaskActive, state.messages])

  const staticTranscriptRows = useMemo(() => (
    mouseMode === 'full'
      ? []
      : wrapTranscriptLines(transcriptLines(messagePartition.staticMessages, { expandedTools, theme }), transcriptContentColumns(metrics.canvasColumns))
  ), [expandedTools, messagePartition.staticMessages, metrics.canvasColumns, mouseMode, theme])
  const activeTranscriptRows = useMemo(() => (
    mouseMode === 'full'
      ? []
      : wrapTranscriptLines(transcriptLines(messagePartition.activeMessages, { expandedTools, theme }), transcriptContentColumns(metrics.canvasColumns))
  ), [expandedTools, messagePartition.activeMessages, metrics.canvasColumns, mouseMode, theme])
  const inlineMessageViewportPlan = useMemo(() => (
    planMessageViewport({
      hasStaticMessages: staticTranscriptRows.length > 0,
      liveLineCount: activeTranscriptRows.length,
      messageRows: metrics.messageRows,
      keepLivePlaceholder: keepLatestTaskActive && activeTranscriptRows.length === 0,
    })
  ), [activeTranscriptRows.length, keepLatestTaskActive, metrics.messageRows, staticTranscriptRows.length])
  const inlineMessageRows = inlineMessageViewportPlan.kind === 'none'
    ? 0
    : inlineMessageViewportPlan.height
  const messageRowsForCursor = mouseMode === 'full' ? metrics.messageRows : inlineMessageRows
  const liveTranscriptRows = useMemo(() => (
    inlineMessageViewportPlan.kind === 'live'
      ? liveTranscriptViewportLines(activeTranscriptRows, inlineMessageViewportPlan.height)
      : []
  ), [activeTranscriptRows, inlineMessageViewportPlan])

  const transcriptRows = useMemo(() => (
    wrapTranscriptLines(transcriptLines(state.messages, { expandedTools, theme }), transcriptContentColumns(metrics.canvasColumns))
  ), [state.messages, expandedTools, metrics.canvasColumns, theme])
  const previousTranscriptTotalRows = transcriptRowsRef.current.totalRows
  const historyReplacementScrollOffset = mouseMode === 'full' && pendingHistoryReplacementScrollRef.current
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
  transcriptRowsRef.current = mouseMode === 'full'
    ? { totalRows: visibleTranscript.totalRows, viewportRows: metrics.messageRows }
    : { totalRows: activeTranscriptRows.length, viewportRows: Math.max(1, inlineMessageRows) }
  // Slash/panel render *below* the input (Codex popup under composer), so they
  // must not push the IME caret row. panelRows stays 0 for cursor math; total
  // bottomRows / clear geometry still include panel height via metrics.
  const inputCursor = inputCursorPosition({
    headerRows: metrics.headerRows,
    messageRows: messageRowsForCursor,
    activityRows: 1,
    errorRows,
    panelRows: 0,
    hintRows: 1,
    inputBorderTopRows: 1,
    inputPaddingLeftColumns: inputLeftPaddingColumns,
    inputGutterColumns,
    inputCursorLine: promptViewport.cursorLine,
    inputCursorColumn: promptViewport.cursorColumn,
  })
  const liveViewportRows = mouseMode === 'full' ? metrics.rows : metrics.headerRows + messageRowsForCursor + metrics.bottomRows
  liveViewportGeometryRef.current = {
    rows: liveViewportRows,
    cursorRow: Math.min(inputCursor.row, Math.max(0, liveViewportRows - 1)),
  }

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
    const inactive = !terminalReady || state.status === 'running' || state.status === 'stopping'
    if (inactive) {
      cursorPark.setPark(null)
      return
    }
    const up = Math.max(0, liveViewportRows - inputCursor.row)
    cursorPark.setPark({ up, col: inputCursor.column })
  }, [cursorPark, inputCursor.column, inputCursor.row, liveViewportRows, state.status, terminalReady])

  useEffect(() => {
    if (mouseMode !== 'full') {
      pendingHistoryReplacementScrollRef.current = false
      return
    }
    if (!pendingHistoryReplacementScrollRef.current) return
    pendingHistoryReplacementScrollRef.current = false
    setTranscriptScrollOffset(historyReplacementScrollOffset ?? scrollOffsetForHistoryReplacement(visibleTranscript.totalRows, metrics.messageRows))
  }, [historyReplacementScrollOffset, metrics.messageRows, mouseMode, visibleTranscript.totalRows])

  useEffect(() => {
    if (mouseMode !== 'full') return
    if (transcriptScrollOffset !== visibleTranscript.scrollOffset) {
      setTranscriptScrollOffset(visibleTranscript.scrollOffset)
    }
  }, [mouseMode, transcriptScrollOffset, visibleTranscript.scrollOffset])

  const inputHint = state.status === 'running' || state.status === 'stopping'
    ? 'Running · Enter keeps draft · PgUp/PgDn · Ctrl+O tools · Esc stop'
    : 'Enter send · Alt+Enter newline · PgUp/PgDn · Ctrl+O tools · Ctrl+C exit'
  const runningSeconds = runningStartedAt === null ? 0 : Math.floor((now - runningStartedAt) / 1000)
  const activitySeconds = runningStartedAt === null ? lastActivitySeconds : runningSeconds
  const inputSections = inputChromeSections({
    hasError: Boolean(state.error),
    hasPanel: Boolean(activePanel),
    hasSlashSuggestions: slashItems.length > 0,
  })
  const renderInputSection = (section: InputChromeSection) => {
    if (section === 'error') return state.error ? <Box key={section} paddingX={1}><Text color={theme.error} wrap="truncate-end">{state.error}</Text></Box> : null
    if (section === 'hint') return <Box key={section} paddingX={1}><Text color={theme.muted} wrap="truncate-end">{footerPanel?.type === 'status' ? footerPanel.text : inputHint}</Text></Box>
    if (section === 'input') return <InputView key={section} viewport={promptViewport} showCursor={state.status !== 'running' && state.status !== 'stopping'} columns={metrics.canvasColumns} theme={theme} />
    if (section === 'panel') {
      if (mcpPanel) return <McpPanelView key={section} panel={mcpPanel} theme={theme} />
      if (modelPanel) return <ModelPanelView key={section} panel={modelPanel} theme={theme} />
      if (permissionPanel) return <PermissionPanelView key={section} panel={permissionPanel} theme={theme} />
      if (themePanelSelected !== null) return <ThemePanelView key={section} selected={themePanelSelected} currentTheme={themeName} theme={theme} />
      if (selector) return <SelectorView key={section} selector={selector} theme={theme} />
      if (workflowPanel) return <WorkflowPanelView key={section} panel={workflowPanel} theme={theme} />
      if (footerPanel && footerPanel.type !== 'status') return <FooterPanelView key={section} panel={footerPanel} theme={theme} />
      return null
    }
    return slashItems.length > 0 ? <SlashSuggestionsView key={section} suggestions={slashItems} selected={slashSelected} theme={theme} /> : null
  }
  const renderMessageViewport = () => {
    if (mouseMode === 'full') {
      return (
        <MessageViewport
          height={metrics.messageRows}
          columns={metrics.canvasColumns}
          lines={visibleTranscript.lines}
          ready={visibleTranscript.totalRows === 0}
          totalRows={visibleTranscript.totalRows}
          scrollOffset={visibleTranscript.scrollOffset}
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
          showScrollbar={false}
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
        showScrollbar={false}
      />
    )
  }
  if (!terminalReady) return null
  return (
    <>
      {mouseMode === 'full' ? null : (
        <Static key={staticTranscriptGeneration} items={staticTranscriptRows}>
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
          {showWorkflowStatusBar ? <WorkflowStatusBarView rows={workflowStatusRows} theme={theme} /> : hasActivity ? <ActivityView seconds={activitySeconds} label={state.activityLabel ?? runningLabel} tokenUsage={state.tokenUsage} theme={theme} /> : <ActivityPlaceholder />}
          {inputSections.map(renderInputSection)}
        </BottomChrome>
      </Box>
    </>
  )
}
