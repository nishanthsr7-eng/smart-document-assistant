import { useCallback, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import * as api from '../api'
import { EVENTS, track } from '../observability'
import { t } from '../i18n'

const KEY = ['documents']

/**
 * The document corpus, split along the line that matters: the list of indexed documents is
 * server state and lives in the query cache, while the scope selection and the upload toasts are
 * client state and live here.
 *
 * Before this split the list was a `useState` map that every mutation had to patch by hand, so
 * "what the server has" and "what this tab last saw" could disagree with nothing to reconcile
 * them. Now a mutation invalidates the query and the list is re-read.
 */
export function useDocuments(enabled) {
  const queryClient = useQueryClient()
  const [scopedIds, setScopedIds] = useState(new Set())
  const [uploads, setUploads] = useState([])
  // Failed uploads have no server row, so they are the one part of the list that is client
  // state: shown until the user retries or reloads, so a rejection is not silent.
  const [failures, setFailures] = useState({})

  const documents = useQuery({ queryKey: KEY, queryFn: api.getDocuments, enabled })

  const patchUpload = useCallback((uploadId, fields) => {
    setUploads((prev) => prev.map((u) => (u.id === uploadId ? { ...u, ...fields } : u)))
  }, [])

  const dismissUpload = useCallback((uploadId, afterMs) => {
    setTimeout(() => setUploads((prev) => prev.filter((u) => u.id !== uploadId)), afterMs)
  }, [])

  const upload = useMutation({
    mutationFn: async (/** @type {File} */ file) => {
      const uploadId = crypto.randomUUID()
      setUploads((prev) => [
        ...prev,
        { id: uploadId, filename: file.name, progress: 0, status: 'uploading', stage: null, error: null },
      ])
      try {
        const job = await api.uploadDocument(file, (p) => patchUpload(uploadId, { progress: p }))
        patchUpload(uploadId, { status: 'indexing', progress: 85, stage: job.stage })
        let report = job.report
        if (!report) {
          await api.streamJobProgress(job.job_id, {
            onProgress: (stage) => patchUpload(uploadId, { stage, progress: 90 }),
            onReconnect: () => patchUpload(uploadId, { stage: t('upload.reconnecting') }),
            onDone: (done) => {
              report = done.report
            },
          })
        }
        patchUpload(uploadId, { status: 'done', progress: 100, stage: null })
        dismissUpload(uploadId, 2500)
        track(EVENTS.DOCUMENT_UPLOADED, { pages: report?.pages, outcome: report?.outcome })
        return report
      } catch (err) {
        patchUpload(uploadId, { status: 'error', error: err.message })
        setFailures((prev) => ({
          ...prev,
          [`failed:${uploadId}`]: { filename: file.name, status: 'failed', error: err.message },
        }))
        dismissUpload(uploadId, 5000)
        track(EVENTS.UPLOAD_FAILED, { status: err.status })
        throw err
      }
    },
    // Both paths: a failed ingest can still have replaced a document, and the list is cheap.
    onSettled: () => queryClient.invalidateQueries({ queryKey: KEY }),
  })

  const remove = useMutation({
    mutationFn: api.deleteDocument,
    onSuccess: (_, docId) => {
      setScopedIds((prev) => {
        const next = new Set(prev)
        next.delete(docId)
        return next
      })
    },
    onSettled: () => queryClient.invalidateQueries({ queryKey: KEY }),
  })

  const removeDoc = useCallback(
    (docId) => {
      if (docId.startsWith('failed:')) {
        setFailures((prev) => {
          const next = { ...prev }
          delete next[docId]
          return next
        })
        return
      }
      remove.mutate(docId)
    },
    [remove],
  )

  // The shape the drawer and the composer read: newest first, failures alongside the real rows.
  const docs = useMemo(() => {
    const map = {}
    for (const doc of [...(documents.data || [])].reverse()) {
      map[doc.doc_id] = {
        filename: doc.filename,
        status: 'ready',
        error: null,
        pages: doc.pages,
        chunks: doc.num_children,
      }
    }
    return { ...failures, ...map }
  }, [documents.data, failures])

  const readyIds = useMemo(
    () => Object.entries(docs).filter(([, e]) => e.status === 'ready').map(([id]) => id),
    [docs],
  )
  const effectiveIds = scopedIds.size > 0 ? readyIds.filter((id) => scopedIds.has(id)) : readyIds

  const toggleScope = useCallback((docId) => {
    setScopedIds((prev) => {
      const next = new Set(prev)
      if (next.has(docId)) next.delete(docId)
      else next.add(docId)
      return next
    })
  }, [])

  const setScope = useCallback((docId, on) => {
    setScopedIds((prev) => {
      const next = new Set(prev)
      if (on) next.add(docId)
      else next.delete(docId)
      return next
    })
  }, [])

  return {
    docs,
    scopedIds,
    uploads,
    readyIds,
    effectiveIds,
    loaded: documents.isSuccess,
    hasReady: readyIds.length > 0,
    allSelected: readyIds.length > 0 && readyIds.every((id) => scopedIds.has(id)),
    uploadFile: (file) => upload.mutate(file),
    removeDoc,
    toggleScope,
    setScope,
    selectAll: useCallback((ids) => setScopedIds(new Set(ids)), []),
    clearScope: useCallback(() => setScopedIds(new Set()), []),
  }
}
