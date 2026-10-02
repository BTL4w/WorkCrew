import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { AppLocaleProvider } from "@/shared/i18n/locale-provider";
import { EvidenceAssessmentCard } from "./evidence-assessment-card";
import type { DraftAssessment } from "./reporting-contracts";
const id="11111111-1111-4111-8111-111111111111";
const assessment:DraftAssessment={id,draft_id:id,draft_version:1,state:"READY",result:{score:"65",warning_codes:["LOW_SUPPORT"],assessed_count:9,total_count:10,rule_version:"evidence-support.v1"},claims:[{id:"claim",text:"Delivered report",source_span:"items[0].done_text",category:"OUTPUT",checkability:true,task_id:id,evidence_refs:[{evidence_id:id,version:1}]}],findings:[{claim_id:"claim",finding:"PARTIAL",source_refs:[{evidence_id:id,version:1}],limitation:"Appendix not supported"}],coverage:{processed_count:1,total_count:2},warnings:[{id,code:"LOW_SUPPORT"}],limitation:""};
it("shows support meaning, source coverage and requires explicit warning acknowledgment",()=>{
 const ack=vi.fn();
 render(<AppLocaleProvider initialLocale="en"><EvidenceAssessmentCard assessment={assessment} acknowledged={false} onAcknowledge={ack}/></AppLocaleProvider>);
 expect(screen.getByText("65/100")).toBeVisible();
 expect(screen.getByText(/9 of 10/)).toBeVisible();
 expect(screen.getByText(/1 of 2/)).toBeVisible();
 expect(screen.getByText(/correspondence with evidence/)).toBeVisible();
 expect(screen.getByText("Appendix not supported")).toBeVisible();
 expect(screen.getByRole("link",{name:/Original/})).toHaveAttribute("href",`/api/v1/evidence/${id}/versions/1/content`);
 fireEvent.click(screen.getByRole("checkbox"));
 expect(ack).toHaveBeenCalledWith(true);
});
it("discloses unavailable assessment without presenting a score",()=>{
 render(<AppLocaleProvider initialLocale="vi"><EvidenceAssessmentCard assessment={{...assessment,state:"UNAVAILABLE",result:null,warnings:[],findings:[],claims:[]}} acknowledged={false} onAcknowledge={()=>{}}/></AppLocaleProvider>);
 expect(screen.getByText(/Chưa thể đánh giá minh chứng/)).toBeVisible();
 expect(screen.queryByText("65/100")).not.toBeInTheDocument();
 expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
});

it("explains unsupported original formats without presenting a score",()=>{
 const unavailable={...assessment,state:"UNAVAILABLE" as const,result:null,limitation:"ORIGINAL_FORMAT_UNSUPPORTED"};
 render(<AppLocaleProvider initialLocale="en"><EvidenceAssessmentCard assessment={unavailable} acknowledged={false} onAcknowledge={()=>{}}/></AppLocaleProvider>);
 expect(screen.getByText(/PDF.*DOCX/)).toBeVisible();
});
