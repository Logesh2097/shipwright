import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError, errorMessage, http, tokenStore } from './client'

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
}

const TOKENS = { access_token: 'access-1', refresh_token: 'refresh-1', expires_in: 1800 }

describe('api client', () => {
  let fetchMock: ReturnType<typeof vi.fn>

  beforeEach(() => {
    fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    tokenStore.set(null)
  })
  afterEach(() => vi.unstubAllGlobals())

  it('sends the bearer token and builds query strings without empty values', async () => {
    tokenStore.set(TOKENS)
    fetchMock.mockResolvedValueOnce(json({ items: [], total: 0 }))
    await http.get('/tasks', { status: 'running', repository_id: undefined, limit: 20, q: '' })
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/v1/tasks?status=running&limit=20')
    expect((init.headers as Record<string, string>).Authorization).toBe('Bearer access-1')
  })

  it('turns the error envelope into ApiError', async () => {
    fetchMock.mockResolvedValueOnce(json({ error: { code: 'duplicate_task', message: 'Already running', details: { task_id: 't1' } } }, 409))
    const error = await http.post('/tasks', {}).catch((e: unknown) => e)
    expect(error).toBeInstanceOf(ApiError)
    expect(error).toMatchObject({ status: 409, code: 'duplicate_task', message: 'Already running', details: { task_id: 't1' } })
    expect(errorMessage(error)).toBe('Already running')
  })

  it('reports network failures and non-JSON errors readably', async () => {
    fetchMock.mockRejectedValueOnce(new TypeError('Failed to fetch'))
    await expect(http.get('/x')).rejects.toMatchObject({ code: 'network_error', status: 0 })
    fetchMock.mockResolvedValueOnce(new Response('<html>Bad gateway</html>', { status: 502 }))
    await expect(http.get('/x')).rejects.toMatchObject({ code: 'http_error', status: 502 })
  })

  it('refreshes an expired access token once and retries the request', async () => {
    tokenStore.set(TOKENS)
    fetchMock
      .mockResolvedValueOnce(json({ error: { code: 'token_expired', message: 'Token has expired' } }, 401))
      .mockResolvedValueOnce(json({ access_token: 'access-2', refresh_token: 'refresh-2', expires_in: 1800 }))
      .mockResolvedValueOnce(json({ id: 'u1' }))
    await expect(http.get('/auth/me')).resolves.toEqual({ id: 'u1' })
    expect(fetchMock).toHaveBeenCalledTimes(3)
    expect(fetchMock.mock.calls[1]![0]).toBe('/api/v1/auth/refresh')
    expect(JSON.parse((fetchMock.mock.calls[1]![1] as RequestInit).body as string)).toEqual({ refresh_token: 'refresh-1' })
    expect(((fetchMock.mock.calls[2]![1] as RequestInit).headers as Record<string, string>).Authorization).toBe('Bearer access-2')
    expect(tokenStore.get()?.access_token).toBe('access-2')
  })

  it('signs out when the refresh token is rejected, without looping', async () => {
    tokenStore.set(TOKENS)
    const listener = vi.fn()
    const unsubscribe = tokenStore.subscribe(listener)
    fetchMock
      .mockResolvedValueOnce(json({ error: { code: 'token_expired', message: 'Token has expired' } }, 401))
      .mockResolvedValueOnce(json({ error: { code: 'unauthenticated', message: 'Invalid token' } }, 401))
    await expect(http.get('/auth/me')).rejects.toMatchObject({ status: 401 })
    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(tokenStore.get()).toBeNull()
    expect(listener).toHaveBeenCalledWith(null)
    unsubscribe()
  })

  it('returns undefined for 204 responses', async () => {
    fetchMock.mockResolvedValueOnce(new Response(null, { status: 204 }))
    await expect(http.delete('/projects/1')).resolves.toBeUndefined()
  })
})
