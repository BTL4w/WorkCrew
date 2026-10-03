import { z } from "zod";
import { requestJson } from "@/shared/api/client";
import { riskSchema, reviewSchema, notificationSchema, type Review } from "./contracts";
export function currentRisk(task: string) { return requestJson(`/api/v1/risks?task_id=${task}`, { schema: riskSchema.nullable() }); }
export function refreshRisk(task: string, key: string) { return requestJson("/api/v1/risks/refresh", { schema: riskSchema, init: { method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": key }, body: JSON.stringify({ task_id: task }) } }); }
export function riskReviews(id: string) { return requestJson(`/api/v1/risks/${id}/reviews`, { schema: z.array(reviewSchema) }); }
export function recordReview(id: string, body: Pick<Review, "disposition" | "reason">, key: string) { return requestJson(`/api/v1/risks/${id}/reviews`, { schema: reviewSchema, init: { method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": key }, body: JSON.stringify(body) } }); }
export function notifications() { return requestJson("/api/v1/notifications", { schema: z.array(notificationSchema) }); }
export function markRead(id: string, key: string) { return requestJson(`/api/v1/notifications/${id}/read`, { schema: notificationSchema, init: { method: "POST", headers: { "Idempotency-Key": key } } }); }
