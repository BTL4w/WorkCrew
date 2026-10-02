import { render,screen } from "@testing-library/react";
import { QueryClient,QueryClientProvider } from "@tanstack/react-query";
import { NextIntlClientProvider } from "next-intl";
import { vi,expect,it } from "vitest";
import messages from "@/shared/i18n/messages/en.json";
import { DailyUpdateBlock } from "./daily-update-block";

vi.mock("@/features/auth/auth-provider",()=>({useAuth:()=>({actor:{membership:{id:"member",organization_id:"org"}}})}));
vi.mock("@/features/daily-updates/reporting-api",()=>({getDailyDraft:vi.fn(async()=>({id:"draft",version:1,confirmed_update_id:null,items:[{task_id:"task",expected_task_version:2,reported_percent:"50",done_text:"Completed survey",evidence_refs:[]}]}))}));
vi.mock("@/features/daily-updates/daily-update-form",()=>({DailyUpdateForm:({initialDraft}:{initialDraft:unknown})=><div>{initialDraft?"Owner review form":"Empty form"}</div>}));

it("loads the durable draft for owner review without automatically confirming",async()=>{
 const client=new QueryClient({defaultOptions:{queries:{retry:false}}});
 render(<QueryClientProvider client={client}><NextIntlClientProvider locale="en" messages={messages}><DailyUpdateBlock block={{kind:"daily_update",draft_id:"draft",draft_version:1,task_id:"task",task_version:2,assessment_id:null,needs_owner_confirmation:true}}/></NextIntlClientProvider></QueryClientProvider>);
 expect(await screen.findByText("Owner review form")).toBeInTheDocument();
 expect(screen.queryByText("Empty form")).not.toBeInTheDocument();
});
