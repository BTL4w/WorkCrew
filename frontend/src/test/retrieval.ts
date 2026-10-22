export const retrievalSourceId = "11111111-1111-4111-8111-111111111111";
export function retrievalFixture() {
  return {
    kind: "retrieval_answer", schema_version: "1.0",
    context_run_id: "22222222-2222-4222-8222-222222222222", outcome: "ANSWER_READY",
    claims: [{ text: "Review the goal and constraints.", evidence_ids: [retrievalSourceId] }],
    sources: [{ evidence_id: retrievalSourceId, title: "Public planning guide", canonical_url: "https://example.org/guide", method: "SEARCH_SNIPPET", excerpt: "Review the goal and constraints.", published_at: null, source_fetched_at: null }],
    limitations: ["SOURCE_FRESHNESS_UNKNOWN"], observed_at: "2026-10-23T01:00:00Z", availability: "AVAILABLE",
  };
}
