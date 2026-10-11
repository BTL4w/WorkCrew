"use client";

import { useSyncExternalStore } from "react";

const locationEvent = "workcrew:navigation";

function subscribe(onChange: () => void) {
  window.addEventListener("popstate", onChange);
  window.addEventListener(locationEvent, onChange);
  return () => {
    window.removeEventListener("popstate", onChange);
    window.removeEventListener(locationEvent, onChange);
  };
}

export function useWorkspaceSearchParams() {
  const search = useSyncExternalStore(subscribe, () => window.location.search, () => "");
  return new URLSearchParams(search);
}

export function navigateWorkspace(view: string | null, conversationId: string | null = null, replace = false) {
  const url = new URL(window.location.href);
  for (const key of ["view", "conversation", "project", "report", "version"]) url.searchParams.delete(key);
  if (view) url.searchParams.set("view", view);
  if (conversationId) url.searchParams.set("conversation", conversationId);
  const destination = `${url.pathname}${url.search}${url.hash}`;
  if (destination === `${window.location.pathname}${window.location.search}${window.location.hash}`) return;
  if (replace) window.history.replaceState(window.history.state, "", destination);
  else window.history.pushState(null, "", destination);
  window.dispatchEvent(new Event(locationEvent));
}
