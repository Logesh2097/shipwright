// Thin fetch wrapper: bearer auth, one transparent token refresh, uniform errors.

import type { Tokens } from './types'

const BASE = '/api/v1'
const STORAGE_KEY = 'asea.tokens'

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public details: Record<string, unknown> = {},
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

type Listener = (tokens: Tokens | null) => void

class TokenStore {
  private tokens: Tokens | null = null
  private listeners = new Set<Listener>()

  constructor() {
    try {
      const raw = sessionStorage.getItem(STORAGE_KEY)
      this.tokens = raw ? (JSON.parse(raw) as Tokens) : null
    } catch {
      this.tokens = null
    }
  }

  get(): Tokens | null {
    return this.tokens
  }

  // sessionStorage: tokens disappear when the tab closes and are not shared across tabs.
  set(tokens: Tokens | null): void {
    this.tokens = tokens
    try {
      if (tokens) sessionStorage.setItem(STORAGE_KEY, JSON.stringify(tokens))
      else sessionStorage.removeItem(STORAGE_KEY)
    } catch {
      // Storage can be unavailable (private mode); the in-memory copy still works for this page.
    }
    this.listeners.forEach((listener) => listener(tokens))
  }

  subscribe(listener: Listener): () => void {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }
}

export const tokenStore = new TokenStore()

export type Query = Record<string, string | number | boolean | null | undefined>

function buildUrl(path: string, query?: Query): string {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value !== undefined && value !== null && value !== '') params.set(key, String(value))
  }
  const suffix = params.toString()
  return `${BASE}${path}${suffix ? `?${suffix}` : ''}`
}

async function toError(response: Response): Promise<ApiError> {
  let code = 'http_error'
  let message = `Request failed (${response.status})`
  let details: Record<string, unknown> = {}
  try {
    const body = (await response.json()) as { error?: { code?: string; message?: string; details?: Record<string, unknown> } }
    if (body.error) {
      code = body.error.code ?? code
      message = body.error.message ?? message
      details = body.error.details ?? {}
    }
  } catch {
    // Not JSON (for example a proxy error page); keep the generic message.
  }
  return new ApiError(response.status, code, message, details)
}

let refreshing: Promise<boolean> | null = null

async function refreshTokens(): Promise<boolean> {
  const current = tokenStore.get()
  if (!current) return false
  refreshing ??= (async () => {
    try {
      const response = await fetch(buildUrl('/auth/refresh'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refresh_token: current.refresh_token }),
      })
      if (!response.ok) {
        tokenStore.set(null)
        return false
      }
      tokenStore.set((await response.json()) as Tokens)
      return true
    } catch {
      return false
    } finally {
      refreshing = null
    }
  })()
  return refreshing
}

export interface RequestOptions {
  method?: string
  query?: Query
  body?: unknown
  signal?: AbortSignal
  auth?: boolean
}

export async function rawRequest(path: string, options: RequestOptions = {}, retried = false): Promise<Response> {
  const headers: Record<string, string> = {}
  if (options.body !== undefined) headers['Content-Type'] = 'application/json'
  const tokens = tokenStore.get()
  if (options.auth !== false && tokens) headers.Authorization = `Bearer ${tokens.access_token}`
  let response: Response
  try {
    response = await fetch(buildUrl(path, options.query), {
      method: options.method ?? 'GET',
      headers,
      body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
      signal: options.signal,
    })
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === 'AbortError') throw cause
    throw new ApiError(0, 'network_error', 'Cannot reach the server. Check that the backend is running.')
  }
  if (response.status === 401 && options.auth !== false && tokens && !retried) {
    if (await refreshTokens()) return rawRequest(path, options, true)
  }
  if (!response.ok) throw await toError(response)
  return response
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const response = await rawRequest(path, options)
  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

export const http = {
  get: <T>(path: string, query?: Query, signal?: AbortSignal) => request<T>(path, { query, signal }),
  post: <T>(path: string, body?: unknown, query?: Query) => request<T>(path, { method: 'POST', body: body ?? {}, query }),
  put: <T>(path: string, body?: unknown) => request<T>(path, { method: 'PUT', body: body ?? {} }),
  patch: <T>(path: string, body?: unknown) => request<T>(path, { method: 'PATCH', body: body ?? {} }),
  delete: <T>(path: string) => request<T>(path, { method: 'DELETE' }),
}

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message
  if (error instanceof Error) return error.message
  return 'Something went wrong'
}
