import { useState, useCallback } from 'react'
import * as api from '../api'

export function useChat() {
  const [messages, setMessages] = useState([]) // { role, content } | { role:'assistant', answer } | { role:'assistant', error, question }
  const [busy, setBusy] = useState(false)

  const submit = useCallback(async (question, effectiveIds, mode) => {
    if (busy) return
    const history = messages
      .filter((m) => m.role === 'assistant' && m.answer)
      .slice(-3)
      .map((m) => [m.question, m.answer.answer_text])
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
    try {
      await api.streamQuery(question, effectiveIds, mode, history, {
        onToken: (text) => replaceLast({ role: 'assistant', streaming: true, partialText: text }),
        onDone: (answer) => replaceLast({ role: 'assistant', answer, question }),
      })
    } catch (err) {
      replaceLast({ role: 'assistant', error: err.message, question })
    } finally {
      setBusy(false)
    }
  }, [busy, messages])

  const retry = useCallback((question, effectiveIds, mode) => {
    submit(question, effectiveIds, mode)
  }, [submit])

  return { messages, busy, submit, retry }
}
