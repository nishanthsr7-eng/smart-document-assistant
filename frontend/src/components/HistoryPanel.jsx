export default function HistoryPanel({ messages, onClose }) {
  const questions = messages.filter((m) => m.role === 'user')

  return (
    <div className="icon-popover-panel icon-popover-panel-right">
      <div className="drawer-header">
        <span className="drawer-title">History</span>
        <button className="drawer-close" onClick={onClose} aria-label="Close">×</button>
      </div>

      {questions.length === 0 ? (
        <div className="history-empty">No questions yet this session</div>
      ) : (
        <div className="history-list">
          {questions.map((m, i) => (
            <div className="history-row" key={i}>
              {m.content}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
