import { t } from '../i18n'

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
              {s.cites &&
                s.cites.map((id) => (
                  <button
                    key={id}
                    className="cite-chip"
                    onClick={() => onCite(id)}
                    aria-label={t('answer.citation', { id })}
                  >
                    {id}
                  </button>
                ))}
              {s.citation_status === 'unsupported' && (
                <span className="status-label">{t('answer.unsupported')}</span>
              )}
            </span>
            {s.citation_status === 'unsupported' && s.cites?.length > 0 && (
              <div className="answer-sentence-note">
                {t('answer.notFoundIn', { ids: s.cites.join(', ') })}
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}

function AbstainedView({ answer }) {
  const reason = answer.abstain_reason || t('answer.abstainedDefault')
  const nm = answer.near_miss
  const pages = nm && nm.page_start !== nm.page_end ? `${nm.page_start}–${nm.page_end}` : nm?.page_start
  return (
    <>
      <div className="abstained-note">{reason}</div>
      {nm && (
        <div className="abstained-note" style={{ marginTop: '0.5rem' }}>
          <div className="source-head">
            {t('answer.closestMatch', { filename: nm.filename, pages })}
            {nm.section_path?.length > 0 ? ` · ${nm.section_path.join(' > ')}` : ''}{' '}
            {t('answer.score', { score: nm.score?.toFixed(2) })}
          </div>
          <div className="source-text">
            {nm.text?.slice(0, 400)}
            {nm.text?.length > 400 ? '…' : ''}
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
    return <p className="message-text">{t('answer.none')}</p>
  }

  return <Sentences sentences={answer.sentences} onCite={onCite} />
}
