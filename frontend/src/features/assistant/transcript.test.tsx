import { fireEvent, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderWithAppProviders } from "@/test/render";

import type { AssistantMessage } from "./contracts";
import { Transcript } from "./transcript";

const callbacks = {
  onEdit: vi.fn(),
  onRevise: vi.fn(),
  onApprove: vi.fn(),
  onReject: vi.fn(),
};

function activityMessage(
  sequence: number,
  labelKey: string,
  workflowRunId?: string,
): AssistantMessage {
  const activityBlock = {
    kind: "activity",
    label_key: labelKey,
    status: "COMPLETED",
    agent_id: "planning",
    ...(workflowRunId ? { workflow_run_id: workflowRunId } : {}),
  } as unknown as AssistantMessage["content_blocks"][number];
  return {
    id: `00000000-0000-4000-8000-${sequence.toString().padStart(12, "0")}`,
    sequence,
    role: "ASSISTANT",
    content_blocks: [activityBlock],
    created_at: `2026-08-18T00:00:${sequence.toString().padStart(2, "0")}Z`,
  };
}

describe("Transcript", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("renders consecutive workflow activities as one visual card", () => {
    renderWithAppProviders(<Transcript
      messages={[
        activityMessage(1, "ai.activity.workflow_policy_checked"),
        activityMessage(2, "ai.activity.workflow_context_loaded"),
        activityMessage(3, "ai.activity.workflow_generating"),
        activityMessage(4, "ai.activity.workflow_schema_validating"),
        activityMessage(5, "ai.activity.workflow_verifying"),
        activityMessage(6, "ai.activity.workflow_generating"),
        activityMessage(7, "ai.activity.workflow_schema_validating"),
        activityMessage(8, "ai.activity.workflow_verifying"),
        activityMessage(9, "ai.activity.workflow_persisting_proposal"),
      ]}
      canManage
      {...callbacks}
    />);

    expect(screen.getAllByText("Trợ lý đang xử lý một bước an toàn")).toHaveLength(1);
    expect(screen.getAllByRole("button", { name: "Xem chi tiết hoạt động" })).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "Xem chi tiết hoạt động" }));
    expect(screen.getByText("9 bước xử lý dữ liệu công việc đã được kiểm soát")).toBeVisible();
  });

  it("does not group activities across a real conversation message", () => {
    const userMessage: AssistantMessage = {
      id: "10000000-0000-4000-8000-000000000002",
      sequence: 2,
      role: "USER",
      content_blocks: [{ kind: "text", text: "Hãy chỉnh lại kế hoạch" }],
      created_at: "2026-08-18T00:00:02Z",
    };

    renderWithAppProviders(<Transcript
      messages={[
        activityMessage(1, "ai.activity.workflow_generating"),
        userMessage,
        activityMessage(3, "ai.activity.workflow_verifying"),
      ]}
      canManage
      {...callbacks}
    />);

    expect(screen.getByText("Hãy chỉnh lại kế hoạch")).toBeVisible();
    expect(screen.getAllByText("Trợ lý đang xử lý một bước an toàn")).toHaveLength(2);
  });

  it("marks conventional chat sides for user and assistant messages", () => {
    const userMessage: AssistantMessage = {
      id: "10000000-0000-4000-8000-000000000011",
      sequence: 1,
      role: "USER",
      content_blocks: [{ kind: "text", text: "Lập kế hoạch ra mắt" }],
      created_at: "2026-08-18T00:00:01Z",
    };
    const assistantMessage: AssistantMessage = {
      id: "10000000-0000-4000-8000-000000000012",
      sequence: 2,
      role: "ASSISTANT",
      content_blocks: [{ kind: "text", text: "Đây là bản kế hoạch" }],
      created_at: "2026-08-18T00:00:02Z",
    };

    renderWithAppProviders(<Transcript messages={[userMessage, assistantMessage]} canManage {...callbacks} />);

    expect(screen.getByText("Lập kế hoạch ra mắt").closest("article")).toHaveAttribute("data-side", "right");
    expect(screen.getByText("Đây là bản kế hoạch").closest("article")).toHaveAttribute("data-side", "left");
  });

  it("groups one workflow even when a proposal boundary separates its activities", () => {
    const workflowRunId = "20000000-0000-4000-8000-000000000001";
    const proposalBoundary: AssistantMessage = {
      id: "20000000-0000-4000-8000-000000000002",
      sequence: 2,
      role: "ASSISTANT",
      content_blocks: [{
        kind: "planning_run",
        workflow_run_id: workflowRunId,
        status: "AWAITING_HUMAN",
      }],
      created_at: "2026-08-18T00:00:02Z",
    };

    renderWithAppProviders(<Transcript
      messages={[
        activityMessage(1, "ai.activity.workflow_generating", workflowRunId),
        proposalBoundary,
        activityMessage(3, "ai.activity.workflow_waiting_for_decision", workflowRunId),
      ]}
      canManage
      {...callbacks}
    />);

    expect(screen.getByText("Workflow lập kế hoạch: AWAITING_HUMAN")).toBeVisible();
    expect(screen.getAllByText("Trợ lý đang xử lý một bước an toàn")).toHaveLength(1);
  });

  it("keeps one immutable history card per proposal version", async () => {
    const proposalId = "30000000-0000-4000-8000-000000000001";
    const workflowRunId = "30000000-0000-4000-8000-000000000002";
    const content = (title: string) => ({
      project: { title, description: null, start_date: null, due_date: null },
      goal: { title, description: null, expected_outcomes: [], target_date: null },
      milestones: [],
      project_weeks: [],
      tasks: [],
      dependencies: [],
      assumptions: [],
    });
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const version = String(input).endsWith("/versions/1") ? 1 : 2;
      return new Response(JSON.stringify({
        proposal_id: proposalId,
        workflow_run_id: workflowRunId,
        version,
        current_version: version,
        content: content(version === 1 ? "Original" : "Revised"),
        creator_type: "AI_SYSTEM",
      }), { headers: { "Content-Type": "application/json" } });
    }));
    const proposalMessage = (
      sequence: number,
      proposalVersion: number,
      state: "READY_FOR_DECISION" | "SUPERSEDED",
    ): AssistantMessage => ({
      id: `30000000-0000-4000-8000-${sequence.toString().padStart(12, "0")}`,
      sequence,
      role: "ASSISTANT",
      content_blocks: [{
        kind: "proposal",
        workflow_run_id: workflowRunId,
        proposal_id: proposalId,
        proposal_version: proposalVersion,
        state,
        read_only: state === "SUPERSEDED",
        current_version: state === "SUPERSEDED" ? 2 : null,
        error_codes: [],
      }],
      created_at: `2026-08-18T00:00:${sequence.toString().padStart(2, "0")}Z`,
    });

    const revisionRequest: AssistantMessage = {
      id: "30000000-0000-4000-8000-000000000099",
      sequence: 2,
      role: "USER",
      content_blocks: [{ kind: "text", text: "Extend the plan" }],
      created_at: "2026-08-18T00:00:02Z",
    };
    const { container } = renderWithAppProviders(<Transcript
      messages={[
        proposalMessage(1, 1, "READY_FOR_DECISION"),
        revisionRequest,
        proposalMessage(3, 1, "SUPERSEDED"),
        proposalMessage(4, 2, "READY_FOR_DECISION"),
      ]}
      canManage
      {...callbacks}
    />);

    expect(await screen.findAllByText("Proposal v1")).toHaveLength(1);
    expect(await screen.findAllByText("Proposal v2")).toHaveLength(1);
    expect(screen.getByRole("heading", { name: "Original" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "Revised" })).toBeVisible();
    expect(screen.getByText("Version chỉ đọc")).toBeVisible();
    expect(screen.getByText("Card này chỉ đọc. Version hiện tại là v2.")).toBeVisible();
    expect(screen.getAllByRole("button", { name: "Phê duyệt kế hoạch" })).toHaveLength(1);
    expect(screen.getAllByRole("button", { name: "Chỉnh thủ công" })).toHaveLength(1);
    expect(screen.getAllByText(/^Proposal v/)).toHaveLength(2);
    const transcript = container.querySelector(".assistant-transcript")?.textContent ?? "";
    expect(transcript.indexOf("Proposal v1")).toBeLessThan(transcript.indexOf("Extend the plan"));
    expect(transcript.indexOf("Extend the plan")).toBeLessThan(transcript.indexOf("Proposal v2"));
  });

  it("renders team decisions and explicit assignments as assistant transcript blocks", () => {
    const recommendationId = "40000000-0000-4000-8000-000000000001";
    const messages: AssistantMessage[] = [{
      id: "40000000-0000-4000-8000-000000000002",
      sequence: 1,
      role: "ASSISTANT",
      content_blocks: [{
        kind: "team_decision_result",
        recommendation_id: recommendationId,
        recommendation_version: 2,
        decision: "APPROVE",
      }, {
        kind: "assignment_result",
        task_id: "40000000-0000-4000-8000-000000000003",
        task_version: 5,
        membership_id: "40000000-0000-4000-8000-000000000004",
        warning_codes: ["CAPACITY_EXCEEDED"],
      }],
      created_at: "2026-08-18T00:00:01Z",
    }];

    renderWithAppProviders(<Transcript messages={messages} canManage {...callbacks} />);

    expect(screen.getByText("Đã phê duyệt đội ngũ · v2")).toBeVisible();
    expect(screen.getByText("Đã giao Task · v5")).toBeVisible();
    expect(screen.getByText("Khối lượng vượt capacity đã biết")).toBeVisible();
  });
});

it("renders verified retrieval sources in the shared transcript", async () => {
  const { retrievalFixture } = await import("@/test/retrieval");
  const { assistantBlockSchema } = await import("./contracts");
  renderWithAppProviders(<Transcript messages={[{
    id:"99999999-9999-4999-8999-999999999999", sequence:1, role:"ASSISTANT",
    content_blocks:[assistantBlockSchema.parse(retrievalFixture())], created_at:"2026-10-23T01:00:00Z",
  }]} canManage={false} {...callbacks}/>);
  expect(screen.getByRole("region", {name:"Nghiên cứu web"})).toBeVisible();
  expect(screen.getByRole("link", {name:"Nguồn 1: Public planning guide"})).toHaveAttribute("href","https://example.org/guide");
});
