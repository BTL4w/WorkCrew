import {render,screen,fireEvent} from "@testing-library/react";
import {it,expect} from "vitest";
import {NextIntlClientProvider} from "next-intl";
import en from "@/shared/i18n/messages/en.json";
import viMessages from "@/shared/i18n/messages/vi.json";
import {ReviewOutcomes} from "./review-outcomes";
it.each(["en","vi"] as const)("shows unknown outcomes and separates quality from publication in %s",locale=>{
 render(<NextIntlClientProvider locale={locale} messages={{feedback:(locale==="en"?en:viMessages).feedback}} timeZone="UTC"><ReviewOutcomes feedback={[]} outcomes={[]} rates={null}/></NextIntlClientProvider>);
 expect(screen.getByText(locale==="en"?"Actual outcome: unknown":"Kết quả thực tế: chưa có dữ liệu")).toBeVisible();
});
it("counts edited quality separately and displays exact immutable lineage",()=>{
 const review={id:"feedback",report_id:"report",report_version_id:"corrected",original_version_id:"original",generation_id:"generation",actor_membership_id:"actor",decision_id:"approval",kind:"TERMINAL_QUALITY" as const,decision:"EDIT" as const,reason:null,provenance:{prompt_ref:"reporting.v1"},created_at:"2026-10-07T00:00:00Z"};
 render(<NextIntlClientProvider locale="en" messages={{feedback:en.feedback}} timeZone="UTC"><ReviewOutcomes feedback={[review]} outcomes={[]} rates={{reviewed_generation_count:4,accept_count:2,edit_count:1,reject_count:1,accept_percent:"50",edit_percent:"25",reject_percent:"25",pending_generation_count:2,failed_generation_count:3,manual_report_count:4}}/></NextIntlClientProvider>);
 expect(screen.getByText(/ACCEPT: 50%/)).toBeVisible();
 expect(screen.getByText(/original → corrected/)).toBeVisible();
 expect(screen.getByText(/Edited output; publication approved/)).toBeVisible();
 fireEvent.click(screen.getByText("Generation provenance"));
 expect(screen.getByText(/reporting.v1/)).toBeVisible();
});
it.each(["en","vi"] as const)("shows observed 100%% while completion remains unknown in %s",locale=>{
 const feedback={id:"review",report_id:"report",report_version_id:"version",original_version_id:"version",generation_id:"generation",actor_membership_id:"actor",decision_id:"decision",kind:"TERMINAL_QUALITY" as const,decision:"ACCEPT" as const,reason:null,provenance:{},created_at:"2026-10-07T00:00:00Z"};
 const outcome={id:"outcome",feedback_id:"review",actor_membership_id:"actor",schema_version:"feedback-outcome.v1" as const,source:{source_type:"TASK_ACTUALS" as const,source_id:"task",source_version:1},state:"AVAILABLE" as const,facts:{reported_percent:"100",completion_state:"UNKNOWN"},occurred_at:"2026-10-07T00:00:00Z",recorded_at:"2026-10-07T01:00:00Z"};
 render(<NextIntlClientProvider locale={locale} messages={{feedback:(locale==="en"?en:viMessages).feedback}} timeZone="UTC"><ReviewOutcomes feedback={[feedback]} outcomes={[outcome]} rates={null}/></NextIntlClientProvider>);
 expect(screen.getByText(locale==="en"?"Reported progress (%): 100":"Tiến độ tự báo cáo (%): 100")).toBeVisible();
 expect(screen.getByText(locale==="en"?"Completion acceptance: Unknown":"Nghiệm thu hoàn thành: Chưa có dữ liệu")).toBeVisible();
});
