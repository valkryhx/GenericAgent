export function transcriptScrollStep(viewportRows: number): number {
  return Math.max(1, Math.floor(Math.max(0, viewportRows) / 2))
}

export function scrollTranscriptBy(currentOffset: number, delta: number): number {
  return Math.max(0, Math.floor(currentOffset + delta))
}
