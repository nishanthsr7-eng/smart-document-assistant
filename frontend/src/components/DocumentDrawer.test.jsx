import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import DocumentDrawer from './DocumentDrawer'

const docs = {
  d1: { filename: 'policy.pdf', status: 'ready', error: null, pages: 3, chunks: 12 },
  'failed:1': { filename: 'broken.pdf', status: 'failed', error: 'Encrypted.' },
}

function renderDrawer(props = {}) {
  return render(
    <DocumentDrawer
      docs={docs}
      scopedIds={new Set()}
      allSelected={false}
      busy={false}
      canRemove
      onClose={vi.fn()}
      onRemove={vi.fn()}
      onToggleScope={vi.fn()}
      onSelectAll={vi.fn()}
      onClearScope={vi.fn()}
      {...props}
    />,
  )
}

describe('DocumentDrawer', () => {
  it('scopes a question to a document from the keyboard', async () => {
    // Drag-and-drop is the other way to do this and is not a keyboard gesture, so if this row
    // is not a button there is no way to scope a question without a mouse.
    const onToggleScope = vi.fn()
    renderDrawer({ onToggleScope })

    const row = screen.getByRole('button', { name: 'Ask only about policy.pdf' })
    row.focus()
    expect(row).toHaveFocus()
    await userEvent.keyboard('{Enter}')

    expect(onToggleScope).toHaveBeenCalledWith('d1')
  })

  it('names each control after the document it acts on', () => {
    renderDrawer()
    expect(screen.getByRole('button', { name: 'Ask only about policy.pdf' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Remove policy.pdf' })).toBeInTheDocument()
  })

  it('reports whether a document is in scope', () => {
    renderDrawer({ scopedIds: new Set(['d1']) })
    expect(screen.getByRole('button', { name: 'Ask only about policy.pdf' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
  })

  it('does not offer to scope a question to a document that failed to ingest', () => {
    renderDrawer()
    expect(screen.getByRole('button', { name: 'Ask only about broken.pdf' })).toBeDisabled()
  })
})
