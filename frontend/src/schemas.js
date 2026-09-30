import { z } from 'zod'

// Every response is validated here before it reaches a component. The point is not to distrust
// our own API: it is that the SPA and the API deploy separately, so a field that was renamed or
// dropped should surface as one named error at the boundary rather than as `undefined` three
// components deep, where the stack trace no longer says which call produced it.
//
// Objects are non-strict on purpose. A field the API adds must not break a client that predates
// it -- that is what makes an additive API change additive.

export const user = z.object({
  user_id: z.string(),
  email: z.string(),
  role: z.enum(['viewer', 'editor', 'admin']),
  tenant_id: z.string(),
  tenant_name: z.string(),
})

export const session = z.object({
  access_token: z.string(),
  token_type: z.string(),
  expires_in: z.number(),
  user,
})

export const config = z.object({
  retrieval_modes: z.array(z.string()).min(1),
  default_mode: z.string(),
  max_question_chars: z.number(),
})

export const health = z.object({ status: z.string() })

export const document = z.object({
  doc_id: z.string(),
  filename: z.string(),
  pages: z.number(),
  num_children: z.number(),
  owner_id: z.string(),
})

export const documentPage = z.object({
  items: z.array(document),
  next_cursor: z.string().nullable(),
})

export const ingestReport = z.object({
  doc_id: z.string(),
  filename: z.string(),
  pages: z.number(),
  num_parents: z.number(),
  num_children: z.number(),
  outcome: z.string(),
})

export const job = z.object({
  job_id: z.string(),
  doc_id: z.string(),
  filename: z.string(),
  status: z.enum(['queued', 'running', 'done', 'failed']),
  stage: z.string(),
  error: z.string().nullable().optional(),
  report: ingestReport.nullable().optional(),
})

export const source = z.object({
  id: z.number(),
  filename: z.string(),
  pages: z.string(),
  section_path: z.string(),
  text: z.string(),
  char_span_in_parent: z.array(z.number()),
  score: z.number(),
})

export const answer = z.object({
  query_id: z.string(),
  status: z.string(),
  answer_text: z.string(),
  sentences: z.array(
    z.object({ text: z.string(), cites: z.array(z.number()), citation_status: z.string() }),
  ),
  sources: z.array(source),
  conflicts: z.array(z.object({ claim: z.string(), source_ids: z.array(z.number()) })),
  confidence: z
    .object({ label: z.string(), score: z.number(), components: z.record(z.string(), z.number()) })
    .nullable(),
  abstain_reason: z.string().nullable(),
  suggestions: z.array(z.unknown()).default([]),
  near_miss: z.unknown().nullable().default(null),
  trace: z.unknown().default({}),
})

export const deleted = z.object({ status: z.string(), doc_id: z.string() })

/** Validate, or throw one error that names the call and the first bad field. */
export function parse(schema, value, what) {
  const result = schema.safeParse(value)
  if (result.success) return result.data
  const issue = result.error.issues[0]
  const path = issue.path.join('.') || '(root)'
  throw new Error(`${what} returned an unexpected shape: ${path} ${issue.message}`)
}
