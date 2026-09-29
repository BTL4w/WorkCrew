import { z } from "zod";
import { requestJson } from "@/shared/api/client";
import { confirmedUpdateSchema,dailyDraftSchema,observationSchema,reportingContextSchema,type DailyDraft,type ReportingItem } from "./reporting-contracts";
export function getReportingContext(taskId:string){return requestJson(`/api/v1/tasks/${taskId}/reporting-context`,{schema:reportingContextSchema});}
export function getUpdateHistory(taskId:string){return requestJson(`/api/v1/daily-updates?task_id=${taskId}`,{schema:z.array(observationSchema)});}
export function createDailyDraft(items:ReportingItem[],key:string){return requestJson("/api/v1/daily-updates/drafts",{schema:dailyDraftSchema,expectedStatus:201,init:{method:"POST",headers:{"Content-Type":"application/json","Idempotency-Key":key},body:JSON.stringify({items})}});}
export function submitDailyDraft(draft:DailyDraft,key:string){return requestJson("/api/v1/daily-updates",{schema:confirmedUpdateSchema,expectedStatus:201,init:{method:"POST",headers:{"Content-Type":"application/json","Idempotency-Key":key},body:JSON.stringify({draft_id:draft.id,expected_draft_version:draft.version})}});}
