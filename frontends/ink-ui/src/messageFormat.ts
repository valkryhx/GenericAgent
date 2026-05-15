export function formatAssistantText(raw: string): string {
  let text = raw || ''
  text = text.replace(/<summary>([\s\S]*?)<\/summary>/g, (_match, summary) => `Summary: ${String(summary).trim()}`)
  text = text.replace(
    /🛠\s*Tool:\s*`([^`]+)`\s*📥\s*args:\s*\n````text\n[\s\S]*?\n````/g,
    (_match, name) => `Tool: ${name} (args hidden)`,
  )
  text = text.replace(/`{4,}/g, '```')
  text = text.replace(/\n{4,}/g, '\n\n\n')
  return text.trimEnd()
}
