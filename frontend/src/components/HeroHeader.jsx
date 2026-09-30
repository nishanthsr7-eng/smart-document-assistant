import { t } from '../i18n'

export default function HeroHeader() {
  return (
    <div className="hero">
      <h1 className="hero-title">{t('app.name')}</h1>
      <p className="hero-sub">{t('app.tagline')}</p>
    </div>
  )
}
