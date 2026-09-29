import { describe, expect, it } from "vitest";
import { z, type ZodType } from "zod";

import {
  errorDetailSchema,
  errorResponseSchema,
  fieldErrorSchema,
  membershipRoleSchema,
} from "@/shared/api/contracts";

import manifest from "./openapi-contract.json";
import {
  assignmentWarningSchema,
  assigneeSchema,
  explicitAssignmentRequestSchema,
  explicitAssignmentResponseSchema,
  memberPageSchema,
  memberSchema,
  projectCreateSchema,
  projectPageSchema,
  projectSchema,
  projectUpdateSchema,
  taskCreateSchema,
  taskPageSchema,
  taskSchema,
  taskStatusRequestSchema,
  criterionAttestationRequestSchema,
  completionEvidenceRefSchema,
  taskStatusSchema,
  taskUpdateSchema,
} from "./contracts";
import {
  acceptanceCriterionCreateSchema,
  acceptanceCriterionSchema,
  acceptanceCriterionUpdateSchema,
  deleteResultSchema,
  dependencyCreateSchema,
  dependencyUpdateSchema,
  goalCreateSchema,
  goalSchema,
  goalUpdateSchema,
  milestoneCreateSchema,
  milestoneSchema,
  milestoneUpdateSchema,
  planningPageSchema,
  projectWeekCreateSchema,
  projectWeekSchema,
  projectWeekUpdateSchema,
  taskDependencySchema,
} from "@/features/planning/contracts";

type JsonSchema = Record<string, unknown>;

const runtimeSchemas: Record<string, ZodType> = {
  ProjectCreateRequest: projectCreateSchema,
  ProjectUpdateRequest: projectUpdateSchema,
  TaskCreateRequest: taskCreateSchema,
  TaskUpdateRequest: taskUpdateSchema,
  TaskStatusRequest: taskStatusRequestSchema,
  CriterionAttestationRequest: criterionAttestationRequestSchema,
  SelectedEvidence: completionEvidenceRefSchema,
  ProjectResponse: projectSchema,
  ProjectPageResponse: projectPageSchema,
  MemberResponse: memberSchema,
  MemberPageResponse: memberPageSchema,
  AssigneeResponse: assigneeSchema,
  TaskResponse: taskSchema,
  TaskPageResponse: taskPageSchema,
  GoalCreateRequest: goalCreateSchema,
  GoalUpdateRequest: goalUpdateSchema,
  GoalResponse: goalSchema,
  MilestoneCreateRequest: milestoneCreateSchema,
  MilestoneUpdateRequest: milestoneUpdateSchema,
  MilestoneResponse: milestoneSchema,
  ProjectWeekCreateRequest: projectWeekCreateSchema,
  ProjectWeekUpdateRequest: projectWeekUpdateSchema,
  ProjectWeekResponse: projectWeekSchema,
  DependencyCreateRequest: dependencyCreateSchema,
  DependencyUpdateRequest: dependencyUpdateSchema,
  DependencyResponse: taskDependencySchema,
  AcceptanceCriterionCreateRequest: acceptanceCriterionCreateSchema,
  AcceptanceCriterionUpdateRequest: acceptanceCriterionUpdateSchema,
  AcceptanceCriterionResponse: acceptanceCriterionSchema,
  PlanningPageResponse: planningPageSchema,
  DeleteResponse: deleteResultSchema,
  FieldError: fieldErrorSchema,
  ErrorDetail: errorDetailSchema,
  ErrorResponse: errorResponseSchema,
};

function withoutSchemaMarker(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(withoutSchemaMarker);
  if (!value || typeof value !== "object") return value;
  return Object.fromEntries(
    Object.entries(value).filter(([key]) => key !== "$schema").map(([key, item]) => [key, withoutSchemaMarker(item)]),
  );
}

const runtimeJsonSchemas = Object.fromEntries(
  Object.entries(runtimeSchemas).map(([name, schema]) => [name, z.toJSONSchema(schema, { io: "input" }) as JsonSchema]),
) as Record<string, JsonSchema>;

