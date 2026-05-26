import { INK_THEME_NAMES, type InkThemeName } from './theme.js'

export function moveThemeSelection(selected: number, delta: number): number {
  return Math.max(0, Math.min(INK_THEME_NAMES.length - 1, selected + delta))
}

export function themePanelRows(): number {
  return 1 + INK_THEME_NAMES.length + 1
}

export function themeDescription(themeName: InkThemeName): string {
  return themeName === 'lightmode' ? 'Light terminal theme' : 'Current colors'
}
