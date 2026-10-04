# Diagrams

Mermaid sources (`.mmd`) with their rendered exports (`.webp`). Regenerate after changing a source:

```bash
mmdc -i docs/diagrams/01-system-architecture.mmd -o docs/diagrams/01-system-architecture.png -s 2
```

| File | Shows | Best used for |
|---|---|---|
| `01-system-architecture` | Six layers and how they call each other; local vs cloud components colour-coded | Opening architecture page |
| `02-ingestion-pipeline` | Upload to indexed: preflight, parse, normalise, parent/child chunk, dual index | Ingestion section |
| `03-answer-pipeline` | The full query path with the abstention branch and both NO_ANSWER exits | Core logic section — the main diagram |
| `04-hybrid-retrieval` | Dense + tsvector + RRF + cross-encoder, and the consensus flag | Retrieval deep dive |
| `05-trust-layer` | Per-sentence grounding chain and the pre-generation injection defences | Hallucination handling section |
| `06-confidence-weights` | The four weighted signals behind the confidence label | Beside the confidence explanation |
| `07-request-sequence` | One request end to end, showing speculative retrieval and streaming | Performance / concurrency discussion |
