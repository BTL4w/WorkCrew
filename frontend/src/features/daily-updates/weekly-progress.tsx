"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useLocale, useTranslations } from "next-intl";
import { z } from "zod";
import { listProjectWeeks } from "@/features/planning/api";
import { requestJson } from "@/shared/api/client";

const decimal = z.string().regex(/^\d+(\.\d+)?$/).nullable();
const baselineSchema = z.object({
  id: z.uuid(), project_week_id: z.uuid(), captured_at: z.string(), kind: z.string(),
  start_date: z.string(), end_date: z.string(), week_version: z.number().int(), sealed_at: z.string().nullable(),
  task_entries: z.array(z.object({ task_id: z.uuid(), task_version: z.number().int(), title: z.string(), effort_hours: decimal, due_date: z.string().nullable(), predecessor_ids: z.array(z.uuid()) })),
});
const metricsSchema = z.object({
  baseline: baselineSchema, evaluated_at: z.string(), calendar_version: z.string(),
  reported_percent: decimal, planned_percent: decimal, known_effort_fraction: decimal,
  known_effort_hours: z.string(), total_effort_hours: z.string(), task_coverage_fraction: decimal,
  unknown_progress_ids: z.array(z.uuid()), missing_estimate_ids: z.array(z.uuid()), stale_task_ids: z.array(z.uuid()),
  remaining_hours: decimal, unknown_remaining_ids: z.array(z.uuid()),
  task_actuals: z.array(z.object({ task_id: z.uuid(), progress_version: z.number().int(), reported_percent: decimal, remaining_hours: decimal, observation_id: z.uuid().nullable(), reporting_at: z.string().nullable(), status: z.string(), stale: z.boolean() })),
});
export const weeklyProgressSchema = z.object({ original: metricsSchema, current_plan: metricsSchema, added_task_ids: z.array(z.uuid()), removed_task_ids: z.array(z.uuid()), sealed: z.boolean() });
export type WeeklyProgressData = z.infer<typeof weeklyProgressSchema>;

export function WeeklyProgressView({ data }: { data: WeeklyProgressData }) {
  const t = useTranslations("weeklyProgress");
  const locale = useLocale();
  const number = (value: string | null, multiplier = 1) => value === null ? t("unknown") : new Intl.NumberFormat(locale, { maximumFractionDigits: 2 }).format(Number(value) * multiplier);
  return <section className="mt-8" aria-label={t("title")}>
    <h3 className="text-xl font-semibold">{t("title")}</h3>
    <p className="mt-2 text-sm text-slate-600">{t("weightExplanation")}</p>
    {data.sealed ? <p className="mt-2 font-semibold">{t("sealed")}</p> : null}
    <p className="mt-2 text-sm">{t("added", { count: data.added_task_ids.length })} · {t("removed", { count: data.removed_task_ids.length })}</p>
    <div className="mt-4 grid gap-4 md:grid-cols-2">{([ ["original", data.original], ["current", data.current_plan] ] as const).map(([label, metrics]) => <article key={label} className="resource-card">
      <h4 className="font-semibold">{t(label)}</h4>
      <p className="mt-2 text-xs text-slate-600">{metrics.baseline.kind === "ENTRY" ? t("entry") : t("captured")} · {new Intl.DateTimeFormat(locale, { dateStyle: "short", timeStyle: "short", timeZone: "UTC" }).format(new Date(metrics.baseline.captured_at))} UTC</p>
      <p className="mt-3">{t("reported", { value: metrics.reported_percent === null ? t("unknown") : `${number(metrics.reported_percent)}%` })}</p>
      <p>{t("coverage", { value: metrics.known_effort_fraction === null ? t("unknown") : `${number(metrics.known_effort_fraction, 100)}%` })}</p>
      <p>{t("denominator", { known: number(metrics.known_effort_hours), total: number(metrics.total_effort_hours) })}</p>
      <p>{t("planned", { value: metrics.planned_percent === null ? t("unknown") : `${number(metrics.planned_percent)}%` })}</p>
      <p>{t("missing", { count: metrics.missing_estimate_ids.length })} · {t("unknownReports", { count: metrics.unknown_progress_ids.length })}</p>
      <p>{t("stale", { count: metrics.stale_task_ids.length })}</p>
      <p>{t("remaining", { value: number(metrics.remaining_hours), count: metrics.unknown_remaining_ids.length })}</p>
      <p className="mt-2 text-xs text-slate-600">{t("calendar")} · {new Intl.DateTimeFormat(locale, { dateStyle: "short", timeStyle: "short", timeZone: "UTC" }).format(new Date(metrics.evaluated_at))} UTC</p>
      <ul className="mt-3 text-sm">{metrics.baseline.task_entries.map(entry => <li key={entry.task_id}>{entry.title} · {t("effort", { value: number(entry.effort_hours) })}</li>)}</ul>
    </article>)}</div>
  </section>;
}

export function WeeklyProgressPanel({ organizationId, actorMembershipId, projectId }: { organizationId: string; actorMembershipId: string; projectId: string }) {
  const t = useTranslations("weeklyProgress");
  const [selected, setSelected] = useState("");
  const scope = ["work", organizationId, actorMembershipId, "weekly-progress", projectId];
  const weeks = useQuery({ queryKey: [...scope, "weeks"], queryFn: () => listProjectWeeks(projectId) });
  const weekId = weeks.data?.some(week => week.id === selected) ? selected : weeks.data?.[0]?.id;
  const progress = useQuery({ queryKey: [...scope, weekId], enabled: Boolean(weekId), queryFn: () => requestJson(`/api/v1/projects/${projectId}/weeks/${weekId}/progress`, { schema: weeklyProgressSchema }) });
  return <div className="mt-8">
    <label className="text-sm font-medium">{t("week")}<select className="form-input mt-2" value={weekId ?? ""} onChange={event => setSelected(event.target.value)}>{weeks.data?.map(week => <option key={week.id} value={week.id}>{t("weekNumber", {number: week.week_number})}</option>)}</select></label>
    {weeks.error || progress.error ? <div role="alert" className="error-message mt-3"><p>{t("unavailable")}</p><button type="button" className="text-button" onClick={() => { void weeks.refetch(); void progress.refetch(); }}>{t("retry")}</button></div> : weeks.isPending || (weekId && progress.isPending) ? <p role="status">{t("loading")}</p> : !weekId ? <p>{t("empty")}</p> : progress.data ? <WeeklyProgressView data={progress.data} /> : null}
  </div>;
}
