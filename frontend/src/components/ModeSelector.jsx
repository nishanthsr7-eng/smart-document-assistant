import React, { useEffect, useId, useImperativeHandle, useRef, useState, forwardRef } from 'react'
import { t } from '../i18n'

const label = (mode) => t(`mode.${mode}`)

function ChevronIcon() {
  return (
    <svg viewBox="0 -960 960 960" fill="currentColor" aria-hidden="true" focusable="false">
      <path d="M480-345 240-585l56-56 184 184 184-184 56 56-240 240Z" />
    </svg>
  )
}

/**
 * @typedef {object} ModeSelectorProps
 * @property {string[]} modes
 * @property {string} value
 * @property {(mode: string) => void} onChange
 * @property {boolean} disabled
 */

/** @type {React.ForwardRefExoticComponent<ModeSelectorProps & React.RefAttributes<any>>} */
const ModeSelector = forwardRef(function ModeSelector(
  /** @type {ModeSelectorProps} */ { modes, value, onChange, disabled },
  ref,
) {
  const [open, setOpen] = useState(false)
  const rootRef = useRef(null)
  const menuId = useId()

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
        aria-label={t('mode.label')}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
      >
        {label(value)}
        <ChevronIcon />
      </button>

      {open && (
        <div className="mode-select-menu" id={menuId} role="menu">
          {(modes || []).map((m) => (
            <button
              key={m}
              type="button"
              role="menuitemradio"
              aria-checked={m === value}
              className={`mode-select-option${m === value ? ' active' : ''}`}
              onClick={() => { onChange(m); setOpen(false) }}
            >
              {label(m)}
            </button>
          ))}
        </div>
      )}
    </div>
  )
})

export default ModeSelector
