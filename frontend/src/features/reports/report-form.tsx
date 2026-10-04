"use client";

import { useId, useRef, useState, type FormEvent } from "react";
import { useLocale, useTranslations } from "next-intl";
import { isDefinitiveMutationRejection } from "@/shared/api/client";
import { createReport } from "./api";
import type { ReportInput, ReportResult } from "./contracts";
import styles from "./reports.module.css";

export function ReportForm({ projectId, defaults, onCreated, onCancel }: { projectId: string; defaults: { timezone: string; period_start: string }; onCreated: (result: ReportResult) => void; onCancel: () => void }) {
  const t = useTranslations("reports");
  const kindFieldId = useId();
  const locale = useLocale() as "vi" | "en";
  const [kind, setKind] = useState<"DAILY" | "WEEKLY">("DAILY");
  const [day, setDay] = useState(defaults.period_start);
  const [timezone, setTimezone] = useState(defaults.timezone);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState(false);
  const attempt = useRef<{ fingerprint: string; key: string } | null>(null);
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (pending) return;
    const body: ReportInput = { project_id: projectId, kind, locale, narrative_enabled: true,
      ...(day ? { period_start: day } : {}), ...(timezone ? { timezone } : {}) };
    const fingerprint = JSON.stringify(body);
    if (attempt.current?.fingerprint !== fingerprint) attempt.current = { fingerprint, key: crypto.randomUUID() };
    setPending(true); setError(false);
    try { onCreated(await createReport(body, attempt.current.key)); }
    catch (cause) { setError(true); if (isDefinitiveMutationRejection(cause)) attempt.current = null; }
    finally { setPending(false); }
  }
  return <form className={styles.form} onSubmit={submit} aria-label={t("newReport")}>
    <div className={styles.field}><label htmlFor={kindFieldId}>{t("kind")}</label><select id={kindFieldId} className="form-input" value={kind} onChange={e => { const selected = e.target.value as "DAILY" | "WEEKLY"; setKind(selected); if (selected === "WEEKLY" && day) { const date = new Date(`${day}T12:00:00Z`); date.setUTCDate(date.getUTCDate() - (date.getUTCDay() + 6) % 7); setDay(date.toISOString().slice(0, 10)); } }}>{["DAILY", "WEEKLY"].map(value => <option value={value} key={value}>{t(value === "DAILY" ? "daily" : "weekly")}</option>)}</select></div>
    {kind === "WEEKLY" && <p className={styles.description}>{t("weeklyDateHint")}</p>}
    <div className={styles.formGrid}><label className={styles.field}>{t("date")}<input className="form-input" type="date" value={day} onChange={e => setDay(e.target.value)} /></label>
      <label className={styles.field}>{t("timezone")}<input className="form-input" value={timezone} placeholder={t("projectTimezone")} onChange={e => setTimezone(e.target.value)} /></label></div>
    <p className={styles.description}>{t("defaultsExplanation")}</p>
    {error && <p className="error-message mt-3" role="alert">{t("error")}</p>}
    <div className={styles.actions}><button className="primary-button" type="submit" disabled={pending}>{t(pending ? "creating" : "generate")}</button>
      <button className="secondary-button" type="button" disabled={pending} onClick={onCancel}>{t("cancel")}</button></div>
  </form>;
}
