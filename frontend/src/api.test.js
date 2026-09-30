import { describe, expect, it, vi, beforeEach } from 'vitest'

import { answerPayload, frame, jsonResponse, sseResponse } from './test/setup'

// The module holds the token in a closure seeded from localStorage at import, so each test file
// imports it fresh rather than sharing that state.
async function loadApi() {
  vi.resetModules()
  return import('./api')
}

const JOB = {
  job_id: 'j1',
  doc_id: 'd1',
  filename: 'policy.pdf',
  status: 'done',
  stage: 'Indexed',
}

describe('auth headers', () => {
  it('sends no Authorization header until a token is set', async () => {
    const api = await loadApi()
    expect(api.authHeaders()).toEqual({})
  })

  it('carries the bearer token and keeps the caller’s own headers', async () => {
    const api = await loadApi()
    api.setToken('tok')
    expect(api.authHeaders({ 'Content-Type': 'application/json' })).toEqual({
      'Content-Type': 'application/json',
      Authorization: 'Bearer tok',
    })
  })

  it('persists the token so a reload stays signed in', async () => {
    const api = await loadApi()
    api.setToken('tok')
    expect(localStorage.getItem('sda.access_token')).toBe('tok')
    const reloaded = await loadApi()
    expect(reloaded.getToken()).toBe('tok')
  })

  it('clears the stored token on sign-out', async () => {
    const api = await loadApi()
    api.setToken('tok')
    api.setToken(null)
    expect(localStorage.getItem('sda.access_token')).toBeNull()
  })
})

describe('failure handling', () => {
  beforeEach(() => vi.stubGlobal('fetch', vi.fn()))

  it('raises the problem document’s detail rather than a status code', async () => {
    const api = await loadApi()
    fetch.mockResolvedValue(
      jsonResponse(
        { type: 'https://smartdocs.dev/problems/invalid-request', detail: 'Malformed cursor.' },
        { ok: false, status: 400 },
      ),
    )
    await expect(api.getDocuments()).rejects.toThrow('Malformed cursor.')
  })

  it('carries the problem type and Retry-After so the caller can act on them', async () => {
    const api = await loadApi()
    fetch.mockResolvedValue(
      jsonResponse(
        { type: 'https://smartdocs.dev/problems/rate-limited', detail: 'Slow down.' },
        { ok: false, status: 429, headers: { 'Retry-After': '7' } },
      ),
    )
    await expect(api.getDocuments()).rejects.toMatchObject({
      status: 429,
      type: 'https://smartdocs.dev/problems/rate-limited',
      retryAfter: 7,
    })
  })

  it('falls back to the status code when the body is not JSON', async () => {
    const api = await loadApi()
    fetch.mockResolvedValue({
      ok: false,
      status: 400,
      headers: new Headers(),
      json: async () => {
        throw new Error('not json')
      },
    })
    await expect(api.getDocuments()).rejects.toThrow('HTTP 400')
  })

  it('drops the token and calls the expiry handler on a 401', async () => {
    const api = await loadApi()
    api.setToken('stale')
    const expired = vi.fn()
    api.onSessionExpired(expired)
    fetch.mockResolvedValue(
      jsonResponse({ detail: 'Not authenticated' }, { ok: false, status: 401 }),
    )

    await expect(api.getDocuments()).rejects.toThrow('Not authenticated')
    expect(expired).toHaveBeenCalledOnce()
    expect(api.getToken()).toBeNull()
    expect(localStorage.getItem('sda.access_token')).toBeNull()
  })

  it('does not retry a request the server refused on its merits', async () => {
    const api = await loadApi()
    fetch.mockResolvedValue(jsonResponse({ detail: 'Nope.' }, { ok: false, status: 403 }))
    await expect(api.streamQuery('q', [], 'dense', [], {})).rejects.toThrow('Nope.')
    expect(fetch).toHaveBeenCalledOnce()
  })
})

describe('response validation', () => {
  beforeEach(() => vi.stubGlobal('fetch', vi.fn()))

  it('names the field when the server sends a shape the client does not expect', async () => {
    const api = await loadApi()
    // `items` is there but a document has lost its filename: the failure has to name that,
    // not surface three components later as a blank row.
    fetch.mockResolvedValue(
      jsonResponse({ items: [{ doc_id: 'd1', pages: 1, num_children: 1, owner_id: 'u1' }], next_cursor: null }),
    )
    await expect(api.getDocuments()).rejects.toThrow(/items\.0\.filename/)
  })

  it('accepts a field the server added that this client does not know about', async () => {
    const api = await loadApi()
    fetch.mockResolvedValue(
      jsonResponse({
        items: [
          {
            doc_id: 'd1',
            filename: 'a.pdf',
            pages: 1,
            num_children: 1,
            owner_id: 'u1',
            language: 'en',
          },
        ],
        next_cursor: null,
      }),
    )
    // An additive API change must stay additive, so unknown keys are allowed through.
    await expect(api.getDocuments()).resolves.toHaveLength(1)
  })
})

