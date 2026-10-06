"use client";

import { useQuery } from "@tanstack/react-query";
import { useLocale, useTranslations } from "next-intl";
import { getDeliveries } from "./api";
import type { SummarySnapshot, SummaryReportLink } from "./contracts";

export function DailySummaryView({organizationId, membershipId, projectId}: {
  organizationId:string; membershipId:string; projectId?:string;
}) {
  const t = useTranslations("summaries");
  const query = useQuery({queryKey:["summaries",organizationId,membershipId],queryFn:getDeliveries,refetchInterval:15000});
  if(query.isPending) return <p role="status">{t("loading")}</p>;
  if(query.isError) return <p role="alert">{t("loadError")}</p>;
  const cards = query.data.filter(delivery=>!projectId || delivery.snapshot.project_id===projectId);
  return <section className="mt-6 space-y-4" aria-label={t("title")}>
    <h3 className="text-lg font-semibold">{t("title")}</h3>
    {cards.length===0 ? <p className="text-sm text-slate-600">{t("empty")}</p> : cards.map(delivery=><SummaryCard key={delivery.id} snapshot={delivery.snapshot} reportLink={delivery.report_link}/>)}
  </section>;
}

export function SummaryCard({snapshot,reportLink}: {snapshot:SummarySnapshot;reportLink?:SummaryReportLink|null}) {
  const t=useTranslations("summaries"), locale=useLocale();
  const time=(value:string)=>new Intl.DateTimeFormat(locale,{dateStyle:"medium",timeStyle:"short",timeZone:snapshot.window.timezone}).format(new Date(value));
  return <article className="rounded-2xl border border-slate-200 bg-white p-6 shadow-sm">
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div><p className="text-xs font-semibold uppercase tracking-wide text-slate-500">{t(snapshot.scope==="OWN_WORK"?"ownWork":"projectScope")}</p>
        <h4 className="mt-2 text-xl font-semibold">{snapshot.project_name}</h4>
        <p className="mt-1 text-sm text-slate-500">{snapshot.window.local_date} · {t("version",{version:snapshot.window.applied_version})}</p></div>
      <p className="rounded-full bg-slate-100 px-3 py-2 text-sm font-semibold">{t("coverage",{received:snapshot.reported_count,total:snapshot.expected_count})}</p>
    </div>
    <dl className="mt-4 grid gap-3 text-sm sm:grid-cols-2">
      <div><dt className="text-slate-500">{t("snapshotTime")}</dt><dd>{time(snapshot.snapshot_at)}</dd></div>
      <div><dt className="text-slate-500">{t("cutoff")}</dt><dd>{time(snapshot.window.cutoff_at)}</dd></div>
    </dl>
    {snapshot.window.coverage_state==="NO_REPORTERS"?<p className="mt-4 rounded-lg bg-slate-50 p-3 text-sm">{t("noReporters")}</p>:null}
    {snapshot.window.scope_changed?<p className="mt-4 rounded-lg bg-amber-50 p-3 text-sm">{t("scopeChanged")}</p>:null}
    {snapshot.missing_reporters.length>0?<div className="mt-4 rounded-xl bg-amber-50 p-4 text-sm"><p className="font-semibold">{t("missing")}</p>
      <ul className="mt-2 space-y-1">{snapshot.missing_reporters.map(id=><li key={id}>{snapshot.reporters.find(r=>r.membership_id===id)?.name ?? t("unavailableReporter")}</li>)}</ul></div>:null}
    <div className="mt-5 divide-y divide-slate-100">{snapshot.tasks.map(task=><div key={task.id} className="py-3">
      <p className="font-medium">{task.title}</p><p className="mt-1 text-sm text-slate-600">{t("actual",{percent:task.reported_percent??t("unknown"),remaining:task.remaining_hours??t("unknown")})}</p>
      <p className="mt-1 text-xs text-slate-500">{t("sourceVersion",{task:task.task_version,progress:task.progress_version})}{task.observed_at?` · ${time(task.observed_at)}`:""}</p></div>)}</div>
    {snapshot.sources.length>0?<details className="mt-4 rounded-xl border border-slate-200 p-4"><summary className="cursor-pointer font-medium">{t("sources")}</summary>
      <ul className="mt-3 space-y-3 text-sm">{snapshot.sources.map(source=><li key={`${source.kind}:${source.id}:${source.task_id}`}>
        {source.kind==="EVIDENCE"&&source.href&&/^\/api\/v1\/evidence\//.test(source.href)?<a className="text-blue-700 underline" href={source.href}>{t("original",{version:source.version})}</a>:<p>{t(`kind${source.kind}`)} · {source.text || source.state || t("reference")} · v{source.version}</p>}
        {source.kind!=="EVIDENCE"?<div className="mt-1 text-xs text-slate-500"><p>{t("sourceTask",{task:snapshot.tasks.find(task=>task.id===source.task_id)?.title??source.task_id})}</p><p className="break-all">{t("sourceReference",{id:source.id})}</p></div>:null}
        {source.created_at?<p className="text-xs text-slate-500">{time(source.created_at)}</p>:null}</li>)}</ul></details>:null}
    {snapshot.scope === "PROJECT" && reportLink ? <aside className="mt-4 rounded-xl bg-blue-50 p-4 text-sm"><p>{t(reportLink.publication_id ? "narrativePublished" : `narrative${reportLink.generation_state}`)}</p><a className="secondary-button mt-3" href={`/?project=${snapshot.project_id}&report=${reportLink.report_id}${reportLink.publication_id ? `&version=${reportLink.report_version_id}` : ""}`}>{t("openReport")}</a><p className="mt-2">{t("narrativeSeparate")}</p></aside> : null}
    {snapshot.unknown_inputs.length>0?<p className="mt-4 text-sm text-amber-800">{t("unknownInputs")}</p>:null}
  </article>;
}
