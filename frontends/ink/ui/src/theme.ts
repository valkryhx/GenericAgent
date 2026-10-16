export const INK_THEME_NAMES = ['default', 'lightmode'] as const

export type InkThemeName = (typeof INK_THEME_NAMES)[number]

export type InkTheme = {
  name: InkThemeName
  muted: string
  accent: string
  success: string
  warning: string
  error: string
  border: string
  userText: string
  userBackground: string
  code: string
  link: string
  linkUrl: string
  scrollbar: string
}

const defaultTheme: InkTheme = {
  name: 'default',
  muted: 'gray',
  accent: 'cyan',
  success: 'green',
  warning: 'yellow',
  error: 'red',
  border: 'gray',
  userText: 'black',
  userBackground: '#d7d7d7',
  code: 'cyan',
  link: 'cyan',
  linkUrl: 'gray',
  scrollbar: 'gray',
}

const lightmodeTheme: InkTheme = {
  name: 'lightmode',
  muted: '#6b7280',
  accent: '#2563eb',
  success: '#047857',
  warning: '#b45309',
  error: '#b91c1c',
  border: '#9ca3af',
  userText: '#111827',
  userBackground: '#e5e7eb',
  code: '#7c3aed',
  link: '#2563eb',
  linkUrl: '#64748b',
  scrollbar: '#9ca3af',
}

export function getInkTheme(name: InkThemeName): InkTheme {
  return name === 'lightmode' ? lightmodeTheme : defaultTheme
}
