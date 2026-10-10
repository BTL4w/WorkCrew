import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderWithAppProviders } from "@/test/render";
import type { Task } from "@/features/work/contracts";

import { ProjectPlanPanel } from "./project-plan";

const projectId = "11111111-1111-4111-8111-111111111111";
const taskId = "22222222-2222-4222-8222-222222222222";
const secondTaskId = "33333333-3333-4333-8333-333333333333";
const membershipId = "44444444-4444-4444-8444-444444444444";
const timestamp = "2026-08-02T10:00:00Z";
const page = (items: unknown[]) => ({ items, page: 1, page_size: 100, total: items.length });
const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status,
  headers: { "Content-Type": "application/json" },
});
const task: Task = {
  id: taskId,
  project_id: projectId,
  project_week_id: null,
  milestone_id: null,
  title: "Collect documents",
  description: null,
  assignee: { membership_id: membershipId, display_name: "Employee" },
  required_skill_labels: [],
  estimated_effort_hours: null,
  status: "TO_DO",
  due_date: null,
  version: 1,
  created_at: timestamp,
  updated_at: timestamp,
};
const secondTask: Task = { ...task, id: secondTaskId, title: "Review documents" };
const criterion = {
  id: "55555555-5555-4555-8555-555555555555",
  task_id: taskId,
  text: "Customs form accepted",
  position: 1,
  version: 1,
  created_at: timestamp,
  updated_at: timestamp,
};
const goal = {
  id: "88888888-8888-4888-8888-888888888888",
  project_id: projectId,
  title: "Launch service",
  description: "Deliver the first release",
  expected_outcomes: ["First customer onboarded"],
  target_date: "2026-09-01",
  version: 1,
  created_at: timestamp,
  updated_at: timestamp,
};
const milestone = {
  id: "99999999-9999-4999-8999-999999999999",
  project_id: projectId,
  name: "Pilot complete",
  description: null,
  target_date: "2026-08-20",
  position: 2,
  version: 1,
  created_at: timestamp,
  updated_at: timestamp,
};
const projectWeek = {
  id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
  project_id: projectId,
  week_number: 1,
  start_date: "2026-08-10",
  end_date: "2026-08-16",
  objective: "Prepare the pilot",
  status: "PLANNED",
  version: 1,
  created_at: timestamp,
  updated_at: timestamp,
};

function error(code: string) {
  return {
    error: {
      code,
      message_key: "common.error.validation",
      request_id: "planning-request",
      field_errors: [],
      details: {},
    },
  };
}

function planFetch({ criteria = [], mutation, goal = null, milestones = [], weeks = [] }: {
  criteria?: unknown[];
  mutation?: (path: string, init?: RequestInit) => Response | Promise<Response> | undefined;
  goal?: unknown | null;
  milestones?: unknown[];
  weeks?: unknown[];
} = {}) {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    const mutated = mutation?.(path, init);
    if (mutated) return mutated;
    if (path.startsWith("/api/v1/goals?")) return response(page(goal ? [goal] : []));
    if (path.startsWith("/api/v1/milestones?")) return response(page(milestones));
    if (path.startsWith(`/api/v1/projects/${projectId}/weeks?`)) return response(page(weeks));
    if (path.startsWith("/api/v1/task-dependencies?")) return response(page([]));
    if (path.startsWith("/api/v1/tasks?")) return response(page([task, secondTask]));
    if (path === `/api/v1/acceptance-criteria?task_id=${taskId}&page=1&page_size=100`) return response(page(criteria));
    if (path === `/api/v1/acceptance-criteria?task_id=${secondTaskId}&page=1&page_size=100`) return response(page([]));
    throw new Error(`Unexpected request: ${path}`);
  });
}

function renderPanel(options: { canManage?: boolean; taskId?: string; locale?: "vi" | "en"; tasks?: Task[] } = {}) {
  return renderWithAppProviders(
    <ProjectPlanPanel
      actorMembershipId={membershipId}
      canManage={options.canManage ?? true}
      organizationId="66666666-6666-4666-8666-666666666666"
      projectId={projectId}
      taskId={options.taskId}
      tasks={options.tasks ?? [task, secondTask]}
    />,
    options.locale ?? "vi",
  );
}

