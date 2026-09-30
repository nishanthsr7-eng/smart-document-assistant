import { useEffect, useId, useRef } from 'react'

export default function IconPopover({ icon, label, open, onToggle, onClose, children }) {
  const rootRef = useRef(null)
  const triggerRef = useRef(null)
  const panelId = useId()

  useEffect(() => {
    if (!open) return
    const onClickOutside = (e) => {
      if (rootRef.current && !rootRef.current.contains(e.target)) onClose()
    }
    const onKeyDown = (e) => {
      if (e.key === 'Escape') {
        onClose()
        // Focus goes back to the button that opened it: closing a panel with Escape must not
        // drop the keyboard user back at the top of the document.
        triggerRef.current?.focus()
      }
    }
    document.addEventListener('mousedown', onClickOutside)
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('mousedown', onClickOutside)
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [open, onClose])

  // Focus the panel when it opens, so Tab continues inside it rather than past it.
  useEffect(() => {
    if (open) rootRef.current?.querySelector('[role="dialog"]')?.focus()
  }, [open])

  return (
    <div className="icon-popover-root" ref={rootRef}>
      <button
        ref={triggerRef}
        type="button"
        className="icon-popover-btn"
        onClick={onToggle}
        aria-label={label}
        aria-expanded={open}
        aria-haspopup="dialog"
        aria-controls={open ? panelId : undefined}
      >
        {icon}
      </button>
      {open && <div id={panelId}>{children}</div>}
    </div>
  )
}
