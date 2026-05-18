import React, { useEffect, useMemo, useReducer, useRef, useState } from 'react'
import { Box, Text, useApp, useInput, useStdout } from 'ink'
import { startBridge, type BridgeClient } from './bridgeClient.js'
import { applyBridgeEvent, initialState } from './state.js'
import { createPasteStore } from './paste.js'
import type { ChatMessage } from './protocol.js'
import { formatAssistantText } from './messageFormat.js'
import { handleInput } from './inputController.js'
import { assistantDisplayText, visibleMessages } from './messageWindow.js'
import { handleSelectorInput, rewindOptions, type SelectorState } from './selectors.js'
import type { BridgeEvent, ResumeSession } from './protocol.js'
import {
  loadingMcpPanel,
  mcpStatusColor,
  mcpStatusIcon,
  mcpToolsForServer,
  moveMcpSelection,
  panelFromMcpStatus,
  type McpPanelState,
} from './mcpPanel.js'
import { moveModelSelection, panelFromModelStatus, shouldApplyModelStatus, type ModelPanelState } from './modelPanel.js'
import {
  completeSlashCommand,
  moveSlashSelection,
  shouldCompleteSlashCommand,
  slashSuggestions,
  visibleSlashSuggestions,
  type SlashCommand,
} from './slashCommands.js'
import { inputDivider, inputPrompt } from './promptChrome.js'
import { formatRunningStatus, pickRunningVerb } from './activityStatus.js'

type Props = {
  python: string
  bridgeScript: string
}

function MessageView({ message, expandedTools }: { message: ChatMessage; expandedTools: boolean }) {
  const body = message.role === 'assistant' ? formatAssistantText(message.text, { expanded: expandedTools }) || ' ' : message.text || ' '
  if (message.role === 'user') {
    return (
      <Box marginBottom={1}>
        <Text color="black" backgroundColor="#d7d7d7">{inputPrompt(body)} </Text>
      </Box>
    )
  }
  if (message.role === 'system') {
    return (
      <Box marginBottom={1}>
        <Text color="gray">{body}</Text>
      </Box>
    )
  }
  return (
    <Box flexDirection="row" marginBottom={1}>
      <Text color="black">✻ </Text>
      <Box flexDirection="column" flexShrink={1}>
        <Text>{assistantDisplayText(body, { expanded: expandedTools, done: message.done })}</Text>
      </Box>
    </Box>
  )
}

function formatResumeSession(session: ResumeSession): string {
  const minutes = Math.max(0, Math.round((Date.now() - session.mtime * 1000) / 60000))
  const age = minutes < 60 ? `${minutes}m ago` : minutes < 1440 ? `${Math.floor(minutes / 60)}h ago` : `${Math.floor(minutes / 1440)}d ago`
  const preview = session.preview.replace(/\s+/g, ' ').slice(0, 80) || '(no preview)'
  return `${age} - ${session.rounds} turns - ${preview}`
}

function SelectorView({ selector }: { selector: SelectorState }) {
  const rows = selector.mode === 'resume'
    ? selector.sessions.map(session => formatResumeSession(session))
    : selector.options.map(option => option.text.replace(/\s+/g, ' ').slice(0, 90) || '(empty)')
  const title = selector.mode === 'resume' ? 'Resume Conversation' : 'Rewind Conversation'
  const empty = selector.mode === 'resume' && selector.loading ? 'Loading conversations...' : selector.mode === 'resume' ? 'No resumable sessions found.' : 'Nothing to rewind to yet.'
  return (
    <Box flexDirection="column" paddingX={1}>
      <Text color="gray">{inputDivider(48)}</Text>
      <Text bold>{title}</Text>
      {rows.length === 0 ? <Text color="gray">{empty}</Text> : rows.map((row, index) => (
        <Text key={`${selector.mode}-${index}`} color={index === selector.selected ? 'cyan' : undefined}>
          {index === selector.selected ? '> ' : '  '}{row}
        </Text>
      ))}
      <Text color="gray">Enter select - Up/Down move - Esc cancel</Text>
      <Text color="gray">{inputDivider(48)}</Text>
    </Box>
  )
}

