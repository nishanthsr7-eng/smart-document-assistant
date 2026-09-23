function Sentences({ sentences, onCite }) {
  if (!sentences || sentences.length === 0) return null
  return (
    <div>
      {sentences.map((s, i) => {
        const cls = [
          'answer-sentence',
          s.citation_status === 'weak' ? 'answer-sentence-weak' : '',
          s.citation_status === 'unsupported' ? 'answer-sentence-unsupported' : '',
        ]
          .filter(Boolean)
          .join(' ')

        return (
          <div key={i}>
            <span className={cls}>
              {s.text}
              {s.cites && s.cites.map((id) => (
                <button key={id} className="cite-chip" onClick={() => onCite(id)}>{id}</button>
              ))}
              {s.citation_status === 'unsupported' && (
                <span className="status-label">unsupported</span>
              )}
            </span>
            {s.citation_status === 'unsupported' && s.cites?.length > 0 && (
              <div className="answer-sentence-note">
                not found in [{s.cites.join(', ')}]
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}

function AbstainedView({ answer }) {
  const reason = answer.abstain_reason || "I couldn't find that in the selected documents."
  const nm = answer.near_miss
  return (
    <>
      <div className="abstained-note">{reason}</div>
      {nm && (
        <div className="abstained-note" style={{ marginTop: '0.5rem' }}>
          <div className="source-head">
            Closest match: {nm.filename} p.{nm.page_start}
            {nm.page_start !== nm.page_end ? `–${nm.page_end}` : ''}
            {nm.section_path?.length > 0 ? ` · ${nm.section_path.join(' > ')}` : ''}
            {' '}(score {nm.score?.toFixed(2)})
          </div>
          <div className="source-text">
            {nm.text?.slice(0, 400)}{nm.text?.length > 400 ? '…' : ''}
          </div>
        </div>
      )}
    </>
  )
}

export default function AnswerCard({ answer, onCite }) {
  if (answer.status === 'abstained') {
    return <AbstainedView answer={answer} />
  }
  if (answer.status !== 'answered') {
    return <p className="message-text">No answer was generated.</p>
  }

  return (
    <Sentences sentences={answer.sentences} onCite={onCite} />
  )
}
