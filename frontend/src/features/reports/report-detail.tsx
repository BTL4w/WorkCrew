import { useLocale, useTranslations } from "next-intl";
import type { ReportResult } from "./contracts";
import { PublicationHistory } from "./publication-history";
import { SourceList } from "./source-list";
import { MetricGrid } from "./metric-grid";
import styles from "./reports.module.css";

export function ReportDetail({ data, organizationId, actorMembershipId, onPublished, onStale }: { onPublished: (data: ReportResult) => void; onStale: () => void; data: ReportResult; organizationId: string; actorMembershipId: string }) {
  const t = useTranslations("reports");
  const locale = useLocale();
  const zone = data.snapshot.period.timezone;
  const format = (at: string) => new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeStyle: "short", timeZone: zone }).format(new Date(at));
  return <article className={styles.detail} aria-label={t("detail")}>
    <div className={styles.header}><div><h3 className={styles.title}>{t(data.report.kind === "WEEKLY" ? "weekly" : "daily")} · {data.snapshot.period.local_start}</h3>
      <p className={styles.description}>{t("captureExplanation")}</p></div><span className={styles.badge}>{t("metricsOnly")}</span></div>
    <div className={styles.meta}><span>{t("captured", { at: format(data.snapshot.captured_at) })} · {zone}</span>
      <span>{t("version", { version: data.report.version })}</span>
      {data.snapshot.period.partial_period && <span>{t("partialPeriod")}</span>}</div>
    <p className={styles.description}>{t("periodBounds", {start:data.snapshot.period.local_start,end:data.snapshot.period.local_end})}</p>
    <p className={styles.description}>{t("observedThrough", {at:format(data.snapshot.period.observed_through)})}</p>
    <p className={styles.description}>{t("utcBounds", {start:data.snapshot.period.start_utc,end:data.snapshot.period.end_utc})}</p>
    <PublicationHistory key={`${data.report.id}:${data.report.version}`} data={data} onPublished={onPublished} onStale={onStale} />
    <MetricGrid snapshot={data.snapshot} />
    <SourceList key={data.report.id} reportId={data.report.id} organizationId={organizationId} actorMembershipId={actorMembershipId} />
    <p className={styles.notice}>{t(data.generation_state === "AI_UNAVAILABLE" ? "aiUnavailable" : "manualOnly")}</p>
  </article>;
}
