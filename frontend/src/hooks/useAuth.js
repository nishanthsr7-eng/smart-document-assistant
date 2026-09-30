import { useState, useEffect, useCallback } from 'react'
import * as api from '../api'
import { setUserContext } from '../observability'

export function useAuth() {
  const [user, setUser] = useState(null)
  const [checking, setChecking] = useState(Boolean(api.getToken()))
  const [error, setError] = useState(null)

  // A stored token is only trusted once /auth/me has accepted it: it may have expired, or the
  // signing key may have rotated, while this browser was away.
  useEffect(() => {
    api.onSessionExpired(() => {
      setUser(null)
      setUserContext(null)
    })
    if (!api.getToken()) return
    let cancelled = false
    api
      .getMe()
      .then((me) => {
        if (cancelled) return
        setUser(me)
        setUserContext(me)
      })
      .catch(() => api.setToken(null))
      .finally(() => !cancelled && setChecking(false))
    return () => {
      cancelled = true
    }
  }, [])

  const accept = useCallback((session) => {
    api.setToken(session.access_token)
    setUser(session.user)
    setUserContext(session.user)
    setError(null)
  }, [])

  const signIn = useCallback(
    async (email, password) => {
      setError(null)
      try {
        accept(await api.login(email, password))
      } catch (err) {
        setError(err.message)
      }
    },
    [accept],
  )

  const signUp = useCallback(
    async (tenantName, email, password) => {
      setError(null)
      try {
        accept(await api.register(tenantName, email, password))
      } catch (err) {
        setError(err.message)
      }
    },
    [accept],
  )

  const signOut = useCallback(() => {
    api.setToken(null)
    setUser(null)
    setUserContext(null)
    setError(null)
  }, [])

  return {
    user,
    checking,
    error,
    signIn,
    signUp,
    signOut,
    canUpload: user?.role === 'editor' || user?.role === 'admin',
  }
}