describe('getDocuments', () => {
  beforeEach(() => vi.stubGlobal('fetch', vi.fn()))

  it('follows cursors to the end rather than showing only the first page', async () => {
    const api = await loadApi()
    const doc = (id) => ({ doc_id: id, filename: `${id}.pdf`, pages: 1, num_children: 1, owner_id: 'u1' })
    fetch
      .mockResolvedValueOnce(jsonResponse({ items: [doc('d1')], next_cursor: 'cur1' }))
      .mockResolvedValueOnce(jsonResponse({ items: [doc('d2')], next_cursor: null }))

    const docs = await api.getDocuments()

    expect(docs.map((d) => d.doc_id)).toEqual(['d1', 'd2'])
    expect(fetch.mock.calls[0][0]).toBe('/api/v1/documents')
    expect(fetch.mock.calls[1][0]).toBe('/api/v1/documents?cursor=cur1')
  })
})

describe('streamQuery', () => {
  beforeEach(() => vi.stubGlobal('fetch', vi.fn()))

  it('delivers tokens in order and then the done payload', async () => {
    const api = await loadApi()
    fetch.mockResolvedValue(
      sseResponse([
        frame('stage', { stage: 'Retrieving' }),
        frame('token', { text: 'Leave ' }),
        frame('token', { text: 'is 25 days.' }),
        frame('done', answerPayload()),
      ]),
    )
    const tokens = []
    const onDone = vi.fn()

    await api.streamQuery('q', [], 'hybrid_rerank', [], { onToken: (t) => tokens.push(t), onDone })

    expect(tokens.join('')).toBe('Leave is 25 days.')
    expect(onDone).toHaveBeenCalledWith(expect.objectContaining({ status: 'answered' }))
  })

  it('reassembles a frame that arrives split across two reads', async () => {
    const api = await loadApi()
    const whole = frame('done', answerPayload())
    fetch.mockResolvedValue(sseResponse([whole.slice(0, 12), whole.slice(12)]))
    const onDone = vi.fn()

    await api.streamQuery('q', [], 'hybrid_rerank', [], { onDone })

    expect(onDone).toHaveBeenCalledOnce()
  })

  it('ignores the keep-alive comments a quiet stream sends', async () => {
    const api = await loadApi()
    fetch.mockResolvedValue(
      sseResponse([
        ': keep-alive\n\n',
        ': keep-alive\n\n',
        frame('done', answerPayload({ status: 'abstained' })),
      ]),
    )
    const onDone = vi.fn()

    await api.streamQuery('q', [], 'hybrid_rerank', [], { onDone })

    expect(onDone).toHaveBeenCalledOnce()
  })

  it('surfaces an in-band error frame as a rejection', async () => {
    const api = await loadApi()
    fetch.mockResolvedValue(sseResponse([frame('error', { detail: 'provider exploded' })]))

    await expect(api.streamQuery('q', [], 'hybrid_rerank', [], {})).rejects.toThrow(
      'provider exploded',
    )
  })

  it('retries a connection that failed before any token arrived', async () => {
    const api = await loadApi()
    fetch
      .mockRejectedValueOnce(new TypeError('network down'))
      .mockResolvedValueOnce(sseResponse([frame('done', answerPayload())]))
    const onDone = vi.fn()

    await api.streamQuery('q', [], 'dense', [], { onDone })

    expect(fetch).toHaveBeenCalledTimes(2)
    expect(onDone).toHaveBeenCalledOnce()
  })

  it('does not retry once tokens have been shown', async () => {
    const api = await loadApi()
    // The stream dies after a token and before `done`. Re-asking would either duplicate half an
    // answer or silently replace it with a different one, so the half answer plus an error is
    // the honest outcome.
    fetch
      .mockResolvedValueOnce(sseResponse([frame('token', { text: 'Leave ' })]))
      .mockResolvedValueOnce(sseResponse([frame('done', answerPayload())]))
    const onDone = vi.fn()

    await api.streamQuery('q', [], 'dense', [], { onDone })

    expect(fetch).toHaveBeenCalledOnce()
    expect(onDone).not.toHaveBeenCalled()
  })

  it('sends the question, documents and mode the caller asked for', async () => {
    const api = await loadApi()
    api.setToken('tok')
    fetch.mockResolvedValue(sseResponse([frame('done', answerPayload())]))

    await api.streamQuery('what is the policy', ['doc-1'], 'dense', [['user', 'hi']], {})

    const [url, options] = fetch.mock.calls[0]
    expect(url).toBe('/api/v1/query')
    expect(options.headers.Authorization).toBe('Bearer tok')
    expect(JSON.parse(options.body)).toEqual({
      question: 'what is the policy',
      doc_ids: ['doc-1'],
      mode: 'dense',
      history: [['user', 'hi']],
    })
  })
})

