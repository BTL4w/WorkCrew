import type { ZodType } from "zod";

import {
  requestJson,
  requestJsonWithMetadata,
  requestNoContent,
  type ApiResult,
} from "@/shared/api/client";
import { taskPageSchema, type Task } from "@/features/work/contracts";

import {
  acceptanceCriterionPageSchema,
  acceptanceCriterionSchema,
  dependencyPageSchema,
  goalPageSchema,
  goalSchema,
  milestonePageSchema,
  milestoneSchema,
  projectWeekPageSchema,
  projectWeekSchema,
  taskDependencySchema,
  type AcceptanceCriterion,
  type AcceptanceCriterionInput,
  type AcceptanceCriterionPatch,
  type DependencyInput,
  type DependencyPatch,
  type Goal,
  type GoalInput,
  type GoalPatch,
  type Milestone,
  type MilestoneInput,
  type MilestonePatch,
  type ProjectPlan,
  type ProjectWeek,
  type ProjectWeekInput,
  type ProjectWeekPatch,
  type TaskDependency,
} from "./contracts";

const jsonHeaders = { "Content-Type": "application/json" };
type Page<T> = { items: T[]; page: number; page_size: number; total: number };
export type ProjectPlanBundle = { plan: ProjectPlan; tasks: Task[] };

function query(resource: string, filters: Record<string, string>, page: number) {
  const params = new URLSearchParams({ ...filters, page: String(page), page_size: "100" });
  return `/api/v1/${resource}?${params.toString()}`;
}

async function allPages<T>(
  resource: string,
  filters: Record<string, string>,
  schema: ZodType<Page<T>>,
): Promise<T[]> {
  const items: T[] = [];
  for (let page = 1; ; page += 1) {
    const result = await requestJson(query(resource, filters, page), { schema });
    items.push(...result.items);
    if (items.length >= result.total || result.items.length === 0) return items;
  }
}

function mutate<T>(
  path: string,
  method: "POST" | "PATCH",
  input: unknown,
  schema: ZodType<T>,
  key: string,
  version?: number,
): Promise<ApiResult<T>> {
  return requestJsonWithMetadata(path, {
    schema,
    init: {
      method,
      headers: {
        ...jsonHeaders,
        "Idempotency-Key": key,
        ...(version === undefined ? {} : { "If-Match": `"${version}"` }),
      },
      body: JSON.stringify(input),
    },
  });
}

function remove(path: string, version: number, key: string): Promise<void> {
  return requestNoContent(path, {
    method: "DELETE",
    headers: { "Idempotency-Key": key, "If-Match": `"${version}"` },
  });
}

export async function getProjectPlanBundle(projectId: string): Promise<ProjectPlanBundle> {
  const [goals, milestones, projectWeeks, dependencies, tasks] = await Promise.all([
    allPages("goals", { project_id: projectId }, goalPageSchema),
    allPages("milestones", { project_id: projectId }, milestonePageSchema),
    allPages(`projects/${projectId}/weeks`, {}, projectWeekPageSchema).catch(() => []),
    allPages("task-dependencies", { project_id: projectId }, dependencyPageSchema),
    allPages<Task>("tasks", { project_id: projectId }, taskPageSchema),
  ]);
  const criteriaByTask = await Promise.all(
    tasks.map((task) => allPages("acceptance-criteria", { task_id: task.id }, acceptanceCriterionPageSchema)),
  );
  return {
    plan: {
      goal: goals[0] ?? null,
      milestones,
      project_weeks: projectWeeks,
      dependencies,
      acceptance_criteria: criteriaByTask.flat(),
    },
    tasks,
  };
}

export function listProjectWeeks(projectId: string): Promise<ProjectWeek[]> {
  return allPages(`projects/${projectId}/weeks`, {}, projectWeekPageSchema);
}

export async function listAcceptanceCriteria(taskId: string): Promise<AcceptanceCriterion[]> {
  return allPages("acceptance-criteria", { task_id: taskId }, acceptanceCriterionPageSchema);
}

export async function getProjectPlan(projectId: string): Promise<ProjectPlan> {
  return (await getProjectPlanBundle(projectId)).plan;
}

export function createGoal(input: GoalInput, key: string): Promise<ApiResult<Goal>> {
  return mutate("/api/v1/goals", "POST", input, goalSchema, key);
}
export function updateGoal(id: string, input: GoalPatch, version: number, key: string): Promise<ApiResult<Goal>> {
  return mutate(`/api/v1/goals/${id}`, "PATCH", input, goalSchema, key, version);
}
export function deleteGoal(id: string, version: number, key: string): Promise<void> {
  return remove(`/api/v1/goals/${id}`, version, key);
}

export function createMilestone(input: MilestoneInput, key: string): Promise<ApiResult<Milestone>> {
  return mutate("/api/v1/milestones", "POST", input, milestoneSchema, key);
}
export function updateMilestone(id: string, input: MilestonePatch, version: number, key: string): Promise<ApiResult<Milestone>> {
  return mutate(`/api/v1/milestones/${id}`, "PATCH", input, milestoneSchema, key, version);
}
export function deleteMilestone(id: string, version: number, key: string): Promise<void> {
  return remove(`/api/v1/milestones/${id}`, version, key);
}

export function createProjectWeek(projectId: string, input: ProjectWeekInput, key: string): Promise<ApiResult<ProjectWeek>> {
  return mutate(`/api/v1/projects/${projectId}/weeks`, "POST", input, projectWeekSchema, key);
}
export function updateProjectWeek(projectId: string, id: string, input: ProjectWeekPatch, version: number, key: string): Promise<ApiResult<ProjectWeek>> {
  return mutate(`/api/v1/projects/${projectId}/weeks/${id}`, "PATCH", input, projectWeekSchema, key, version);
}
export function deleteProjectWeek(projectId: string, id: string, version: number, key: string): Promise<void> {
  return remove(`/api/v1/projects/${projectId}/weeks/${id}`, version, key);
}

export function createDependency(input: DependencyInput, key: string): Promise<ApiResult<TaskDependency>> {
  return mutate("/api/v1/task-dependencies", "POST", input, taskDependencySchema, key);
}
export function updateDependency(id: string, input: DependencyPatch, version: number, key: string): Promise<ApiResult<TaskDependency>> {
  return mutate(`/api/v1/task-dependencies/${id}`, "PATCH", input, taskDependencySchema, key, version);
}
export function deleteDependency(id: string, version: number, key: string): Promise<void> {
  return remove(`/api/v1/task-dependencies/${id}`, version, key);
}

export function createAcceptanceCriterion(input: AcceptanceCriterionInput, key: string): Promise<ApiResult<AcceptanceCriterion>> {
  return mutate("/api/v1/acceptance-criteria", "POST", input, acceptanceCriterionSchema, key);
}
export function updateAcceptanceCriterion(id: string, input: AcceptanceCriterionPatch, version: number, key: string): Promise<ApiResult<AcceptanceCriterion>> {
  return mutate(`/api/v1/acceptance-criteria/${id}`, "PATCH", input, acceptanceCriterionSchema, key, version);
}
export function deleteAcceptanceCriterion(id: string, version: number, key: string): Promise<void> {
  return remove(`/api/v1/acceptance-criteria/${id}`, version, key);
}
