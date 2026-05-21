export const inputFrameBorderStyle = {
  topLeft: '',
  top: '─',
  topRight: '',
  right: '',
  bottomRight: '',
  bottom: '─',
  bottomLeft: '',
  left: '',
} as const

export function inputPrompt(input: string): string {
  return `> ${input}`
}