describe('streamJobProgress', () => {
  beforeEach(() => vi.stubGlobal('fetch', vi.fn()))

  it('reports each stage and then the finished job', async () => {
    const api = await loadApi()
    fetch.mockResolvedValue(
      sseResponse([
        frame('progress', { status: 'running', stage: 'Parsing' }),
        frame('progress', { status: 'running', stage: 'Embedding' }),
        frame('done', JOB),
      ]),
    )
    const stages = []
    const onDone = vi.fn()

    await api.streamJobProgress('j1', { onProgress: (s) => stages.push(s), onDone })

    expect(stages).toEqual(['Parsing', 'Embedding'])
    expect(onDone).toHaveBeenCalledWith(expect.objectContaining({ job_id: 'j1' }))
  })

  it('reconnects when the stream ends without a terminal frame', async () => {
    const api = await loadApi()
    // Job state lives in Redis and the endpoint re-sends the current stage on connect, so a
    // reconnect resumes where the socket died. Giving up would strand a finished document
    // behind a spinner.
    fetch
      .mockResolvedValueOnce(sseResponse([frame('progress', { status: 'running', stage: 'Parsing' })]))
      .mockResolvedValueOnce(sseResponse([frame('done', JOB)]))
    const onReconnect = vi.fn()
    const onDone = vi.fn()

    await api.streamJobProgress('j1', { onReconnect, onDone })

    expect(onReconnect).toHaveBeenCalledOnce()
    expect(onDone).toHaveBeenCalledOnce()
  })

  it('gives up rather than reconnecting forever', async () => {
    const api = await loadApi()
    // A fresh response each attempt: a Response body is a stream and can only be read once.
    fetch.mockImplementation(async () => sseResponse([]))

    await expect(api.streamJobProgress('j1', {})).rejects.toThrow('ended early')
    // The first attempt plus one per backoff step.
    expect(fetch).toHaveBeenCalledTimes(6)
  }, 30000)

  it('encodes the job id into the path', async () => {
    const api = await loadApi()
    fetch.mockResolvedValue(sseResponse([frame('done', JOB)]))

    await api.streamJobProgress('a/b c', {})

    expect(fetch.mock.calls[0][0]).toBe('/api/v1/jobs/a%2Fb%20c/events')
  })
})

describe('deleteDocument', () => {
  beforeEach(() => vi.stubGlobal('fetch', vi.fn()))

  it('encodes the document id so it cannot escape its path segment', async () => {
    const api = await loadApi()
    fetch.mockResolvedValue(jsonResponse({ status: 'deleted', doc_id: 'x' }))

    await api.deleteDocument('../../etc/passwd')

    expect(fetch.mock.calls[0][0]).toBe('/api/v1/documents/..%2F..%2Fetc%2Fpasswd')
  })
})

describe('probe endpoints', () => {
  beforeEach(() => vi.stubGlobal('fetch', vi.fn()))

  it('reads health from the unversioned path', async () => {
    const api = await loadApi()
    fetch.mockResolvedValue(jsonResponse({ status: 'ok' }))

    await api.getHealth()

    // /health, /livez and /metrics are infrastructure and are deliberately not versioned: a
    // probe should not have to know which contract is deployed. Asking for /api/v1/health is
    // a 404, and the SPA sits on the loading screen forever retrying it.
    expect(fetch.mock.calls[0][0]).toBe('/api/health')
  })

  it('reads config from the versioned path', async () => {
    const api = await loadApi()
    fetch.mockResolvedValue(
      jsonResponse({ retrieval_modes: ['dense'], default_mode: 'dense', max_question_chars: 10 }),
    )

    await api.getConfig()

    expect(fetch.mock.calls[0][0]).toBe('/api/v1/config')
  })
})
