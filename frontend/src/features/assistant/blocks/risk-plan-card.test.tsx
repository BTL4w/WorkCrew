import { render, screen, fireEvent } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { expect, it, vi } from "vitest";
import messages from "@/shared/i18n/messages/en.json";
import type { ProposalContent } from "@/features/ai-proposals/contracts";
import { RiskPlanCard } from "./risk-plan-card";

const content = {
  project: {
    title: "Delivery",
    description: null,
    start_date: null,
    due_date: null,
  },
  goal: {
    title: "Delivery",
    description: null,
    expected_outcomes: [],
    target_date: null,
  },
  milestones: [],
  project_weeks: [
    {
      ref: "w1",
      week_number: 1,
      start_date: "2026-10-05",
      end_date: "2026-10-11",
      objective: "Deliver",
    },
    {
      ref: "w2",
      week_number: 2,
      start_date: "2026-10-12",
      end_date: "2026-10-18",
      objective: "Review",
    },
  ],
  dependencies: [],
  assumptions: [],
  tasks: [
    {
      ref: "t1",
      title: "Survey",
      description: null,
      due_date: "2026-10-18",
      project_week_ref: "w2",
      milestone_ref: null,
      assignee_membership_id: null,
      required_skill_labels: [],
      estimated_effort_hours: 8,
      acceptance_criteria: [],
    },
  ],
  risk_replan: {
    binding: {
      task_id: "t1",
      risk_assessment_id: "risk",
      fingerprint: "a".repeat(64),
      observation_ids: ["observation:0"],
      affected_week_ids: ["w1"],
    },
    before: {
      project_id: "p",
      project_version: 1,
      project_title: "Delivery",
      project_description: null,
      weeks: [
        {
          id: "w1",
          version: 1,
          week_number: 1,
          start_date: "2026-10-05",
          end_date: "2026-10-11",
          objective: "Deliver",
          status: "IN_PROGRESS",
          baseline_id: null,
          baseline_sequence: null,
        },
        {
          id: "w2",
          version: 1,
          week_number: 2,
          start_date: "2026-10-12",
          end_date: "2026-10-18",
          objective: "Review",
          status: "PLANNED",
          baseline_id: null,
          baseline_sequence: null,
        },
      ],
      tasks: [
        {
          id: "t1",
          version: 1,
          project_week_id: "w1",
          title: "Survey",
          description: null,
          due_date: "2026-10-11",
          estimated_effort_hours: 8,
          assignee_membership_id: "person",
          status: "IN_PROGRESS",
          required_skill_labels: [],
          acceptance_criteria: [],
        },
      ],
    },
  },
} as ProposalContent;

it("shows weekly before/after diff and keeps execution behind human actions", () => {
  const approve = vi.fn(),
    reject = vi.fn();
  render(
    <NextIntlClientProvider locale="en" messages={messages}>
      <RiskPlanCard
        content={content}
        version={1}
        stale={false}
        canManage={true}
        canApprove={true}
        onEdit={vi.fn()}
        onRevise={vi.fn()}
        onApprove={approve}
        onReject={reject}
      />
    </NextIntlClientProvider>,
  );
  expect(screen.getByText("Survey")).toBeInTheDocument();
  expect(screen.getByText("Week 1")).toBeInTheDocument();
  expect(screen.getByText("Week 2")).toBeInTheDocument();
  expect(approve).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Reject" }));
  expect(reject).toHaveBeenCalledOnce();
});

it("lets the manager inspect and edit the full new task before approving", async () => {
  const detailed: ProposalContent = {
    ...content,
    tasks: [
      ...content.tasks,
      {
        ...content.tasks[0],
        ref: "new:0",
        title: "Follow up",
        description: "Contact the delivery owner",
        acceptance_criteria: ["Owner confirms receipt"],
      },
    ],
  };
  const edit = vi.fn().mockResolvedValue(true);
  render(
    <NextIntlClientProvider locale="en" messages={messages}>
      <RiskPlanCard
        content={detailed}
        version={1}
        stale={false}
        canManage={true}
        canApprove={true}
        onEdit={edit}
        onRevise={vi.fn()}
        onApprove={vi.fn()}
        onReject={vi.fn()}
      />
    </NextIntlClientProvider>,
  );
  expect(screen.getByText("Contact the delivery owner")).toBeInTheDocument();
  expect(screen.getByText("Owner confirms receipt")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Edit changes" }));
  fireEvent.change(screen.getByLabelText("Acceptance criteria"), {
    target: { value: "Receipt documented" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Save edited proposal" }));
  expect(edit.mock.calls[0][0].tasks[1].acceptance_criteria).toEqual([
    "Receipt documented",
  ]);
});
