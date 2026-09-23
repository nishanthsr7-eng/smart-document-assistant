export const DOC_DRAG_TYPE = 'application/x-doc-id'

export default function DocumentDrawer({
  docs,
  scopedIds,
  allSelected,
  busy,
  onClose,
  onRemove,
  onToggleScope,
  onSelectAll,
  onClearScope,
}) {
  const entries = Object.entries(docs)
  const readyIds = entries.filter(([, e]) => e.status === 'ready').map(([id]) => id)

  return (
    <div className="icon-popover-panel icon-popover-panel-left">
      <div className="drawer-header">
        <span className="drawer-title">Documents</span>
        <button className="drawer-close" onClick={onClose} aria-label="Close">×</button>
      </div>

      {entries.length === 0 ? (
        <div className="history-empty">No documents yet</div>
      ) : (
        <>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '0.4rem' }}>
            <span style={{ fontSize: '0.74rem', color: 'var(--text-faint)' }}>
              Drag a doc into chat, or
            </span>
            <button
              className={`select-all-btn${allSelected ? ' active' : ''}`}
              onClick={() => allSelected ? onClearScope() : onSelectAll(readyIds)}
              disabled={busy || readyIds.length === 0}
            >
              {allSelected ? 'Clear all' : 'Select all'}
            </button>
          </div>
          <div className="doc-list-scroll">
            {entries.map(([docId, entry]) => (
              <div
                className="doc-row"
                key={docId}
                draggable={entry.status === 'ready'}
                onDragStart={(e) => e.dataTransfer.setData(DOC_DRAG_TYPE, docId)}
              >
                <div className="doc-row-info">
                  <div
                    className={`doc-row-name${entry.status !== 'ready' ? ' failed' : ''}${scopedIds.has(docId) ? ' active' : ''}`}
                    onClick={() => entry.status === 'ready' && onToggleScope(docId)}
                  >
                    {entry.filename}
                  </div>
                </div>
                <button
                  className="doc-remove-btn"
                  onClick={() => onRemove(docId)}
                  disabled={busy}
                  title="Remove"
                >
                  ×
                </button>
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  )
}
