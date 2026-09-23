const TIPS = [
  { text: 'Attach a PDF or TXT file to begin', action: 'attach' },
  { text: 'Ask a question — answers cite the exact source', action: 'focus' },
  { text: 'Switch retrieval mode (Hybrid + Rerank) for different tradeoffs', action: 'mode' },
  { text: 'Every answer includes citations and a confidence score', action: 'focus' },
]

export default function SuggestionList({ onAttach, onFocusInput, onOpenMode }) {
  const handlers = {
    attach: onAttach,
    focus: onFocusInput,
    mode: onOpenMode,
  }

  return (
    <div className="suggestion-list">
      {TIPS.map((tip, i) => (
        <button
          type="button"
          className="suggestion-row"
          key={i}
          onClick={handlers[tip.action]}
        >
          <span className="suggestion-arrow">&#8618;</span>
          <span className="suggestion-text">{tip.text}</span>
        </button>
      ))}
    </div>
  )
}
