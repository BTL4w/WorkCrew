import { requestJson } from "@/shared/api/client";
import {
  draftSchema,
  scheduleSchema,
  viewSchema,
  type ScheduleCommand,
} from "./contracts";
const base = "/api/v1/automations/daily-summaries";
export function getSchedule(projectId: string) {
  return requestJson(`${base}?project_id=${projectId}`, { schema: viewSchema });
}
export function previewSchedule(
  command: ScheduleCommand,
  expectedVersion: number,
  key: string,
) {
  return requestJson(`${base}/preview`, {
    schema: draftSchema,
    init: {
      method: "POST",
      headers: { "Content-Type": "application/json", "Idempotency-Key": key },
      body: JSON.stringify({ command, expected_version: expectedVersion }),
    },
  });
}
export function confirmSchedule(
  draftId: string,
  expectedVersion: number,
  key: string,
) {
  return requestJson(`${base}/confirm`, {
    schema: scheduleSchema,
    init: {
      method: "POST",
      headers: { "Content-Type": "application/json", "Idempotency-Key": key },
      body: JSON.stringify({
        draft_id: draftId,
        expected_version: expectedVersion,
      }),
    },
  });
}
export function pauseSchedule(
  id: string,
  expectedVersion: number,
  paused: boolean,
  key: string,
) {
  return requestJson(`${base}/${id}/pause`, {
    schema: scheduleSchema,
    init: {
      method: "POST",
      headers: { "Content-Type": "application/json", "Idempotency-Key": key },
      body: JSON.stringify({ expected_version: expectedVersion, paused }),
    },
  });
}
