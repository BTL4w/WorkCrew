"""Public source selection instructions, versioned independently of private context."""

RETRIEVAL_PROMPT_VERSION = "public-retrieval-v1"
RETRIEVAL_INSTRUCTIONS = """For explicit public web research use Retrieve with READ_ONLY mode.
Its typed_input is PublicSearchIntent: search_queries (1-2 exact excerpts of the CURRENT
message), message_spans (one per query: start/end Unicode character offsets and exact text),
requirements, source_mode WEB/DOCUMENTS/BOTH/AUTO, answer_mode SUMMARY/EVIDENCE_ONLY,
and optional filters. Never derive queries from private conversation or Work facts.
If public scope is unclear ask the user for a public query. Documents are unavailable (I2 inactive);
never send private document terms to web. Work Task deadlines/status use direct Work tools.
For an explicit request to research AND create a plan, create a Retrieve EVIDENCE_ONLY step
and a Planning PROPOSAL step depending on it. Do not add research for a plain planning request
or an active draft revision. Never put raw source content or source IDs in Planning typed_input;
the application binds validated source references after the retrieval step completes.
"""
