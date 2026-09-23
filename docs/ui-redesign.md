# UI Redesign Spec — Chat & Answer Experience

Target feel: **Claude.ai / ChatGPT / Gemini** — calm, centered, text-first, low-chrome.
Scope: chat transcript, answer rendering, citations, sources, composer, document drawer, split view.
Out of scope: upload toast (already locked), hero/landing screen (reuse as-is).

---

## 1. Design principles

1. **Text is the interface.** The model's words carry the weight — not cards, not borders, not icons. Chrome only appears around *input* (composer) and *optional detail* (sources, confidence), never around the answer itself.
2. **One column, centered.** Nothing should span the full viewport width. Long lines are hard to read; all three reference products cap line length around 650–780px.
3. **Progressive disclosure.** Sources, confidence, and trace data are collapsed by default and expand on demand. The default view is just: question → answer → citation markers.
4. **No avatars, no role labels.** Alignment and typography distinguish user vs. assistant. A 👤/📄 emoji pair (current `Avatar()` in `ChatWindow.jsx`) reads as a prototype, not a product.
5. **Motion is a whisper.** Fades and 150–250ms ease-outs only. No bouncing, no scaling, no gradient glows behind static elements.

---

## 2. Color palette (exact — reuse existing tokens, no new hex values)

All values already exist in `frontend/src/index.css:4-22`. This redesign does **not** introduce a new palette — it changes how these tokens are *applied* (see §4).

```css
:root {
  /* Surfaces */
  --panel:        rgba(0, 0, 0, 0.60);   /* modal/popover backgrounds only */
  --panel-soft:   rgba(0, 0, 0, 0.50);   /* rarely used, deep recess */
  --panel-hover:  rgba(255, 255, 255, 0.05); /* user bubble, hover states */

  /* Lines */
  --line:         #404040;               /* hairline borders, dividers */
  --line-soft:    #525252;               /* hover/active border */

  /* Text */
  --text:         #ffffff;               /* user bubble text, headings */
  --text-muted:   #e5e5e5;               /* assistant body text */
  --text-dim:     #a3a3a3;               /* source snippets, secondary */
  --text-faint:   #737373;               /* labels, meta, timestamps */

  /* Accent */
  --accent:       #8b5cf6;               /* citation chips, active states, links */
  --accent-dim:   rgba(139, 92, 246, 0.18); /* chip fill, subtle highlight bg */
  --danger:       #f87171;               /* errors only */
  --glow:         #6d28d9;               /* remove from static UI — see §7.4 */

  /* Radii */
  --radius-sm:    8px;                   /* chips, small buttons */
  --radius-md:    12px;                  /* cards, popovers */
  --radius-lg:    20px;                  /* composer, user bubble */
  --radius-pill:  999px;                 /* citation chips, tags */
}
```

**Usage rule of thumb** (this is the actual fix — current UI likely over-uses `--panel`/borders everywhere):

| Element | Background | Border |
|---|---|---|
| App shell | `#060608` + bg image overlay (unchanged) | none |
| Assistant answer text | transparent | none |
| User bubble | `--panel-hover` | none |
| Composer | `--panel` | `1px solid var(--line)` |
| Sources row (collapsed) | transparent | none |
| Source card (expanded) | transparent | `1px solid var(--line)`, only on the card itself |
| Popover / drawer | `--panel` + `backdrop-filter: blur(12px)` | `1px solid var(--line)` |
| Citation chip | `--accent-dim` | none |

---

## 3. Typography

Font stack unchanged: `'Inter', system-ui, -apple-system, sans-serif`.

| Role | Size | Weight | Line-height | Color |
|---|---|---|---|---|
| Assistant answer body | 15px | 400 | 1.65 | `--text-muted` |
| User bubble text | 14px | 400 | 1.5 | `--text` |
| Citation chip | 11px | 600 | 1 | `--accent` |
| Sources row label ("3 sources") | 12.5px | 500 | 1 | `--text-faint` |
| Source card filename | 13px | 600 | 1.3 | `--text-muted` |
| Source card snippet | 13px | 400 | 1.55 | `--text-dim` |
| Confidence / meta label | 11px | 500 | 1 | `--text-faint` |
| Suggestion chip | 13px | 500 | 1.3 | `--text-muted` |

No text below 11px. No text heavier than 600.

---

## 4. Layout & spacing grid

```
┌───────────────────────────────────────────────────────────┐
│                     .app-bg (full viewport)                │
│   ┌───────────────────────────────────────────────────┐   │
│   │              .chat-scroll (max-width: 720px,        │   │
│   │               centered, own scroll container)       │   │
│   │                                                       │   │
│   │   [user turn — right, 70% max-width]                 │   │
│   │                                                       │
│   │   [assistant turn — left, full column width]         │   │
│   │     answer text …                                     │   │
│   │     ▸ 3 sources                                       │   │
│   │     [suggestion chips]                                │   │
│   │                                                       │   │
│   │   ── 2.5rem gap between turns ──                      │   │
│   └───────────────────────────────────────────────────┘   │
│                                                              │
│   ┌───────────────────────────────────────────────────┐   │
│   │        composer (sticky bottom, 720px, centered)     │   │
│   └───────────────────────────────────────────────────┘   │
└───────────────────────────────────────────────────────────┘
```

