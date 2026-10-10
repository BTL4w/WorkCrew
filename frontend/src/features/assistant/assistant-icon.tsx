import type { ReactNode } from "react";

export type AssistantIconName = "new" | "projects" | "tasks" | "people" | "evaluation" | "assign" | "collapse" | "expand" | "logout" | "send";

const paths: Record<AssistantIconName, ReactNode> = {
  new: <><path d="M12 5v14M5 12h14" /></>,
  projects: <><path d="M3 7a2 2 0 0 1 2-2h5l2 2h7a2 2 0 0 1 2 2v10H5a2 2 0 0 1-2-2Z" /><path d="M3 10h18" /></>,
  tasks: <><path d="m3 6 1.5 1.5L7 5M11 6h10m-18 6 1.5 1.5L7 11m4 1h10m-18 6 1.5 1.5L7 17m4 1h10" /></>,
  people: <><circle cx="9" cy="8" r="3" /><path d="M3 20v-2a6 6 0 0 1 12 0v2M16 5a3 3 0 0 1 0 6m2 3a5 5 0 0 1 3 4v2" /></>,
  evaluation: <><path d="M4 4v16h16M8 15v-4m5 4V7m5 8v-6" /></>,
  assign: <><circle cx="8" cy="7" r="3" /><path d="M2 20v-2a6 6 0 0 1 9-5m3-1h7m-3-3 3 3-3 3" /></>,
  collapse: <><rect x="3" y="4" width="18" height="16" rx="2" /><path d="M9 4v16m7-11-3 3 3 3" /></>,
  expand: <><rect x="3" y="4" width="18" height="16" rx="2" /><path d="M9 4v16m4-11 3 3-3 3" /></>,
  logout: <><path d="M9 4H4v16h5m5-12 4 4-4 4m-6-4h10" /></>,
  send: <path d="M12 19V5m-6 6 6-6 6 6" />,
};

export function AssistantIcon({ name }: { name: AssistantIconName }) {
  return <svg aria-hidden="true" focusable="false" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.5">{paths[name]}</svg>;
}
