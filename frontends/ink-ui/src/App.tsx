import React, { useEffect, useMemo, useReducer, useRef, useState } from 'react'
import { Box, Text, useApp, useInput } from 'ink'
import { startBridge, type BridgeClient } from './bridgeClient.js'
import { applyBridgeEvent, initialState } from './state.js'
import { createPasteStore, expandPastedTextRefs, foldPastedText } from './paste.js'
import type { ChatMessage } from './protocol.js'
import { formatAssistantText } from './messageFormat.js'

type Props = {
  python: string
  bridgeScript: string
}

function MessageView({ message }: { message: ChatMessage }) {
  const prefix = message.role === 'user' ? '>' : message.done ? 'GA' : 'GA ...'
  const color = message.role === 'user' ? 'cyan' : message.done ? 'green' : 'yellow'
  return (
    <Box flexDirection="column" marginBottom={1}>
      <Text color={color}>{prefix}</Text>
      <Text>{message.role === 'assistant' ? formatAssistantText(message.text) || ' ' : message.text || ' '}</Text>
    </Box>
  )
}

function visibleMessages(messages: ChatMessage[]): ChatMessage[] {
  return messages.slice(-80)
}

export function App({ python, bridgeScript }: Props) {
  const { exit } = useApp()
  const [state, dispatch] = useReducer(applyBridgeEvent, initialState)
  const [input, setInput] = useState('')
  const bridgeRef = useRef<BridgeClient | null>(null)
  const pasteStore = useMemo(() => createPasteStore(), [])

  useEffect(() => {
    bridgeRef.current = startBridge(python, bridgeScript, dispatch, code => {
      dispatch({ type: 'error', code: 'bridge_exit', message: `bridge exited: ${code ?? 'signal'}` })
    })
    return () => bridgeRef.current?.stop()
  }, [bridgeScript, python])

  useInput((rawInput, key) => {
    if (key.ctrl && rawInput === 'c') {
      bridgeRef.current?.stop()
      exit()
      return
    }
    if (key.ctrl && rawInput === 'j') {
      setInput(value => `${value}\n`)
      return
    }
    if (key.backspace || key.delete) {
      setInput(value => value.slice(0, -1))
      return
    }
    if (key.return) {
      const expanded = expandPastedTextRefs(input, pasteStore).trimEnd()
      if (expanded && state.status !== 'running' && state.status !== 'stopping') {
        bridgeRef.current?.send({ type: 'submit', text: expanded })
        setInput('')
      }
      return
    }
    if (rawInput) {
      setInput(value => value + foldPastedText(rawInput, pasteStore))
    }
  })

  const statusColor = state.status === 'running' ? 'yellow' : state.status === 'idle' ? 'green' : 'gray'
  const shownMessages = visibleMessages(state.messages)
  return (
    <Box flexDirection="column">
      <Box justifyContent="space-between">
        <Text bold>GenericAgent Ink</Text>
        <Text color={statusColor}>{state.status}</Text>
      </Box>
      <Box borderStyle="single" flexDirection="column" paddingX={1} minHeight={12}>
        {shownMessages.length === 0 ? <Text color="gray">Ready.</Text> : shownMessages.map(message => <MessageView key={message.id} message={message} />)}
      </Box>
      {state.error ? <Text color="red">{state.error}</Text> : null}
      <Box borderStyle="round" paddingX={1}>
        <Text color="cyan">❯ </Text>
        <Text>{input}</Text>
      </Box>
      <Text color="gray">Enter send · Ctrl+J newline · Ctrl+C exit</Text>
    </Box>
  )
}
