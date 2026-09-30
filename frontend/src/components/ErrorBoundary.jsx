import { Component } from 'react'
import { t } from '../i18n'
import { reportError } from '../observability'

/**
 * The last line of defence: a render error anywhere below this unmounts the tree, and without a
 * boundary React 19 leaves a blank page with the reason only in the console. A blank page is
 * indistinguishable from an outage to the person looking at it, and it loses the one thing that
 * makes the report actionable -- so this shows the error, and hands over a reference id that is
 * also attached to the report sent to the error tracker.
 */
export default class ErrorBoundary extends Component {
  state = { error: null, reference: null }

  static getDerivedStateFromError(error) {
    return { error, reference: crypto.randomUUID().slice(0, 8) }
  }

  componentDidCatch(error, info) {
    reportError(error, { componentStack: info.componentStack, reference: this.state.reference })
  }

  render() {
    if (!this.state.error) return this.props.children
    return (
      <div className="boundary" role="alert">
        <h1 className="boundary-title">{t('error.title')}</h1>
        <p className="boundary-body">{t('error.body')}</p>
        <p className="boundary-reference">{t('error.reference', { id: this.state.reference })}</p>
        <button
          className="auth-submit boundary-reload"
          type="button"
          onClick={() => window.location.reload()}
        >
          {t('error.reload')}
        </button>
      </div>
    )
  }
}
