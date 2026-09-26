import { useState } from 'react'

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
      <form className="auth-card" onSubmit={submit}>
        <h1 className="auth-title">Smart Document Assistant</h1>
        <p className="auth-sub">
          {signingUp ? 'Create a workspace' : 'Sign in to your workspace'}
        </p>

        {signingUp && (
          <label className="auth-field">
            <span>Organization</span>
            <input
              value={tenantName}
              onChange={(e) => setTenantName(e.target.value)}
              placeholder="Acme Inc"
              minLength={2}
              required
              autoComplete="organization"
            />
          </label>
        )}

        <label className="auth-field">
          <span>Email</span>
          <input
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="you@acme.com"
            required
            autoComplete="username"
          />
        </label>

        <label className="auth-field">
          <span>Password</span>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder={signingUp ? 'At least 10 characters' : ''}
            minLength={signingUp ? 10 : 1}
            required
            autoComplete={signingUp ? 'new-password' : 'current-password'}
          />
        </label>

        {error && <p className="auth-error">{error}</p>}

        <button className="auth-submit" type="submit" disabled={busy}>
          {busy ? 'Working…' : signingUp ? 'Create workspace' : 'Sign in'}
        </button>

        <button
          className="auth-toggle"
          type="button"
          onClick={() => setSigningUp((s) => !s)}
        >
          {signingUp ? 'I already have an account' : 'Create a new workspace'}
        </button>

        <p className="auth-note">
          Documents are scoped to your workspace. The first user of a new workspace is its admin
          and can invite others.
        </p>
      </form>
    </div>
  )
}
