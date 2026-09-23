import { useState, useRef, useEffect, useImperativeHandle, forwardRef } from 'react'
import ModeSelector from './ModeSelector'

const MAX_CHARS = 2000

function AttachIcon() {
  return (
    <svg viewBox="0 -960 960 960" fill="currentColor">
      <path d="M720-330q0 104-73 177T470-80q-104 0-177-73t-73-177v-370q0-75 52.5-127.5T400-880q75 0 127.5 52.5T580-700v350q0 46-32 78t-78 32q-46 0-78-32t-32-78v-370h80v370q0 13 8.5 21.5T470-300q13 0 21.5-8.5T500-330v-370q0-42-29-71t-71-29q-42 0-71 29t-29 71v370q0 71 49.5 120.5T470-160q71 0 120.5-49.5T640-330v-390h80v390Z" />
    </svg>
  )
}

function SendIcon() {
  return (
    <svg viewBox="0 -960 960 960" fill="currentColor">
      <path d="M440-160v-487L216-423l-56-57 320-320 320 320-56 57-224-224v487h-80Z" />
    </svg>
  )
}

const Composer = forwardRef(function Composer({
  busy,
  hasReady,
  docs,
  scopedIds,
  allSelected,
  modes,
  mode,
  onModeChange,
  onSubmit,
  onUpload,
  onToggleScope,
}, ref) {
  const [text, setText] = useState('')
  const textareaRef = useRef()
  const fileInputRef = useRef()
  const modeSelectorRef = useRef()

  useImperativeHandle(ref, () => ({
    focusInput: () => textareaRef.current?.focus(),
    openAttach: () => fileInputRef.current?.click(),
    openModeMenu: () => modeSelectorRef.current?.open(),
  }))

  // Auto-resize textarea
  useEffect(() => {
    const el = textareaRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = Math.min(el.scrollHeight, 240) + 'px'
  }, [text])

  const canSend = !busy && hasReady && text.trim().length > 0 && text.length <= MAX_CHARS

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      if (canSend) handleSubmit()
    }
  }

  const handleSubmit = () => {
    if (!canSend) return
    const q = text.trim()
    setText('')
    onSubmit(q)
  }

  const attached = [...scopedIds].filter((id) => docs[id]?.status === 'ready')
  const showChips = attached.length > 0 && !allSelected

  const charsLeft = MAX_CHARS - text.length
  const warnChars = charsLeft < 100

  const handleFilePick = (e) => {
    for (const f of e.target.files) onUpload(f)
    e.target.value = ''
  }

  return (
      <div className="composer-wrap">
        {/* Attached doc chips */}
        {showChips && (
          <div className="attached-chips" style={{ paddingLeft: '0.25rem', marginBottom: '0.35rem' }}>
            {attached.map((id) => (
              <button
                key={id}
                className="attached-chip"
                onClick={() => onToggleScope(id)}
                disabled={busy}
              >
                {docs[id].filename}
                <span className="attached-chip-x">✕</span>
              </button>
            ))}
          </div>
        )}

        <div className="composer">
          <textarea
            ref={textareaRef}
            className="composer-textarea"
            placeholder={hasReady ? 'Ask a question…' : 'Upload a document to get started'}
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={handleKeyDown}
            disabled={busy}
            maxLength={MAX_CHARS + 50}
            rows={2}
          />

          <div className="composer-row">
            <div className="composer-left">
              <button
                id="attach-btn"
                className="pill-btn"
                onClick={() => fileInputRef.current?.click()}
                disabled={busy}
                title="Upload documents"
              >
                <AttachIcon />
                Attach
              </button>
              <input
                ref={fileInputRef}
                type="file"
                accept=".pdf,.txt"
                multiple
                onChange={handleFilePick}
                style={{ display: 'none' }}
              />
              <ModeSelector
                ref={modeSelectorRef}
                modes={modes}
                value={mode}
                onChange={onModeChange}
                disabled={busy}
              />
            </div>

            <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
              {text.length > MAX_CHARS - 200 && (
                <span className={`char-count${warnChars ? ' warn' : ''}`}>
                  {charsLeft}
                </span>
              )}
              <button
                id="send-btn"
                className="send-btn"
                onClick={handleSubmit}
                disabled={!canSend}
              >
                <SendIcon />
                Send
              </button>
            </div>
          </div>
        </div>
      </div>
  )
})

export default Composer
