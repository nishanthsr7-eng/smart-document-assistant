import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import ErrorBoundary from './ErrorBoundary'

function Boom() {
  throw new Error('render exploded')
}

describe('ErrorBoundary', () => {
  it('shows a recoverable message instead of a blank page', () => {
    // React logs the caught error itself; silenced so a passing test is not a wall of red.
    vi.spyOn(console, 'error').mockImplementation(() => {})
    render(
      <ErrorBoundary>
        <Boom />
      </ErrorBoundary>,
    )
    expect(screen.getByRole('alert')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /reload/i })).toBeInTheDocument()
  })

  it('shows a reference the user can quote and the report carries', () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    render(
      <ErrorBoundary>
        <Boom />
      </ErrorBoundary>,
    )
    expect(screen.getByText(/Reference: [0-9a-f]{8}/)).toBeInTheDocument()
  })

  it('renders its children when nothing is wrong', () => {
    render(
      <ErrorBoundary>
        <p>all fine</p>
      </ErrorBoundary>,
    )
    expect(screen.getByText('all fine')).toBeInTheDocument()
  })
})
