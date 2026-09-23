import { useEffect, useRef, useState } from 'react'

const LABEL_COLORS = { High: '#8b5cf6', Medium: '#fbbf24', Low: '#f87171' }

function citationOrder(sentences, available) {
  const order = []
  for (const sent of sentences || []) {
    for (const id of sent.cites || []) {
      if (available.includes(id) && !order.includes(id)) order.push(id)
    }
  }
  for (const id of available) {
    if (!order.includes(id)) order.push(id)
  }
  return order
}

function ChevronIcon({ rotated }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className={rotated ? 'rotated' : ''}>
      <polyline points="6 9 12 15 18 9" />
    </svg>
  )
}

export default function SourcesPanel({ answer, activeId, onSelect }) {
  const [open, setOpen] = useState(false)
  const cardRefs = useRef({})

  useEffect(() => {
    if (activeId && open) {
      cardRefs.current[activeId]?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
    }
  }, [activeId, open])

  if (answer.status !== 'answered') return null
  const sources = answer.sources || []
  if (sources.length === 0) return null

  const byId = Object.fromEntries(sources.map((s) => [s.id, s]))
  const available = sources.map((s) => s.id)
  const order = citationOrder(answer.sentences, available)

  const conf = answer.confidence
  const dotColor = LABEL_COLORS[conf?.label] || 'var(--text-faint)'

  return (
    <div>
      <button className="sources-toggle" onClick={() => setOpen((o) => !o)}>
        <ChevronIcon rotated={open} />
        <span>How this was answered · {order.length} source{order.length !== 1 ? 's' : ''}</span>
      </button>
      {open && (
        <div className="sources-list">
          {conf && (
            <div className="confidence-block">
              <div className="confidence-row">
                <span className="verdict-dot" style={{ background: dotColor }} />
                <span>{conf.label} confidence</span>
              </div>
              {conf.components && (
                <div className="confidence-breakdown">
                  {Object.entries(conf.components).map(([name, value]) => (
                    <div className="confidence-signal" key={name}>
                      <span className="confidence-signal-name">{name}</span>
                      <div className="confidence-signal-bar">
                        <div
                          className="confidence-signal-fill"
                          style={{ width: `${Math.round(Math.max(0, Math.min(1, value)) * 100)}%` }}
                        />
                      </div>
                      <span className="confidence-signal-value">{value.toFixed(2)}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
          {order.map((id) => {
            const s = byId[id]
            return (
              <div
                key={id}
                ref={(el) => { cardRefs.current[id] = el }}
                className={`source-card${id === activeId ? ' active' : ''}`}
                onClick={() => onSelect(id)}
              >
                <div className="source-card-head">[{id}] {s.filename}</div>
                <div className="source-card-meta">
                  p.{s.pages}{s.score != null ? ` · score ${s.score.toFixed(2)}` : ''}
                </div>
                {s.text && <div className="source-card-snippet">{s.text}</div>}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
