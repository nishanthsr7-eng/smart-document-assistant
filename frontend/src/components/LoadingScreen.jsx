import { t } from '../i18n'

export default function LoadingScreen({ loaded }) {
  return (
    <div className="loading-screen" role="status" aria-live="polite">
      <div className="hero">
        <h1 className="hero-title">{t('app.name')}</h1>
        <p className="hero-sub">{t('app.tagline')}</p>
      </div>

      {loaded ? (
        <div className="loaded-badge">
          <span className="loaded-check" aria-hidden="true">✓</span>
          <span>{t('loading.ready')}</span>
        </div>
      ) : (
        <>
          <div className="loading-orb" aria-hidden="true">
            <div className="orb-ring" />
            <div className="orb-ring" />
          </div>
          <span className="loading-text">{t('loading.models')}</span>
        </>
      )}
    </div>
  )
}
