// Parser for unified diffs as produced by the backend (one file per diff).

export type DiffLineKind = 'context' | 'add' | 'del' | 'meta'

export interface DiffLine {
  kind: DiffLineKind
  text: string
  oldNumber: number | null
  newNumber: number | null
}

export interface DiffHunk {
  header: string
  lines: DiffLine[]
}

export interface ParsedDiff {
  oldPath: string | null
  newPath: string | null
  hunks: DiffHunk[]
  additions: number
  deletions: number
}

const HUNK_RE = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/

function stripPrefix(path: string): string | null {
  const cleaned = path.trim().split('\t')[0] ?? ''
  if (cleaned === '/dev/null') return null
  return cleaned.replace(/^[ab]\//, '')
}

export function parseUnifiedDiff(diff: string): ParsedDiff {
  const result: ParsedDiff = { oldPath: null, newPath: null, hunks: [], additions: 0, deletions: 0 }
  let current: DiffHunk | null = null
  let oldNumber = 0
  let newNumber = 0
  const lines = diff.split('\n')
  if (lines[lines.length - 1] === '') lines.pop()

  for (const line of lines) {
    if (current === null) {
      if (line.startsWith('--- ')) {
        result.oldPath = stripPrefix(line.slice(4))
        continue
      }
      if (line.startsWith('+++ ')) {
        result.newPath = stripPrefix(line.slice(4))
        continue
      }
    }
    const hunk = HUNK_RE.exec(line)
    if (hunk) {
      oldNumber = Number(hunk[1])
      newNumber = Number(hunk[3])
      current = { header: line, lines: [] }
      result.hunks.push(current)
      continue
    }
    if (current === null) continue
    if (line.startsWith('\\')) {
      current.lines.push({ kind: 'meta', text: line.slice(2), oldNumber: null, newNumber: null })
    } else if (line.startsWith('+')) {
      current.lines.push({ kind: 'add', text: line.slice(1), oldNumber: null, newNumber: newNumber++ })
      result.additions++
    } else if (line.startsWith('-')) {
      current.lines.push({ kind: 'del', text: line.slice(1), oldNumber: oldNumber++, newNumber: null })
      result.deletions++
    } else {
      current.lines.push({ kind: 'context', text: line.slice(1), oldNumber: oldNumber++, newNumber: newNumber++ })
    }
  }
  return result
}

export function diffStats(diff: string): { additions: number; deletions: number } {
  let additions = 0
  let deletions = 0
  for (const line of diff.split('\n')) {
    if (line.startsWith('+') && !line.startsWith('+++')) additions++
    else if (line.startsWith('-') && !line.startsWith('---')) deletions++
  }
  return { additions, deletions }
}
