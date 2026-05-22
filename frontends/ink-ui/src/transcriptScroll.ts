export function transcriptScrollStep(viewportRows: number): number {
  return Math.max(1, Math.floor(Math.max(0, viewportRows) / 2))
}

export function transcriptWheelStep(): number {
  return 3
}

export function maxTranscriptScrollOffset(totalRows: number, viewportRows: number): number {
  return Math.max(0, Math.floor(totalRows) - Math.max(1, Math.floor(viewportRows)))
}

export function clampTranscriptScrollOffset(offset: number, totalRows: number, viewportRows: number): number {
  return Math.min(maxTranscriptScrollOffset(totalRows, viewportRows), Math.max(0, Math.floor(offset)))
}

export function scrollTranscriptBy(currentOffset: number, delta: number, totalRows: number, viewportRows: number): number {
  return clampTranscriptScrollOffset(currentOffset + delta, totalRows, viewportRows)
}

export function preserveTranscriptScrollOnContentChange(
  currentOffset: number,
  previousTotalRows: number,
  nextTotalRows: number,
  viewportRows: number,
): number {
  if (currentOffset <= 0) return 0
  const deltaRows = Math.floor(nextTotalRows) - Math.floor(previousTotalRows)
  return clampTranscriptScrollOffset(currentOffset + Math.max(0, deltaRows), nextTotalRows, viewportRows)
}
