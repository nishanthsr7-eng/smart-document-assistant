import en from './locales/en'

/**
 * Translation, in about forty lines.
 *
 * A library would buy pluralization rules for languages this catalogue does not yet have, lazy
 * catalogue loading, and a message format with genders and dates. None of that is load-bearing
 * for one locale, and all of it is bundle. What is load-bearing is that every user-visible
 * string lives in `locales/` rather than inline in a component, because that is the part that is
 * expensive to retrofit -- adding a locale later is then a file, and swapping this module for
 * `react-intl` is then a mechanical change with nothing to extract first.
 */

const CATALOGS = { en }
const FALLBACK = 'en'

/** The first requested language we actually have a catalogue for. */
export function resolveLocale(requested = navigator.languages || [navigator.language || FALLBACK]) {
  for (const tag of requested) {
    if (!tag) continue
    const exact = tag.toLowerCase()
    if (CATALOGS[exact]) return exact
    const base = exact.split('-')[0]
    if (CATALOGS[base]) return base
  }
  return FALLBACK
}

export const locale = resolveLocale()

function interpolate(template, vars) {
  return template.replace(/\{(\w+)\}/g, (match, name) =>
    Object.prototype.hasOwnProperty.call(vars, name) ? String(vars[name]) : match,
  )
}

/**
 * Look up `key`, substituting `{name}` placeholders from `vars`.
 *
 * `vars.count` selects a `_one` / `_other` variant when one exists. A missing key returns the
 * key itself: a visibly untranslated string in the UI is easier to find and fix than a blank.
 */
export function t(key, vars = {}) {
  const catalog = CATALOGS[locale] || CATALOGS[FALLBACK]
  let resolved = key
  if ('count' in vars) {
    const variant = `${key}_${vars.count === 1 ? 'one' : 'other'}`
    if (catalog[variant] !== undefined) resolved = variant
  }
  const template = catalog[resolved] ?? CATALOGS[FALLBACK][resolved]
  if (template === undefined) return key
  return interpolate(template, vars)
}
