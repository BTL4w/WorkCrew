import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { managerActor, renderWithAppProviders } from "@/test/render";
import { AppLocaleProvider } from "@/shared/i18n/locale-provider";

import { formatCalendarDate, WorkWorkspace } from "./workspace";

const project = {
  id: "11111111-1111-4111-8111-111111111111",
  name: "Customer onboarding",
  description: "Standardize onboarding",
  version: 1,
  created_at: "2026-08-01T10:00:00Z",
  updated_at: "2026-08-01T10:00:00Z",
};
const employeeId = "44444444-4444-4444-8444-444444444444";
const projectWeek = {
  id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
  project_id: project.id,
  week_number: 1,
  start_date: "2026-08-10",
  end_date: "2026-08-16",
  objective: "Collect onboarding documents",
  status: "PLANNED",
  version: 1,
  created_at: "2026-08-01T10:00:00Z",
  updated_at: "2026-08-01T10:00:00Z",
};
const task = {
  id: "22222222-2222-4222-8222-222222222222",
  project_id: project.id,
  project_week_id: projectWeek.id,
  milestone_id: null,
  title: "Collect documents",
  description: "Collect required documents",
  assignee: { membership_id: employeeId, display_name: "Demo Employee" },
  required_skill_labels: [],
  estimated_effort_hours: 8,
  status: "TO_DO",
  due_date: "2026-08-12",
  version: 1,
  created_at: "2026-08-01T10:00:00Z",
  updated_at: "2026-08-01T10:00:00Z",
};

function page(items: unknown[]) {
  return { items, page: 1, page_size: 20, total: items.length };
}

