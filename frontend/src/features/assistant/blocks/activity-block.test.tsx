import { render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { expect, it } from "vitest";
import en from "@/shared/i18n/messages/en.json";
import { ActivityBlock } from "./activity-block";

it("shows the latest known retrieval stage without exposing tool labels", () => {
  const first = {kind:"activity" as const,label_key:"assistant.retrieval.searching",status:"RUNNING" as const,agent_id:"retrieve"};
  const last = {...first,label_key:"assistant.retrieval.reading",status:"COMPLETED" as const};
  render(<NextIntlClientProvider locale="en" messages={en}><ActivityBlock block={first} groupedBlocks={[first,{...first,status:"COMPLETED"},last]}/></NextIntlClientProvider>);
  expect(screen.getByText("Selected sources read")).toBeVisible();
});
