import { useState, useCallback } from 'react'
import * as api from '../api'
import { EVENTS, track } from '../observability'

export function useChat() {
  const [messages, setMessages] = useState([]) // { role, content } | { role:'assistant', answer } | { role:'assistant', error, question }
  const [busy, setBusy] = useState(false)

  const submit = useCallback(async (question, effectiveIds, mode) => {
    if (busy) return
    const history = messages
      .filter((m) => m.role === 'assistant' && m.answer)
      .slice(-3)
      .map((m) => /** @type {[string, string]} */ ([m.question, m.answer.answer_text]))
    setMessages((prev) => [
      ...prev,
      { role: 'user', content: question },
      { role: 'assistant', streaming: true, partialText: '' },
    ])
    setBusy(true)
    const replaceLast = (message) =>
      setMessages((prev) => {
        const next = [...prev]
        next[next.length - 1] = message
        return next
      })
    // No question text and no answer text in analytics: both are tenant data. What is useful
    // and safe is how long it took, whether it abstained, and how sure it was.
    const started = performance.now()
    track(EVENTS.QUESTION_ASKED, { mode, scoped: effectiveIds.length })
    try {
      await api.streamQuery(question, effectiveIds, mode, history, {
        onToken: (text) => replaceLast({ role: 'assistant', streaming: true, partialText: text }),
        onDone: (answer) => {
          replaceLast({ role: 'assistant', answer, question })
          track(EVENTS.ANSWER_SHOWN, {
            status: answer.status,
            confidence: answer.confidence?.label,
            sources: answer.sources.length,
            ms: Math.round(performance.now() - started),
          })
        },
      })
    } catch (err) {
      replaceLast({ role: 'assistant', error: err.message, question })
      track(EVENTS.ANSWER_FAILED, {
        status: err.status,
        ms: Math.round(performance.now() - started),
      })
    } finally {
      setBusy(false)
    }
  }, [busy, messages])

  const retry = useCallback((question, effectiveIds, mode) => {
    submit(question, effectiveIds, mode)
  }, [submit])

  return { messages, busy, submit, retry }
}
