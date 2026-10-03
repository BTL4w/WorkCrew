import { fireEvent,render,screen,waitFor,within } from "@testing-library/react";
import { QueryClient,QueryClientProvider } from "@tanstack/react-query";
import { afterEach,expect,it,vi } from "vitest";
import { AppLocaleProvider } from "@/shared/i18n/locale-provider";
import { BlockerPanel } from "./blocker-panel";

const task="11111111-1111-4111-8111-111111111111";
afterEach(()=>vi.unstubAllGlobals());
it("adds a blocker to a draft without a business write",async()=>{
 const fetch=vi.fn(async(path:string, options?:RequestInit)=>{void path;void options;return new Response(JSON.stringify([]),{status:200,headers:{"Content-Type":"application/json"}});});
 vi.stubGlobal("fetch",fetch);
 const changed=vi.fn();
 render(<QueryClientProvider client={new QueryClient()}><AppLocaleProvider initialLocale="en"><BlockerPanel taskId={task} taskVersion={1} organizationId="org" actorMembershipId="member" commands={[]} onCommands={changed}/></AppLocaleProvider></QueryClientProvider>);
 fireEvent.change(screen.getByLabelText("Blocker description"),{target:{value:"Awaiting materials"}});
 fireEvent.click(screen.getByRole("button",{name:"Add to report"}));
 await waitFor(()=>expect(changed).toHaveBeenCalled());
 expect(changed.mock.calls[0][0][0]).toMatchObject({action:"CREATE",text:"Awaiting materials",task_id:task});
 expect(fetch.mock.calls.every(call=>call[1]?.method !== "POST")).toBe(true);
});

it("shows historical text, severity and original evidence removed by an edit",async()=>{
 const id="22222222-2222-4222-8222-222222222222";
 const evidence="33333333-3333-4333-8333-333333333333";
 const member="44444444-4444-4444-8444-444444444444";
 const current={id,task_id:task,created_by_membership_id:member,version:2,severity:"LOW",text:"Materials received",status:"OPEN",archived:false,evidence_refs:[],created_at:"2026-10-03T00:00:00Z",updated_at:"2026-10-03T01:00:00Z"};
 const snapshot={...current,version:1,severity:"HIGH",text:"Awaiting supplier delivery",evidence_refs:[{evidence_id:evidence,version:3}]};
 const history=[{id:"55555555-5555-4555-8555-555555555555",blocker_id:id,version:1,actor_membership_id:member,action:"CREATE",from_status:null,to_status:"OPEN",at:snapshot.created_at,snapshot}];
 vi.stubGlobal("fetch",vi.fn(async(path:string)=>new Response(JSON.stringify(path.endsWith("/history")?history:[current]),{status:200,headers:{"Content-Type":"application/json"}})));
 render(<QueryClientProvider client={new QueryClient()}><AppLocaleProvider initialLocale="en"><BlockerPanel taskId={task} taskVersion={1} organizationId="org" actorMembershipId={member}/></AppLocaleProvider></QueryClientProvider>);
 fireEvent.click(await screen.findByRole("button",{name:"Blocker history"}));
 const entry=within(await screen.findByRole("listitem"));
 expect(entry.getByText("Awaiting supplier delivery")).toBeInTheDocument();
 expect(entry.getByText("High")).toBeInTheDocument();
 expect(entry.getByRole("link",{name:"Original evidence · v3"})).toHaveAttribute("href",`/api/v1/evidence/${evidence}/versions/3/content`);
});
