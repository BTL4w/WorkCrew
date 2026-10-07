import { fireEvent, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { renderWithAppProviders } from "@/test/render";
import { EvaluationStatus } from "./evaluation-status";

const dataset = "11111111-1111-4111-8111-111111111111";
const run = "22222222-2222-4222-8222-222222222222";
const response = {id:run,dataset_version_id:dataset,dataset_version:1,dataset_hash:"a".repeat(64),dataset_policy_version:"report-eval-dataset.v1",provider:"mock",provider_policy_version:"report-eval-provider.v1",status:"FAILED",failure_kind:"GATE",safe_error_code:"EVALUATION_GATE_FAILED",result:{total:2,passed:2,failed:0,skipped:0,gate_passed:false,gate:{missing_coverage:["sources"]},hosted_quality:"NOT_RUN",limitations:["SYNTHETIC_SCAFFOLD"]}};
afterEach(()=>vi.unstubAllGlobals());
it("starts a known dataset and displays gate failure separately from measured case counts",async()=>{
  const requests: RequestInit[]=[];
  vi.stubGlobal("fetch",vi.fn(async (_:RequestInfo|URL, init?:RequestInit)=>{
    if(init?.method==="POST") requests.push(init);
    return new Response(JSON.stringify(response),{status:init?.method==="POST"?202:200,headers:{"Content-Type":"application/json"}});
  }));
  renderWithAppProviders(<EvaluationStatus organizationId="org" membershipId="admin" role="ADMIN"/>);
  fireEvent.change(screen.getByLabelText("ID phiên bản dataset"),{target:{value:dataset}});
  fireEvent.click(screen.getByRole("button",{name:"Chạy đánh giá"}));
  await screen.findByText("Gate chưa đạt");
  expect(screen.getByText("a".repeat(64))).toBeVisible();
  expect(screen.getByText("2 / 2")).toBeVisible();
  expect(JSON.parse(String(requests[0].body))).toEqual({dataset_version_id:dataset,provider:"mock"});
  expect(new Headers(requests[0].headers).get("Idempotency-Key")).toBeTruthy();
});
it("removes private status and makes no request after Admin role revocation",async()=>{
  const fetcher=vi.fn();vi.stubGlobal("fetch",fetcher);
  const view=renderWithAppProviders(<EvaluationStatus organizationId="org" membershipId="admin" role="ADMIN"/>);
  view.rerender(<EvaluationStatus organizationId="org" membershipId="admin" role="EMPLOYEE"/>);
  expect(screen.queryByLabelText("ID phiên bản dataset")).not.toBeInTheDocument();
  expect(fetcher.mock.calls.some(([url])=>String(url).includes("/evaluations/"))).toBe(false);
});

it("retries the same run ID after a temporary status read failure",async()=>{
 let reads=0;
 vi.stubGlobal("fetch",vi.fn(async(input:RequestInfo|URL)=>{
  if(String(input).includes(`/evaluations/runs/${run}`)){
   reads+=1;
   return new Response(JSON.stringify(reads===1?{}:response),{status:reads===1?503:200,headers:{"Content-Type":"application/json"}});
  }
  return new Response("{}",{headers:{"Content-Type":"application/json"}});
 }));
 renderWithAppProviders(<EvaluationStatus organizationId="org" membershipId="admin" role="ADMIN"/>);
 fireEvent.change(screen.getByLabelText("ID lần đánh giá"),{target:{value:run}});
 fireEvent.click(screen.getByRole("button",{name:"Mở kết quả"}));
 await screen.findByRole("alert");
 fireEvent.click(screen.getByRole("button",{name:"Mở kết quả"}));
 await screen.findByText("a".repeat(64));
 expect(reads).toBe(2);
});
