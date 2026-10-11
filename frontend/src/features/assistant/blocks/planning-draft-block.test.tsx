import { fireEvent, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { jsonResponse, renderWithAppProviders } from "@/test/render";
import { PlanningDraftBlock } from "./planning-draft-block";

const draftId = "11111111-1111-4111-8111-111111111111";
const runId = "22222222-2222-4222-8222-222222222222";
const block = {
  kind: "planning_draft" as const,
  workflow_run_id: runId,
  draft_id: draftId,
  draft_version: 1,
  summary: "Launch course",
  state: "DRAFT_AVAILABLE" as const,
  conflicts: [],
};
const field = {
  value: "Six weeks",
  status: "AI_ASSUMPTION",
  source_refs: [],
  assumption_reason: "No date supplied",
};
const snapshot = {
  draft_id: draftId,
  workflow_run_id: runId,
  draft_version: 1,
  current_version: 1,
  fingerprint: "a".repeat(64),
  content: {
    schema_version: "planning-draft.v1",
    brief: {
      objective: {
        value: "100 learners",
        status: "USER_STATED",
        source_refs: ["message:1"],
      },
      audience: field,
      timing: field,
      budget: null,
    },
    duration_days: 42,
    phases: [{ ref: "p1", title: "Prepare course", start_day: 0, end_day: 14 }],
    tasks: [],
    dependencies: [],
    assumptions: [field],
    open_questions: [],
  },
  question: { text: "Do you have a launch date?" },
  validation: { errors: [], conflicts: [] },
  versions: {},
  change_summary: null,
};
afterEach(() => vi.unstubAllGlobals());
it.each(["vi", "en"] as const)(
  "shows early draft, assumptions and optional question in %s without approval",
  async (locale) => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse(snapshot)),
    );
    const prepare = vi.fn();
    renderWithAppProviders(
      <PlanningDraftBlock block={block} canManage onPrepare={prepare} />,
      locale,
    );
    expect(await screen.findByText("100 learners")).toBeVisible();
    expect(screen.getAllByText("Six weeks").length).toBeGreaterThan(0);
    expect(screen.getByText("Do you have a launch date?")).toBeVisible();
    expect(
      screen.queryByRole("button", { name: /Phê duyệt|Approve/ }),
    ).not.toBeInTheDocument();
    const input = screen.getByLabelText(
      locale === "vi" ? "Ngày bắt đầu" : "Start date",
    );
    fireEvent.change(input, { target: { value: "2026-10-20" } });
    fireEvent.click(
      screen.getByRole("button", {
        name:
          locale === "vi" ? "Chuẩn bị tạo dự án" : "Prepare project creation",
      }),
    );
    expect(prepare).toHaveBeenCalledWith(block, "2026-10-20");
  },
);
it("keeps an old card read-only when current version changes", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => jsonResponse({ ...snapshot, current_version: 2 })),
  );
  renderWithAppProviders(
    <PlanningDraftBlock block={block} canManage onPrepare={vi.fn()} />,
  );
  expect(await screen.findByText("100 learners")).toBeVisible();
  expect(
    screen.queryByRole("button", { name: "Chuẩn bị tạo dự án" }),
  ).not.toBeInTheDocument();
});

it("keeps an applied draft read-only", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => jsonResponse({ ...snapshot, can_prepare: false })),
  );
  renderWithAppProviders(
    <PlanningDraftBlock block={block} canManage onPrepare={vi.fn()} />,
  );
  expect(await screen.findByText("100 learners")).toBeVisible();
  expect(
    screen.queryByRole("button", { name: "Chuẩn bị tạo dự án" }),
  ).not.toBeInTheDocument();
});

it("shows the application-clock suggestion and binds the displayed date when preparing", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () =>
      jsonResponse({ ...snapshot, suggested_anchor_date: "2026-10-20" }),
    ),
  );
  const prepare = vi.fn();
  renderWithAppProviders(
    <PlanningDraftBlock block={block} canManage onPrepare={prepare} />,
  );
  expect(await screen.findByLabelText("Ngày bắt đầu")).toHaveValue(
    "2026-10-20",
  );
  fireEvent.click(screen.getByRole("button", { name: "Chuẩn bị tạo dự án" }));
  expect(prepare).toHaveBeenCalledWith(block, "2026-10-20");
});