const explicitAssignmentRuntimeSchemas: Record<string, ZodType> = {
  ExplicitAssignmentRequest: explicitAssignmentRequestSchema,
  AssignmentWarningResponse: assignmentWarningSchema,
  ExplicitAssignmentResponse: explicitAssignmentResponseSchema,
};
const explicitAssignmentJsonSchemas = Object.fromEntries(
  Object.entries(explicitAssignmentRuntimeSchemas).map(([name, schema]) => [name, z.toJSONSchema(schema, { io: "input" }) as JsonSchema]),
) as Record<string, JsonSchema>;

function referencedSchema(fragment: JsonSchema): string | null {
  const normalized = JSON.stringify(withoutSchemaMarker(fragment));
  return Object.entries({ ...runtimeJsonSchemas, ...explicitAssignmentJsonSchemas }).find(([, candidate]) =>
    JSON.stringify(withoutSchemaMarker(candidate)) === normalized,
  )?.[0] ?? null;
}

function describeFragment(fragment: JsonSchema): string {
  const anyOf = fragment.anyOf;
  if (Array.isArray(anyOf)) {
    return anyOf.map((item) => describeFragment(item as JsonSchema)).sort().join("|");
  }
  if (fragment.type === "array") {
    const items = fragment.items as JsonSchema;
    const reference = referencedSchema(items);
    return `array:${reference ? `ref:${reference}` : describeFragment(items)}`;
  }
  if (fragment.type === "object") {
    const reference = referencedSchema(fragment);
    return reference ? `ref:${reference}` : "object";
  }
  if (fragment.type === "string" && Array.isArray(fragment.enum)) {
    const reference = Object.entries(manifest.enums).find(([, values]) =>
      JSON.stringify(values) === JSON.stringify(fragment.enum),
    )?.[0];
    return reference ? `enum:${reference}` : "enum:unknown";
  }
  if (fragment.type === "string") {
    let result = "string";
    if (fragment.format) result += `:${String(fragment.format)}`;
    if ("minLength" in fragment || "maxLength" in fragment) {
      result += `[${String(fragment.minLength ?? "")},${String(fragment.maxLength ?? "")}]`;
    }
    return result;
  }
  return String(fragment.type);
}

function describeSchema(schema: JsonSchema) {
  const properties = schema.properties as Record<string, JsonSchema>;
  return {
    required: (schema.required as string[] | undefined) ?? [],
    properties: Object.fromEntries(
      Object.entries(properties).map(([name, fragment]) => [name, describeFragment(fragment)]),
    ),
  };
}

const uuid = "11111111-1111-4111-8111-111111111111";
const timestamp = "2026-08-01T10:00:00Z";
const project = { id: uuid, name: "Project", description: null, version: 1, created_at: timestamp, updated_at: timestamp };
const member = { membership_id: uuid, display_name: "Employee", role: "EMPLOYEE", is_active: true };
const task = { id: uuid, project_id: uuid, project_week_id: uuid, milestone_id: null, title: "Task", description: null, assignee: { membership_id: uuid, display_name: "Employee" }, required_skill_labels: [], estimated_effort_hours: 8, status: "TO_DO", due_date: "2026-08-12", version: 1, created_at: timestamp, updated_at: timestamp };
const page = (item: unknown) => ({ items: [item], page: 1, page_size: 20, total: 1 });

