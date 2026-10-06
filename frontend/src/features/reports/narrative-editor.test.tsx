import {render, screen, fireEvent, waitFor} from "@testing-library/react";
import {expect, it, vi} from "vitest";
import {AppLocaleProvider} from "@/shared/i18n/locale-provider";
import {NarrativeEditor} from "./narrative-editor";
import type {ReportResult} from "./contracts";
const id="11111111-1111-4111-8111-111111111111";
const data={report:{id,version:2},snapshot:{snapshot_hash:"a".repeat(64)},selected_version:{id,origin:"AI_PROPOSED",narrative:{locale:"en",snapshot_id:id,snapshot_hash:"a".repeat(64),blocks:[{id:"advice",kind:"RECOMMENDATION",section:"recommendations",text:"Review captured work.",source_refs:[],assumptions:["Review is available"]}]}},verification_state:"VERIFIED",review_state:"PENDING",generation_state:"AWAITING_REVIEW"} as unknown as ReportResult;
it.each(["en","vi"] as const)("offers edit, reject and exact review in %s", locale=>{
 render(<AppLocaleProvider initialLocale={locale}><NarrativeEditor data={data} onUpdated={vi.fn()} onStale={vi.fn()}/></AppLocaleProvider>);
 expect(screen.getByRole("button",{name:locale==="en"?"Review and publish":"Duyệt và xuất bản"})).toBeEnabled();
 expect(screen.getByRole("button",{name:locale==="en"?"Edit narrative":"Sửa nhận xét"})).toBeEnabled();
});
it("blocks publication while an edited version awaits verification",()=>{
 render(<AppLocaleProvider initialLocale="en"><NarrativeEditor data={{...data,verification_state:"PENDING"}} onUpdated={vi.fn()} onStale={vi.fn()}/></AppLocaleProvider>);
 expect(screen.getByRole("button",{name:"Review and publish"})).toBeDisabled();
});
it("saves immutable edited prose with the exact parent, hash and resource version",async()=>{
 const edit=vi.fn().mockResolvedValue({...data,verification_state:"PENDING"});const updated=vi.fn();
 render(<AppLocaleProvider initialLocale="en"><NarrativeEditor data={data} edit={edit} onUpdated={updated} onStale={vi.fn()}/></AppLocaleProvider>);
 fireEvent.click(screen.getByRole("button",{name:"Edit narrative"}));
 fireEvent.change(screen.getByLabelText("Narrative · advice"),{target:{value:"Review before deciding."}});
 fireEvent.click(screen.getByRole("button",{name:"Save and verify"}));
 await waitFor(()=>expect(updated).toHaveBeenCalledOnce());
 expect(edit.mock.calls[0][1].narrative.blocks[0].text).toBe("Review before deciding.");
 expect(edit.mock.calls[0][1].parent_version_id).toBe(id);
 expect(edit.mock.calls[0][2]).toBe(2);
});
it("retains an uncertain publish key and blocks stale actions",async()=>{
 const publish=vi.fn().mockRejectedValueOnce(new TypeError("offline")).mockRejectedValueOnce({status:412});const stale=vi.fn();
 render(<AppLocaleProvider initialLocale="en"><NarrativeEditor data={data} publish={publish} onUpdated={vi.fn()} onStale={stale}/></AppLocaleProvider>);
 fireEvent.click(screen.getByRole("button",{name:"Review and publish"}));await screen.findByRole("alert");
 fireEvent.click(screen.getByRole("button",{name:"Review and publish"}));await waitFor(()=>expect(stale).toHaveBeenCalledOnce());
 expect(publish.mock.calls[0][3]).toBe(publish.mock.calls[1][3]);
 expect(screen.getByRole("button",{name:"Review and publish"})).toBeDisabled();
});
