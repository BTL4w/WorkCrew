"use client";

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslations } from "next-intl";
import { useAuth } from "@/features/auth/auth-provider";
import { getDraft, getSchedule, pauseSchedule } from "@/features/automations/api";
import type { ScheduleDraft, ScheduleView } from "@/features/automations/contracts";
import { DailySummarySettings } from "@/features/automations/daily-summary-settings";
import { isDefinitiveMutationRejection } from "@/shared/api/client";
import type { AssistantBlock } from "../contracts";

type Block=Extract<AssistantBlock,{kind:"daily_summary"}>;
export function DailySummaryBlock({block}:{block:Block}) {
  const t=useTranslations("summaries"), {actor}=useAuth();
  const [closed,setClosed]=useState(false),[busy,setBusy]=useState(false),[error,setError]=useState(false);
  const [key,setKey]=useState(()=>crypto.randomUUID());
  const query=useQuery<ScheduleDraft | ScheduleView>({queryKey:["schedule-card",actor?.membership.organization_id,actor?.membership.id,block.project_id,block.draft_id],
    queryFn:()=>block.draft_id?getDraft(block.draft_id):getSchedule(block.project_id),enabled:!!actor&&!closed});
  if(!actor) return null;
  if(closed) return <p>{t("closed")}</p>;
  if(query.isPending) return <p role="status">{t("loading")}</p>;
  if(query.isError || !query.data) return <p role="alert">{t("loadError")}</p>;
  if(block.draft_id && "command" in query.data) return <div>
    <DailySummarySettings projectId={block.project_id} organizationId={actor.membership.organization_id}
      membershipId={actor.membership.id} initialDraft={query.data} onApplied={()=>setClosed(true)}/>
    <button className="secondary-button mt-3" onClick={()=>setClosed(true)}>{t("reject")}</button>
  </div>;
  const saved="schedule" in query.data?query.data.schedule:null;
  async function confirm() {
    if(!saved) return;
    setBusy(true);setError(false);
    try {await pauseSchedule(saved.id,block.expected_version,block.operation==="PAUSE",key);setClosed(true);}
    catch(caught){setError(true);if(isDefinitiveMutationRejection(caught))setKey(crypto.randomUUID());}
    finally{setBusy(false);}
  }
  return <section className="rounded-2xl border border-slate-200 bg-white p-5">
    <h3 className="font-semibold">{t(block.operation==="PAUSE"?"confirmPause":"confirmResume")}</h3>
    <p className="mt-2 text-sm">{saved?.timezone} · {saved?.cutoff} · {t("version",{version:block.expected_version})}</p>
    {error?<p role="alert">{t("loadError")}</p>:null}
    <div className="mt-4 flex gap-3"><button className="primary-button" disabled={busy||!saved||saved.version!==block.expected_version} onClick={()=>void confirm()}>{t("confirm")}</button>
      <button className="secondary-button" disabled={busy} onClick={()=>setClosed(true)}>{t("reject")}</button></div>
  </section>;
}
