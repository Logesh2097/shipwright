import { describe, expect, it } from 'vitest'

import { diffStats, parseUnifiedDiff } from './diff'
import { formatDuration, formatRelative, titleCase, toneFor } from './format'
import { escapeHtml, renderMarkdown } from './markdown'
import { parseSseChunk } from './sse'

const DIFF = `--- a/app/pricing.py
+++ b/app/pricing.py
@@ -8,7 +8,7 @@
     """Return price reduced by percent."""
     if percent < 0 or percent > 100:
         raise ValueError("percent must be between 0 and 100")
-    return price - price * percent
+    return price - price * percent / 100


 def order_total(items):
`

describe('parseUnifiedDiff', () => {
  it('parses paths, hunks and line numbers', () => {
    const parsed = parseUnifiedDiff(DIFF)
    expect(parsed.oldPath).toBe('app/pricing.py')
    expect(parsed.newPath).toBe('app/pricing.py')
    expect(parsed.hunks).toHaveLength(1)
    expect(parsed.additions).toBe(1)
    expect(parsed.deletions).toBe(1)
    const lines = parsed.hunks[0]!.lines
    expect(lines.map((l) => l.kind)).toEqual(['context', 'context', 'context', 'del', 'add', 'context', 'context', 'context'])
    const removed = lines[3]!
    const added = lines[4]!
    expect(removed).toMatchObject({ oldNumber: 11, newNumber: null, text: '    return price - price * percent' })
    expect(added).toMatchObject({ oldNumber: null, newNumber: 11, text: '    return price - price * percent / 100' })
    expect(lines.at(-1)).toMatchObject({ oldNumber: 14, newNumber: 14 })
  })

  it('handles new files, deleted files and missing trailing newlines', () => {
    const created = parseUnifiedDiff('--- /dev/null\n+++ b/docs/NOTES.md\n@@ -0,0 +1,2 @@\n+# Notes\n+text\n\\ No newline at end of file\n')
    expect(created.oldPath).toBeNull()
    expect(created.newPath).toBe('docs/NOTES.md')
    expect(created.additions).toBe(2)
    expect(created.hunks[0]!.lines.at(-1)).toMatchObject({ kind: 'meta', text: 'No newline at end of file' })
    expect(created.hunks[0]!.lines[0]).toMatchObject({ newNumber: 1 })

    const deleted = parseUnifiedDiff('--- a/old.py\n+++ /dev/null\n@@ -1,1 +0,0 @@\n-x = 1\n')
    expect(deleted.newPath).toBeNull()
    expect(deleted.deletions).toBe(1)
  })

  it('does not mistake content lines for file headers', () => {
    // A removed line whose text starts with "-- " renders as "--- ..." inside a hunk.
    const parsed = parseUnifiedDiff('--- a/q.sql\n+++ b/q.sql\n@@ -1,2 +1,2 @@\n--- old comment\n+-- new comment\n select 1\n')
    expect(parsed.oldPath).toBe('q.sql')
    expect(parsed.deletions).toBe(1)
    expect(parsed.additions).toBe(1)
    expect(parsed.hunks[0]!.lines[0]).toMatchObject({ kind: 'del', text: '-- old comment' })
  })

  it('returns no hunks for empty input', () => {
    expect(parseUnifiedDiff('').hunks).toEqual([])
    expect(diffStats(DIFF)).toEqual({ additions: 1, deletions: 1 })
  })
})

