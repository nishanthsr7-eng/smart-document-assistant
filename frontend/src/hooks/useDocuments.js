import { useState, useCallback } from 'react'
import * as api from '../api'

export function useDocuments() {
  const [docs, setDocs] = useState({}) // { docId: { filename, status, error, pages, chunks } }
  const [scopedIds, setScopedIds] = useState(new Set())
  const [uploads, setUploads] = useState([]) // [{ id, filename, progress, status, error }]

  const loadFromServer = useCallback(async () => {
    try {
      const list = await api.getDocuments()
      const map = {}
      for (const d of [...list].reverse()) {
        map[d.doc_id] = {
          filename: d.filename,
          status: 'ready',
          error: null,
          pages: d.pages,
          chunks: d.num_children,
        }
      }
      setDocs(map)
    } catch {}
  }, [])

  const uploadFile = useCallback(async (file) => {
    const uploadId = crypto.randomUUID()
    setUploads((prev) => [
      ...prev,
      { id: uploadId, filename: file.name, progress: 0, status: 'uploading', stage: null, error: null },
    ])
    const patch = (fields) =>
      setUploads((prev) => prev.map((u) => (u.id === uploadId ? { ...u, ...fields } : u)))
    try {
      const job = await api.uploadDocument(file, (p) => patch({ progress: p }))
      patch({ status: 'indexing', progress: 85, stage: job.stage })
      let report = job.report
      if (!report) {
        await api.streamJobProgress(job.job_id, {
          onProgress: (stage) => patch({ stage, progress: 90 }),
          onDone: (done) => {
            report = done.report
          },
        })
      }
      setDocs((prev) => ({
        [report.doc_id]: {
          filename: report.filename,
          status: 'ready',
          error: null,
          pages: report.pages,
          chunks: report.num_children,
        },
        ...prev,
      }))
      patch({ status: 'done', progress: 100, stage: null })
      setTimeout(() => {
        setUploads((prev) => prev.filter((u) => u.id !== uploadId))
      }, 2500)
    } catch (err) {
      patch({ status: 'error', error: err.message })
      setDocs((prev) => {
        const n = { ...prev }
        // Store error entry with a temp key
        n['__err_' + uploadId] = {
          filename: file.name,
          status: 'failed',
          error: err.message,
          pages: null,
          chunks: null,
        }
        return n
      })
      setTimeout(() => {
        setUploads((prev) => prev.filter((u) => u.id !== uploadId))
      }, 5000)
    }
  }, [])

  const removeDoc = useCallback(async (docId) => {
    try {
      await api.deleteDocument(docId)
    } catch {}
    setDocs((prev) => {
      const n = { ...prev }
      delete n[docId]
      return n
    })
    setScopedIds((prev) => {
      const n = new Set(prev)
      n.delete(docId)
      return n
    })
  }, [])

  const toggleScope = useCallback((docId) => {
    setScopedIds((prev) => {
      const n = new Set(prev)
      if (n.has(docId)) n.delete(docId)
      else n.add(docId)
      return n
    })
  }, [])

  const setScope = useCallback((docId, on) => {
    setScopedIds((prev) => {
      const n = new Set(prev)
      if (on) n.add(docId)
      else n.delete(docId)
      return n
    })
  }, [])

  const selectAll = useCallback((readyIds) => {
    setScopedIds(new Set(readyIds))
  }, [])

  const clearScope = useCallback(() => setScopedIds(new Set()), [])

  const readyDocs = Object.entries(docs).filter(([, e]) => e.status === 'ready')
  const readyIds = readyDocs.map(([id]) => id)
  const effectiveIds = scopedIds.size > 0
    ? readyIds.filter((id) => scopedIds.has(id))
    : readyIds

  return {
    docs,
    scopedIds,
    uploads,
    readyIds,
    effectiveIds,
    hasReady: readyIds.length > 0,
    allSelected: readyIds.length > 0 && readyIds.every((id) => scopedIds.has(id)),
    loadFromServer,
    uploadFile,
    removeDoc,
    toggleScope,
    setScope,
    selectAll,
    clearScope,
  }
}
