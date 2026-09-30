import { t } from '../i18n'

function status(upload) {
  if (upload.status === 'error') return upload.error
  if (upload.status === 'done') return t('upload.done')
  if (upload.status === 'indexing')
    return t('upload.indexing', { stage: upload.stage || t('upload.indexingDefault') })
  return t('upload.uploading', { percent: upload.progress })
}

export default function ProgressToast({ uploads }) {
  if (!uploads.length) return null
  return (
    // A status region, not an alert: an upload finishing is worth announcing, and worth not
    // interrupting whatever is being read at the time.
    <div className="toast-stack" role="status" aria-live="polite" aria-label={t('upload.progress')}>
      {uploads.map((u) => (
        <div className="toast" key={u.id}>
          <div className="toast-filename">{u.filename}</div>
          <div
            className="toast-bar-track"
            role="progressbar"
            aria-valuenow={u.progress}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-label={u.filename}
          >
            <div className="toast-bar-fill" style={{ width: `${u.progress}%` }} />
          </div>
          <div
            className={`toast-status ${
              u.status === 'error' ? 'error' : u.status === 'done' ? 'done' : ''
            }`}
          >
            {status(u)}
          </div>
        </div>
      ))}
    </div>
  )
}
