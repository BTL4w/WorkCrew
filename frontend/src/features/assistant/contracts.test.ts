import { describe, expect, it } from "vitest";

import {
  assistantBlockSchema,
  conversationSnapshotSchema,
  postMessageInputSchema,
  type AssistantBlock,
} from "./contracts";

const workflowRunId = "11111111-1111-4111-8111-111111111111";
const proposalId = "22222222-2222-4222-8222-222222222222";
const projectId = "33333333-3333-4333-8333-333333333333";
const recommendationId = "44444444-4444-4444-8444-444444444444";
const taskId = "55555555-5555-4555-8555-555555555555";
const membershipId = "66666666-6666-4666-8666-666666666666";

const blocks: AssistantBlock[] = [
  { kind: "text", text: "I found the current project status." },
  { kind: "activity", label_key: "assistant.activity.planning", status: "RUNNING", agent_id: "planning" },
  {
    kind: "work_evidence",
    summary: "Two tasks are blocked.",
    evidence: [{ evidence_id: "task-1", resource_type: "task", resource_id: proposalId, version: 2 }],
  },
  { kind: "question", question: "What is the target date?", response_context: { workflow_run_id: workflowRunId } },
  { kind: "capability_unavailable", capability: "daily_update", message_key: "assistant.unavailable.dailyUpdate" },
  { kind: "planning_run", workflow_run_id: workflowRunId, status: "RUNNING" },
  {
    kind: "proposal",
    workflow_run_id: workflowRunId,
    proposal_id: proposalId,
    proposal_version: 2,
    approval_id: null,
    state: "READY_FOR_DECISION",
    can_approve: true,
    read_only: false,
    current_version: null,
    error_codes: [],
    manual_fallback: null,
  },
  { kind: "decision_result", workflow_run_id: workflowRunId, decision: "APPROVE", proposal_id: proposalId, proposal_version: 2, project_id: projectId, continue_team: true },
  {
    kind: "team_recommendation",
    project_id: projectId,
    recommendation_id: recommendationId,
    recommendation_version: 2,
    status: "PROPOSED",
    explanation_status: "AVAILABLE",
  },
  {
    kind: "team_decision_result",
    recommendation_id: recommendationId,
    recommendation_version: 2,
    decision: "APPROVE",
  },
  {
    kind: "assignment_result",
    task_id: taskId,
    task_version: 4,
    membership_id: membershipId,
    warning_codes: ["CAPACITY_EXCEEDED"],
  },
  { kind: "safe_error", code: "MODEL_UNAVAILABLE", message_key: "assistant.error.safe", manual_fallback: "Create the project manually." },
];

describe("Assistant contracts", () => {
  it("strictly parses every public block kind", () => {
    for (const block of blocks) expect(assistantBlockSchema.parse(block)).toEqual(block);
  });

  it("rejects unknown kinds and internal fields", () => {
    expect(() => assistantBlockSchema.parse({ kind: "reasoning", text: "hidden" })).toThrow();
    expect(() => assistantBlockSchema.parse({
      kind: "activity",
      label_key: "assistant.activity.planning",
      status: "RUNNING",
      raw_prompt: "secret",
    })).toThrow();
  });

  it("requires the exact reference fields for each card action", () => {
    expect(postMessageInputSchema.parse({
      message: "Revise the team",
      locale: "en",
      card_action: { kind: "TEAM_REVISE", recommendation_id: recommendationId, recommendation_version: 2 },
    }).card_action).toEqual({ kind: "TEAM_REVISE", recommendation_id: recommendationId, recommendation_version: 2 });
    expect(() => postMessageInputSchema.parse({
      message: "Revise the team",
      locale: "en",
      card_action: { kind: "TEAM_REVISE", recommendation_id: recommendationId },
    })).toThrow();
    expect(() => postMessageInputSchema.parse({
      message: "Revise the plan",
      locale: "en",
      card_action: { kind: "PLANNING_REVISE", proposal_id: proposalId },
    })).toThrow();
  });

  it("validates a canonical REST conversation snapshot", () => {
    const parsed = conversationSnapshotSchema.parse({
      conversation: {
        id: workflowRunId,
        locale: "vi",
        title: null,
        status: "ACTIVE",
        last_message_sequence: 1,
        last_event_sequence: 3,
        created_at: "2026-08-13T10:00:00Z",
        updated_at: "2026-08-13T10:01:00Z",
      },
      messages: [{
        id: proposalId,
        sequence: 1,
        role: "ASSISTANT",
        content_blocks: blocks,
        created_at: "2026-08-13T10:01:00Z",
      }],
    });

    expect(parsed.messages[0]?.content_blocks).toHaveLength(12);
  });
});

// Retrieval is an additive strict block; authority/provider fields never reach UI.
describe("retrieval public contract", () => {
  it("preserves nullable source dates", async () => {
    const { retrievalFixture } = await import("@/test/retrieval");
    expect(assistantBlockSchema.parse(retrievalFixture()).kind).toBe("retrieval_answer");
  });
  it.each(["citation", "url", "metadata", "version"])("rejects invalid %s", async mutation => {
    const { retrievalFixture } = await import("@/test/retrieval");
    const value = retrievalFixture();
    if (mutation === "citation") value.claims[0].evidence_ids = ["33333333-3333-4333-8333-333333333333"];
    if (mutation === "url") value.sources[0].canonical_url = "javascript:alert(1)";
    if (mutation === "version") value.schema_version = "99.0";
    const payload = mutation === "metadata" ? {...value, usage: {provider_requests: 1}} : value;
    expect(assistantBlockSchema.safeParse(payload).success).toBe(false);
  });
});
