import { requestJson } from "@/shared/api/client";
import { evaluationRunSchema, type EvaluationRequest } from "./contracts";
export function startEvaluation(body:EvaluationRequest,key:string){
 return requestJson("/api/v1/evaluations/runs",{schema:evaluationRunSchema,expectedStatus:202,init:{method:"POST",headers:{"Content-Type":"application/json","Idempotency-Key":key},body:JSON.stringify(body)}});
}
export function getEvaluation(id:string){return requestJson(`/api/v1/evaluations/runs/${encodeURIComponent(id)}`,{schema:evaluationRunSchema});}
