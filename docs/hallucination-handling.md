# Hallucination Handling

The system uses a multi-layer defense against hallucination:

## 1. Abstention Gate (pre-generation)
Before calling the LLM, the system checks whether retrieved passages are relevant enough to answer the question. If the best retrieval score is below the calibrated threshold (0.3, swept against the golden set), it refuses to answer rather than generating from weak evidence. A retrieval-consensus override prevents false refusals when both dense and lexical search independently rank the same chunk highly.

## 2. Source-Constrained Prompting
The LLM is prompted to answer only from the numbered source blocks and to cite each sentence with inline `[n]` markers tied to source IDs, which `prompts.parse_citations` extracts from the plain-text response. The prompt explicitly instructs the model to say it cannot answer if no source covers the question.

## 3. Citation Validation (post-generation)
After generation, each sentence's cited sources are validated using the cross-encoder reranker. If a citation doesn't match its claimed source passage above a support threshold, it's flagged as "unverified" in the UI. Sentences with no valid citations are marked "unsupported."

## 4. Confidence Scoring
A calibrated confidence score combines retrieval quality and citation validity. The score and label (High/Medium/Low) are shown to the user, so they can judge how much to trust the answer.

## 5. Conflict Detection
When different sources provide conflicting information (e.g., different numeric values for the same claim), the system surfaces the conflict explicitly rather than silently picking one.

## 6. Prompt Injection Defense
Uploaded documents are untrusted input. Source blocks in the prompt are wrapped in random-nonce XML tags, and any tag-like text inside the document is stripped, preventing a malicious document from forging citation boundaries or injecting instructions.

The *question* is untrusted too. `query.sanitize` drops clauses that instruct the assistant
("ignore all previous instructions", "you are now in developer mode", "reveal your system prompt")
and keeps the clauses that ask about documents, before anything else sees the text: the embedding,
the cross-encoder pair, the abstain gate and the prompt all run on what was actually asked. If
every clause is an instruction there is nothing left to retrieve on, the original goes through
unchanged, and the abstain gate refuses it. Matching is literal, not a classifier -- a fuzzy rule
here would start deleting genuine questions about what a policy prohibits.

## Query decomposition

A cross-encoder scores one (question, passage) pair. A compound question -- "are employees
prohibited from accepting gifts, and how many hours of sick leave can be used for bereavement?" --
dilutes every pair it forms: a passage that fully answers one half is penalised for the half it
does not answer, and the top score can land under the abstain threshold with the right passage
sitting at rank 1. `query.subqueries` splits on sentence ends, on a coordinated second question
(the right side has to open like a question, so "office hours for FAS, RMA and FSA" is left
alone), and on a leading attribution preamble. Each passage keeps its best score across the parts,
all of which go out as one batch. A question that does not split costs exactly what it did before.
