import * as schemas from './schemas'
import { reportError } from './observability'

// /v1 is the contract; the unversioned paths still resolve, but nothing new should use them.
const BASE = '/api/v1'
// Probes are infrastructure, not product API, and are deliberately not versioned: a health
// check should not have to know which version of the contract is deployed.
const INFRA = '/api'
const TOKEN_KEY = 'sda.access_token'

// The access token is the only client-side session state. Every call carries it as a bearer
// header; the server derives the tenant from the token, never from a request parameter.
let token = localStorage.getItem(TOKEN_KEY)
let onUnauthorized = null

export function setToken(value) {
  token = value
  if (value) localStorage.setItem(TOKEN_KEY, value)
  else localStorage.removeItem(TOKEN_KEY)
}

export function getToken() {
  return token
}

export function onSessionExpired(handler) {
  onUnauthorized = handler
}

export function authHeaders(extra = {}) {
  return token ? { ...extra, Authorization: `Bearer ${token}` } : extra
}

/** An API error carrying the RFC 9457 members the UI can act on. */
export class ApiError extends Error {
  /** @param {string} message @param {{status?: number, type?: string, retryAfter?: number|null}} [details] */
  constructor(message, { status, type, retryAfter } = {}) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.type = type
    this.retryAfter = retryAfter
  }
}

async function failure(res) {
  // Errors are problem+json: `detail` is the sentence to show, `type` is the stable URI to
  // branch on, and Retry-After is what a 429 or a 503 wants the client to wait.
  let body = {}
  try {
    body = await res.json()
  } catch {
    body = {}
  }
  if (res.status === 401) {
    setToken(null)
    onUnauthorized?.()
  }
  const retryAfter = Number(res.headers.get('Retry-After')) || null
  return new ApiError(body.detail || `HTTP ${res.status}`, {
    status: res.status,
    type: body.type,
    retryAfter,
  })
}

/** @param {string} path @param {{schema?: any, what?: string, base?: string} & RequestInit} [init] */
async function request(path, { schema, what, base = BASE, ...options } = {}) {
  const res = await fetch(base + path, { ...options, headers: authHeaders(options.headers) })
  if (!res.ok) throw await failure(res)
  const body = await res.json()
  return schema ? schemas.parse(schema, body, what || path) : body
}

export async function register(tenantName, email, password) {
  return request('/auth/register', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ tenant_name: tenantName, email, password }),
    schema: schemas.session,
    what: 'Registration',
  })
}

export async function login(email, password) {
  return request('/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password }),
    schema: schemas.session,
    what: 'Sign-in',
  })
}

export async function getMe() {
  return request('/auth/me', { schema: schemas.user, what: 'The session check' })
}

export async function getHealth() {
  return request('/health', { base: INFRA, schema: schemas.health, what: 'The health check' })
}

export async function getConfig() {
  return request('/config', { schema: schemas.config, what: 'The config call' })
}

// The collection is paginated and the SPA shows all of a tenant's documents, so it follows the
// cursors to the end rather than showing a first page the user cannot scroll past.
export async function getDocuments() {
  const items = []
  let cursor = null
  do {
    const query = cursor ? `?cursor=${encodeURIComponent(cursor)}` : ''
    const page = await request('/documents' + query, {
      schema: schemas.documentPage,
      what: 'The document list',
    })
    items.push(...page.items)
    cursor = page.next_cursor
  } while (cursor)
  return items
}

export async function uploadDocument(file, onProgress) {
  // XHR rather than fetch: only XHR reports upload progress, and a 90-second ingest with no
  // progress bar is indistinguishable from a hang.
  const raw = await new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open('POST', BASE + '/ingest')
    const headers = authHeaders()
    if (headers.Authorization) xhr.setRequestHeader('Authorization', headers.Authorization)
    // The same bytes retried under this key are one job, not two documents.
    xhr.setRequestHeader('Idempotency-Key', crypto.randomUUID())
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && onProgress) {
        onProgress(Math.round((e.loaded / e.total) * 80))
      }
    }
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        if (onProgress) onProgress(80)
        try {
          resolve(JSON.parse(xhr.responseText))
        } catch {
          reject(new ApiError('The upload response could not be read.', { status: xhr.status }))
        }
        return
      }
      let detail = `HTTP ${xhr.status}`
      let type
      try {
        const body = JSON.parse(xhr.responseText)
        detail = body.detail || detail
        type = body.type
      } catch {
        /* a proxy error page, not problem+json */
      }
      if (xhr.status === 401) {
        setToken(null)
        onUnauthorized?.()
      }
      reject(new ApiError(detail, { status: xhr.status, type }))
    }
    xhr.onerror = () => reject(new ApiError('Network error'))
    const fd = new FormData()
    fd.append('file', file)
    xhr.send(fd)
  })
  return schemas.parse(schemas.job, raw, 'The upload')
}

