import { describe, expect, it } from 'vitest'

import en from './locales/en'
import { resolveLocale, t } from './i18n'

describe('i18n', () => {
  it('substitutes named placeholders', () => {
    expect(t('documents.remove', { filename: 'policy.pdf' })).toBe('Remove policy.pdf')
  })

  it('picks the plural variant from count', () => {
    expect(t('sources.toggle', { count: 1 })).toContain('1 source')
    expect(t('sources.toggle', { count: 3 })).toContain('3 sources')
  })

  it('returns the key when there is no message, so a gap is visible rather than blank', () => {
    expect(t('nope.not.a.key')).toBe('nope.not.a.key')
  })

  it('leaves a placeholder alone when nothing was passed for it', () => {
    expect(t('documents.remove')).toBe('Remove {filename}')
  })

  it('falls back to the base language, then to English', () => {
    expect(resolveLocale(['en-GB'])).toBe('en')
    expect(resolveLocale(['fr-CA', 'de'])).toBe('en')
    expect(resolveLocale([])).toBe('en')
  })

  it('has no message left as an empty string', () => {
    // A blank message renders as a missing label rather than an obvious bug, so it is worth
    // catching here instead of in the UI.
    expect(Object.entries(en).filter(([, value]) => !value.trim())).toEqual([])
  })
})
