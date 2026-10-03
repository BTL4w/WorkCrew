import { z } from "zod";
import { requestJson } from "@/shared/api/client";
import { blockerSchema, transitionSchema, type BlockerCommand } from "./contracts";

export function listBlockers(taskId: string) {
  return requestJson(`/api/v1/blockers?task_id=${taskId}`, { schema: z.array(blockerSchema) });
}
export function blockerHistory(id: string) {
  return requestJson(`/api/v1/blockers/${id}/history`, { schema: z.array(transitionSchema) });
}
export function applyBlocker(command: BlockerCommand, key: string) {
  return requestJson(command.blocker_id ? `/api/v1/blockers/${command.blocker_id}` : "/api/v1/blockers", {
    schema: blockerSchema,
    init: {
      method: command.action === "ARCHIVE" ? "DELETE" : command.blocker_id ? "PATCH" : "POST",
      headers: { "Content-Type": "application/json", "Idempotency-Key": key },
      body: JSON.stringify(command),
    },
  });
}
