import { t } from '../i18n'

export const DOC_DRAG_TYPE = 'application/x-doc-id'

export default function DocumentDrawer({
  docs,
  scopedIds,
  allSelected,
  busy,
  onClose,
  onRemove,
  canRemove,
  onToggleScope,
  onSelectAll,
  onClearScope,
}) {
  const entries = Object.entries(docs)
  const readyIds = entries.filter(([, e]) => e.status === 'ready').map(([id]) => id)

  return (
    <div
      className="icon-popover-panel icon-popover-panel-left"
      role="dialog"
      aria-label={t('documents.panel')}
      tabIndex={-1}
    >
      <div className="drawer-header">
        <span className="drawer-title">{t('documents.title')}</span>
        <button className="drawer-close" onClick={onClose} aria-label={t('common.close')}>
          ×
        </button>
      </div>

      {entries.length === 0 ? (
        <div className="history-empty">{t('documents.empty')}</div>
      ) : (
        <>
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              marginBottom: '0.4rem',
            }}
          >
            <span style={{ fontSize: '0.74rem', color: 'var(--text-faint)' }}>
              {t('documents.dragHint')}
            </span>
            <button
              className={`select-all-btn${allSelected ? ' active' : ''}`}
              onClick={() => (allSelected ? onClearScope() : onSelectAll(readyIds))}
              disabled={busy || readyIds.length === 0}
            >
              {allSelected ? t('documents.clearAll') : t('documents.selectAll')}
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
                  {/* A real button, not a div with onClick: scoping a question to one document
                      was previously unreachable without a mouse, and drag-and-drop -- the other
                      way to do it -- is not a keyboard gesture either. */}
                  <button
                    type="button"
                    className={`doc-row-name${entry.status !== 'ready' ? ' failed' : ''}${
                      scopedIds.has(docId) ? ' active' : ''
                    }`}
                    onClick={() => entry.status === 'ready' && onToggleScope(docId)}
                    disabled={entry.status !== 'ready'}
                    aria-pressed={entry.status === 'ready' ? scopedIds.has(docId) : undefined}
                    aria-label={t('documents.toggleScope', { filename: entry.filename })}
                    title={entry.error || undefined}
                  >
                    {entry.filename}
                  </button>
                </div>
                {canRemove && (
                  <button
                    className="doc-remove-btn"
                    onClick={() => onRemove(docId)}
                    disabled={busy}
                    aria-label={t('documents.remove', { filename: entry.filename })}
                  >
                    ×
                  </button>
                )}
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  )
}
