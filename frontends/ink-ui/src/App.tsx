import React, { useEffect, useMemo, useReducer, useRef, useState } from 'react'
import { Box, Static, Text, useApp, useInput, useStdout } from 'ink'
import { startBridge, type BridgeClient } from './bridgeClient.js'
import { applyBridgeEvent, initialState } from './state.js'
import { createPasteStore } from './paste.js'
import type { SkillStatus } from './protocol.js'
import { handleInput } from './inputController.js'
import { createInputHistory, nextInput, previousInput, recordInput } from './inputHistory.js'
import {
  transcriptLines,
  type TranscriptLine,
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
import type { BridgeEvent, ChatMessage, ResumeSession } from './protocol.js'
import {
  loadingMcpPanel,
  mcpStatusColor,
  mcpStatusIcon,
  mcpToolsForServer,
  moveMcpSelection,
  panelFromMcpStatus,
  type McpPanelState,
} from './mcpPanel.js'
import { modelPanelRows, moveModelSelection, panelFromModelStatus, shouldApplyModelStatus, type ModelPanelState } from './modelPanel.js'
import {
  formatSlashSuggestionLine,
  moveSlashSelection,
  shouldCompleteSlashCommand,
  slashSelectionAction,
  slashSuggestions,
  visibleSlashSuggestions,
  type SlashCommand,
} from './slashCommands.js'
import { inputPrompt } from './promptChrome.js'
import { formatRunningStatus, pickRunningVerb } from './activityStatus.js'
import { inputChromeSections, type InputChromeSection } from './inputLayout.js'
import { modelSwitchPanelText, type FooterPanel } from './footerPanel.js'
import {
  clearLocalCommandOutput,
  commandTextForLocalDecision,
  dismissedLocalCommandOutput,
  localCommandResultOutput,
} from './localCommandTranscript.js'
import { computeLayoutMetrics } from './layoutMetrics.js'

type Props = {
  python: string
  bridgeScript: string
}

function TranscriptLineView({ line }: { line: TranscriptLine }) {
  return (
    <Text color={line.color} backgroundColor={line.backgroundColor}>
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

function SelectorView({ selector }: { selector: SelectorState }) {
  const rows = visibleSelectorRows(selector, 8)
  const title = selector.mode === 'resume' ? 'Resume Conversation' : 'Rewind Conversation'
  const empty = selector.mode === 'resume' && selector.loading ? 'Loading conversations...' : selector.mode === 'resume' ? 'No resumable sessions found.' : 'Nothing to rewind to yet.'
  return (
    <Box flexDirection="column" paddingX={1}>
      <Text bold>{title}</Text>
      {rows.length === 0 ? <Text color="gray">{empty}</Text> : rows.map(row => {
        const text = selector.mode === 'resume'
          ? formatResumeSession(selector.sessions[row.index]!)
          : selector.options[row.index]!.text.replace(/\s+/g, ' ').slice(0, 90) || '(empty)'
        return (
        <Text key={`${selector.mode}-${row.index}`} color={row.selected ? 'cyan' : undefined} inverse={row.selected}>
          {row.selected ? '> ' : '  '}{text}
        </Text>
        )
      })}
      <Text color="gray">Enter select - Up/Down move - Esc cancel</Text>
    </Box>
  )
}

function SlashSuggestionsView({ suggestions, selected }: { suggestions: SlashCommand[]; selected: number }) {
  const visible = visibleSlashSuggestions(suggestions, selected)
  return (
    <Box flexDirection="column" paddingX={1}>
      {visible.items.map((command, offset) => {
        const index = visible.startIndex + offset
        const active = index === selected
        return (
          <Text key={command.name} color={active ? 'cyan' : undefined}>
            {active ? '> ' : '  '}{formatSlashSuggestionLine(command)}
          </Text>
        )
      })}
      <Text color="gray">Tab/Enter complete - Up/Down move - Esc cancel</Text>
    </Box>
  )
}

function McpPanelView({ panel }: { panel: McpPanelState }) {
  const selected = panel.servers[panel.selected]
  const selectedTools = selected ? mcpToolsForServer(panel, selected.name) : []
  return (
    <Box flexDirection="column" paddingX={1}>
      <Text bold>MCP Servers</Text>
      {panel.configPath ? <Text color="gray">Config: {panel.configPath}</Text> : null}
      {panel.loading ? <Text color="gray">Loading MCP status...</Text> : null}
      {!panel.loading && panel.servers.length === 0 ? <Text color="gray">No MCP servers configured.</Text> : null}
      {panel.servers.map((server, index) => (
        <Text key={server.name} color={index === panel.selected ? 'cyan' : undefined}>
          {index === panel.selected ? '> ' : '  '}
          <Text color={mcpStatusColor(server.status)}>{mcpStatusIcon(server.status)}</Text>
          {` ${server.name} - ${server.status} - ${server.transport} - ${server.tool_count} tools`}
        </Text>
      ))}
      {selected ? <Text color="gray">Actions: /mcp reconnect {selected.name} - /mcp {selected.disabled ? 'enable' : 'disable'} {selected.name}</Text> : null}
      {selected && panel.errors[selected.name] ? <Text color="red">{panel.errors[selected.name]}</Text> : null}
      {selectedTools.slice(0, 20).map(tool => (
        <Text key={tool.function.name} color="gray">  - {tool.function.name}</Text>
      ))}
      <Text color="gray">Up/Down move - Esc close</Text>
    </Box>
  )
}

function ModelPanelView({ panel }: { panel: ModelPanelState }) {
  return (
    <Box flexDirection="column" paddingX={1} flexShrink={0}>
      <Text bold>Models</Text>
      {panel.models.length === 0 ? <Text color="gray">No models configured.</Text> : null}
      {panel.models.map((model, index) => (
        <Text key={model.index} color={index === panel.selected ? 'cyan' : undefined}>
          {index === panel.selected ? '> ' : '  '}
          <Text color={model.current ? 'green' : 'gray'}>{model.current ? '✓' : ' '}</Text>
          {` ${model.index}: ${model.name}`}
        </Text>
      ))}
      <Text color="gray">Enter select - Up/Down move - Esc cancel</Text>
    </Box>
  )
}

function FooterPanelView({ panel }: { panel: FooterPanel }) {
  return (
    <Box flexDirection="column" paddingX={1}>
      {panel.text.split('\n').map((line, index) => (
        <Text key={index} color={index === 0 && panel.type === 'help' ? undefined : 'gray'}>{line}</Text>
      ))}
      <Text color="gray">Esc close</Text>
    </Box>
  )
}

function StaticMessage({ message, columns, expandedTools }: { message: ChatMessage; columns: number; expandedTools: boolean }) {
  const lines = wrapTranscriptLines(transcriptLines([message], { expandedTools }), Math.max(1, columns - 2))
  return (
    <Box flexDirection="column" paddingX={1} width={columns}>
      {lines.map(line => <TranscriptLineView key={line.id} line={line} />)}
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

function InputView({ input, showCursor, columns }: { input: string; showCursor: boolean; columns: number }) {
  return (
    <Box
      flexDirection="row"
      alignItems="flex-start"
      borderStyle="round"
      borderColor="gray"
      borderLeft={false}
      borderRight={false}
      borderBottom
      width={columns}
      paddingLeft={1}
      overflow="hidden"
    >
      <Text color="cyan" wrap="truncate-end">
        {inputPrompt(input)}
        {showCursor && <Text inverse> </Text>}
      </Text>
    </Box>
  )
}

function ActivityView({ seconds, label }: { seconds: number; label: string }) {
  return (
    <Box>
      <Text color="yellow">{formatRunningStatus(seconds, label)}</Text>
    </Box>
  )
}

function helpText(): string {
  return [
    'Commands:',
    '/resume, /continue - pick a previous conversation',
    '/resume N, /continue N - resume by index',
    '/rewind, /checkpoint - restore conversation to before a user message',
    '/compact [instructions] - summarize and replace long conversation context',
    '/clear - clear display only',
    '/status - show current frontend status',
    '/model, /llm - show and switch AI models',
    '/stop - stop current backend task',
    '/exit, /quit - exit',
  ].join('\n')
}

export function App({ python, bridgeScript }: Props) {
  const { exit } = useApp()
  const { stdout } = useStdout()
  const [state, dispatch] = useReducer(applyBridgeEvent, initialState)
  const [input, setInput] = useState('')
  const [inputHistory, setInputHistory] = useState(() => createInputHistory())
  const [skills, setSkills] = useState<SkillStatus[]>([])
  const [selector, setSelector] = useState<SelectorState | null>(null)
  const [mcpPanel, setMcpPanel] = useState<McpPanelState | null>(null)
  const [modelPanel, setModelPanel] = useState<ModelPanelState | null>(null)
  const [footerPanel, setFooterPanel] = useState<FooterPanel | null>(null)
  const [slashSelected, setSlashSelected] = useState(0)
  const [expandedTools, setExpandedTools] = useState(false)
  const [runningStartedAt, setRunningStartedAt] = useState<number | null>(null)
  const [runningLabel, setRunningLabel] = useState(() => pickRunningVerb())
  const [now, setNow] = useState(() => Date.now())
  const bridgeRef = useRef<BridgeClient | null>(null)
  const resumePendingRef = useRef(false)
  const mcpPanelOpenRef = useRef(false)
  const modelPanelPendingRef = useRef(false)
  const modelPanelOpenRef = useRef(false)
  const pendingLocalCommandRef = useRef<string | null>(null)
  const pasteStore = useMemo(() => createPasteStore(), [])
  const slashItems = useMemo(() => selector || mcpPanel || modelPanel || footerPanel ? [] : slashSuggestions(input, skills), [input, selector, mcpPanel, modelPanel, footerPanel, skills])
  const skillNames = useMemo(() => new Set(skills.map(skill => skill.name)), [skills])

  const appendLocalCommandInput = (commandText: string) => {
    dispatch({ type: 'local_command_input', text: commandText })
    pendingLocalCommandRef.current = commandText
  }

  const appendLocalCommandOutput = (text: string) => {
    dispatch({ type: 'local_command_output', text })
    pendingLocalCommandRef.current = null
  }

  const dismissPendingLocalCommand = () => {
    const commandText = pendingLocalCommandRef.current
    if (!commandText) return
    appendLocalCommandOutput(dismissedLocalCommandOutput(commandText))
  }

  const applyInputDecision = (decision: ReturnType<typeof handleInput>) => {
    setInput(decision.value)
    const localCommandText = commandTextForLocalDecision(decision)
    if (decision.action?.type === 'clear') {
      setFooterPanel(null)
      pendingLocalCommandRef.current = null
      dispatch({ type: 'clear' })
      dispatch({ type: 'local_command_input', text: localCommandText ?? '/clear' })
      dispatch({ type: 'local_command_output', text: clearLocalCommandOutput() })
      if (decision.exit) exit()
      return
    }
    if (localCommandText) {
      appendLocalCommandInput(localCommandText)
    }
    if (decision.command) {
      setFooterPanel(null)
      const command = decision.command
      if (command.type === 'submit') {
        setInputHistory(history => recordInput(history, command.text))
      } else if (command.type === 'skill_invoke') {
        setInputHistory(history => recordInput(history, `/${command.skill}${command.args ? ` ${command.args}` : ''}`))
      }
      if (command.type === 'stop' && localCommandText) {
        appendLocalCommandOutput('Stop requested')
      }
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
    } else if (decision.action?.type === 'help') {
      setFooterPanel({ type: 'help', text: helpText() })
    } else if (decision.action?.type === 'status') {
      appendLocalCommandOutput(localCommandResultOutput('/status', state.status, state.messages.length))
    }
    if (decision.exit) {
      exit()
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
    stdout.write('\u001B[?25l')
    return () => {
      stdout.write('\u001B[?25h')
    }
  }, [stdout])

  useEffect(() => {
    if (state.status === 'running' || state.status === 'stopping') {
      setRunningStartedAt(value => {
        if (value !== null) return value
        setRunningLabel(pickRunningVerb())
        return Date.now()
      })
      setNow(Date.now())
      const timer = setInterval(() => setNow(Date.now()), 1000)
      return () => clearInterval(timer)
    }
    setRunningStartedAt(null)
    setNow(Date.now())
    return undefined
  }, [state.status])

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
        dispatch(event)
        if (commandText) {
          dispatch({ type: 'local_command_input', text: commandText })
        }
        return
      }
      if (event.type === 'rewind_done') {
        const commandText = pendingLocalCommandRef.current
        setSelector(null)
        setInput(event.text)
        flushDeltas()
        dispatch(event)
        if (commandText) {
          dispatch({ type: 'local_command_input', text: commandText })
          appendLocalCommandOutput('Rewound to selected message')
        }
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
        const taskIdStr = String(event.taskId)
        const cachedDelta = pendingDeltas[taskIdStr] || ''
        delete pendingDeltas[taskIdStr]
        dispatch({ type: 'assistant_done', taskId: event.taskId, text: cachedDelta + event.text })
        return
      }
      dispatch(event)
    }

    bridgeRef.current = startBridge(python, bridgeScript, onEvent, code => {
      dispatch({ type: 'error', code: 'bridge_exit', message: `bridge exited: ${code ?? 'signal'}` })
    })
    return () => {
      bridgeRef.current?.stop()
      if (throttleTimer) clearTimeout(throttleTimer)
    }
  }, [bridgeScript, python])

  useInput((rawInput, key) => {
    if (key.ctrl && (rawInput === 'c' || rawInput === '\u0003')) {
      bridgeRef.current?.send({ type: 'shutdown' })
      exit()
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
            return
          }
        } else {
          applyInputDecision(handleInput(action.value, '', { return: true }, state.status, pasteStore, skillNames))
          return
        }
      }
      if (key.escape) {
        setInput('')
        return
      }
    }
    if (key.upArrow) {
      const result = previousInput(inputHistory, input)
      setInputHistory(result.history)
      setInput(result.value)
      return
    }
    if (key.downArrow) {
      const result = nextInput(inputHistory, input)
      setInputHistory(result.history)
      setInput(result.value)
      return
    }
    const decision = handleInput(input, rawInput, key, state.status, pasteStore, skillNames)
    applyInputDecision(decision)
  })

  const statusColor = state.status === 'running' ? 'yellow' : state.status === 'idle' ? 'green' : 'gray'
  const columns = Math.max(1, stdout.columns || 80)
  const activePanel = mcpPanel || modelPanel || selector || footerPanel
  const metrics = computeLayoutMetrics({
    rows: stdout.rows,
    columns,
    hasActivity: (state.status === 'running' || state.status === 'stopping') && runningStartedAt !== null,
    hasError: Boolean(state.error),
    hasPanel: Boolean(activePanel),
    hasSlashSuggestions: slashItems.length > 0,
    panelRows: modelPanel
      ? modelPanelRows(modelPanel)
      : selector
        ? Math.min(selectorSize(selector), 8) + 2
      : slashItems.length > 0
        ? visibleSlashSuggestions(slashItems, slashSelected).items.length + 1
        : undefined,
  })
  const staticMessages = state.messages.filter(message => message.done)
  const liveLines = useMemo(() => {
    const liveMessages = state.messages.filter(message => !message.done)
    return wrapTranscriptLines(transcriptLines(liveMessages, { expandedTools }), Math.max(1, metrics.columns - 2))
  }, [state.messages, expandedTools, metrics.columns])

  const inputHint = state.status === 'running' || state.status === 'stopping'
    ? `Running: keep typing, Enter waits - Native terminal scrollback - Ctrl+O ${expandedTools ? 'collapse' : 'expand'} tools - /stop or Esc stops`
    : `Enter send - Alt+Enter newline - Native terminal scrollback - Ctrl+O ${expandedTools ? 'collapse' : 'expand'} tools - Ctrl+C exit`
  const runningSeconds = runningStartedAt === null ? 0 : Math.floor((now - runningStartedAt) / 1000)
  const inputSections = inputChromeSections({
    hasError: Boolean(state.error),
    hasPanel: Boolean(activePanel),
    hasSlashSuggestions: slashItems.length > 0,
  })
  const renderInputSection = (section: InputChromeSection) => {
    if (section === 'error') return state.error ? <Text key={section} color="red">{state.error}</Text> : null
    if (section === 'hint') return <Text key={section} color="gray" wrap="truncate-end">{inputHint}</Text>
    if (section === 'input') return <InputView key={section} input={input} showCursor={state.status !== 'running' && state.status !== 'stopping'} columns={metrics.columns} />
    if (section === 'panel') {
      if (mcpPanel) return <McpPanelView key={section} panel={mcpPanel} />
      if (modelPanel) return <ModelPanelView key={section} panel={modelPanel} />
      if (selector) return <SelectorView key={section} selector={selector} />
      if (footerPanel) return <FooterPanelView key={section} panel={footerPanel} />
      return null
    }
    return slashItems.length > 0 ? <SlashSuggestionsView key={section} suggestions={slashItems} selected={slashSelected} /> : null
  }
  return (
    <>
      <Static items={staticMessages}>
        {message => <StaticMessage key={message.id} message={message} columns={metrics.columns} expandedTools={expandedTools} />}
      </Static>
      <Box flexDirection="column" width={metrics.columns}>
      <Box justifyContent="space-between" width={metrics.columns} height={metrics.headerRows} flexShrink={0} overflow="hidden">
        <Text bold wrap="truncate-end">GenericAgent Ink</Text>
        <Text color={statusColor} wrap="truncate-end">{state.status}</Text>
      </Box>
        <Box flexDirection="column" paddingX={1} width={metrics.columns}>
          {staticMessages.length === 0 && liveLines.length === 0 ? <Text color="gray">Ready.</Text> : null}
          {liveLines.map(line => <TranscriptLineView key={line.id} line={line} />)}
        </Box>
        <BottomChrome columns={metrics.columns} height={metrics.bottomRows}>
          {(state.status === 'running' || state.status === 'stopping') && runningStartedAt !== null ? <ActivityView seconds={runningSeconds} label={state.activityLabel ?? runningLabel} /> : null}
          {inputSections.map(renderInputSection)}
        </BottomChrome>
      </Box>
    </>
  )
}
