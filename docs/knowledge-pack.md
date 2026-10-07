# Knowledge pack & retrieval interface

This document defines the seam a future retrieval backend (BM25 / vector / RAG) implements **without
changing any other code**.

## Knowledge pack

A *knowledge pack* is a deployer-supplied bundle. It is loaded by `load_knowledge_pack(store)`.

| Field | Type | Required | Meaning |
|---|---|---|---|
| `operating_context` | string | yes | Compact operating summary (rules, priorities, roles) sent on every call. |
| `raw_baseline` | string | no | Full source text, included as bounded excerpts only when a message references history. |
| `people` | object | no | Capabilities/roles keyed however the deployer likes; filtered to people referenced per call. |
| `documents` | list | no | `[{id, title, text}]` — company docs, SOPs, manuals. Advisory context only. |
| `background` | object | no | Additional retrieved background (docs, role evidence, historical turns). |

Current default implementation reads these from the object store (`knowledge/v1/ledger-source.md`,
`knowledge/v1/capabilities.json`, `knowledge/v2/background.json`). A future pack may instead come from a
database or a file bundle; only `load_knowledge_pack` changes.

## Retrieval interface

```python
retrieve_relevant(state, query, kind='conversation', limit=12) -> list[dict]
```

Each returned item:

```python
{"text": str, "source": str, "kind": str, "score": float, "time": str|None, "sender": str|None}
```

- `source` is a provenance label (e.g. a source id/url/provider).
- `kind` distinguishes sources (`"source"`) from future kinds (e.g. `"document"`, `"person"`).
- `score` is backend-defined (keyword overlap today; BM25/vector later); callers only rely on ordering.

### Contract

1. Pure and read-only — no state mutation.
2. Deterministic for a given `(state, query, kind, limit)`.
3. Returns at most `limit` items, highest `score` first.
4. Provenance (`source`) is always populated so answers can cite it.

### Phase D

A BM25/vector backend implements `retrieve_relevant` with the same signature and returns the same item
shape. **No caller changes are required.** Documents retrieved this way are advisory: they can inform an
answer or a suggestion ("Suggestion: … — not confirmed yet") but never authorize a task change.