export async function deleteDocument(docId) {
  return request(`/documents/${encodeURIComponent(docId)}`, {
    method: 'DELETE',
    schema: schemas.deleted,
    what: 'The delete call',
  })
}

/**
 * Ingest progress, reconnected on a dropped connection.
 *
 * This stream is resumable and the answer stream is not, and the difference is where the state
 * lives: job state is in Redis and the endpoint re-sends the current stage on connect, so a
 * reconnect picks up exactly where the socket died. Giving up here would strand a finished
 * document behind a spinner.
 */
/**
 * @param {string} jobId
 * @param {{onProgress?: (stage: string) => void, onDone?: (job: any) => void, onReconnect?: (attempt: number) => void}} [handlers]
 */
export async function streamJobProgress(jobId, { onProgress, onDone, onReconnect } = {}) {
  let attempt = 0
  let finished = false
  while (!finished) {
    try {
      const res = await fetch(`${BASE}/jobs/${encodeURIComponent(jobId)}/events`, {
        headers: authHeaders(),
      })
      if (!res.ok || !res.body) throw await failure(res)
      await readSse(res, (event, data) => {
        if (event === 'progress') onProgress?.(data.stage)
        else if (event === 'done') {
          finished = true
          onDone?.(schemas.parse(schemas.job, data, 'The ingest job'))
        } else if (event === 'error') throw new ApiError(data.detail, { status: 200 })
      })
      // The stream ended without a terminal frame: the socket went, not the job.
      if (!finished) throw new ApiError('The progress stream ended early.')
    } catch (err) {
      if (finished || !isTransient(err) || attempt >= RECONNECT_DELAYS.length) throw err
      const delay = RECONNECT_DELAYS[attempt++]
      onReconnect?.(attempt)
      await sleep(delay)
    }
  }
}

// Full jitter on an exponential base: a backend restart disconnects every open stream at once,
// and a fixed schedule would bring all of them back in the same instant.
const RECONNECT_DELAYS = [500, 1000, 2000, 4000, 8000]
const sleep = (ms) => new Promise((r) => setTimeout(r, Math.round(ms * (0.5 + Math.random() / 2))))

/** A dropped connection or a busy server is worth retrying. A 4xx is the request itself. */
function isTransient(err) {
  if (err.name === 'ApiError' && err.status) return err.status === 503 || err.status >= 500
  return true
}

/**
 * Stream one answer.
 *
 * Reconnection stops at the first token, and that is a correctness limit rather than an
 * oversight: an answer is not stored anywhere a second connection could resume it, so
 * re-asking after tokens have been shown would either duplicate half an answer or silently
 * replace it with a different one. Before the first token there is nothing to lose, so a
 * connection that never got started is retried.
 */
/**
 * @param {string} question
 * @param {string[]} docIds
 * @param {string} mode
 * @param {[string, string][]} history
 * @param {{onToken?: (text: string) => void, onDone?: (answer: any) => void}} [handlers]
 */
export async function streamQuery(question, docIds, mode, history, { onToken, onDone } = {}) {
  let attempt = 0
  for (;;) {
    let started = false
    try {
      const res = await fetch(BASE + '/query', {
        method: 'POST',
        headers: authHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({ question, doc_ids: docIds, mode, history }),
      })
      if (!res.ok || !res.body) throw await failure(res)
      await readSse(res, (event, data) => {
        if (event === 'token') {
          started = true
          onToken?.(data.text)
        } else if (event === 'done') {
          started = true
          onDone?.(schemas.parse(schemas.answer, data, 'The answer'))
        } else if (event === 'error') throw new ApiError(data.detail, { status: 200 })
      })
      return
    } catch (err) {
      if (started || !isTransient(err) || attempt >= QUERY_RETRIES) throw err
      await sleep(err.retryAfter ? err.retryAfter * 1000 : RECONNECT_DELAYS[attempt])
      attempt += 1
    }
  }
}

const QUERY_RETRIES = 2

async function readSse(res, handle) {
  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    let sep
    while ((sep = buffer.indexOf('\n\n')) !== -1) {
      const raw = buffer.slice(0, sep)
      buffer = buffer.slice(sep + 2)
      const eventMatch = raw.match(/^event: (.+)$/m)
      const dataMatch = raw.match(/^data: (.+)$/m)
      if (!dataMatch) continue
      let payload
      try {
        payload = JSON.parse(dataMatch[1])
      } catch (err) {
        // A frame we cannot parse is a contract break worth reporting, not worth crashing on.
        reportError(err, { where: 'sse', frame: raw.slice(0, 200) })
        continue
      }
      handle(eventMatch ? eventMatch[1] : 'message', payload)
    }
  }
}
