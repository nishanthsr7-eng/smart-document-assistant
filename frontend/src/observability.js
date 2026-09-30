/**
 * Error reporting and product analytics for the SPA.
 *
 * Both sinks are opt-in by environment variable and both are loaded lazily, so a build with
 * neither configured ships neither: the bundle budget in `package.json` is measured against the
 * default build, and a monitoring SDK that everyone pays for and nobody has configured is how
 * that budget quietly stops meaning anything.
 *
 * The backend already reports to Sentry (`SENTRY_DSN`) and traces through OTel, so a browser
 * error and the request it came from can be joined on the release and the request id.
 */

const DSN = import.meta.env.VITE_SENTRY_DSN
const ANALYTICS_ENDPOINT = import.meta.env.VITE_ANALYTICS_ENDPOINT
const RELEASE = import.meta.env.VITE_RELEASE || 'dev'
const ENVIRONMENT = import.meta.env.VITE_DEPLOY_ENV || import.meta.env.MODE

let sentry = null
let started = false

/** Load the reporter if one is configured. Safe to call more than once. */
export async function start() {
  if (started || !DSN) return
  started = true
  sentry = await import('@sentry/react')
  sentry.init({
    dsn: DSN,
    release: RELEASE,
    environment: ENVIRONMENT,
    // The build emits hidden source maps: they are uploaded at deploy time and are not served
    // next to the bundle, so a stack trace is readable here without publishing our source.
    tracesSampleRate: 0,
    // A question and an answer are tenant data. Nothing about a user's documents belongs in an
    // error report, so the default PII capture stays off and breadcrumbs carry no bodies.
    sendDefaultPii: false,
    beforeBreadcrumb: (crumb) => (crumb.category === 'console' ? null : crumb),
  })
}

export function reportError(error, context = {}) {
  if (sentry) sentry.captureException(error, { extra: context })
  else console.error(error, context)
}

export function setUserContext(user) {
  // The id and the tenant, never the email: enough to tell "one tenant" from "everyone" when
  // triaging, and not a mailing list sitting in a third-party service.
  sentry?.setUser(user ? { id: user.user_id, tenant: user.tenant_id } : null)
}

// --- analytics ---

// A fixed vocabulary, not free-form strings: these are the events the SLOs in docs/slos.md are
// written against, so an event nobody declared is a typo rather than a new metric.
export const EVENTS = {
  QUESTION_ASKED: 'question_asked',
  ANSWER_SHOWN: 'answer_shown',
  ANSWER_FAILED: 'answer_failed',
  DOCUMENT_UPLOADED: 'document_uploaded',
  UPLOAD_FAILED: 'upload_failed',
  CITATION_OPENED: 'citation_opened',
}

let queue = []
let flushTimer = null

export function track(event, properties = {}) {
  if (!ANALYTICS_ENDPOINT) return
  queue.push({ event, properties, at: Date.now(), release: RELEASE })
  if (flushTimer === null) flushTimer = setTimeout(flush, 5000)
}

/** Send whatever is queued. Uses `sendBeacon` so a tab being closed still reports its last event. */
export function flush() {
  clearTimeout(flushTimer)
  flushTimer = null
  if (!ANALYTICS_ENDPOINT || queue.length === 0) return
  const body = JSON.stringify({ events: queue })
  queue = []
  if (navigator.sendBeacon) navigator.sendBeacon(ANALYTICS_ENDPOINT, body)
  else fetch(ANALYTICS_ENDPOINT, { method: 'POST', body, keepalive: true }).catch(() => {})
}

if (typeof document !== 'undefined') {
  // `visibilitychange` rather than `unload`: mobile browsers do not reliably fire `unload`.
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') flush()
  })
}
