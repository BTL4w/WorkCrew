import {requestJson} from "@/shared/api/client";
import {feedbackResultSchema,type FeedbackInput} from "./contracts";
export function recordFeedback(body:FeedbackInput,key:string){
 return requestJson("/api/v1/feedback",{schema:feedbackResultSchema,expectedStatus:201,init:{method:"POST",headers:{"Content-Type":"application/json","Idempotency-Key":key},body:JSON.stringify(body)}});
}
