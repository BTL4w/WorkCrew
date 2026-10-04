import { render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { expect, it } from "vitest";
import messages from "@/shared/i18n/messages/en.json";
import { RiskBlock } from "./risk-block";
import type { AssistantBlock } from "../contracts";

const block = {kind:"risk",task_id:"task",fingerprint:"a".repeat(64),content:{
 task_id:"task",task_version:2,risk_assessment_id:"risk",version:1,fingerprint:"a".repeat(64),
 state:"READY",score:"92",band:"HIGH",scope:"MANAGER",rationale:"Stored rationale",
 permitted_sources:[{id:"task:1:v2",kind:"TASK",values:{title:"Survey",status:"IN_PROGRESS"}}],
 observations:[{id:"observation:0",text:"Near deadline",source_ids:["task:1:v2"]}],
 explanation:{observation_explanations:[{text:"Deadline needs attention",observation_ids:["observation:0"],source_ids:["task:1:v2"],assertions:[]}],limitations:[],recommendations:["Additional advisory action"],replan_requested:false},
 limitations:["No original evidence supplied"],recommendations:["Review blocker"],affected_week_ids:[],fallback:false,
}} satisfies Extract<AssistantBlock,{kind:"risk"}>;

it("shows persisted score, explanations, citations, limitations and advisory actions",()=>{
 render(<NextIntlClientProvider locale="en" messages={messages}><RiskBlock block={block}/></NextIntlClientProvider>);
 expect(screen.getByText("92")).toBeInTheDocument();
 expect(screen.getByText("Deadline needs attention")).toBeInTheDocument();
 expect(screen.getAllByText("task:1:v2").length).toBeGreaterThan(0);
 expect(screen.getByText("No original evidence supplied")).toBeInTheDocument();
 expect(screen.getByText("Review blocker")).toBeInTheDocument();
 expect(screen.getByText("Additional advisory action")).toBeInTheDocument();
 expect(screen.queryByRole("button",{name:/approve/i})).not.toBeInTheDocument();
});
it("shows own-work scope and unknown score without fabricating zero",()=>{
 render(<NextIntlClientProvider locale="en" messages={messages}><RiskBlock block={{...block,content:{...block.content,
 score:null,band:null,state:"UNAVAILABLE",scope:"OWN_WORK",risk_assessment_id:null,fallback:true}}}/></NextIntlClientProvider>);
 expect(screen.getByText("Your work only")).toBeInTheDocument();
 expect(screen.queryByText("92")).not.toBeInTheDocument();
 expect(screen.getByText("Unknown")).toBeInTheDocument();
});