function SlashSuggestionsView({ suggestions, selected }: { suggestions: SlashCommand[]; selected: number }) {
  const visible = visibleSlashSuggestions(suggestions, selected)
  return (
    <Box flexDirection="column" paddingX={1}>
      <Text color="gray">{inputDivider(48)}</Text>
      {visible.items.map((command, offset) => {
        const index = visible.startIndex + offset
        const active = index === selected
        return (
          <Text key={command.name} color={active ? 'cyan' : undefined}>
            {active ? '> ' : '  '}{command.name.padEnd(12)} {command.description}
          </Text>
        )
      })}
      <Text color="gray">Tab/Enter complete - Up/Down move - Esc cancel</Text>
      <Text color="gray">{inputDivider(48)}</Text>
    </Box>
  )
}

function McpPanelView({ panel }: { panel: McpPanelState }) {
  const selected = panel.servers[panel.selected]
  const selectedTools = selected ? mcpToolsForServer(panel, selected.name) : []
  return (
    <Box flexDirection="column" paddingX={1}>
      <Text color="gray">{inputDivider(48)}</Text>
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
      <Text color="gray">{inputDivider(48)}</Text>
    </Box>
  )
}

function ModelPanelView({ panel }: { panel: ModelPanelState }) {
  return (
    <Box flexDirection="column" paddingX={1}>
      <Text color="gray">{inputDivider(48)}</Text>
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
      <Text color="gray">{inputDivider(48)}</Text>
    </Box>
  )
}

function InputView({ input }: { input: string }) {
  return (
    <Text color="cyan">
      {inputPrompt(input)}
      <Text inverse> </Text>
    </Text>
  )
}