describe("ProjectPlanPanel", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("shows empty Manager CTAs and moves focus into the Goal dialog", async () => {
    vi.stubGlobal("fetch", planFetch());
    renderPanel();

    fireEvent.click(await screen.findByRole("button", { name: "Thêm mục tiêu" }));

    const dialog = screen.getByRole("dialog", { name: "Thêm mục tiêu" });
    expect(dialog).toHaveFocus();
    expect(screen.getByRole("button", { name: "Thêm milestone" })).toBeEnabled();

    const close = screen.getByRole("button", { name: "Đóng" });
    const save = screen.getByRole("button", { name: "Lưu mục tiêu" });
    save.focus();
    fireEvent.keyDown(document, { key: "Tab" });
    expect(close).toHaveFocus();
    close.focus();
    fireEvent.keyDown(document, { key: "Tab", shiftKey: true });
    expect(save).toHaveFocus();
  });

  it("keeps the permitted Employee plan read-only", async () => {
    vi.stubGlobal("fetch", planFetch({ goal, milestones: [milestone] }));
    renderPanel({ canManage: false });

    expect(await screen.findByRole("heading", { name: "Kế hoạch project" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "Thêm mục tiêu" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Thêm milestone" })).not.toBeInTheDocument();
    expect(screen.getByText("Chỉ đọc")).toBeVisible();
    expect(screen.getByText("First customer onboarded")).toBeVisible();
    expect(screen.getByText(/1\/9\/2026/)).toBeVisible();
    expect(screen.getByText("Vị trí: 2")).toBeVisible();
    expect(screen.getByText(/20\/8\/2026/)).toBeVisible();
  });

  it("creates a Goal and Milestone through the manual plan", async () => {
    vi.stubGlobal("fetch", planFetch({
      mutation: (path, init) => {
        if (path === "/api/v1/goals" && init?.method === "POST") return response(goal, 201);
        if (path === "/api/v1/milestones" && init?.method === "POST") return response(milestone, 201);
        return undefined;
      },
    }));
    renderPanel();

    fireEvent.click(await screen.findByRole("button", { name: "Thêm mục tiêu" }));
    fireEvent.change(screen.getByLabelText("Tiêu đề mục tiêu"), { target: { value: goal.title } });
    fireEvent.click(screen.getByRole("button", { name: "Lưu mục tiêu" }));
    expect(await screen.findByText(goal.title)).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: "Thêm milestone" }));
    fireEvent.change(screen.getByLabelText("Tên milestone"), { target: { value: milestone.name } });
    fireEvent.click(screen.getByRole("button", { name: "Lưu milestone" }));
    expect(await screen.findByText(milestone.name)).toBeVisible();
  });

  it("creates, edits and deletes a Project Week through the manual plan", async () => {
    vi.stubGlobal("fetch", planFetch({
      mutation: (path, init) => {
        if (path === `/api/v1/projects/${projectId}/weeks` && init?.method === "POST") return response(projectWeek, 201);
        if (path === `/api/v1/projects/${projectId}/weeks/${projectWeek.id}` && init?.method === "PATCH") return response({ ...projectWeek, objective: "Run the pilot", version: 2 });
        if (path === `/api/v1/projects/${projectId}/weeks/${projectWeek.id}` && init?.method === "DELETE") return new Response(null, { status: 204 });
        return undefined;
      },
    }));
    renderPanel();

    fireEvent.click(await screen.findByRole("button", { name: "Thêm tuần" }));
    fireEvent.change(screen.getByLabelText("Ngày bắt đầu"), { target: { value: projectWeek.start_date } });
    fireEvent.change(screen.getByLabelText("Ngày kết thúc"), { target: { value: projectWeek.end_date } });
    fireEvent.change(screen.getByLabelText("Mục tiêu tuần"), { target: { value: projectWeek.objective } });
    fireEvent.click(screen.getByRole("button", { name: "Lưu tuần" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(screen.getByText(projectWeek.objective, { selector: "p" })).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: "Sửa" }));
    fireEvent.change(screen.getByLabelText("Mục tiêu tuần"), { target: { value: "Run the pilot" } });
    fireEvent.click(screen.getByRole("button", { name: "Lưu tuần" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(screen.getByText("Run the pilot", { selector: "p" })).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: "Xóa" }));
    fireEvent.click(screen.getByRole("button", { name: "Xác nhận xóa" }));
    expect(await screen.findByText("Chưa có tuần dự án.")).toBeVisible();
  });

  it("reuses a Goal idempotency key after an uncertain network failure", async () => {
    const keys: Array<string | null> = [];
    vi.stubGlobal("fetch", planFetch({
      mutation: (path, init) => {
        if (path !== "/api/v1/goals" || init?.method !== "POST") return undefined;
        keys.push(new Headers(init.headers).get("Idempotency-Key"));
        if (keys.length === 1) throw new TypeError("simulated connection loss");
        return response(goal, 201);
      },
    }));
    renderPanel();

    fireEvent.click(await screen.findByRole("button", { name: "Thêm mục tiêu" }));
    fireEvent.change(screen.getByLabelText("Tiêu đề mục tiêu"), { target: { value: goal.title } });
    fireEvent.click(screen.getByRole("button", { name: "Lưu mục tiêu" }));
    expect(await screen.findByRole("alert")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Lưu mục tiêu" }));

    expect(await screen.findByText(goal.title)).toBeVisible();
    expect(keys).toHaveLength(2);
    expect(keys[0]).toBe(keys[1]);
  });

  it("lets a Manager add, edit and delete Acceptance Criteria from Task detail", async () => {
    let criteria = [criterion];
    vi.stubGlobal("fetch", planFetch({
      criteria,
      mutation: (path, init) => {
        if (path === "/api/v1/acceptance-criteria" && init?.method === "POST") {
          criteria = [{ ...criterion, id: "77777777-7777-4777-8777-777777777777", text: "Signed form" }];
          return response(criteria[0], 201);
        }
        if (path === `/api/v1/acceptance-criteria/${criterion.id}` && init?.method === "PATCH") {
          criteria = [{ ...criterion, text: "Customs form verified", version: 2 }];
          return response(criteria[0]);
        }
        if (path === `/api/v1/acceptance-criteria/${criterion.id}` && init?.method === "DELETE") {
          criteria = [];
          return response({ id: criterion.id, version: 2 });
        }
        return undefined;
      },
    }));
    renderPanel({ taskId });

    expect(await screen.findByText(criterion.text)).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Sửa tiêu chí" }));
    fireEvent.change(screen.getByLabelText("Nội dung tiêu chí"), { target: { value: "Customs form verified" } });
    fireEvent.click(screen.getByRole("button", { name: "Lưu tiêu chí" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(screen.getByText("Customs form verified", { selector: "p" })).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: "Xóa tiêu chí" }));
    fireEvent.click(screen.getByRole("button", { name: "Xác nhận xóa" }));
    expect(await screen.findByText("Chưa có tiêu chí chấp nhận.")).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: "Thêm tiêu chí" }));
    fireEvent.change(screen.getByLabelText("Nội dung tiêu chí"), { target: { value: "Signed form" } });
    fireEvent.click(screen.getByRole("button", { name: "Lưu tiêu chí" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(screen.getByText("Signed form", { selector: "p" })).toBeVisible();
  });

  it("focuses the dependency error summary when the backend rejects a cycle", async () => {
    vi.stubGlobal("fetch", planFetch({
      mutation: (path, init) => path === "/api/v1/task-dependencies" && init?.method === "POST"
        ? response(error("VALIDATION_FAILED"), 422)
        : undefined,
    }));
    renderPanel();

    fireEvent.click(await screen.findByRole("button", { name: "Thêm dependency" }));
    fireEvent.change(screen.getByLabelText("Task trước"), { target: { value: taskId } });
    fireEvent.change(screen.getByLabelText("Task sau"), { target: { value: secondTaskId } });
    fireEvent.click(screen.getByRole("button", { name: "Lưu dependency" }));

    const summary = await screen.findByRole("alert");
    expect(summary).toHaveTextContent("Dependency không hợp lệ hoặc tạo chu trình.");
    await waitFor(() => expect(summary).toHaveFocus());
  });

  it("offers dependency task options beyond the currently visible work page", async () => {
    vi.stubGlobal("fetch", planFetch());
    renderPanel({ tasks: [task] });

    fireEvent.click(await screen.findByRole("button", { name: "Thêm dependency" }));

    expect(screen.getAllByRole("option", { name: secondTask.title })).toHaveLength(2);
  });

  it("offers a stale reload action and keeps manual controls enabled without an AI provider", async () => {
    let goalLoads = 0;
    vi.stubGlobal("fetch", planFetch({ goal,
      mutation: (path, init) => {
        if (path.startsWith("/api/v1/goals?") && !init?.method) goalLoads += 1;
        if (path === `/api/v1/goals/${goal.id}` && init?.method === "PATCH") {
          expect(new Headers(init.headers).get("If-Match")).toBe('"1"');
          return response(error("RESOURCE_VERSION_MISMATCH"), 412);
        }
        return undefined;
      },
    }));
    renderPanel({ locale: "en" });

    fireEvent.click(await screen.findByRole("button", { name: "Edit goal" }));
    fireEvent.click(screen.getByRole("button", { name: "Save goal" }));
    const reload = await screen.findByRole("button", { name: "Reload plan" });
    fireEvent.click(reload);

    await waitFor(() => expect(goalLoads).toBeGreaterThan(1));
    expect(screen.getByRole("button", { name: "Add milestone" })).toBeEnabled();
  });
});
