import { useState } from 'react'
import { t } from '../i18n'

const MIN_PASSWORD_CHARS = 10

export default function AuthScreen({ error, onSignIn, onSignUp }) {
  const [signingUp, setSigningUp] = useState(false)
  const [tenantName, setTenantName] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)

  const submit = async (e) => {
    e.preventDefault()
    setBusy(true)
    if (signingUp) await onSignUp(tenantName, email, password)
    else await onSignIn(email, password)
    setBusy(false)
  }

  return (
    <div className="auth-screen">
      <form className="auth-card" onSubmit={submit} aria-describedby="auth-note">
        <h1 className="auth-title">{t('app.name')}</h1>
        <p className="auth-sub">{signingUp ? t('auth.signUpTitle') : t('auth.signInTitle')}</p>

        {signingUp && (
          <label className="auth-field">
            <span>{t('auth.organization')}</span>
            <input
              value={tenantName}
              onChange={(e) => setTenantName(e.target.value)}
              placeholder={t('auth.organizationPlaceholder')}
              minLength={2}
              required
              autoComplete="organization"
            />
          </label>
        )}

        <label className="auth-field">
          <span>{t('auth.email')}</span>
          <input
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder={t('auth.emailPlaceholder')}
            required
            autoComplete="username"
          />
        </label>

        <label className="auth-field">
          <span>{t('auth.password')}</span>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder={signingUp ? t('auth.passwordHint', { min: MIN_PASSWORD_CHARS }) : ''}
            minLength={signingUp ? MIN_PASSWORD_CHARS : 1}
            required
            autoComplete={signingUp ? 'new-password' : 'current-password'}
          />
        </label>

        {/* A failed sign-in has to reach a screen reader, and it appears without focus moving. */}
        {error && (
          <p className="auth-error" role="alert">
            {error}
          </p>
        )}

        <button className="auth-submit" type="submit" disabled={busy}>
          {busy ? t('auth.working') : signingUp ? t('auth.signUp') : t('auth.signIn')}
        </button>

        <button className="auth-toggle" type="button" onClick={() => setSigningUp((s) => !s)}>
          {signingUp ? t('auth.haveAccount') : t('auth.needAccount')}
        </button>

        <p className="auth-note" id="auth-note">
          {t('auth.note')}
        </p>
      </form>
    </div>
  )
}
