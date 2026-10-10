import { QueryClient } from "@tanstack/react-query";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useState } from "react";

import { managerActor, renderWithAppProviders } from "@/test/render";

import { AssistantShell } from "./assistant-shell";

const conversationId = "11111111-1111-4111-8111-111111111111";
const workflowRunId = "22222222-2222-4222-8222-222222222222";
const messageId = "33333333-3333-4333-8333-333333333333";
const conversation = {
  id: conversationId,
  locale: "vi",
  title: "Kế hoạch ra mắt",
  status: "ACTIVE",
  last_message_sequence: 2,
  last_event_sequence: 4,
  created_at: "2026-08-13T10:00:00Z",
  updated_at: "2026-08-13T10:01:00Z",
};
const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status,
  headers: { "Content-Type": "application/json" },
});
const noEvents = () => ({ close() {} });
const proposalId = "55555555-5555-4555-8555-555555555555";
const approvalId = "66666666-6666-4666-8666-666666666666";
const recommendationId = "77777777-7777-4777-8777-777777777777";
const proposalContent = {
  project: { title: "Launch", description: null, start_date: "2026-09-01", due_date: "2026-09-14" },
  goal: { title: "Ship safely", description: null, expected_outcomes: ["Live"], target_date: "2026-09-14" },
  milestones: [],
  project_weeks: [{ ref: "w1", week_number: 1, start_date: "2026-09-01", end_date: "2026-09-07", objective: "Prepare" }],
  tasks: [{ ref: "t1", project_week_ref: "w1", milestone_ref: null, title: "Prepare launch", description: null, due_date: "2026-09-07", assignee_membership_id: null, required_skill_labels: ["communication"], estimated_effort_hours: 8, acceptance_criteria: ["Checklist ready"] }],
  dependencies: [],
  assumptions: [],
};
const readyRun = {
  id: workflowRunId,
  project_id: null,
  status: "WAITING_FOR_DECISION",
  workflow_name: "project_planning",
  workflow_version: "1.0.0",
  verifier_version: "1.0.0",
  input_goal_text: "Plan a launch",
  version: 3,
  created_at: "2026-08-13T10:00:00Z",
  updated_at: "2026-08-13T10:01:00Z",
  current_stage: "await_manager_decision",
  current_proposal: {
    proposal_id: proposalId,
    approval_id: approvalId,
    status: "READY_FOR_DECISION",
    version: 2,
    validation_result: { can_approve: true, errors: [], warnings: [] },
    content: proposalContent,
    change_summary: null,
    field_provenance: { default: "AI_PROPOSED" },
    creator_type: "AI_SYSTEM",
    previous_version: null,
  },
  public_timeline: [],
  allowed_actions: ["EDIT_PROPOSAL", "DECIDE_APPROVAL"],
};
const proposalVersion = (currentVersion = 2) => ({
  proposal_id: proposalId,
  workflow_run_id: workflowRunId,
  version: 2,
  current_version: currentVersion,
  content: proposalContent,
  creator_type: "AI_SYSTEM",
});

function proposalSnapshot(readOnly = false) {
  return { conversation, messages: [{
    id: messageId,
    sequence: 1,
    role: "ASSISTANT",
    content_blocks: [{
      kind: "proposal",
      workflow_run_id: workflowRunId,
      proposal_id: proposalId,
      proposal_version: 2,
      approval_id: approvalId,
      state: "READY_FOR_DECISION",
      can_approve: true,
      read_only: readOnly,
      current_version: readOnly ? 3 : null,
      error_codes: [],
      manual_fallback: null,
    }],
    created_at: "2026-08-13T10:01:00Z",
  }] };
}