function ActivityView({ seconds, label }: { seconds: number; label: string }) {
  return (
    <Box marginBottom={1}>
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
  const [selector, setSelector] = useState<SelectorState | null>(null)
  const [mcpPanel, setMcpPanel] = useState<McpPanelState | null>(null)
  const [modelPanel, setModelPanel] = useState<ModelPanelState | null>(null)
  const [slashSelected, setSlashSelected] = useState(0)
  const [expandedTools, setExpandedTools] = useState(false)
  const [runningStartedAt, setRunningStartedAt] = useState<number | null>(null)
  const [runningLabel, setRunningLabel] = useState(() => pickRunningVerb())
  const [now, setNow] = useState(() => Date.now())
  const bridgeRef = useRef<BridgeClient | null>(null)
  const resumePendingRef = useRef(false)
  const modelPanelPendingRef = useRef(false)
  const modelPanelOpenRef = useRef(false)
  const pasteStore = useMemo(() => createPasteStore(), [])
  const slashItems = useMemo(() => selector || mcpPanel || modelPanel ? [] : slashSuggestions(input), [input, selector, mcpPanel, modelPanel])

  useEffect(() => {
    setSlashSelected(0)
  }, [input])

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
    function onEvent(event: BridgeEvent) {
      if (event.type === 'mcp_status') {
        setMcpPanel(panelFromMcpStatus(event))
        return
      }
      if (event.type === 'model_status') {
        if (!shouldApplyModelStatus(modelPanelPendingRef.current, modelPanelOpenRef.current)) return
        modelPanelPendingRef.current = false
        setModelPanel(panelFromModelStatus(event))
        return
      }
      if (event.type === 'resume_sessions') {
        if (!resumePendingRef.current) return
        resumePendingRef.current = false
        setSelector({ mode: 'resume', selected: 0, sessions: event.sessions })
        return
      }
      if (event.type === 'history_replace') {
        setSelector(null)
        resumePendingRef.current = false
      }
      if (event.type === 'rewind_done') {
        setSelector(null)
        setInput(event.text)
      }
      dispatch(event)
    }

    bridgeRef.current = startBridge(python, bridgeScript, onEvent, code => {
      dispatch({ type: 'error', code: 'bridge_exit', message: `bridge exited: ${code ?? 'signal'}` })
    })
    return () => bridgeRef.current?.stop()
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
    if (mcpPanel) {
      if (key.escape) {
        setMcpPanel(null)
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
        setModelPanel(null)
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
        if ((key as { tab?: boolean }).tab || shouldCompleteSlashCommand(input, selectedCommand)) {
          setInput(completeSlashCommand(selectedCommand))
          return
        }
      }
      if (key.escape) {
        setInput('')
        return
      }
    }
    const decision = handleInput(input, rawInput, key, state.status, pasteStore)
    setInput(decision.value)
    if (decision.command) {
      bridgeRef.current?.send(decision.command)
    }
    if (decision.action?.type === 'open_resume') {
      resumePendingRef.current = true
      setSelector({ mode: 'resume', selected: 0, sessions: [], loading: true })
      bridgeRef.current?.send({ type: 'list_resume_sessions' })
    } else if (decision.action?.type === 'open_rewind') {
      const options = rewindOptions(state.messages)
      setSelector({ mode: 'rewind', selected: Math.max(0, options.length - 1), options })
    } else if (decision.action?.type === 'open_mcp') {
      setMcpPanel(loadingMcpPanel())
      bridgeRef.current?.send({ type: 'mcp_status' })
    } else if (decision.action?.type === 'open_model') {
      modelPanelPendingRef.current = true
      bridgeRef.current?.send({ type: 'model_status' })
    } else if (decision.action?.type === 'clear') {
      dispatch({ type: 'clear' })
    } else if (decision.action?.type === 'help') {
      dispatch({ type: 'system', text: helpText() })
    } else if (decision.action?.type === 'status') {
      dispatch({ type: 'system', text: `status=${state.status} messages=${state.messages.length}` })
    }
    if (decision.exit) {
      exit()
    }
  })

  const statusColor = state.status === 'running' ? 'yellow' : state.status === 'idle' ? 'green' : 'gray'
  const shownMessages = visibleMessages(state.messages)
  const columns = Math.max(40, stdout.columns || 80)
  const dividerWidth = Math.max(20, columns - 1)
  const inputHint = state.status === 'running' || state.status === 'stopping'
    ? `Running: keep typing, Enter waits - Ctrl+O ${expandedTools ? 'collapse' : 'expand'} tools - /stop or Esc stops`
    : `Enter send - Alt+Enter newline - Ctrl+O ${expandedTools ? 'collapse' : 'expand'} tools - Ctrl+C exit`
  const runningSeconds = runningStartedAt === null ? 0 : Math.floor((now - runningStartedAt) / 1000)
  return (
    <Box flexDirection="column" width={columns}>
      <Box justifyContent="space-between" width={columns}>
        <Text bold>GenericAgent Ink</Text>
        <Text color={statusColor}>{state.status}</Text>
      </Box>
      <Box flexDirection="column" paddingX={1} minHeight={12} width={columns}>
        {shownMessages.length === 0 ? <Text color="gray">Ready.</Text> : shownMessages.map(message => <MessageView key={message.id} message={message} expandedTools={expandedTools} />)}
      </Box>
      {(state.status === 'running' || state.status === 'stopping') && runningStartedAt !== null ? <ActivityView seconds={runningSeconds} label={runningLabel} /> : null}
      {mcpPanel ? <McpPanelView panel={mcpPanel} /> : null}
      {modelPanel ? <ModelPanelView panel={modelPanel} /> : null}
      {selector ? <SelectorView selector={selector} /> : null}
      {!selector && slashItems.length > 0 ? <SlashSuggestionsView suggestions={slashItems} selected={slashSelected} /> : null}
      {state.error ? <Text color="red">{state.error}</Text> : null}
      <Text color="gray">{inputHint}</Text>
      <Text color="gray">{inputDivider(dividerWidth)}</Text>
      <InputView input={input} />
      <Text color="gray">{inputDivider(dividerWidth)}</Text>
    </Box>
  )
}
