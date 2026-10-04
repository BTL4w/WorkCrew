import { useLocale, useTranslations } from "next-intl";
import type { Metric, ReportResult } from "./contracts";
import styles from "./reports.module.css";

const labels: Record<string, string> = {
  "tasks.status.total_count": "total", "tasks.status.to_do_count": "todo",
  "tasks.status.in_progress_count": "inProgress", "tasks.status.done_count": "done",
  "tasks.deadline.unknown_count": "noDeadline", "tasks.deadline.overdue_count": "overdue",
  "tasks.deadline.upcoming_count": "upcoming", "tasks.activity.created_count": "created",
  "tasks.activity.done_transition_count": "doneTransitions", "tasks.activity.reopened_count": "reopened",
  "progress.observation_coverage": "coverage", "progress.reported_percent": "reported",
  "progress.known_effort_hours": "observedEffort", "progress.total_effort_hours": "estimatedTotal",
  "progress.estimated_effort_known_subtotal_hours": "estimatedSubtotal",
  "progress.known_effort_fraction": "effortCoverage", "progress.missing_estimate_count": "missingEstimate",
  "remaining.known_subtotal_hours": "remainingSubtotal", "remaining.total_hours": "remainingTotal",
  "remaining.unknown_count": "remainingUnknown", "remaining.stale_count": "remainingStale",
  "work_logs.effective_hours": "loggedHours", "blockers.open_count": "blockersOpen",
  "blockers.severe_count": "blockersSevere", "blockers.oldest_age_days": "blockerAge",
  "risk.ready_count": "riskReady", "risk.pending_count": "riskPending", "risk.stale_count": "riskStale",
  "risk.unavailable_count": "riskUnavailable", "risk.missing_count": "riskMissing",
  "reporter.coverage": "reporterCoverage", "weekly.in_period_week_count": "weekCount",
  "workload.capacity_hours": "capacityTotal", "workload.capacity_known_subtotal_hours": "capacitySubtotal",
  "workload.capacity_unknown_count": "capacityUnknown",
};
const suffixes: Record<string, string> = { estimated_effort_known_subtotal_hours:"estimatedSubtotal",reported_percent:"reported",planned_percent:"planned",known_effort_hours:"observedEffort",total_effort_hours:"estimatedTotal",task_coverage:"coverage",added_count:"added",removed_count:"removed",baseline_available:"baselineAvailable",capacity_hours:"capacity",allocated_hours:"allocated",allocated_known_subtotal_hours:"allocatedSubtotal",residual_hours:"residual",ratio:"workloadRatio",missing_estimate_count:"missingEstimate",coverage:"reporterCoverage",score:"riskScore" };
export function MetricGrid({ snapshot }: { snapshot: ReportResult["snapshot"] }) {
  const t = useTranslations("reports");
  const locale = useLocale();
  const format = (metric: Metric) => {
    if (metric.value === null) return t(metric.state === "NOT_APPLICABLE" ? "notApplicable" : "unknown");
    const value = Number(metric.value);
    const number = new Intl.NumberFormat(locale, { maximumFractionDigits: 2 }).format(metric.unit === "FRACTION" ? value * 100 : value);
    if (metric.unit === "FRACTION" || metric.unit === "PERCENT") return `${number}%`;
    if (metric.unit === "HOURS") return t("hoursValue", {value:number});
    if (metric.unit === "DAYS") return t("daysValue", {value:number});
    if (metric.unit === "SCORE") return `${number}/100`;
    return number;
  };
  const groups = Object.groupBy(Object.values(snapshot.metrics), metric => {
    if (metric.key.startsWith("tasks.status.")) return "status";
    if (metric.key.startsWith("tasks.activity.")) return "activity";
    if (metric.key.startsWith("tasks.deadline.")) return "deadline";
    return metric.key.split(".")[0];
  });
  const label = (metric: Metric) => {
    if (labels[metric.key]) return t(`metrics.${labels[metric.key]}`);
    const parts = metric.key.split(".");
    const title = t(`metrics.${suffixes[parts.at(-1)!] ?? "other"}`);
    if (parts[0] === "weekly") {
      const source = snapshot.sources.find(s => s.resource_type === "WEEKLY_BASELINE" && s.facts?.project_week_id === parts[1]);
      return `${source?.label ?? t("weekLabel")} · ${t(`baseline.${parts[2] === "original" ? "original" : parts[2] === "current" ? "current" : "scope"}`)} · ${title}`;
    }
    if (parts[0] === "workload") return `${title} · ${parts[2]?.slice(0,8)}`;
    if (parts[0] === "reporter") return `${title} · ${snapshot.sources.find(s => s.resource_id === parts[2])?.label ?? t("windowLabel")}`;
    return title;
  };
  return <div className={styles.catalog}>{Object.entries(groups).map(([group, metrics]) => <section key={group}>
    <div className={styles.header}><h4 className={styles.groupTitle}>{t(`groups.${group}`)}</h4><span className={styles.description}>{t(group === "work_logs" ? "declaredReportingDate" : group === "activity" ? "inPeriod" : "atCapture")}</span></div>
    {group === "risk" && <p className={styles.description}>{t("riskExplanation")}</p>}
    {group === "workload" && <p className={styles.description}>{t("workloadExplanation")}</p>}
    <dl className={styles.metrics}>{metrics?.map(metric => <div className={styles.metric} key={metric.key}>
      <dt className={styles.metricLabel}>{label(metric)}</dt>
      <dd className={metric.value === null ? styles.metricUnknown : styles.metricValue}>{format(metric)}</dd>
      {metric.state === "PARTIAL" && <p className={styles.metricLabel}>{t("partialData")}</p>}
      {metric.state === "STALE" && <p className={styles.metricLabel}>{t("staleData")}</p>}
      {metric.key.startsWith("risk.") && metric.key.endsWith(".score") && <p className={styles.metricLabel}>{t("assessmentSource", {id:metric.source_refs[0]?.resource_id ?? "—",model:String(metric.source_refs[0]?.facts?.model_ref ?? "—")})}</p>}
    </div>)}</dl>
  </section>)}</div>;
}
