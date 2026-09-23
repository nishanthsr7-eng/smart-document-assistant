import { useEffect, useRef } from 'react'

export default function IconPopover({ icon, label, open, onToggle, onClose, children }) {
  const rootRef = useRef(null)

  useEffect(() => {
    if (!open) return
    const onClickOutside = (e) => {
      if (rootRef.current && !rootRef.current.contains(e.target)) onClose()
    }
    const onKeyDown = (e) => {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('mousedown', onClickOutside)
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('mousedown', onClickOutside)
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [open, onClose])

  return (
    <div className="icon-popover-root" ref={rootRef}>
      <button type="button" className="icon-popover-btn" onClick={onToggle} title={label}>
        {icon}
      </button>
      {open && children}
    </div>
  )
}
