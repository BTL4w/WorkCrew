import { useLocale, useTranslations } from "next-intl";
import type { ReportResult } from "./contracts";
import { MetricGrid } from "./metric-grid";
import styles from "./reports.module.css";

export function ReportDetail({ data }: { data: ReportResult }) {
  const t = useTranslations("reports");
  const locale = useLocale();
  const zone = data.snapshot.period.timezone;
  const format = (at: string) => new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeStyle: "short", timeZone: zone }).format(new Date(at));
  return <article className={styles.detail} aria-label={t("detail")}>
    <div className={styles.header}><div><h3 className={styles.title}>{t("daily")} · {data.snapshot.period.local_start}</h3>
      <p className={styles.description}>{t("captureExplanation")}</p></div><span className={styles.badge}>{t("metricsOnly")}</span></div>
    <div className={styles.meta}><span>{t("captured", { at: format(data.snapshot.captured_at) })} · {zone}</span>
      <span>{t("version", { version: data.report.version })}</span>
      {data.snapshot.period.partial_period && <span>{t("partialPeriod")}</span>}</div>
    <MetricGrid snapshot={data.snapshot} />
    <p className={styles.notice}>{t(data.generation_state === "AI_UNAVAILABLE" ? "aiUnavailable" : "manualOnly")}</p>
  </article>;
}
