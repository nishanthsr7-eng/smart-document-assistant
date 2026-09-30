// Load profile for the answer path. Run against a stack that is already up and has documents
// ingested; it is not part of CI, because the numbers only mean something on hardware you are
// going to deploy on.
//
//   k6 run -e BASE_URL=http://127.0.0.1:8000 -e EMAIL=... -e PASSWORD=... tests/load/query.js
//
// What it is looking for: the API sheds load with 503 rather than queueing, and the answers that
// do get served stay inside their latency budget while it does. A run where p95 holds and 503s
// appear is a pass. A run with no 503s and a climbing p95 is the failure mode this exists to
// catch -- an unbounded queue behind a fixed thread pool.

import http from 'k6/http'
import { check } from 'k6'
import { Counter, Trend } from 'k6/metrics'

const BASE = __ENV.BASE_URL || 'http://127.0.0.1:8000'
const QUESTION = __ENV.QUESTION || 'What is the annual leave policy?'

const shed = new Counter('answers_shed')
const answered = new Counter('answers_served')
const firstByte = new Trend('answer_ttfb_ms', true)

export const options = {
  scenarios: {
    // Past QUERY_CONCURRENCY the service is meant to refuse, so the ramp deliberately goes well
    // beyond its default of 8.
    ramp: {
      executor: 'ramping-vus',
      stages: [
        { duration: '30s', target: 4 },
        { duration: '1m', target: 16 },
        { duration: '1m', target: 32 },
        { duration: '30s', target: 0 },
      ],
    },
  },
  thresholds: {
    // A refusal is a correct outcome here; a 5xx never is.
    'http_req_failed{expected_response:true}': ['rate<0.01'],
    http_req_duration: ['p(95)<30000'],
    answer_ttfb_ms: ['p(95)<5000'],
  },
}

export function setup() {
  const res = http.post(
    `${BASE}/auth/login`,
    JSON.stringify({ email: __ENV.EMAIL, password: __ENV.PASSWORD }),
    { headers: { 'Content-Type': 'application/json' } }
  )
  check(res, { 'logged in': (r) => r.status === 200 })
  const token = res.json('access_token')

  const docs = http.get(`${BASE}/documents`, { headers: { Authorization: `Bearer ${token}` } })
  return { token, docIds: docs.json().map((d) => d.doc_id) }
}

export default function (data) {
  const res = http.post(
    `${BASE}/query`,
    JSON.stringify({ question: QUESTION, doc_ids: data.docIds, mode: 'hybrid_rerank' }),
    {
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${data.token}` },
      // 429 and 503 are the service working, not failing, so they are not counted as errors.
      responseCallback: http.expectedStatuses(200, 429, 503),
      timeout: '120s',
    }
  )

  if (res.status === 503 || res.status === 429) {
    shed.add(1)
    check(res, { 'a refusal says when to retry': (r) => !!r.headers['Retry-After'] })
    return
  }

  answered.add(1)
  firstByte.add(res.timings.waiting)
  check(res, {
    'the stream is an event stream': (r) => (r.headers['Content-Type'] || '').startsWith('text/event-stream'),
    'the stream ends in a done frame': (r) => r.body.includes('event: done'),
    'no error frame': (r) => !r.body.includes('event: error'),
  })
}