describe("AssistantShell", () => {
  afterEach(() => {
    globalThis.history.replaceState({}, "", "/");
    vi.unstubAllGlobals();
  });

  it("updates the sidebar title when the conversation snapshot changes", async () => {
    let title: string | null = null;
    let poll: (() => void) | undefined;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      if (String(input) === "/api/v1/ai/conversations") {
        return response({ items: [{ ...conversation, title }] });
      }
      return response({ conversation: { ...conversation, title }, messages: [] });
    }));
    renderWithAppProviders(<AssistantShell actor={managerActor} connectEvents={(options) => {
        poll = options.onPoll;
        return noEvents();
      }} />);
    await screen.findByRole("button", { name: "Cuộc trò chuyện mới", current: "page" });
    await waitFor(() => expect(poll).toBeDefined());
    title = "Kế hoạch ra mắt sản phẩm";
    poll!();
    expect(await screen.findByRole("button", { name: title })).toBeVisible();
  });

  it("refreshes a pending title while the user is in another section", async () => {
    let title: string | null = null;
    const updatedAt = new Date().toISOString();
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      if (String(input) !== "/api/v1/ai/conversations") {
        throw new Error("No selected conversation snapshot should be fetched");
      }
      return response({ items: [{ ...conversation, title, updated_at: updatedAt }] });
    }));
    renderWithAppProviders(<AssistantShell actor={managerActor} activeSection="projects"
      workspaceTitle="Projects" workspaceContent={<p>Workspace</p>} connectEvents={noEvents} />);
    await waitFor(() => expect(document.querySelector(".assistant-history button"))
      .toHaveTextContent("Cuộc trò chuyện mới"));
    title = "Kế hoạch ra mắt sản phẩm";
    expect(await screen.findByRole("button", { name: title }, { timeout: 6500 })).toBeVisible();
  }, 10000);

  it("refreshes a new conversation title after its first post and navigation away", async () => {
    let created = false;
    let accepted = false;
    let title: string | null = null;
    const updatedAt = new Date().toISOString();
    const current = () => ({ ...conversation, title, updated_at: updatedAt,
      last_message_sequence: accepted ? 1 : 0 });
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path === "/api/v1/ai/conversations" && init?.method === "POST") {
        created = true;
        return response(current(), 201);
      }
      if (path === "/api/v1/ai/conversations") {
        return response({ items: created ? [current()] : [] });
      }
      if (init?.method === "POST") {
        accepted = true;
        return response({ conversation_id: conversationId, message_id: messageId,
          turn_id: workflowRunId, orchestration_run_id: workflowRunId, status: "QUEUED" }, 202);
      }
      return response({ conversation: current(), messages: [] });
    }));
    function Host() {
      const [section, setSection] = useState<"assistant" | "projects">("assistant");
      return <AssistantShell actor={managerActor} activeSection={section}
        workspaceTitle="Projects" workspaceContent={<p>Workspace</p>}
        onOpenProjects={() => setSection("projects")} connectEvents={noEvents} />;
    }
    renderWithAppProviders(<Host />);
    await waitFor(() => expect(screen.getByLabelText("Nhắn cho Trợ lý AI")).toBeEnabled());
    fireEvent.change(screen.getByLabelText("Nhắn cho Trợ lý AI"), { target: { value: "Lập kế hoạch" } });
    fireEvent.click(screen.getByRole("button", { name: /^Gửi$/ }));
    await waitFor(() => expect(screen.getByLabelText("Nhắn cho Trợ lý AI")).toHaveValue(""));
    fireEvent.click(screen.getByRole("button", { name: /^Projects$/ }));
    await screen.findByText("Workspace");
    title = "Kế hoạch ra mắt sản phẩm";
    expect(await screen.findByRole("button", { name: title }, { timeout: 6500 })).toBeVisible();
  }, 10000);

  it("stores the selected conversation in the URL for refresh reconstruction", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path === "/api/v1/ai/conversations") return response({ items: [conversation] });
      if (path === `/api/v1/ai/conversations/${conversationId}`) {
        return response({ conversation, messages: [] });
      }
      throw new Error(`Unexpected request: ${path}`);
    }));

    renderWithAppProviders(<AssistantShell
      actor={managerActor}
      activeSection="projects"
      workspaceTitle="Projects"
      workspaceContent={<div>Workspace</div>}
      onOpenAssistant={() => undefined}
      connectEvents={noEvents}
    />);

    fireEvent.click(await screen.findByRole("button", { name: "Kế hoạch ra mắt" }));

    expect(globalThis.location.search).toBe(`?conversation=${conversationId}`);
  });

  it("recovers the shared conversation history after one transient load failure", async () => {
    let attempts = 0;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      if (String(input) !== "/api/v1/ai/conversations") {
        throw new Error(`Unexpected request: ${String(input)}`);
      }
      attempts += 1;
      if (attempts === 1) throw new TypeError("temporary network failure");
      return response({ items: [conversation] });
    }));

    renderWithAppProviders(<AssistantShell
      actor={managerActor}
      activeSection="projects"
      workspaceTitle="Projects"
      workspaceContent={<div>Workspace</div>}
      connectEvents={noEvents}
    />);

    expect(await screen.findByText("Kế hoạch ra mắt", {}, { timeout: 3_000 })).toBeVisible();
    expect(attempts).toBe(2);
  });

  it("reconstructs the selected transcript after one transient snapshot failure", async () => {
    let snapshotAttempts = 0;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path === "/api/v1/ai/conversations") return response({ items: [conversation] });
      if (path === `/api/v1/ai/conversations/${conversationId}`) {
        snapshotAttempts += 1;
        if (snapshotAttempts === 1) throw new TypeError("temporary network failure");
        return response({ conversation, messages: [{
          id: messageId,
          sequence: 1,
          role: "ASSISTANT",
          content_blocks: [{ kind: "text", text: "Recovered transcript" }],
          created_at: "2026-08-13T10:01:00Z",
        }] });
      }
      throw new Error(`Unexpected request: ${path}`);
    }));

    renderWithAppProviders(<AssistantShell actor={managerActor} connectEvents={noEvents} />);

    expect(await screen.findByText("Recovered transcript", {}, { timeout: 3_000 })).toBeVisible();
    expect(snapshotAttempts).toBe(2);
  });

  it("keeps one SSE connection while durable snapshot sequences advance", async () => {
    let snapshotSequence = 1;
    let poll: (() => void) | undefined;
    const connectEvents = vi.fn((options: { onPoll?: () => void }) => {
      poll = options.onPoll;
      return { close() {} };
    });
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path === "/api/v1/ai/conversations") return response({ items: [conversation] });
      if (path === `/api/v1/ai/conversations/${conversationId}`) {
        const current = snapshotSequence;
        snapshotSequence += 1;
        return response({
          conversation: { ...conversation, last_event_sequence: current },
          messages: [],
        });
      }
      throw new Error(`Unexpected request: ${path}`);
    }));

    renderWithAppProviders(<AssistantShell actor={managerActor} connectEvents={connectEvents} />);
    await screen.findByText("Kế hoạch ra mắt");
    await waitFor(() => expect(connectEvents).toHaveBeenCalledTimes(1));

    poll?.();
    await waitFor(() => expect(snapshotSequence).toBeGreaterThan(2));
    expect(connectEvents).toHaveBeenCalledTimes(1);
  });

  it("focuses the fixed composer in the empty state", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({ items: [] })));

    renderWithAppProviders(<AssistantShell actor={managerActor} connectEvents={noEvents} />);

    expect(screen.getByText("WorkCrew")).toBeVisible();
    expect(screen.getByRole("navigation", { name: "Điều hướng chính" })).toBeVisible();
    expect(screen.getByText(managerActor.user.display_name)).toBeVisible();
    const composer = await screen.findByRole("textbox", { name: "Nhắn cho Trợ lý AI" });
    expect(composer).toHaveFocus();
    expect(composer.closest("form")).toHaveClass("assistant-composer");
    expect(screen.queryByText("Hiểu mục tiêu")).not.toBeInTheDocument();
  });

  it("renders the canonical REST transcript chronologically", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      if (String(input) === "/api/v1/ai/conversations") return response({ items: [conversation] });
      return response({ conversation, messages: [
        { id: messageId, sequence: 2, role: "ASSISTANT", content_blocks: [{ kind: "text", text: "Second response" }], created_at: "2026-08-13T10:01:00Z" },
        { id: workflowRunId, sequence: 1, role: "USER", content_blocks: [{ kind: "text", text: "First request" }], created_at: "2026-08-13T10:00:00Z" },
      ] });
    }));

    const { container } = renderWithAppProviders(<AssistantShell actor={managerActor} connectEvents={noEvents} />);

    await screen.findByText("Second response");
    const transcript = container.querySelector(".assistant-transcript");
    expect(transcript?.textContent?.indexOf("First request")).toBeLessThan(
      transcript?.textContent?.indexOf("Second response") ?? 0,
    );
  });

  it("normalizes a malformed success response without exposing unsafe fields", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      if (String(input) === "/api/v1/ai/conversations") return response({ items: [conversation] });
      return response({ conversation, messages: [{
        id: messageId,
        sequence: 1,
        role: "ASSISTANT",
        content_blocks: [{ kind: "text", text: "Safe", provider_error: "secret provider body" }],
        created_at: "2026-08-13T10:01:00Z",
      }] });
    }));

    renderWithAppProviders(<AssistantShell actor={managerActor} connectEvents={noEvents} />);

    expect(await screen.findByRole("alert")).toHaveTextContent("Không thể hoàn tất yêu cầu lúc này");
    expect(document.body.textContent).not.toContain("secret provider body");
  });

  it("preserves the message idempotency key after an uncertain retry", async () => {
    const keys: string[] = [];
    let created = false;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path === "/api/v1/ai/conversations" && init?.method === "POST") {
        created = true;
        return response({ ...conversation, last_message_sequence: 0 }, 201);
      }
      if (path === "/api/v1/ai/conversations") return response({ items: created ? [conversation] : [] });
      if (path === `/api/v1/ai/conversations/${conversationId}` && !init?.method) return response({ conversation, messages: [] });
      if (path.endsWith("/messages") && init?.method === "POST") {
        keys.push(new Headers(init.headers).get("Idempotency-Key") ?? "");
        if (keys.length === 1) throw new TypeError("connection lost after send");
        return response({ conversation_id: conversationId, message_id: messageId, turn_id: workflowRunId, orchestration_run_id: workflowRunId, status: "QUEUED" }, 202);
      }
      throw new Error(`Unexpected request: ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    renderWithAppProviders(<AssistantShell actor={managerActor} connectEvents={noEvents} />);

    const composer = await screen.findByRole("textbox", { name: "Nhắn cho Trợ lý AI" });
    fireEvent.change(composer, { target: { value: "Plan a launch" } });
    fireEvent.click(screen.getByRole("button", { name: "Gửi" }));
    await screen.findByRole("alert");
    fireEvent.click(screen.getByRole("button", { name: "Gửi" }));

    await waitFor(() => expect(keys).toHaveLength(2));
    expect(keys[0]).toBeTruthy();
    expect(keys[1]).toBe(keys[0]);
  });

  it("answers a projected manager question as a linked chat turn", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input) === "/api/v1/ai/conversations") return response({ items: [conversation] });
      if (!init?.method) return response({ conversation, messages: [{
        id: messageId,
        sequence: 1,
        role: "ASSISTANT",
        content_blocks: [{ kind: "question", question: "Ngày ra mắt là khi nào?", response_context: { workflow_run_id: workflowRunId } }],
        created_at: "2026-08-13T10:01:00Z",
      }] });
      return response({
        conversation_id: conversationId,
        message_id: messageId,
        turn_id: workflowRunId,
        orchestration_run_id: workflowRunId,
        status: "QUEUED",
      }, 202);
    });
    vi.stubGlobal("fetch", fetchMock);
    renderWithAppProviders(<AssistantShell actor={managerActor} connectEvents={noEvents} />);

    await screen.findByText("Ngày ra mắt là khi nào?");
    fireEvent.change(screen.getByRole("textbox", { name: "Nhắn cho Trợ lý AI" }), { target: { value: "Ngày 30/9" } });
    fireEvent.click(screen.getByRole("button", { name: "Gửi" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      `/api/v1/ai/conversations/${conversationId}/messages`,
      expect.objectContaining({ method: "POST" }),
    ));
    const post = fetchMock.mock.calls.find(([, init]) => init?.method === "POST");
    expect(JSON.parse(String(post?.[1]?.body))).toMatchObject({
      message: "Ngày 30/9",
      card_action: { kind: "PLANNING_INPUT", workflow_run_id: workflowRunId },
    });
  });

  it("does not reuse an answered planning question for a later chat message", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input) === "/api/v1/ai/conversations") return response({ items: [conversation] });
      if (!init?.method) return response({ conversation, messages: [
        { id: messageId, sequence: 1, role: "ASSISTANT", content_blocks: [{ kind: "question", question: "Ngày ra mắt?", response_context: { workflow_run_id: workflowRunId } }], created_at: "2026-08-13T10:00:00Z" },
        { id: proposalId, sequence: 2, role: "USER", content_blocks: [{ kind: "text", text: "Ngày 30/9" }], created_at: "2026-08-13T10:01:00Z" },
      ] });
      return response({ conversation_id: conversationId, message_id: messageId, turn_id: workflowRunId, orchestration_run_id: workflowRunId, status: "QUEUED" }, 202);
    });
    vi.stubGlobal("fetch", fetchMock);
    renderWithAppProviders(<AssistantShell actor={managerActor} connectEvents={noEvents} />);

    await screen.findByText("Ngày ra mắt?");
    fireEvent.change(screen.getByRole("textbox", { name: "Nhắn cho Trợ lý AI" }), { target: { value: "Còn task nào bị chặn?" } });
    fireEvent.click(screen.getByRole("button", { name: "Gửi" }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([, init]) => init?.method === "POST")).toBe(true));
    const post = fetchMock.mock.calls.find(([, init]) => init?.method === "POST");
    expect(JSON.parse(String(post?.[1]?.body))).not.toHaveProperty("card_action");
  });

  it("posts Ask AI revision with the projected exact proposal version", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path === "/api/v1/ai/conversations") return response({ items: [conversation] });
      if (path === `/api/v1/ai/conversations/${conversationId}`) return response(proposalSnapshot());
      if (path === `/api/v1/proposals/${proposalId}/versions/2`) return response(proposalVersion());
      if (path === `/api/v1/workflow-runs/${workflowRunId}`) return response(readyRun);
      if (path.endsWith("/messages") && init?.method === "POST") return response({
        conversation_id: conversationId, message_id: messageId, turn_id: workflowRunId,
        orchestration_run_id: workflowRunId, status: "QUEUED",
      }, 202);
      throw new Error(`Unexpected request: ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    renderWithAppProviders(<AssistantShell actor={managerActor} connectEvents={noEvents} />);

    fireEvent.click(await screen.findByRole("button", { name: "Nhờ AI chỉnh" }));
    fireEvent.change(screen.getByLabelText("Yêu cầu AI chỉnh proposal"), { target: { value: "Dời launch sang tuần 2" } });
    fireEvent.click(screen.getByRole("button", { name: "Gửi yêu cầu chỉnh" }));

    await waitFor(() => expect(fetchMock.mock.calls.some(([, init]) => init?.method === "POST")).toBe(true));
    const post = fetchMock.mock.calls.find(([, init]) => init?.method === "POST");
    expect(new Headers(post?.[1]?.headers).get("If-Match")).toBe('"2"');
    expect(JSON.parse(String(post?.[1]?.body))).toMatchObject({
      card_action: { kind: "PLANNING_REVISE", workflow_run_id: workflowRunId, proposal_id: proposalId },
    });
  });

  it("sends an exact TEAM_REVISE action from the inline recommendation card", async () => {
    const recommendation = {
      recommendation_id: recommendationId,
      version: 1,
      requirement_set_id: "88888888-8888-4888-8888-888888888888",
      requirement_version: 1,
      policy_version: "ranking-v1",
      status: "PROPOSED",
      selections: [],
      alternatives: [],
      uncovered: [],
      demands: [],
      diff: null,
      explanation_status: "UNAVAILABLE",
    };
    const teamSnapshot = { conversation, messages: [{
      id: messageId,
      sequence: 1,
      role: "ASSISTANT",
      content_blocks: [{
        kind: "team_recommendation",
        project_id: "99999999-9999-4999-8999-999999999999",
        recommendation_id: recommendationId,
        recommendation_version: 1,
        status: "PROPOSED",
        explanation_status: "UNAVAILABLE",
      }],
      created_at: "2026-08-13T10:01:00Z",
    }] };
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path === "/api/v1/ai/conversations") return response({ items: [conversation] });
      if (path === `/api/v1/ai/conversations/${conversationId}`) return response(teamSnapshot);
      if (path === `/api/v1/recommendations/${recommendationId}/versions/1`) return response(recommendation);
      if (path.endsWith("/messages") && init?.method === "POST") return response({
        conversation_id: conversationId, message_id: messageId, turn_id: workflowRunId,
        orchestration_run_id: workflowRunId, status: "QUEUED",
      }, 202);
      throw new Error(`Unexpected request: ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    renderWithAppProviders(<AssistantShell actor={managerActor} connectEvents={noEvents} />);

    fireEvent.click(await screen.findByRole("button", { name: "Yêu cầu chỉnh đội" }));
    expect(screen.getByText("Đang chỉnh đề xuất đội ngũ từ v1")).toBeVisible();
    const composer = screen.getByRole("textbox", { name: "Nhắn cho Trợ lý AI" });
    fireEvent.change(composer, { target: { value: "Đổi Lan thành Minh vì tuần 2 quá tải" } });
    fireEvent.keyDown(composer, { key: "Enter" });

    await waitFor(() => expect(fetchMock.mock.calls.some(([path, init]) => String(path).endsWith("/messages") && init?.method === "POST")).toBe(true));
    const post = fetchMock.mock.calls.find(([path, init]) => String(path).endsWith("/messages") && init?.method === "POST");
    expect(new Headers(post?.[1]?.headers).get("If-Match")).toBe('"1"');
    expect(JSON.parse(String(post?.[1]?.body))).toMatchObject({
      message: "Đổi Lan thành Minh vì tuần 2 quá tải",
      card_action: { kind: "TEAM_REVISE", recommendation_id: recommendationId, recommendation_version: 1 },
    });
  });

  it("keeps direct inline team edits bound to the visible immutable version", async () => {
    const recommendation = {
      recommendation_id: recommendationId, version: 1,
      requirement_set_id: "88888888-8888-4888-8888-888888888888", requirement_version: 1,
      policy_version: "ranking-v1", status: "PROPOSED", selections: [], alternatives: [], uncovered: [], demands: [], diff: null,
      explanation_status: "AVAILABLE",
    };
    const teamSnapshot = { conversation, messages: [{
      id: messageId, sequence: 1, role: "ASSISTANT", created_at: "2026-08-13T10:01:00Z",
      content_blocks: [{ kind: "team_recommendation", project_id: proposalId, recommendation_id: recommendationId, recommendation_version: 1, status: "PROPOSED", explanation_status: "AVAILABLE" }],
    }] };
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path === "/api/v1/ai/conversations") return response({ items: [conversation] });
      if (path === `/api/v1/ai/conversations/${conversationId}`) return response(teamSnapshot);
      if (path === `/api/v1/recommendations/${recommendationId}/versions/1`) return response(recommendation);
      if (path === `/api/v1/recommendations/${recommendationId}` && init?.method === "PATCH") return response({ ...recommendation, version: 2, diff: { added_membership_ids: [], removed_membership_ids: [], before: [], after: [] } });
      if (path === `/api/v1/recommendations/${recommendationId}/feedback` && init?.method === "POST") return response({ id: workflowRunId, recommendation_id: recommendationId, version: 2, kind: "override", comment: "" }, 201);
      throw new Error(`Unexpected request: ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    renderWithAppProviders(<AssistantShell actor={managerActor} connectEvents={noEvents} />);

    fireEvent.click(await screen.findByRole("button", { name: "Đổi thành viên" }));
    fireEvent.click(screen.getByRole("button", { name: "Lưu thành phiên bản 2" }));

    await waitFor(() => expect(fetchMock.mock.calls.some(([path, init]) => String(path) === `/api/v1/recommendations/${recommendationId}` && init?.method === "PATCH")).toBe(true));
    const patch = fetchMock.mock.calls.find(([path, init]) => String(path) === `/api/v1/recommendations/${recommendationId}` && init?.method === "PATCH");
    expect(new Headers(patch?.[1]?.headers).get("If-Match")).toBe('"1"');
    expect(new Headers(patch?.[1]?.headers).get("Idempotency-Key")).toBeTruthy();
    await waitFor(() => expect(fetchMock.mock.calls.some(([path]) => String(path).endsWith("/feedback"))).toBe(true));
    const feedback = fetchMock.mock.calls.find(([path]) => String(path).endsWith("/feedback"));
    expect(JSON.parse(String(feedback?.[1]?.body))).toEqual({ version: 2, kind: "override", comment: "" });
  });

  it("edits the proposal inline, expands weeks on demand, and keeps exact-version mutations", async () => {
    const invalidateQueries = vi.spyOn(QueryClient.prototype, "invalidateQueries");
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path === "/api/v1/ai/conversations") return response({ items: [conversation] });
      if (path === `/api/v1/ai/conversations/${conversationId}`) return response(proposalSnapshot());
      if (path === `/api/v1/proposals/${proposalId}/versions/2`) return response(proposalVersion());
      if (path === `/api/v1/workflow-runs/${workflowRunId}`) return response(readyRun);
      if (path === `/api/v1/proposals/${proposalId}` && init?.method === "PATCH") return response({ proposal_id: proposalId, workflow_run_id: workflowRunId, status: "DRAFT", version: 3, content: proposalContent }, 202);
      if (path === `/api/v1/approvals/${approvalId}/decision` && init?.method === "POST") return response({
        approval: { id: approvalId, status: "APPROVED" }, proposal: { id: proposalId, version: 2, status: "APPROVED" },
        created: { project_id: null, goal_id: null, milestone_ids: [], task_ids: [], dependency_ids: [], acceptance_criterion_ids: [] },
        workflow_run_id: workflowRunId, finalization_job_id: "77777777-7777-4777-8777-777777777777",
      });
      throw new Error(`Unexpected request: ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    renderWithAppProviders(<AssistantShell actor={managerActor} connectEvents={noEvents} />);

    fireEvent.click(await screen.findByRole("button", { name: "Chỉnh thủ công" }));
    expect(await screen.findByLabelText("Tên Project")).toBeVisible();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Mục tiêu tuần")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Tuần 1.*Prepare.*2026-09-01.*2026-09-07/ }));
    expect(screen.getByLabelText("Mục tiêu tuần")).toBeVisible();

    fireEvent.change(await screen.findByLabelText("Tên Project"), { target: { value: "Launch revised" } });
    fireEvent.click(screen.getByRole("button", { name: "Lưu version mới" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(`/api/v1/proposals/${proposalId}`, expect.objectContaining({ method: "PATCH" })));
    const patchCall = fetchMock.mock.calls.find(([, init]) => init?.method === "PATCH");
    expect(new Headers(patchCall?.[1]?.headers).get("If-Match")).toBe('"2"');

    fireEvent.click(await screen.findByRole("button", { name: "Phê duyệt kế hoạch" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(`/api/v1/approvals/${approvalId}/decision`, expect.objectContaining({ method: "POST" })));
    const approvalCall = fetchMock.mock.calls.find(([path]) => String(path).includes("/approvals/"));
    expect(new Headers(approvalCall?.[1]?.headers).get("If-Match")).toBe('"2"');
    expect(invalidateQueries).toHaveBeenCalledWith({
      queryKey: ["work", managerActor.membership.organization_id, managerActor.membership.id],
    });
  });

  it("renders a superseded proposal read-only and references the current version", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      if (String(input) === "/api/v1/ai/conversations") return response({ items: [conversation] });
      if (String(input) === `/api/v1/proposals/${proposalId}/versions/2`) return response(proposalVersion(3));
      if (String(input).includes("workflow-runs")) return response({ ...readyRun, current_proposal: { ...readyRun.current_proposal, version: 3 } });
      return response(proposalSnapshot(true));
    }));
    renderWithAppProviders(<AssistantShell actor={managerActor} connectEvents={noEvents} />);

    expect(await screen.findByText("Card này chỉ đọc. Version hiện tại là v3.")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Phê duyệt kế hoạch" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Nhờ AI chỉnh" })).not.toBeInTheDocument();
  });
});

it("reuses the original chat mutation identity after a committed response is lost", async () => {
  let current = { ...conversation, version: 1, is_pinned: false };
  const requests: Headers[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (init?.method === "PATCH") {
      requests.push(new Headers(init.headers));
      if (requests.length === 1) {
        current = { ...current, title: "Đã đổi tên", version: 2 };
        throw new TypeError("Response lost after commit");
      }
      return response(current);
    }
    return String(input) === "/api/v1/ai/conversations" ? response({ items: [current] }) : response({ conversation: current, messages: [] });
  }));
  renderWithAppProviders(<AssistantShell actor={managerActor} connectEvents={noEvents} />);
  await screen.findByRole("button", { name: conversation.title });
  fireEvent.click(screen.getByRole("button", { name: "Tùy chọn chat" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "Đổi tên" }));
  fireEvent.change(screen.getByLabelText("Tên cuộc trò chuyện"), { target: { value: "Đã đổi tên" } });
  fireEvent.click(screen.getByRole("button", { name: "Lưu" }));
  await screen.findByText(/Không thể cập nhật chat/);
  fireEvent.click(screen.getByRole("button", { name: "Lưu" }));
  await waitFor(() => expect(screen.queryByLabelText("Tên cuộc trò chuyện")).not.toBeInTheDocument());
  expect(requests).toHaveLength(2);
  expect(requests[1].get("Idempotency-Key")).toBe(requests[0].get("Idempotency-Key"));
  expect(requests[1].get("If-Match")).toBe('"1"');
  vi.unstubAllGlobals();
});
