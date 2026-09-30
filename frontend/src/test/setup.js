import '@testing-library/jest-dom/vitest'
import { afterEach, vi } from 'vitest'
import { cleanup } from '@testing-library/react'

afterEach(() => {
  cleanup()
  localStorage.clear()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

// A Response whose body is a ReadableStream of SSE frames, which is what the api layer reads.
// jsdom's fetch mock returns whatever we give it, so the frames can be delivered in whatever
// chunking a test wants -- including a frame split across two network reads.
export function sseResponse(chunks, { ok = true, status = 200 } = {}) {
  const encoder = new TextEncoder()
  const body = new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk))
      controller.close()
    },
  })
  return { ok, status, body, headers: new Headers(), json: async () => ({}) }
}

export function frame(event, data) {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`
}


/** A minimal but schema-valid answer payload: the api layer validates every `done` frame. */
export function answerPayload(overrides = {}) {
  return {
    query_id: 'q1',
    status: 'answered',
    answer_text: 'Leave is 25 days.',
    sentences: [{ text: 'Leave is 25 days.', cites: [1], citation_status: 'supported' }],
    sources: [],
    conflicts: [],
    confidence: null,
    abstain_reason: null,
    ...overrides,
  }
}

/** A plain JSON response, with the headers the api layer reads for Retry-After. */
export function jsonResponse(body, { ok = true, status = 200, headers = {} } = {}) {
  return { ok, status, headers: new Headers(headers), json: async () => body }
}
