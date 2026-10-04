import { useRef, useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import { isDefinitiveMutationRejection } from "@/shared/api/client";
import { publishReport } from "./api";
import type { ReportResult } from "./contracts";
import { MetricGrid } from "./metric-grid";
import styles from "./reports.module.css";

export function PublicationHistory({ data, publish = publishReport, onPublished, onStale }: {
  data: ReportResult; publish?: typeof publishReport; onPublished: (data: ReportResult) => void; onStale: () => void;
}) {
  const t = useTranslations("reports");
  const locale = useLocale();
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<"publishError" | "publishStale" | null>(null);
  const [viewing, setViewing] = useState<string | null>(null);
  const intent = useRef<{binding: string; key: string} | null>(null);
  const format = (at: string) => new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeStyle: "short", timeZone: data.snapshot.period.timezone }).format(new Date(at));
  const selected = data.publications.find(item => item.id === viewing);
  async function submit() {
    if (pending || error === "publishStale") return;
    const binding = `${data.report.id}:${data.report.version}:${data.selected_version.id}:${data.snapshot.snapshot_hash}`;
    if (intent.current?.binding !== binding) intent.current = {binding, key: crypto.randomUUID()};
    setPending(true); setError(null);
    try {
      const result = await publish(data.report.id, {mode:"METRICS_ONLY", report_version_id:data.metrics_version_id ?? data.selected_version.id, snapshot_hash:data.snapshot.snapshot_hash}, data.report.version, intent.current.key);
      intent.current = null;
      onPublished(result);
    } catch (cause) {
      const stale = typeof cause === "object" && cause !== null && "status" in cause && cause.status === 412;
      setError(stale ? "publishStale" : "publishError");
      if (isDefinitiveMutationRejection(cause)) intent.current = null;
      if (stale) onStale();
    } finally { setPending(false); }
  }
  return <section className={styles.publications} aria-label={t("publications")}>
    <div className={styles.header}><div><h4 className={styles.groupTitle}>{t("publications")}</h4><p className={styles.description}>{t("publicationExplanation")}</p></div>
      <button className="secondary-button" type="button" disabled={pending || error === "publishStale"} onClick={() => void submit()}>{t(pending ? "publishing" : "publishMetrics")}</button></div>
    {error && <p role="alert" className="error-message">{t(error)}</p>}
    {!data.publications.length && <p className={styles.description}>{t("unpublished")}</p>}
    {data.publications.map(item => <div className={styles.historyItem} key={item.id}>
      <div><p>{t("publishedAt", {at:format(item.published_at)})} {item.id === data.report.current_publication_id && <span className={styles.badge}>{t("currentPublication")}</span>}</p>
        <p className={styles.sourceId}>{t("publishedBy", {id:item.publisher_membership_id})}</p></div>
      <button className="text-button" type="button" onClick={() => setViewing(item.id)}>{t("viewPublication")}</button>
    </div>)}
    {selected && <section aria-label={t("publishedMetrics")} className={styles.publicationViewer}>
      <div className={styles.header}><h4 className={styles.groupTitle}>{t("publishedMetrics")}</h4><button className="text-button" type="button" onClick={() => setViewing(null)}>{t("closePublication")}</button></div>
      <p className={styles.description}>{t("publishedAt", {at:format(selected.published_at)})}</p>
      <p className={styles.sourceId}>{t("publishedVersion", {id:selected.report_version_id})}</p>
      <details><summary>{t("snapshotReceipt")}</summary><p className={styles.sourceId}>{selected.snapshot_hash}</p></details>
      {selected.snapshot_hash === data.snapshot.snapshot_hash ? <MetricGrid snapshot={data.snapshot} /> : <p role="alert">{t("error")}</p>}
    </section>}
  </section>;
}
