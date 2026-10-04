import { useLocale, useTranslations } from "next-intl";
import type { ReportResult } from "./contracts";
import styles from "./reports.module.css";

const labels: Record<string, string> = {
  "tasks.status.total_count": "metrics.total", "tasks.status.to_do_count": "metrics.todo",
  "tasks.status.in_progress_count": "metrics.inProgress", "tasks.status.done_count": "metrics.done",
  "tasks.deadline.unknown_count": "metrics.noDeadline", "progress.observation_coverage": "metrics.coverage",
};
export function MetricGrid({ snapshot }: { snapshot: ReportResult["snapshot"] }) {
  const t = useTranslations("reports");
  const locale = useLocale();
  return <dl className={styles.metrics}>{Object.values(snapshot.metrics).map(metric => <div className={styles.metric} key={metric.key}>
    <dt className={styles.metricLabel}>{labels[metric.key] ? t(labels[metric.key]) : metric.key}</dt>
    <dd className={metric.value === null ? styles.metricUnknown : styles.metricValue}>{metric.value === null ? t("unknown") : new Intl.NumberFormat(locale, { maximumFractionDigits: 2 }).format(Number(metric.value))}</dd>
  </div>)}</dl>;
}
