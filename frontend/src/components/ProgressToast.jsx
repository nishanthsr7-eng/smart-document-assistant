export default function ProgressToast({ uploads }) {
  if (!uploads.length) return null
  return (
    <div className="toast-stack">
      {uploads.map((u) => (
        <div className="toast" key={u.id}>
          <div className="toast-filename">{u.filename}</div>
          <div className="toast-bar-track">
            <div className="toast-bar-fill" style={{ width: `${u.progress}%` }} />
          </div>
          <div className={`toast-status ${u.status === 'error' ? 'error' : u.status === 'done' ? 'done' : ''}`}>
            {u.status === 'error'
              ? u.error
              : u.status === 'done'
              ? 'Added'
              : `Uploading… ${u.progress}%`}
          </div>
        </div>
      ))}
    </div>
  )
}
