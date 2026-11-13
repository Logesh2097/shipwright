// Minimal Markdown renderer for agent reports.
//
// The input is untrusted (it is model output that may quote repository content), so every
// character is HTML-escaped first and only a fixed set of tags is ever emitted. Links are
// rendered as text unless they are plain http(s) URLs. No raw HTML passes through.

const ESCAPES: Record<string, string> = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }

export function escapeHtml(text: string): string {
  return text.replace(/[&<>"']/g, (char) => ESCAPES[char] ?? char)
}

function inline(escaped: string): string {
  const codes: string[] = []
  // Code spans are lifted out first so their content is not treated as emphasis or links.
  let text = escaped.replace(/`([^`\n]+)`/g, (_, code: string) => {
    codes.push(`<code>${code}</code>`)
    return `\u0000${codes.length - 1}\u0000`
  })
  text = text.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
  text = text.replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s).,;:!?]|$)/g, '$1<em>$2</em>')
  text = text.replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g, (_, label: string, url: string) => {
    return `<a href="${url}" target="_blank" rel="noopener noreferrer nofollow">${label}</a>`
  })
  return text.replace(/\u0000(\d+)\u0000/g, (_, index: string) => codes[Number(index)] ?? '')
}

export function renderMarkdown(source: string): string {
  const lines = source.replace(/\r\n?/g, '\n').split('\n')
  const html: string[] = []
  let paragraph: string[] = []
  let list: { tag: 'ul' | 'ol'; items: string[] } | null = null
  let fence: string[] | null = null

  const flushParagraph = () => {
    if (paragraph.length) html.push(`<p>${paragraph.map((l) => inline(escapeHtml(l))).join('<br>')}</p>`)
    paragraph = []
  }
  const flushList = () => {
    if (list) html.push(`<${list.tag}>${list.items.map((item) => `<li>${item}</li>`).join('')}</${list.tag}>`)
    list = null
  }

  for (const line of lines) {
    if (fence !== null) {
      if (/^\s*```/.test(line)) {
        html.push(`<pre><code>${escapeHtml(fence.join('\n'))}</code></pre>`)
        fence = null
      } else {
        fence.push(line)
      }
      continue
    }
    if (/^\s*```/.test(line)) {
      flushParagraph()
      flushList()
      fence = []
      continue
    }
    const heading = /^(#{1,4})\s+(.*)$/.exec(line)
    if (heading) {
      flushParagraph()
      flushList()
      const level = Math.min(heading[1]!.length + 1, 5)
      html.push(`<h${level}>${inline(escapeHtml(heading[2]!))}</h${level}>`)
      continue
    }
    if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
      flushParagraph()
      flushList()
      html.push('<hr>')
      continue
    }
    const bullet = /^\s*[-*+]\s+(.*)$/.exec(line)
    const numbered = /^\s*\d+[.)]\s+(.*)$/.exec(line)
    if (bullet || numbered) {
      flushParagraph()
      const tag = bullet ? 'ul' : 'ol'
      if (list && list.tag !== tag) flushList()
      list ??= { tag, items: [] }
      list.items.push(inline(escapeHtml((bullet ?? numbered)![1]!)))
      continue
    }
    if (line.trim() === '') {
      flushParagraph()
      flushList()
      continue
    }
    if (list && /^\s{2,}\S/.test(line)) {
      // Continuation of the previous list item.
      list.items[list.items.length - 1] += ' ' + inline(escapeHtml(line.trim()))
      continue
    }
    flushList()
    paragraph.push(line)
  }
  if (fence !== null) html.push(`<pre><code>${escapeHtml(fence.join('\n'))}</code></pre>`)
  flushParagraph()
  flushList()
  return html.join('\n')
}
