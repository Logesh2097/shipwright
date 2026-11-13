// Server-sent events over fetch (EventSource cannot send an Authorization header).

export interface SseMessage {
  event: string
  data: string
  id: string | null
}

export function parseSseChunk(buffer: string): { messages: SseMessage[]; rest: string } {
  const messages: SseMessage[] = []
  const blocks = buffer.split('\n\n')
  const rest = blocks.pop() ?? ''
  for (const block of blocks) {
    let event = 'message'
    let id: string | null = null
    const data: string[] = []
    for (const line of block.split('\n')) {
      if (line.startsWith(':') || line === '') continue
      const separator = line.indexOf(':')
      const field = separator === -1 ? line : line.slice(0, separator)
      const value = separator === -1 ? '' : line.slice(separator + 1).replace(/^ /, '')
      if (field === 'event') event = value
      else if (field === 'data') data.push(value)
      else if (field === 'id') id = value
    }
    if (data.length) messages.push({ event, data: data.join('\n'), id })
  }
  return { messages, rest }
}

export async function readSse(response: Response, onMessage: (message: SseMessage) => void): Promise<void> {
  if (!response.body) return
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, '\n')
    const parsed = parseSseChunk(buffer)
    buffer = parsed.rest
    parsed.messages.forEach(onMessage)
  }
}
