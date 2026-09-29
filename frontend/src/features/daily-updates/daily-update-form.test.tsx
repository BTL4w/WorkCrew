import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, expect, it, vi } from "vitest";
import { AppLocaleProvider } from "@/shared/i18n/locale-provider";
import { DailyUpdateForm } from "./daily-update-form";

const taskId="11111111-1111-4111-8111-111111111111";
const context={task_id:taskId,task_version:1,progress_version:0,reported_percent:null,remaining_hours:null,reporting_timezone:"UTC",reporting_date:"2026-09-29",project_week_state:"NO_PROJECT_WEEK",evidence_refs:[]};
afterEach(()=>vi.unstubAllGlobals());
it("prepares a manual draft and confirms without changing Task status",async()=>{
 const fetchMock=vi.fn().mockImplementation(async(path:string,options?:RequestInit)=>{
  let body:unknown=context;
  if(path.includes("task_id=")) body=[];
  if(path.endsWith("drafts")&&options?.method==="POST")body={id:taskId,version:1,content_hash:"a".repeat(64),items:JSON.parse(String(options.body)).items,assessment_state:"UNAVAILABLE",reporting_timezone:"UTC",confirmed_update_id:null};
  if(path.endsWith("/daily-updates")&&options?.method==="POST")body={id:taskId,draft_id:taskId,assessment_state:"UNAVAILABLE",observations:[]};
  return new Response(JSON.stringify(body),{status:options?.method==="POST"?201:200,headers:{"Content-Type":"application/json"}});
 });
 vi.stubGlobal("fetch",fetchMock);
 render(<QueryClientProvider client={new QueryClient({defaultOptions:{queries:{retry:false}}})}><AppLocaleProvider initialLocale="en"><DailyUpdateForm taskId={taskId} organizationId="org" actorMembershipId="member"/></AppLocaleProvider></QueryClientProvider>);
 await screen.findByLabelText("Reported progress (%)");
 expect(screen.getByText(/No Project Week/)).toBeVisible();
 fireEvent.change(screen.getByLabelText("Reported progress (%)"),{target:{value:"99"}});
 fireEvent.change(screen.getByLabelText("Work completed"),{target:{value:"Prepared"}});
 fireEvent.click(screen.getByRole("button",{name:"Review report"}));
 await screen.findByText(/Evidence assessment is unavailable/);
 fireEvent.click(screen.getByRole("button",{name:"Confirm report"}));
 await waitFor(()=>expect(screen.getByRole("status")).toHaveTextContent("Report confirmed"));
 expect(fetchMock.mock.calls.some(([p])=>String(p).endsWith("/status"))).toBe(false);
 const confirmation=fetchMock.mock.calls.find(([p,o])=>String(p).endsWith("/daily-updates")&&o?.method==="POST");
 expect(confirmation?.[1].headers["Idempotency-Key"]).toBeTruthy();
});

it("retries an uncertain confirmation with the same draft and idempotency key",async()=>{
 const submissions:Array<{body:string;key:string}>=[];
 const fetchMock=vi.fn().mockImplementation(async(path:string,options?:RequestInit)=>{
  let payload:unknown=context;
  if(path.includes("task_id="))payload=[];
  if(path.endsWith("drafts")&&options?.method==="POST")payload={id:taskId,version:1,content_hash:"a".repeat(64),items:JSON.parse(String(options.body)).items,assessment_state:"UNAVAILABLE",reporting_timezone:"UTC",confirmed_update_id:null};
  if(path.endsWith("/daily-updates")&&options?.method==="POST"){
   submissions.push({body:String(options.body),key:new Headers(options.headers).get("Idempotency-Key")!});
   if(submissions.length===1)throw new TypeError("connection interrupted after confirmation");
   payload={id:taskId,draft_id:taskId,assessment_state:"UNAVAILABLE",observations:[]};
  }
  return new Response(JSON.stringify(payload),{status:options?.method==="POST"?201:200,headers:{"Content-Type":"application/json"}});
 });
 vi.stubGlobal("fetch",fetchMock);
 render(<QueryClientProvider client={new QueryClient({defaultOptions:{queries:{retry:false}}})}><AppLocaleProvider initialLocale="en"><DailyUpdateForm taskId={taskId} organizationId="org" actorMembershipId="member"/></AppLocaleProvider></QueryClientProvider>);
 await screen.findByLabelText("Reported progress (%)");
 fireEvent.change(screen.getByLabelText("Reported progress (%)"),{target:{value:"99"}});
 fireEvent.change(screen.getByLabelText("Work completed"),{target:{value:"Prepared"}});
 fireEvent.click(screen.getByRole("button",{name:"Review report"}));
 fireEvent.click(await screen.findByRole("button",{name:"Confirm report"}));
 await screen.findByText("The result is uncertain. Confirm again to retry the same submission.");
 expect(screen.queryByRole("button",{name:"Edit report"})).not.toBeInTheDocument();
 fireEvent.click(screen.getByRole("button",{name:"Confirm report"}));
 await waitFor(()=>expect(screen.getByRole("status")).toHaveTextContent("Report confirmed"));
 expect(submissions).toHaveLength(2);
 expect(submissions[0]).toEqual(submissions[1]);
});