describe('renderMarkdown', () => {
  it('renders the supported subset', () => {
    const html = renderMarkdown('# Title\n\nSome **bold** and `code` text.\n\n- one\n- two\n\n1. first\n2. second\n\n---\n\n```\nx < 1\n```')
    expect(html).toContain('<h2>Title</h2>')
    expect(html).toContain('<strong>bold</strong>')
    expect(html).toContain('<code>code</code>')
    expect(html).toContain('<ul><li>one</li><li>two</li></ul>')
    expect(html).toContain('<ol><li>first</li><li>second</li></ol>')
    expect(html).toContain('<hr>')
    expect(html).toContain('<pre><code>x &lt; 1</code></pre>')
  })

  it('escapes HTML and never emits scripts, handlers or unsafe links', () => {
    const hostile = [
      '<script>alert(1)</script>',
      '<img src=x onerror=alert(1)>',
      '[click](javascript:alert(1))',
      '`<b>in code</b>`',
      '**<i>bold</i>**',
      '[ok](https://example.com/a?b=1&c=2)',
      '[quote](https://example.com/"onmouseover="alert(1))',
    ].join('\n\n')
    const html = renderMarkdown(hostile)
    expect(html).not.toMatch(/<script|<img|<i>|<b>/i)
    expect(html).not.toContain('href="javascript:')
    expect(html).toContain('&lt;script&gt;')
    expect(html).toContain('<code>&lt;b&gt;in code&lt;/b&gt;</code>')
    expect(html).toContain('<a href="https://example.com/a?b=1&amp;c=2" target="_blank" rel="noopener noreferrer nofollow">ok</a>')
    // Quotes are escaped before link detection, so an attribute cannot be broken out of.
    expect(html).not.toMatch(/href="[^"]*"\s*onmouseover/)
    const container = document.createElement('div')
    container.innerHTML = html
    expect(container.querySelectorAll('script, img, [onerror], [onmouseover]')).toHaveLength(0)
    for (const link of container.querySelectorAll('a')) expect(link.getAttribute('href')).toMatch(/^https:\/\//)
  })

  it('keeps emphasis markers inside code spans literal', () => {
    expect(renderMarkdown('Use `**kwargs` here')).toContain('<code>**kwargs</code>')
    expect(escapeHtml(`"'&<>`)).toBe('&quot;&#39;&amp;&lt;&gt;')
  })
})

describe('parseSseChunk', () => {
  it('splits complete events and keeps the remainder', () => {
    const { messages, rest } = parseSseChunk('id: 4\nevent: agent_event\ndata: {"a":1}\n\n: keep-alive\n\nevent: status\ndata: {"status":"completed"}\n\nevent: par')
    expect(messages).toEqual([
      { id: '4', event: 'agent_event', data: '{"a":1}' },
      { id: null, event: 'status', data: '{"status":"completed"}' },
    ])
    expect(rest).toBe('event: par')
  })

  it('joins multi-line data', () => {
    expect(parseSseChunk('data: a\ndata: b\n\n').messages[0]).toMatchObject({ event: 'message', data: 'a\nb' })
  })
})

describe('format helpers', () => {
  it('formats durations and relative times', () => {
    expect(formatDuration(450)).toBe('450 ms')
    expect(formatDuration(2500)).toBe('2.5 s')
    expect(formatDuration(125_000)).toBe('2 min 5 s')
    expect(formatDuration(null)).toBe('—')
    const now = new Date('2026-01-01T12:00:00Z')
    expect(formatRelative('2026-01-01T11:59:50Z', now)).toBe('just now')
    expect(formatRelative('2026-01-01T11:30:00Z', now)).toBe('30 min ago')
    expect(formatRelative('2026-01-01T09:00:00Z', now)).toBe('3 h ago')
    expect(formatRelative(null, now)).toBe('—')
  })

  it('maps statuses to tones without treating "not executed" as success', () => {
    expect(toneFor('passed')).toBe('ok')
    expect(toneFor('failed')).toBe('bad')
    expect(toneFor('not_executed')).toBe('warn')
    expect(toneFor('denied')).toBe('bad')
    expect(toneFor('awaiting_approval')).toBe('warn')
    expect(toneFor('something_new')).toBe('neutral')
    expect(titleCase('awaiting_approval')).toBe('Awaiting approval')
    expect(titleCase(null)).toBe('—')
  })
})

describe('paging', () => {
  it('shows five rows a page and keeps the page in range', async () => {
    const { clampPage, pageCount, pageOf, rangeText, PAGE_SIZE } = await import('./paging')
    const rows = Array.from({ length: 12 }, (_, index) => index + 1)
    expect(PAGE_SIZE).toBe(5)
    expect(pageCount(12)).toBe(3)
    expect(pageCount(0)).toBe(1)
    expect(pageOf(rows, 1)).toEqual([1, 2, 3, 4, 5])
    expect(pageOf(rows, 3)).toEqual([11, 12])
    expect(pageOf(rows, 99)).toEqual([11, 12])
    expect(clampPage(0, 12)).toBe(1)
    expect(rangeText(12, 2)).toBe('6–10 of 12')
    expect(rangeText(0, 1)).toBe('0 of 0')
  })
})
