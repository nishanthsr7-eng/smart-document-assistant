import { useEffect, useImperativeHandle, useRef, useState, forwardRef } from 'react'

const LABELS = {
  dense: 'Dense',
  hybrid: 'Hybrid',
  hybrid_rerank: 'Hybrid + Rerank',
}

function ChevronIcon() {
  return (
    <svg viewBox="0 -960 960 960" fill="currentColor">
      <path d="M480-345 240-585l56-56 184 184 184-184 56 56-240 240Z" />
    </svg>
  )
}

const ModeSelector = forwardRef(function ModeSelector({ modes, value, onChange, disabled }, ref) {
  const [open, setOpen] = useState(false)
  const rootRef = useRef(null)

  useImperativeHandle(ref, () => ({
    open: () => setOpen(true),
  }))

  useEffect(() => {
    if (!open) return
    const onClickOutside = (e) => {
      if (rootRef.current && !rootRef.current.contains(e.target)) setOpen(false)
    }
    const onKeyDown = (e) => {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onClickOutside)
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('mousedown', onClickOutside)
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [open])

  return (
    <div className="mode-select-root" ref={rootRef}>
      <button
        type="button"
        className="mode-select-btn"
        disabled={disabled}
        onClick={() => setOpen((o) => !o)}
        title="Retrieval mode"
      >
        {LABELS[value] || value}
        <ChevronIcon />
      </button>

      {open && (
        <div className="mode-select-menu">
          {(modes || []).map((m) => (
            <button
              key={m}
              type="button"
              className={`mode-select-option${m === value ? ' active' : ''}`}
              onClick={() => { onChange(m); setOpen(false) }}
            >
              {LABELS[m] || m}
            </button>
          ))}
        </div>
      )}
    </div>
  )
})

export default ModeSelector
