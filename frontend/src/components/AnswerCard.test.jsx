import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import AnswerCard from './AnswerCard'

const answered = (sentences) => ({ status: 'answered', sentences })

describe('AnswerCard', () => {
  it('renders each sentence with a chip per citation', () => {
    render(
      <AnswerCard
        answer={answered([{ text: 'Leave is 25 days.', cites: [1, 2], citation_status: 'supported' }])}
        onCite={vi.fn()}
      />
    )
    expect(screen.getByText('Leave is 25 days.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Show source 1' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Show source 2' })).toBeInTheDocument()
  })

  it('opens the source a citation points at', async () => {
    const onCite = vi.fn()
    render(
      <AnswerCard answer={answered([{ text: 'Leave is 25 days.', cites: [3] }])} onCite={onCite} />
    )
    await userEvent.click(screen.getByRole('button', { name: 'Show source 3' }))
    expect(onCite).toHaveBeenCalledWith(3)
  })

  it('marks an unsupported sentence and names the sources that failed to support it', () => {
    render(
      <AnswerCard
        answer={answered([{ text: 'Leave is 40 days.', cites: [1, 2], citation_status: 'unsupported' }])}
        onCite={vi.fn()}
      />
    )
    // The trust signal is the whole point of the citation checker: a sentence the verifier could
    // not ground must be visibly different from one it could.
    expect(screen.getByText('unsupported')).toBeInTheDocument()
    expect(screen.getByText('not found in [1, 2]')).toBeInTheDocument()
  })

  it('shows the abstention reason instead of an empty answer', () => {
    render(
      <AnswerCard
        answer={{ status: 'abstained', abstain_reason: 'Not in the selected documents.' }}
        onCite={vi.fn()}
      />
    )
    expect(screen.getByText('Not in the selected documents.')).toBeInTheDocument()
  })

  it('offers the closest match when an abstention has one', () => {
    render(
      <AnswerCard
        answer={{
          status: 'abstained',
          near_miss: {
            filename: 'policy.pdf',
            page_start: 4,
            page_end: 5,
            section_path: ['Leave'],
            score: 0.274,
            text: 'Carryover is capped.',
          },
        }}
        onCite={vi.fn()}
      />
    )
    expect(screen.getByText(/Closest match: policy.pdf p.4–5/)).toBeInTheDocument()
    expect(screen.getByText(/score 0.27/)).toBeInTheDocument()
    expect(screen.getByText('Carryover is capped.')).toBeInTheDocument()
  })

  it('falls back to a plain message for any other status', () => {
    render(<AnswerCard answer={{ status: 'error' }} onCite={vi.fn()} />)
    expect(screen.getByText('No answer was generated.')).toBeInTheDocument()
  })
})
