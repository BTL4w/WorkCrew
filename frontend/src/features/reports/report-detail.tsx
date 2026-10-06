import { useLocale, useTranslations } from "next-intl";
import type { ReportResult } from "./contracts";
import {FeedbackForm} from "../feedback/feedback-form";
import { NarrativeView } from "./narrative-view";
import { PublicationHistory } from "./publication-history";
import { SourceList } from "./source-list";
import { MetricGrid } from "./metric-grid";
import styles from "./reports.module.css";

export function ReportDetail({ data, readOnly = false, organizationId, actorMembershipId, onPublished, onStale }: { readOnly?: boolean; onPublished: (data: ReportResult) => void; onStale: () => void; data: ReportResult; organizationId: string; actorMembershipId: string }) {
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
    <PublicationHistory readOnly={readOnly} key={`publication:${data.report.id}:${data.report.version}`} data={data} onPublished={onPublished} onStale={onStale} />
    <p className={styles.sourceId}>{t("referencedVersion", {version:data.selected_version.id})}</p>
    <MetricGrid snapshot={data.snapshot} />
    <SourceList key={data.report.id} reportId={data.report.id} organizationId={organizationId} actorMembershipId={actorMembershipId} />
    <NarrativeView readOnly={readOnly} key={`narrative:${data.report.id}:${data.report.version}`} data={data} onUpdated={onPublished} onStale={onStale} />
    {!readOnly&&data.selected_version.narrative&&<FeedbackForm key={data.selected_version.id} reportId={data.report.id} versionId={data.selected_version.id}/>}
  </article>;
}
