import { render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { expect, it } from "vitest";
import messages from "@/shared/i18n/messages/en.json";
import { ReportBlock } from "./report-block";

const id="00000000-0000-4000-8000-000000000001";
const card={kind:"report" as const,context_run_id:id,project_id:id,project_label:"Conference 2026",report_id:id,report_version_id:id,snapshot_id:id,snapshot_hash:"a".repeat(64),period_start:"2026-10-05",period_end:"2026-10-12",timezone:"Asia/Ho_Chi_Minh",report_kind:"WEEKLY" as const,captured_at:"2026-10-06T02:00:00Z",generation_state:"QUEUED",metrics:[{key:"tasks.status.total_count",value:"12",unit:"COUNT",state:"KNOWN" as const,time_basis:"AT_CAPTURE"}],sources:[{resource_type:"TASK",resource_id:id,version:1}],limitations:[],href:`/?project=${id}&report=${id}&version=${id}`,needs_manager_review:true as const};
it("shows exact report reference, explicit period, facts, sources and review link",()=>{
 render(<NextIntlClientProvider locale="en" messages={messages}><ReportBlock block={card} canManage/></NextIntlClientProvider>);
 expect(screen.getByText("Conference 2026")).toBeInTheDocument();
 expect(screen.getByText("12")).toBeInTheDocument();
 expect(screen.getByRole("link",{name:"Open report"})).toHaveAttribute("href",card.href);
 expect(screen.getByText(/2026-10-05/)).toBeInTheDocument();
 expect(screen.queryByRole("button",{name:/publish/i})).not.toBeInTheDocument();
});
it("hides report from Employee and shows unavailable analysis with unknown facts",()=>{
 const {unmount}=render(<NextIntlClientProvider locale="en" messages={messages}><ReportBlock block={card} canManage={false}/></NextIntlClientProvider>);
 expect(screen.queryByText("12")).not.toBeInTheDocument();unmount();
 const {report_id,report_version_id,generation_state,href,needs_manager_review,...common}=card;
 void report_id;void report_version_id;void generation_state;void href;void needs_manager_review;
 render(<NextIntlClientProvider locale="en" messages={messages}><ReportBlock block={{...common,kind:"project_status",context_run_id:id,analysis:[],analysis_state:"UNAVAILABLE",metrics:[{...card.metrics[0],value:null,state:"UNKNOWN"}]}} canManage/></NextIntlClientProvider>);
 expect(screen.getByText("Unknown")).toBeInTheDocument();
 expect(screen.getByText("AI analysis is unavailable. Verified metrics remain available.")).toBeInTheDocument();
 expect(screen.queryByRole("link",{name:"Open report"})).not.toBeInTheDocument();
});
