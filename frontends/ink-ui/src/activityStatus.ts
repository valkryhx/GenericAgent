export const RUNNING_VERBS = [
  'Accomplishing',
  'Architecting',
  'Bootstrapping',
  'Calculating',
  'Channeling',
  'Composing',
  'Computing',
  'Considering',
  'Cooking',
  'Crafting',
  'Crunching',
  'Deciphering',
  'Deliberating',
  'Generating',
  'Hashing',
  'Hyperspacing',
  'Imagining',
  'Inferring',
  'Manifesting',
  'Orchestrating',
  'Percolating',
  'Pondering',
  'Processing',
  'Ruminating',
  'Simmering',
  'Synthesizing',
  'Thinking',
  'Tinkering',
  'Transmuting',
  'Working',
  'Wrangling',
]

export function pickRunningVerb(random = Math.random): string {
  const index = Math.min(RUNNING_VERBS.length - 1, Math.floor(random() * RUNNING_VERBS.length))
  return RUNNING_VERBS[index] ?? 'Working'
}

export function formatElapsed(seconds: number): string {
  const safeSeconds = Math.max(0, Math.floor(seconds))
  if (safeSeconds < 60) return `${safeSeconds}s`
  const minutes = Math.floor(safeSeconds / 60)
  const rest = safeSeconds % 60
  return `${minutes}m ${rest}s`
}

export function formatRunningStatus(seconds: number, label = 'Hyperspacing'): string {
  return `✻ ${label} (${formatElapsed(seconds)})`
}