describe("deep work OpenAPI compatibility manifest", () => {
  it("keeps all enums aligned", () => {
    expect(membershipRoleSchema.options).toEqual(manifest.enums.MembershipRole);
    expect(taskStatusSchema.options).toEqual(manifest.enums.TaskStatus);
  });

  it("mechanically aligns every runtime schema, nested reference and request body", () => {
    expect(Object.keys(manifest.schemas)).toEqual(expect.arrayContaining(Object.keys(runtimeSchemas)));
    for (const name of Object.keys(runtimeSchemas)) {
      const expected = manifest.schemas[name as keyof typeof manifest.schemas];
      expect(describeSchema(runtimeJsonSchemas[name]), name).toEqual(expected);
    }
  });

  it("mechanically aligns the explicit assignment request and response contracts", () => {
    expect(Object.keys(manifest.explicit_assignment.schemas)).toEqual(Object.keys(explicitAssignmentRuntimeSchemas));
    for (const name of Object.keys(explicitAssignmentRuntimeSchemas)) {
      const expected = manifest.explicit_assignment.schemas[name as keyof typeof manifest.explicit_assignment.schemas];
      expect(describeSchema(explicitAssignmentJsonSchemas[name]), name).toEqual(expected);
    }
  });

  it.each([
    [projectSchema, project, "id"],
    [projectPageSchema, page(project), "items"],
    [memberSchema, member, "membership_id"],
    [memberPageSchema, page(member), "items"],
    [taskSchema, task, "id"],
    [taskPageSchema, page(task), "items"],
  ] as const)("accepts a fully typed response and rejects missing required data", (schema, value, requiredField) => {
    expect(schema.safeParse(value).success).toBe(true);
    const missingRequired = { ...value } as Record<string, unknown>;
    delete missingRequired[requiredField];
    expect(schema.safeParse(missingRequired).success).toBe(false);
  });

  it("enforces formats, nested responses, nullability and unknown fields", () => {
    expect(projectSchema.safeParse({ ...project, id: "not-a-uuid" }).success).toBe(false);
    expect(projectSchema.safeParse({ ...project, created_at: "not-a-date" }).success).toBe(false);
    expect(taskSchema.safeParse({ ...task, due_date: "08/12/2026" }).success).toBe(false);
    expect(taskSchema.safeParse({ ...task, assignee: { membership_id: uuid } }).success).toBe(false);
    expect(taskSchema.safeParse({ ...task, description: undefined }).success).toBe(false);
    expect(taskSchema.safeParse({ ...task, unexpected: true }).success).toBe(true);
  });

  it("enforces mutation request bodies and constraints", () => {
    expect(projectCreateSchema.safeParse({ name: "P" }).success).toBe(true);
    expect(projectCreateSchema.safeParse({ name: "" }).success).toBe(false);
    expect(projectCreateSchema.safeParse({ name: "x".repeat(161) }).success).toBe(false);
    expect(projectUpdateSchema.safeParse({ description: null }).success).toBe(true);
    expect(taskCreateSchema.safeParse({ project_id: uuid, project_week_id: uuid, title: "T", assignee_membership_id: null, estimated_effort_hours: 8, due_date: null }).success).toBe(true);
    expect(taskCreateSchema.safeParse({ project_id: uuid, project_week_id: uuid, milestone_id: uuid, title: "T", estimated_effort_hours: 8 }).success).toBe(true);
    expect(taskCreateSchema.safeParse({ project_id: uuid, title: "", assignee_membership_id: "bad" }).success).toBe(false);
    expect(taskCreateSchema.safeParse({ project_id: uuid, milestone_id: "bad", title: "T", assignee_membership_id: uuid }).success).toBe(false);
    expect(taskUpdateSchema.safeParse({ due_date: "2026-08-12" }).success).toBe(true);
    expect(taskUpdateSchema.safeParse({ milestone_id: null }).success).toBe(true);
    expect(taskStatusRequestSchema.safeParse({ to_status: "BLOCKED" }).success).toBe(false);
    expect(explicitAssignmentRequestSchema.safeParse({ assignee_membership_id: uuid, expected_task_version: 1 }).success).toBe(true);
    expect(explicitAssignmentRequestSchema.safeParse({ assignee_membership_id: "bad", expected_task_version: 0 }).success).toBe(false);
  });

  it("validates the deterministic assignment workload projection", () => {
    const assignment = {
      task,
      warnings: [{ code: "ASSIGNEE_OVER_CAPACITY" }],
      effective_capacity_hours: 40,
      workload_before_hours: 35,
      workload_after_hours: 43,
    };
    expect(explicitAssignmentResponseSchema.safeParse(assignment).success).toBe(true);
    expect(explicitAssignmentResponseSchema.safeParse({ ...assignment, warnings: [{}] }).success).toBe(false);
    expect(explicitAssignmentResponseSchema.safeParse({ ...assignment, workload_after_hours: -1 }).success).toBe(false);
  });

  it("enforces the structured error envelope", () => {
    const error = { error: { code: "VALIDATION_ERROR", message_key: "common.error.validation", request_id: "request-1", field_errors: [{ field: "name", code: "too_short", message_key: "common.error.invalidField" }], details: {} } };
    expect(errorResponseSchema.safeParse(error).success).toBe(true);
    expect(errorResponseSchema.safeParse({ error: { code: "VALIDATION_ERROR" } }).success).toBe(false);
  });
});
