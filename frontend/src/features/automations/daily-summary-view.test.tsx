import { render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { expect, it } from "vitest";
import messages from "@/shared/i18n/messages/en.json";
import { SummaryCard } from "./daily-summary-view";
import type { SummarySnapshot } from "./contracts";

it("shows partial coverage, missing reporters, immutable source time and original evidence links", () => {
  const snapshot: SummarySnapshot = {
    id:"summary", schedule_id:"schedule", project_id:"project", project_name:"Supplier preparation",
    window:{id:"window",schedule_id:"schedule",applied_version:1,local_date:"2026-10-04",timezone:"UTC",
      starts_at:"2026-10-04T00:00:00Z",cutoff_at:"2026-10-04T17:00:00Z",ends_at:"2026-10-05T00:00:00Z",
      expected_reporters:["member"],reported_members:[],coverage_state:"PARTIAL",full_coverage:false,
      enabled:true,roster_observed_at:"2026-10-04T00:00:00Z",scope_changed:true},
    reason:"CUTOFF",snapshot_at:"2026-10-04T17:00:00Z",scope:"PROJECT",expected_count:1,reported_count:0,
    missing_reporters:["member"],reporters:[{membership_id:"member",name:"Lan"}],
    tasks:[{id:"task",title:"Book venue",task_version:1,assignee_id:null,status:"TO_DO",observation_id:null,progress_version:0,reported_percent:null,remaining_hours:null,observed_at:null}],unknown_inputs:["RISK:task"],sources:[{id:"source",task_id:"task",version:1,kind:"EVIDENCE",
      text:null,state:null,created_at:null,evidence_id:"evidence",evidence_version:1,
      href:"/api/v1/evidence/evidence/versions/1/content"}, {id:"risk-reference",task_id:"task",version:1,kind:"RISK",text:null,state:"READY",created_at:"2026-10-04T16:30:00Z",evidence_id:null,evidence_version:null,href:null}],
  };
  render(<NextIntlClientProvider locale="en" messages={messages}><SummaryCard snapshot={snapshot}/></NextIntlClientProvider>);
  expect(screen.getByText("0/1 reporters" )).toBeInTheDocument();
  expect(screen.getByText("Reference: risk-reference")).toBeInTheDocument();
  expect(screen.getByText("Task: Book venue")).toBeInTheDocument();
  expect(screen.getByText("Assignments or reporting scope changed after this window opened.")).toBeInTheDocument();
  expect(screen.getByText("Lan")).toBeInTheDocument();
  expect(screen.getByRole("link",{name:"Original evidence · v1"})).toHaveAttribute("href",snapshot.sources[0].href);
  expect(screen.getByText("Some inputs are unavailable or outside this snapshot.")).toBeInTheDocument();
});