**Spacing scale** (use consistently, replace ad-hoc rem values in current CSS):

```
--space-1: 0.25rem   (4px)   chip padding
--space-2: 0.5rem    (8px)   tight stacks
--space-3: 0.75rem   (12px)  answer → sources gap
--space-4: 1rem      (16px)  composer padding
--space-5: 1.5rem    (24px)  side gutters, mobile
--space-6: 2.5rem    (40px)  between message turns
```

(These aren't currently declared as CSS vars — add them alongside the existing `:root` token block so spacing stays consistent across every component touched in this redesign.)

**Column widths:**
- Chat transcript: `max-width: 720px; margin: 0 auto;`
- Composer: same `720px`, same centering, so input aligns exactly under the transcript.
- Split view (document open): chat pane shrinks to `max-width: 560px`; document pane takes the remainder.

---

## 5. Component specs

### 5.1 Message shell (`ChatWindow.jsx`)

- **Delete** `Avatar()` entirely and its two call sites.
- `.message` → flex row becomes flex column; alignment via `align-self` per role instead of avatar + bubble pairing.
- `.message.user`: `align-self: flex-end; max-width: 70%;`
- `.message` (assistant): `align-self: stretch; max-width: 100%;` (bounded by the 720px parent).

### 5.2 User turn

```css
.message.user .message-text {
  background: var(--panel-hover);
  color: var(--text);
  border-radius: var(--radius-lg);
  padding: 0.6rem 1rem;
  font-size: 14px;
  line-height: 1.5;
  display: inline-block;
}
```
No label, no timestamp, no avatar.

### 5.3 Assistant turn (`AnswerCard.jsx`)

- Remove any wrapping card/panel background — text sits directly on the app background.
- `.answer-sentence`: `font-size: 15px; line-height: 1.65; color: var(--text-muted);`
- Weak/unsupported sentence states keep functional color (not decorative): `answer-sentence-weak` → `color: var(--text-dim)` with a dotted underline instead of a full highlight block; `answer-sentence-unsupported` → same, plus the existing inline `unsupported` label restyled as a tiny pill (`background: var(--accent-dim)`; do **not** reuse `--danger` here — unsupported ≠ error).

### 5.4 Citation chips

```css
.cite-chip {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 16px;
  height: 16px;
  padding: 0 5px;
  margin: 0 1px;
  font-size: 11px;
  font-weight: 600;
  color: var(--accent);
  background: var(--accent-dim);
  border-radius: var(--radius-pill);
  border: none;
  vertical-align: super;
  cursor: pointer;
  transition: background 0.15s ease;
}
.cite-chip:hover { background: rgba(139, 92, 246, 0.28); }
.cite-chip.active { background: var(--accent); color: #fff; }
```
Superscript-style — reads as a footnote marker, not a button.

### 5.5 Sources — collapsed by default (`SourcesPanel.jsx`)

Replace the current always-expanded panel with a two-state component:

**Collapsed (default):**
```html
<button class="sources-toggle">
  <ChevronIcon rotated={open} />
  <span>3 sources</span>
</button>
```
```css
.sources-toggle {
  display: flex; align-items: center; gap: 0.35rem;
  font-size: 12.5px; color: var(--text-faint);
  background: none; border: none; padding: 0;
  margin-top: var(--space-3);
}
.sources-toggle:hover { color: var(--text-dim); }
```

**Expanded:** vertical list of compact source cards, each:
```css
.source-card {
  border: 1px solid var(--line);
  border-radius: var(--radius-md);
  padding: 0.6rem 0.8rem;
  margin-top: 0.4rem;
  cursor: pointer;
  transition: border-color 0.15s ease;
}
.source-card.active { border-color: var(--accent); }
.source-card-head { font-size: 13px; font-weight: 600; color: var(--text-muted); }
.source-card-meta { font-size: 11px; color: var(--text-faint); margin-top: 2px; }
.source-card-snippet {
  font-size: 13px; color: var(--text-dim); line-height: 1.55;
  margin-top: 0.4rem;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
  overflow: hidden;
}
```
Clicking a `.cite-chip` sets `open = true` on the sources block for that message and scrolls the matching `.source-card` into view (reuse existing `activeId` wiring in `ChatWindow.jsx:60-67`).

### 5.6 Confidence / trace (`Details.jsx`)

- Collapse into a single small icon button next to the sources toggle: `ⓘ`.
- Click opens a popover (`--panel` background, `blur(12px)`, `1px solid var(--line)`, `border-radius: var(--radius-md)`) anchored below the icon, containing confidence score, retrieval mode, timing — whatever `Details.jsx` currently renders inline.
- Never visible by default.

### 5.7 Suggestions

Keep as horizontal-wrap chip row, restyle to match citation-chip family but larger:
```css
.suggestion-btn {
  font-size: 13px; font-weight: 500; color: var(--text-muted);
  background: var(--panel-hover);
  border: 1px solid var(--line);
  border-radius: var(--radius-pill);
  padding: 0.4rem 0.8rem;
}
.suggestion-btn:hover { border-color: var(--line-soft); background: rgba(255,255,255,0.08); }
```

### 5.8 Composer

- Keep current structural shape (rounded input + Attach / Mode / Send).
- **Remove** the radial gradient glow behind the composer (visible in current screenshots) — flatten to plain `--panel` background. Reference products never put a glow behind a static input; glow/accent is reserved for active/focus states only.
- `border-radius: var(--radius-lg)`, `border: 1px solid var(--line)`, `background: var(--panel)`, `backdrop-filter: blur(12px)`.
- On focus-within: `border-color: var(--line-soft)` — no glow, no shadow bloom.
- Becomes `position: sticky; bottom: 1.5rem;` once `messages.length > 0` (currently only positioned statically in the hero state per `App.jsx`).
- Add a top fade mask on `.chat-scroll`: `mask-image: linear-gradient(to bottom, transparent 0, black 24px, black 100%);` so content doesn't hard-clip under the sticky composer.

### 5.9 Document drawer / history drawer (`DocumentDrawer.jsx`, `HistoryPanel.jsx`)

Already close to the target (popover panel, list rows). Refinements only:
- `.doc-row-name.active` should use `color: var(--accent)` + a `4px` left accent bar, not a full background fill — matches the "selection = accent line" language used for `.source-card.active`.
- Keep `--panel` + blur for the popover shell — this one is correct as-is.

### 5.10 Split view (document viewer open)

- Chat pane: `max-width: 560px` (narrower than the 720px single-column default, since it now shares the viewport).
- Divider: `1px solid var(--line)` hairline instead of a hard panel-colored gutter.
- Document viewer pane background: same `#060608` base, no separate panel tone — avoid a visible "seam" between panes beyond the hairline.

### 5.11 States

| State | Treatment |
|---|---|
| Thinking | 3-dot pulse, left-aligned, no avatar, `color: var(--text-faint)` dots |
| Abstained | Italic paragraph, `color: var(--text-dim)`, no border/card — reads as the model speaking, not an error |
| Error | Only state that keeps a bordered card: `border: 1px solid var(--danger); border-radius: var(--radius-md); padding: 0.6rem 0.9rem;` — this is intentional, errors should visually interrupt |
| Near-miss (inside abstain) | Same source-card styling as §5.5, no special casing |

---

## 6. Motion

```css
--ease: cubic-bezier(0.4, 0, 0.2, 1);
--dur-fast: 150ms;
--dur-base: 220ms;
```
- New message appears: `opacity 0 → 1`, `translateY(4px) → 0`, `var(--dur-base) var(--ease)`. No scale, no bounce.
- Sources expand/collapse: `max-height` + `opacity` transition, `var(--dur-base)`.
- Citation chip hover: background only, `var(--dur-fast)`.
- Nothing pulses except the thinking dots.

---

## 7. Things to explicitly remove

1. `Avatar()` function and both call sites in `ChatWindow.jsx`.
2. Any card/panel background wrapping assistant answer text.
3. Always-expanded `SourcesPanel` + `Details` render — replace with collapsed-by-default toggle (§5.5, §5.6).
4. Radial gradient / glow behind the composer (visible in current screenshots — likely a `background: radial-gradient(...)` on a wrapping div using `--glow`).
5. Full-width chat layout — every direct child of `.chat-window`/`.chat-scroll` must respect the 720px column.
6. Emoji-based avatars (👤/📄) — no emoji anywhere per project's own code rules.

---

## 8. File map for implementation

| File | Change |
|---|---|
| `frontend/src/index.css` | Add `--space-*` and `--ease`/`--dur-*` tokens; rewrite `.message`, `.message.user`, `.cite-chip`, `.sources-toggle`, `.source-card`, `.suggestion-btn`, `.composer*`, `.chat-scroll` rules |
| `frontend/src/components/ChatWindow.jsx` | Remove `Avatar()`; restructure `.message` markup for alignment-only distinction |
| `frontend/src/components/AnswerCard.jsx` | Restyle sentence states; no structural change needed |
| `frontend/src/components/SourcesPanel.jsx` | Add collapsed/expanded toggle state; compact card markup |
| `frontend/src/components/Details.jsx` | Convert to icon + popover |
| `frontend/src/components/Composer.jsx` | Remove glow wrapper; add sticky positioning once `messages.length > 0` |
| `frontend/src/App.jsx` | Pass `messages.length > 0` down (or read via hook) to toggle composer's sticky class |

---

## 9. Explicitly unchanged

- Hero/landing screen (`HeroHeader.jsx`, `SuggestionList.jsx`) — not in scope.
- Upload toast (`ProgressToast.jsx`) — locked per earlier decision, transparent background already applied.
- Color palette values themselves — only their *application* changes, per §2.
- Document drawer / history drawer overall structure — minor refinement only (§5.9).
