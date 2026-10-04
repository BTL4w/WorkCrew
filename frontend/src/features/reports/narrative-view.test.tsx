import {render,screen,fireEvent,waitFor} from "@testing-library/react";
import {it,expect,vi} from "vitest";
import {AppLocaleProvider} from "@/shared/i18n/locale-provider";
import {NarrativeView} from "./narrative-view";
import type {ReportResult} from "./contracts";
const id="11111111-1111-4111-8111-111111111111";
const data={report:{id,version:2},snapshot:{snapshot_hash:"a".repeat(64)},selected_version:{id},generation_state:"AI_UNAVAILABLE"} as unknown as ReportResult;
it.each(["en","vi"] as const)("shows pending state without retrying automatically in %s",locale=>{
 const generate=vi.fn();
 render(<AppLocaleProvider initialLocale={locale}><NarrativeView data={{...data,generation_state:"RUNNING"}} onUpdated={vi.fn()} onStale={vi.fn()} generate={generate}/></AppLocaleProvider>);
 expect(screen.getByRole("status")).toHaveTextContent(locale==="en"?"AI draft is being verified":"Đang kiểm chứng bản nháp AI");
 expect(screen.queryByRole("button")).toBeNull();expect(generate).not.toHaveBeenCalled();
});
it("keeps the same intent after an uncertain response and submits only on click",async()=>{
 const generate=vi.fn().mockRejectedValueOnce(new TypeError("offline")).mockResolvedValueOnce({...data,generation_state:"QUEUED"});
 const updated=vi.fn();
 render(<AppLocaleProvider initialLocale="en"><NarrativeView data={data} onUpdated={updated} onStale={vi.fn()} generate={generate}/></AppLocaleProvider>);
 expect(generate).not.toHaveBeenCalled();
 fireEvent.click(screen.getByRole("button",{name:"Generate AI draft"}));
 await screen.findByRole("alert");
 fireEvent.click(screen.getByRole("button",{name:"Generate AI draft"}));
 await waitFor(()=>expect(updated).toHaveBeenCalledOnce());
 expect(generate.mock.calls[0][3]).toBe(generate.mock.calls[1][3]);
 expect(generate.mock.calls[0].slice(0,3)).toEqual([id,{base_version_id:id,snapshot_hash:data.snapshot.snapshot_hash},2]);
});
it("refreshes and blocks stale retry until the new version is reviewed",async()=>{
 const generate=vi.fn().mockRejectedValue({status:412});const stale=vi.fn();
 render(<AppLocaleProvider initialLocale="en"><NarrativeView data={data} onUpdated={vi.fn()} onStale={stale} generate={generate}/></AppLocaleProvider>);
 fireEvent.click(screen.getByRole("button",{name:"Generate AI draft"}));
 await screen.findByRole("alert");expect(stale).toHaveBeenCalledOnce();expect(screen.getByRole("button")).toBeDisabled();
});
it("renders only server facts and marks prose, assumptions and source versions",()=>{
 const selected={id,report_id:id,snapshot_id:id,origin:"AI_PROPOSED",locale:"en",created_at:"2026-10-05T00:00:00Z",narrative:{locale:"en",snapshot_id:id,snapshot_hash:data.snapshot.snapshot_hash,blocks:[{id:"done",section:"progress",kind:"FACT",template:"METRIC",bindings:[{metric_key:"tasks.status.done_count"}],source_bindings:[]},{id:"advice",section:"recommendations",kind:"RECOMMENDATION",text:"Review the task",source_refs:[{resource_type:"TASK",resource_id:id,version:3,fingerprint:null}],assumptions:["Owner is available"]}]},rendered_facts:{done:"Tasks in DONE: 1 COUNT = 1 (at capture)."}} as ReportResult["selected_version"];
 render(<AppLocaleProvider initialLocale="en"><NarrativeView data={{...data,generation_state:"AWAITING_REVIEW",selected_version:selected}} onUpdated={vi.fn()} onStale={vi.fn()}/></AppLocaleProvider>);
 expect(screen.getByText("Tasks in DONE: 1 COUNT = 1 (at capture).")).toBeVisible();expect(screen.getByText("Advisory recommendation")).toBeVisible();expect(screen.getByText("Assumption: Owner is available")).toBeVisible();expect(screen.getByText(/TASK ·/)).toHaveTextContent("3");
});
