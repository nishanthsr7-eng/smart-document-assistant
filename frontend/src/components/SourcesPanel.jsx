import { useEffect, useId, useRef, useState } from 'react'
import { t } from '../i18n'

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
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      className={rotated ? 'rotated' : ''}
      aria-hidden="true"
      focusable="false"
    >
      <polyline points="6 9 12 15 18 9" />
    </svg>
  )
}

export default function SourcesPanel({ answer, activeId, onSelect }) {
  const [open, setOpen] = useState(false)
  const cardRefs = useRef({})
  const listId = useId()

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
      <button
        className="sources-toggle"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        aria-controls={listId}
      >
        <ChevronIcon rotated={open} />
        <span>{t('sources.toggle', { count: order.length })}</span>
      </button>
      {open && (
        <div className="sources-list" id={listId}>
          {conf && (
            <div className="confidence-block">
              <div className="confidence-row">
                <span className="verdict-dot" style={{ background: dotColor }} aria-hidden="true" />
                <span>{t('sources.confidence', { label: conf.label })}</span>
              </div>
              {conf.components && (
                <div className="confidence-breakdown">
                  {Object.entries(conf.components).map(([name, value]) => (
                    <div className="confidence-signal" key={name}>
                      <span className="confidence-signal-name">{name}</span>
                      {/* The bar is a picture of the number printed next to it, so it is not a
                          second thing for a screen reader to read out. */}
                      <div className="confidence-signal-bar" aria-hidden="true">
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
              <button
                key={id}
                type="button"
                ref={(el) => {
                  cardRefs.current[id] = el
                }}
                className={`source-card${id === activeId ? ' active' : ''}`}
                onClick={() => onSelect(id)}
                aria-pressed={id === activeId}
                aria-label={t('sources.open', { id, filename: s.filename })}
              >
                <div className="source-card-head">
                  [{id}] {s.filename}
                </div>
                <div className="source-card-meta">
                  {t('sources.page', { pages: s.pages })}
                  {s.score != null ? t('sources.scoreSuffix', { score: s.score.toFixed(2) }) : ''}
                </div>
                {s.text && <div className="source-card-snippet">{s.text}</div>}
              </button>
            )
          })}
        </div>
      )}
    </div>
  )
}