describe("WorkWorkspace", () => {
  afterEach(() => {
    globalThis.localStorage.clear();
    globalThis.history.replaceState({}, "", "/");
    vi.unstubAllGlobals();
  });

  it("formats due dates as timezone-free calendar dates", () => {
    expect(formatCalendarDate("2026-08-12", "en-US")).toBe("8/12/2026");
  });

  it("keeps the conversation sidebar and history mounted across every workspace view", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      if (String(input) === "/api/v1/projects") return response(page([]));
      if (String(input) === "/api/v1/my-tasks?page=1&page_size=20") return response(page([]));
      if (String(input) === "/api/v1/ai/conversations") return response({ items: [{
        id: "77777777-7777-4777-8777-777777777777",
        locale: "vi",
        title: "Kế hoạch ra mắt",
        status: "ACTIVE",
        last_message_sequence: 2,
        last_event_sequence: 4,
        created_at: "2026-08-13T10:00:00Z",
        updated_at: "2026-08-13T10:01:00Z",
      }] });
      throw new Error(`Unexpected request: ${String(input)}`);
    }));

    const { container } = renderWithAppProviders(<WorkWorkspace actor={managerActor} />);

    expect(await screen.findByText("Kế hoạch ra mắt")).toBeVisible();
    expect(screen.getByText("Task Management")).toBeVisible();
    expect(container.querySelectorAll("aside")).toHaveLength(1);

    fireEvent.click(screen.getByRole("button", { name: "Task của tôi" }));
    expect(await screen.findByRole("heading", { name: "Task của tôi" })).toBeVisible();
    expect(screen.getByText("Kế hoạch ra mắt")).toBeVisible();

    expect(screen.getByRole("button", { name: "Giao task" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "Giao task" }));
    expect(screen.getByText("Chọn một project để tạo và giao task mới.")).toBeVisible();
    expect(screen.getByText("Kế hoạch ra mắt")).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: "Cuộc trò chuyện mới" }));
    expect(await screen.findByRole("heading", { name: "Trợ lý AI" })).toBeVisible();
    expect(screen.getByRole("textbox", { name: "Nhắn cho Trợ lý AI" })).toBeVisible();
    expect(screen.getByText("Kế hoạch ra mắt")).toBeVisible();
    expect(container.querySelectorAll("aside")).toHaveLength(1);
  });

  it("restores the selected Assistant conversation from the URL after refresh", async () => {
    const conversationId = "77777777-7777-4777-8777-777777777777";
    const conversation = {
      id: conversationId,
      locale: "vi",
      title: "Kế hoạch ra mắt",
      status: "ACTIVE",
      last_message_sequence: 0,
      last_event_sequence: 0,
      created_at: "2026-08-13T10:00:00Z",
      updated_at: "2026-08-13T10:01:00Z",
    };
    globalThis.history.replaceState({}, "", `/?conversation=${conversationId}`);
    vi.stubGlobal("EventSource", class {
      addEventListener() {}
      close() {}
    });
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path === "/api/v1/ai/conversations") return response({ items: [conversation] });
      if (path === `/api/v1/ai/conversations/${conversationId}`) {
        return response({ conversation, messages: [] });
      }
      throw new Error(`Unexpected request: ${path}`);
    }));

    renderWithAppProviders(<WorkWorkspace actor={managerActor} />);

    expect(await screen.findByRole("heading", { name: "Trợ lý AI" })).toBeVisible();
    expect(screen.queryByRole("heading", { name: "Projects" })).not.toBeInTheDocument();
  });

  it("lets a Manager create a Project and assign a Task", async () => {
    let projects = [] as typeof project[];
    const unassignedTask = { ...task, assignee: null };
    let tasks: Array<typeof task | typeof unassignedTask> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const path = String(input);
        if (path === "/api/v1/me") return response(managerActor);
        if (path === "/api/v1/projects" && init?.method === "POST") {
          projects = [project];
          return response(project, 201, { ETag: '"1"' });
        }
        if (path === "/api/v1/projects") return response(page(projects));
        if (path.startsWith("/api/v1/tasks?") && !init?.method) return response(page(tasks));
        if (path.startsWith("/api/v1/members")) {
          return response(page([{ membership_id: employeeId, display_name: "Demo Employee", role: "EMPLOYEE", is_active: true }]));
        }
        if (path.startsWith(`/api/v1/projects/${project.id}/weeks?`)) return response(page([projectWeek]));
        if (path === "/api/v1/tasks" && init?.method === "POST") {
          tasks = [unassignedTask];
          return response(unassignedTask, 201, { ETag: '"1"' });
        }
        if (path === `/api/v1/projects/${project.id}/team`) return response({ memberships: [{
          id: "55555555-5555-4555-8555-555555555555", project_id: project.id, membership_id: employeeId,
          decision_id: "66666666-6666-4666-8666-666666666666", active: true, created_at: "2026-08-01T10:00:00Z",
        }] });
        if (path.startsWith("/api/v1/workload?")) return response([{ membership_id: employeeId,
          project_week_id: projectWeek.id, effective_capacity_hours: 40, allocated_effort_hours: 0,
          residual_capacity_hours: 40, workload_ratio: "0.0000" }]);
        if (path === "/api/v1/skills") return response([]);
        if (path === `/api/v1/members/${employeeId}/skills`) return response([]);
        if (path === `/api/v1/tasks/${task.id}/assign` && init?.method === "POST") {
          tasks = [task];
          return response({ task, warnings: [], effective_capacity_hours: 40,
            workload_before_hours: 0, workload_after_hours: task.estimated_effort_hours });
        }
        throw new Error(`Unexpected request: ${path}`);
      }),
    );

    renderWithAppProviders(<WorkWorkspace actor={managerActor} />);
    fireEvent.click(await screen.findByRole("button", { name: "Tạo project" }));
    fireEvent.change(screen.getByLabelText("Tên project"), { target: { value: project.name } });
    fireEvent.click(screen.getByRole("button", { name: "Lưu project" }));

    expect(await screen.findByRole("heading", { name: project.name })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Tạo task" }));
    fireEvent.change(await screen.findByLabelText("Tiêu đề task"), { target: { value: task.title } });
    fireEvent.change(screen.getByLabelText("Tuần dự án"), { target: { value: projectWeek.id } });
    fireEvent.click(screen.getByRole("button", { name: "Lưu task" }));

    fireEvent.click(await screen.findByText(task.title));
    const taskCard = screen.getByRole("heading", { name: task.title }).closest("section")!;
    fireEvent.click(within(taskCard).getByRole("button", { name: "Giao task" }));
    fireEvent.change(await within(taskCard).findByLabelText("Thành viên dự án"), { target: { value: employeeId } });
    fireEvent.click(await within(taskCard).findByRole("button", { name: "Xác nhận giao cho Demo Employee" }));

    expect(await within(taskCard).findByText("Đã giao cho Demo Employee")).toBeVisible();
  });

  it("lets an Employee start a Task but requires a report before completion", async () => {
    const employeeActor = {
      ...managerActor,
      user: { ...managerActor.user, display_name: "Demo Employee", email: "employee@example.test" },
      membership: { ...managerActor.membership, id: employeeId, role: "EMPLOYEE" as const },
    };
    let currentTask = task;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const path = String(input);
        if (path === "/api/v1/me") return response(employeeActor);
        if (path.startsWith("/api/v1/my-tasks") && !init?.method) return response(page([currentTask]));
        if (path.startsWith("/api/v1/acceptance-criteria?")) return response({items:[],page:1,page_size:100,total:0});
        if (path.endsWith("/reporting-context")) return response({task_id:task.id,task_version:currentTask.version,progress_version:0,reported_percent:null,remaining_hours:null,reporting_timezone:"UTC",reporting_date:"2026-09-29",project_week_state:"LINKED",evidence_refs:[]});
        if (path.endsWith("/status") && init?.method === "POST") {
          const target = JSON.parse(String(init.body)).to_status as "IN_PROGRESS" | "DONE";
          currentTask = { ...currentTask, status: target, version: currentTask.version + 1 };
          return response(currentTask, 200, { ETag: `"${currentTask.version}"` });
        }
        throw new Error(`Unexpected request: ${path}`);
      }),
    );

    renderWithAppProviders(<WorkWorkspace actor={employeeActor} />);
    expect(screen.getByRole("button", { name: "Cuộc trò chuyện mới" })).toBeEnabled();
    fireEvent.click(await screen.findByText(task.title));
    expect(screen.queryByRole("button", { name: "Sửa task" })).not.toBeInTheDocument();
    const taskCard = screen.getByRole("heading", { name: task.title }).closest("section")!;
    expect(within(taskCard).queryByRole("button", { name: "Giao task" })).not.toBeInTheDocument();
    expect(within(taskCard).getByText("Demo Employee")).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: "Bắt đầu task" }));
    expect(await screen.findByText("Đang thực hiện")).toBeVisible();
    expect(await screen.findByRole("button", { name: "Hoàn tất" })).toBeDisabled();
    expect(await screen.findByRole("link", { name: "Mở báo cáo hằng ngày" })).toBeVisible();
  });

  it("never reuses actor-independent cached work data for another account", async () => {
    const employeeActor = {
      ...managerActor,
      user: { ...managerActor.user, display_name: "Demo Employee", email: "employee@example.test" },
      membership: { ...managerActor.membership, id: employeeId, role: "EMPLOYEE" as const },
    };
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false, staleTime: 30_000 } },
    });
    queryClient.setQueryData(["work", "myTasks", "ALL"], page([task]));
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).startsWith("/api/v1/my-tasks")) return response(page([]));
      throw new Error(`Unexpected request: ${String(input)}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <AppLocaleProvider initialLocale="vi">
        <QueryClientProvider client={queryClient}>
          <WorkWorkspace actor={employeeActor} />
        </QueryClientProvider>
      </AppLocaleProvider>,
    );

    expect(await screen.findByText("Bạn chưa có task nào phù hợp.")).toBeVisible();
    expect(screen.queryByText(task.title)).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/my-tasks?page=1&page_size=20",
      expect.objectContaining({ credentials: "include" }),
    );
  });

  it("switches the workspace between Vietnamese and English", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      if (String(input) === "/api/v1/me") return response(managerActor);
      if (String(input) === "/api/v1/projects") return response(page([]));
      throw new Error(`Unexpected request: ${String(input)}`);
    }));

    renderWithAppProviders(<WorkWorkspace actor={managerActor} />);
    fireEvent.click(screen.getByRole("button", { name: "en" }));

    expect(await screen.findByRole("button", { name: "Create project" })).toBeVisible();
    expect(document.documentElement.lang).toBe("en");
  });

  it("opens the bilingual People and skills workspace without replacing the Assistant shell", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path === "/api/v1/ai/conversations") return response({ items: [] });
      if (path === "/api/v1/members?is_active=true&page=1&page_size=100") return response(page([{ membership_id: managerActor.membership.id, display_name: "Demo Manager", role: "MANAGER", is_active: true }]));
      if (path === "/api/v1/skills") return response([]);
      if (path === `/api/v1/members/${managerActor.membership.id}/skills`) return response([]);
      if (path === `/api/v1/members/${managerActor.membership.id}/work-evidence`) return response([]);
      throw new Error(`Unexpected request: ${path}`);
    }));

    const { container } = renderWithAppProviders(<WorkWorkspace actor={managerActor} />, "en");
    fireEvent.click(await screen.findByRole("button", { name: "People & skills" }));

    expect(await screen.findByRole("heading", { name: "People & skills" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "Demo Manager" })).toBeVisible();
    expect(container.querySelectorAll("aside")).toHaveLength(1);
  });

  it("reuses the status idempotency key after an uncertain network failure", async () => {
    const employeeActor = {
      ...managerActor,
      user: { ...managerActor.user, display_name: "Demo Employee", email: "employee@example.test" },
      membership: { ...managerActor.membership, id: employeeId, role: "EMPLOYEE" as const },
    };
    const keys: Array<string | null> = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path === "/api/v1/automations/daily-summaries/deliveries") return response([]);
      if (path === "/api/v1/me") return response(employeeActor);
      if (path.startsWith("/api/v1/my-tasks")) return response(page([task]));
      if (path.endsWith("/reporting-context")) return response({task_id:task.id,task_version:1,progress_version:0,reported_percent:null,remaining_hours:null,reporting_timezone:"UTC",reporting_date:"2026-09-29",project_week_state:"LINKED",evidence_refs:[]});
      if (path.startsWith("/api/v1/daily-updates?")) return response([]);
      if (path.endsWith("/status") && init?.method === "POST") {
        keys.push(new Headers(init.headers).get("Idempotency-Key"));
        if (keys.length === 1) throw new TypeError("simulated connection loss");
        return response({ ...task, status: "IN_PROGRESS", version: 2 });
      }
      throw new Error(`Unexpected request: ${path}`);
    }));

    renderWithAppProviders(<WorkWorkspace actor={employeeActor} />);
    fireEvent.click(await screen.findByText(task.title));
    fireEvent.click(screen.getByRole("button", { name: "Bắt đầu task" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Không thể hoàn tất yêu cầu");
    fireEvent.click(screen.getByRole("button", { name: "Bắt đầu task" }));

    expect(await screen.findByText("Đang thực hiện")).toBeVisible();
    expect(keys).toHaveLength(2);
    expect(keys[0]).toBe(keys[1]);
  });

  it("reuses a form idempotency key after a malformed successful response", async () => {
    const keys: Array<string | null> = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path === "/api/v1/projects" && init?.method === "POST") {
        keys.push(new Headers(init.headers).get("Idempotency-Key"));
        return keys.length === 1 ? response({ id: project.id }, 201) : response(project, 201);
      }
      if (path === "/api/v1/projects") return response(page([]));
      throw new Error(`Unexpected request: ${path}`);
    }));

    renderWithAppProviders(<WorkWorkspace actor={managerActor} />);
    fireEvent.click(await screen.findByRole("button", { name: "Tạo project" }));
    fireEvent.change(screen.getByLabelText("Tên project"), { target: { value: project.name } });
    fireEvent.click(screen.getByRole("button", { name: "Lưu project" }));
    expect(await screen.findByRole("alert")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Lưu project" }));

    expect(await screen.findByRole("heading", { name: project.name })).toBeVisible();
    expect(keys).toHaveLength(2);
    expect(keys[0]).toBe(keys[1]);
  });

  it("moves to the previous filtered page when its last Task leaves the filter", async () => {
    const employeeActor = {
      ...managerActor,
      membership: { ...managerActor.membership, id: employeeId, role: "EMPLOYEE" as const },
    };
    const requestedPages: number[] = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.startsWith("/api/v1/my-tasks") && !init?.method) {
        const requestedPage = Number(new URL(path, "http://test").searchParams.get("page"));
        requestedPages.push(requestedPage);
        return response({ items: requestedPage === 2 ? [task] : Array.from({ length: 20 }, (_, index) => ({ ...task, id: `00000000-0000-4000-8000-${String(index + 1).padStart(12, "0")}`, title: `Task ${index + 1}` })), page: requestedPage, page_size: 20, total: 21 });
      }
      if (path.endsWith("/status") && init?.method === "POST") return response({ ...task, status: "IN_PROGRESS", version: 2 });
      throw new Error(`Unexpected request: ${path}`);
    }));

    renderWithAppProviders(<WorkWorkspace actor={employeeActor} />);
    fireEvent.change(await screen.findByLabelText("Lọc theo trạng thái"), { target: { value: "TO_DO" } });
    fireEvent.click(await screen.findByRole("button", { name: "Trang sau" }));
    fireEvent.click(await screen.findByText(task.title));
    fireEvent.click(screen.getByRole("button", { name: "Bắt đầu task" }));
    fireEvent.click(await screen.findByRole("button", { name: /Quay lại/ }));

    await waitFor(() => expect(requestedPages.at(-1)).toBe(1));
    expect(await screen.findByText("Task 1")).toBeVisible();
  });

  it("keeps assignment out of the generic Task editor", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path === "/api/v1/projects") return response(page([project]));
      if (path.startsWith("/api/v1/tasks?")) return response(page([]));
      if (path.startsWith(`/api/v1/projects/${project.id}/weeks?`)) return response(page([projectWeek]));
      throw new Error(`Unexpected request: ${path}`);
    }));

    renderWithAppProviders(<WorkWorkspace actor={managerActor} />);
    fireEvent.click(await screen.findByRole("button", { name: new RegExp(project.name) }));
    fireEvent.click(screen.getByRole("button", { name: "Tạo task" }));
    expect(await screen.findByLabelText("Tiêu đề task")).toBeVisible();
    expect(screen.queryByLabelText("Người thực hiện")).not.toBeInTheDocument();
  });

  it("distinguishes a Project fetch failure from an empty list and retries", async () => {
    let projectRequests = 0;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path === "/api/v1/me") return response(managerActor);
      if (path === "/api/v1/projects") {
        projectRequests += 1;
        if (projectRequests === 1) return response({
          error: {
            code: "SERVICE_UNAVAILABLE",
            message_key: "common.error.unavailable",
            request_id: "request-retry",
            field_errors: [],
            details: {},
          },
        }, 503);
        return response(page([]));
      }
      throw new Error(`Unexpected request: ${path}`);
    }));

    renderWithAppProviders(<WorkWorkspace actor={managerActor} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("request-retry");
    fireEvent.click(screen.getByRole("button", { name: "Thử lại" }));

    expect(await screen.findByText("Chưa có project nào.")).toBeVisible();
    expect(projectRequests).toBe(2);
  });

  it("offers a reload action for a stale Project edit", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path === "/api/v1/me") return response(managerActor);
      if (path === "/api/v1/projects") return response(page([project]));
      if (path.startsWith("/api/v1/tasks?")) return response(page([]));
      if (path === `/api/v1/projects/${project.id}` && init?.method === "PATCH") {
        return response({
          error: {
            code: "RESOURCE_VERSION_MISMATCH",
            message_key: "common.error.conflict",
            request_id: "request-conflict",
            field_errors: [],
            details: {},
          },
        }, 409);
      }
      throw new Error(`Unexpected request: ${path}`);
    }));

    renderWithAppProviders(<WorkWorkspace actor={managerActor} />);
    fireEvent.click(await screen.findByRole("button", { name: new RegExp(project.name) }));
    fireEvent.click(screen.getByRole("button", { name: "Sửa project" }));
    fireEvent.click(screen.getByRole("button", { name: "Lưu project" }));

    expect(await screen.findByRole("button", { name: "Tải lại dữ liệu" })).toBeVisible();
  });

  it("opens the inline Project Plan and Task Acceptance Criteria from work details", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path === "/api/v1/projects") return response(page([project]));
      if (path === `/api/v1/tasks?project_id=${project.id}&page=1&page_size=20`) return response(page([task]));
      if (path.startsWith("/api/v1/goals?")) return response({ ...page([]), page_size: 100 });
      if (path.startsWith("/api/v1/milestones?")) return response({ ...page([]), page_size: 100 });
      if (path.startsWith("/api/v1/task-dependencies?")) return response({ ...page([]), page_size: 100 });
      if (path === `/api/v1/tasks?project_id=${project.id}&page=1&page_size=100`) return response({ ...page([task]), page_size: 100 });
      if (path === `/api/v1/acceptance-criteria?task_id=${task.id}&page=1&page_size=100`) return response({ ...page([]), page_size: 100 });
      throw new Error(`Unexpected request: ${path}`);
    }));

    renderWithAppProviders(<WorkWorkspace actor={managerActor} />);
    fireEvent.click(await screen.findByRole("button", { name: new RegExp(project.name) }));
    fireEvent.click(screen.getByRole("tab", { name: "Kế hoạch" }));
    expect(await screen.findByRole("heading", { name: "Kế hoạch project" })).toBeVisible();

    fireEvent.click(screen.getByRole("tab", { name: "Tasks" }));
    fireEvent.click(await screen.findByText(task.title));
    fireEvent.click(screen.getByRole("button", { name: "Tiêu chí chấp nhận" }));
    expect(await screen.findByRole("heading", { name: "Tiêu chí chấp nhận" })).toBeVisible();
  });

  it("opens Project Team for the exact selected project without replacing Assistant", async () => {
    const requests: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      requests.push(path);
      if (path === "/api/v1/projects") return response(page([project]));
      if (path === `/api/v1/tasks?project_id=${project.id}&page=1&page_size=20`) return response(page([]));
      if (path === `/api/v1/tasks?project_id=${project.id}&page=1&page_size=100`) return response({ ...page([]), page_size: 100 });
      if (path === `/api/v1/projects/${project.id}/team-requirements`) return response({ error: {
        code: "RESOURCE_NOT_FOUND", message_key: "common.error.notFound", request_id: "test",
        field_errors: [], details: {},
      } }, 404);
      if (path === "/api/v1/skills") return response([]);
      if (path === `/api/v1/projects/${project.id}/weeks?page=1&page_size=100`) return response({ ...page([]), page_size: 100 });
      throw new Error(`Unexpected request: ${path}`);
    }));

    const { container } = renderWithAppProviders(<WorkWorkspace actor={managerActor} />);
    fireEvent.click(await screen.findByRole("button", { name: new RegExp(project.name) }));
    fireEvent.click(screen.getByRole("tab", { name: "Đội ngũ" }));

    expect(await screen.findByRole("heading", { name: "Yêu cầu đội ngũ" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Tạo yêu cầu từ task" })).toBeEnabled();
    expect(requests).toContain(`/api/v1/projects/${project.id}/team-requirements`);
    expect(container.querySelectorAll("aside")).toHaveLength(1);
  });

  it("offers the explicit Project Team assignment action from a Manager Task card", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path === "/api/v1/projects") return response(page([project]));
      if (path === `/api/v1/tasks?project_id=${project.id}&page=1&page_size=20`) return response(page([{ ...task, assignee: null }]));
      throw new Error(`Unexpected request: ${path}`);
    }));

    renderWithAppProviders(<WorkWorkspace actor={managerActor} />);
    fireEvent.click(await screen.findByRole("button", { name: new RegExp(project.name) }));
    fireEvent.click(await screen.findByText(task.title));

    const taskCard = screen.getByRole("heading", { name: task.title }).closest("section");
    expect(taskCard).not.toBeNull();
    expect(within(taskCard!).getByRole("button", { name: "Giao task" })).toBeVisible();
  });
});

function response(body: unknown, status = 200, headers: Record<string, string> = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}
