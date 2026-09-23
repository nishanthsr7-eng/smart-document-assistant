import { useEffect, useState } from 'react'
import AnswerCard from './AnswerCard'
import SourcesPanel from './SourcesPanel'

const THINKING_STEPS = ['Thinking…', 'Analyzing sources…', 'Checking citations…', 'Composing answer…']

function Thinking() {
  const [step, setStep] = useState(0)
  useEffect(() => {
    const id = setInterval(() => setStep((s) => (s + 1) % THINKING_STEPS.length), 1600)
    return () => clearInterval(id)
  }, [])
  return (
    <div className="thinking">
      <div className="thinking-dots">
        <div className="thinking-dot" />
        <div className="thinking-dot" />
        <div className="thinking-dot" />
      </div>
      <span>{THINKING_STEPS[step]}</span>
    </div>
  )
}

export function Suggestions({ answer, onSubmit }) {
  const suggestions = answer.suggestions || []
  if (!suggestions.length) return null
  return (
    <div className="suggestion-list">
      {suggestions.map((s, i) => (
        <button
          type="button"
          className="suggestion-row"
          key={i}
          onClick={() => onSubmit(s.text)}
        >
          <span className="suggestion-arrow">&#8618;</span>
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
        <div className="error-msg">{message.error}</div>
        <button className="retry-btn" onClick={() => onRetry(message.question)}>
          Retry
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
    <div className="chat-window">
      {messages.map((msg, i) => {
        const isUser = msg.role === 'user'
        return (
          <div key={i} className={`message${isUser ? ' user' : ''}`}>
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
