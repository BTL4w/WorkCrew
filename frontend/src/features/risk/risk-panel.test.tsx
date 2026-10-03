import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, expect, it, vi } from "vitest";
import { AppLocaleProvider } from "@/shared/i18n/locale-provider";
import { RiskPanel } from "./risk-panel";

const id = "11111111-1111-4111-8111-111111111111";
const ready = { id, task_id: id, task_version: 1, state: "READY", evaluated_at: "2026-10-03T00:00:00Z", band: "HIGH", limitation: "", policy_version: "risk.ai.v1", model_ref: "mock", prompt_version: "risk-assessment.v1", schema_version: "risk-judgment.v1", input_snapshot: { task_id: id, task_version: 1, facts: [{ id: "task:1", kind: "TASK", values: { title: "Prepare shipment", overdue: true } }], missing: ["CAPACITY"] }, judgment: { score: "83", rationale: "Delivery needs review", observations: [{ text: "Deadline passed", source_ids: ["task:1"] }], limitations: ["Capacity unavailable"], recommendations: ["Discuss delivery date"] } };
afterEach(() => vi.unstubAllGlobals());
function mount() { render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><AppLocaleProvider initialLocale="en"><RiskPanel taskId={id} organizationId="org" actorMembershipId="member" /></AppLocaleProvider></QueryClientProvider>); }
it("shows the exact AI score, cited facts, advice and separate human disposition", async () => {
 const fetch = vi.fn(async (path: string, init?: RequestInit) => new Response(JSON.stringify(path.endsWith("/reviews") ? init?.method === "POST" ? { id, risk_id: id, actor_membership_id: id, at: ready.evaluated_at, disposition: "ACCEPTED_EXPLANATION", reason: "Supplier confirmed" } : [] : ready), { status: 200, headers: { "Content-Type": "application/json" } }));
 vi.stubGlobal("fetch", fetch); mount();
 expect(await screen.findByText("83 / 100")).toBeInTheDocument();
 expect(screen.getByText("Delivery needs review")).toBeInTheDocument();
 expect(screen.getByText("Discuss delivery date")).toBeInTheDocument();
 fireEvent.click(screen.getByText("task:1"));
 expect(screen.getByText(/Prepare shipment/)).toBeInTheDocument();
 fireEvent.change(screen.getByLabelText("Review reason"), { target: { value: "Supplier confirmed" } });
 fireEvent.change(screen.getByLabelText("Manager disposition"), { target: { value: "ACCEPTED_EXPLANATION" } });
 fireEvent.click(screen.getByRole("button", { name: "Record review" }));
 await waitFor(() => expect(fetch.mock.calls.some(call => call[1]?.method === "POST")).toBe(true));
 expect(screen.getByText("83 / 100")).toBeInTheDocument();
});
it("does not display a numeric score for unavailable or stale results", async () => {
 vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ ...ready, state: "UNAVAILABLE", judgment: null, band: null, limitation: "MODEL_UNAVAILABLE" }), { status: 200, headers: { "Content-Type": "application/json" } })));
 mount(); expect(await screen.findByText("Assessment unavailable")).toBeInTheDocument();
 expect(screen.queryByText("83 / 100")).not.toBeInTheDocument();
});

it.each(["UNAVAILABLE", "READY"])("shows confirmed report warnings independently of %s model citations", async state => {
 const warning = { id: "warning:confirmed", kind: "WARNING", values: { assessment_id: id, update_id: id, acknowledged_at: ready.evaluated_at, warnings: [{ id, code: "LOW_SUPPORT" }] } };
 vi.stubGlobal("fetch", vi.fn(async (path: string) => new Response(JSON.stringify(path.endsWith("/reviews") ? [] : { ...ready, state, judgment: state === "READY" ? ready.judgment : null, band: state === "READY" ? ready.band : null, input_snapshot: { ...ready.input_snapshot, facts: [...ready.input_snapshot.facts, warning] } }), { status: 200, headers: { "Content-Type": "application/json" } })));
 mount();
 expect(await screen.findByText("Confirmed report warnings")).toBeVisible();
 expect(screen.getByText("Evidence support below 70/100")).toBeVisible();
 expect(screen.getByText("These warnings apply to the confirmed report, which may cover several tasks.")).toBeVisible();
});
