import { t } from '../i18n'

export default function HistoryPanel({ messages, onClose }) {
  const questions = messages.filter((m) => m.role === 'user')

  return (
    <div
      className="icon-popover-panel icon-popover-panel-right"
      role="dialog"
      aria-label={t('history.panel')}
      tabIndex={-1}
    >
      <div className="drawer-header">
        <span className="drawer-title">{t('history.title')}</span>
        <button className="drawer-close" onClick={onClose} aria-label={t('common.close')}>
          ×
        </button>
      </div>

      {questions.length === 0 ? (
        <div className="history-empty">{t('history.empty')}</div>
      ) : (
        <ul className="history-list">
          {questions.map((m, i) => (
            <li className="history-row" key={i}>
              {m.content}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
