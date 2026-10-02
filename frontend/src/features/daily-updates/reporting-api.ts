import { z } from "zod";
import { requestJson } from "@/shared/api/client";
import { confirmedUpdateSchema,dailyDraftSchema,observationSchema,reportingContextSchema,draftAssessmentSchema,type DraftAssessment,type DailyDraft,type ReportingItem } from "./reporting-contracts";
export function getReportingContext(taskId:string){return requestJson(`/api/v1/tasks/${taskId}/reporting-context`,{schema:reportingContextSchema});}
export function getUpdateHistory(taskId:string){return requestJson(`/api/v1/daily-updates?task_id=${taskId}`,{schema:z.array(observationSchema)});}
export function createDailyDraft(items:ReportingItem[],key:string){return requestJson("/api/v1/daily-updates/drafts",{schema:dailyDraftSchema,expectedStatus:201,init:{method:"POST",headers:{"Content-Type":"application/json","Idempotency-Key":key},body:JSON.stringify({items})}});}
export function submitDailyDraft(draft:DailyDraft,key:string,assessment?:DraftAssessment,acknowledged=false){return requestJson("/api/v1/daily-updates",{schema:confirmedUpdateSchema,expectedStatus:201,init:{method:"POST",headers:{"Content-Type":"application/json","Idempotency-Key":key},body:JSON.stringify({draft_id:draft.id,expected_draft_version:draft.version,assessment_id:assessment?.id??null,warning_acknowledgments:acknowledged?assessment?.warnings.map(w=>w.id)??[]:[]})}});}

export function getDailyAssessment(draftId:string){return requestJson(`/api/v1/daily-updates/${draftId}/evidence-assessments`,{schema:draftAssessmentSchema});}
export function assessDailyDraft(draft:DailyDraft,key:string){return requestJson(`/api/v1/daily-updates/${draft.id}/assess`,{schema:z.object({id:z.string().uuid(),draft_id:z.string().uuid(),state:z.string()}),expectedStatus:202,init:{method:"POST",headers:{"Content-Type":"application/json","Idempotency-Key":key},body:JSON.stringify({expected_version:draft.version})}});}

export function getDailyDraft(id:string){return requestJson(`/api/v1/daily-updates/drafts/${id}`,{schema:dailyDraftSchema});}
export function reviseDailyDraft(draft:DailyDraft,items:ReportingItem[],key:string){return requestJson(`/api/v1/daily-updates/${draft.id}/draft`,{schema:dailyDraftSchema,init:{method:"PATCH",headers:{"Content-Type":"application/json","Idempotency-Key":key},body:JSON.stringify({expected_version:draft.version,items})}});}
