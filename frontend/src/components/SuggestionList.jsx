import { t } from '../i18n'

const TIPS = [
  { key: 'tips.attach', action: 'attach' },
  { key: 'tips.ask', action: 'focus' },
  { key: 'tips.mode', action: 'mode' },
  { key: 'tips.citations', action: 'focus' },
]

export default function SuggestionList({ onAttach, onFocusInput, onOpenMode }) {
  const handlers = {
    attach: onAttach,
    focus: onFocusInput,
    mode: onOpenMode,
  }

  return (
    <div className="suggestion-list">
      {TIPS.map((tip) => (
        <button
          type="button"
          className="suggestion-row"
          key={tip.key}
          onClick={handlers[tip.action]}
        >
          <span className="suggestion-arrow" aria-hidden="true">&#8618;</span>
          <span className="suggestion-text">{t(tip.key)}</span>
        </button>
      ))}
    </div>
  )
}
