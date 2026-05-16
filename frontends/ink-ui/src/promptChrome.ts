export function inputDivider(width = 64): string {
  return '-'.repeat(Math.max(2, width))
}

export function inputPrompt(input: string): string {
  return `> ${input}`
}
