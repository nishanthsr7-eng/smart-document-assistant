import { useEffect, useState } from 'react'
import AnswerCard from './AnswerCard'
import SourcesPanel from './SourcesPanel'
import { t } from '../i18n'

const THINKING_KEYS = ['chat.thinking_0', 'chat.thinking_1', 'chat.thinking_2', 'chat.thinking_3']

function Thinking() {
  const [step, setStep] = useState(0)
  useEffect(() => {
    const id = setInterval(() => setStep((s) => (s + 1) % THINKING_KEYS.length), 1600)
    return () => clearInterval(id)
  }, [])
  return (
    <div className="thinking">
      {/* Decorative: the status is the text beside it, announced once rather than every 1.6s. */}
      <div className="thinking-dots" aria-hidden="true">
        <div className="thinking-dot" />
        <div className="thinking-dot" />
        <div className="thinking-dot" />
      </div>
      <span>{t(THINKING_KEYS[step])}</span>
    </div>
  )
}

export function Suggestions({ answer, onSubmit }) {
  const suggestions = answer.suggestions || []
  if (!suggestions.length) return null
  return (
    <div className="suggestion-list">
      {suggestions.map((s, i) => (
        <button type="button" className="suggestion-row" key={i} onClick={() => onSubmit(s.text)}>
          <span className="suggestion-arrow" aria-hidden="true">
            &#8618;
          </span>
          <span className="suggestion-text">{s.label || s.text}</span>
        </button>
      ))}
    </div>
  )
}

function AssistantMessage({ message, msgIndex, activeSource, onRetry, onCite }) {
  if (message.error) {
    return (
      <>
        <div className="error-msg" role="alert">
          {message.error}
        </div>
        <button className="retry-btn" onClick={() => onRetry(message.question)}>
          {t('chat.retry')}
        </button>
      </>
    )
  }

  if (message.streaming) {
    if (!message.partialText) return <Thinking />
    return <p className="message-text">{message.partialText}</p>
  }

  const answer = message.answer
  const activeId = activeSource?.msgIndex === msgIndex ? activeSource.sourceId : null
  const cite = (id) => onCite(msgIndex, id)

  return (
    <>
      <AnswerCard answer={answer} onCite={cite} />
      <SourcesPanel answer={answer} activeId={activeId} onSelect={cite} />
    </>
  )
}

export default function ChatWindow({ messages, activeSource, onRetry, onCite }) {
  return (
    // A log, not an alert: new turns are announced in order and do not interrupt what is being
    // read. `aria-live` stays off the token stream itself -- announcing a partial answer word by
    // word as it arrives is unusable, so the finished turn is what gets read.
    <div className="chat-window" role="log" aria-label={t('chat.log')} aria-live="polite" aria-relevant="additions">
      {messages.map((msg, i) => {
        const isUser = msg.role === 'user'
        return (
          <div
            key={i}
            className={`message${isUser ? ' user' : ''}`}
            aria-busy={msg.streaming ? 'true' : undefined}
          >
            <div className="message-body">
              {isUser ? (
                <p className="message-text">{msg.content}</p>
              ) : (
                <AssistantMessage
                  message={msg}
                  msgIndex={i}
                  activeSource={activeSource}
                  onRetry={onRetry}
                  onCite={onCite}
                />
              )}
            </div>
          </div>
        )
      })}
    </div>
  )
}
