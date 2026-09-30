/**
 * Fail the build when the bundle grows past its budget.
 *
 * Measured gzipped, because that is what crosses the network, and measured only over what a
 * first visit actually downloads -- the entry chunk, everything it statically imports, and the
 * CSS. A lazily imported chunk (the error tracker) is reported but not counted: it is only
 * fetched when it is configured, and counting it would make the budget a lie in both directions.
 *
 * The budget is a number in package.json rather than a flag here, so raising it is a diff
 * someone has to justify in review.
 */

import { readFileSync, readdirSync, statSync } from 'node:fs'
import { gzipSync } from 'node:zlib'
import { join } from 'node:path'

const DIST = 'dist/assets'
const budgetKb = JSON.parse(readFileSync('package.json', 'utf8')).bundleBudgetKb
if (!budgetKb) {
  console.error('package.json has no bundleBudgetKb')
  process.exit(1)
}

// Anything the entry HTML references with <script> or <link rel=stylesheet> is on the critical
// path; a chunk reached only by a dynamic import is not.
const html = readFileSync('dist/index.html', 'utf8')
const referenced = new Set([...html.matchAll(/(?:src|href)="\/assets\/([^"]+)"/g)].map((m) => m[1]))

let critical = 0
const rows = []
for (const name of readdirSync(DIST)) {
  if (name.endsWith('.map')) continue
  const path = join(DIST, name)
  if (!statSync(path).isFile()) continue
  const gzipped = gzipSync(readFileSync(path)).length
  const counted = referenced.has(name)
  if (counted) critical += gzipped
  rows.push({ name, kb: (gzipped / 1024).toFixed(1), counted })
}

rows.sort((a, b) => Number(b.kb) - Number(a.kb))
for (const row of rows) {
  console.log(`${row.counted ? ' ' : '~'} ${row.kb.padStart(7)} kB  ${row.name}`)
}
console.log('~ = loaded on demand, not counted against the budget')

const totalKb = critical / 1024
console.log(`\nFirst load: ${totalKb.toFixed(1)} kB gzipped, budget ${budgetKb} kB`)
if (totalKb > budgetKb) {
  console.error(
    `\nBundle budget exceeded by ${(totalKb - budgetKb).toFixed(1)} kB. Either make it smaller, ` +
      'or raise bundleBudgetKb in package.json and say why.',
  )
  process.exit(1)
}
