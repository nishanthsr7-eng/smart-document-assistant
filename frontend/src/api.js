const BASE = '/api'

async function request(path, options = {}) {
  const res = await fetch(BASE + path, options)
  if (!res.ok) {
    let detail = `HTTP ${res.status}`
    try {
      const body = await res.json()
      detail = body.detail || detail
    } catch {}
    throw new Error(detail)
  }
  return res.json()
}

export async function getHealth() {
  return request('/health')
}

export async function getConfig() {
  return request('/config')
}

export async function getDocuments() {
  return request('/documents')
}

export async function uploadDocument(file, onProgress) {
  // FormData upload with XHR so we can track progress
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open('POST', BASE + '/ingest')
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && onProgress) {
        onProgress(Math.round((e.loaded / e.total) * 80))
      }
    }
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          resolve(JSON.parse(xhr.responseText))
        } catch {
          resolve({})
        }
        if (onProgress) onProgress(80)
      } else {
        let msg = `HTTP ${xhr.status}`
        try { msg = JSON.parse(xhr.responseText).detail || msg } catch {}
        reject(new Error(msg))
      }
    }
    xhr.onerror = () => reject(new Error('Network error'))
    const fd = new FormData()
    fd.append('file', file)
    xhr.send(fd)
  })
}

// The upload returns a queued job; indexing progress arrives on this stream.
export async function streamJobProgress(jobId, { onProgress, onDone } = {}) {
  const res = await fetch(`${BASE}/jobs/${encodeURIComponent(jobId)}/events`)
  if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`)
  await readSse(res, (event, data) => {
    if (event === 'progress') onProgress?.(data.stage)
    else if (event === 'done') onDone?.(data)
    else if (event === 'error') throw new Error(data.detail)
  })
}

export async function deleteDocument(docId) {
  return request(`/documents/${encodeURIComponent(docId)}`, { method: 'DELETE' })
}

export async function streamQuery(question, docIds, mode, history, { onToken, onDone } = {}) {
  const res = await fetch(BASE + '/query', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question, doc_ids: docIds, mode, history }),
  })
  if (!res.ok || !res.body) {
    let detail = `HTTP ${res.status}`
    try {
      detail = (await res.json()).detail || detail
    } catch {}
    throw new Error(detail)
  }

  await readSse(res, (event, data) => {
    if (event === 'token') onToken?.(data.text)
    else if (event === 'done') onDone?.(data)
    else if (event === 'error') throw new Error(data.detail)
  })
}

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
      handle(eventMatch ? eventMatch[1] : 'message', JSON.parse(dataMatch[1]))
    }
  }
}
