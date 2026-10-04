"use client";

import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useLocale, useTranslations } from "next-intl";
import { listReports, getReport, getReportDefaults } from "./api";
import { ReportDetail } from "./report-detail";
import { ReportForm } from "./report-form";
import styles from "./reports.module.css";

export function ReportPanel({ projectId, organizationId, actorMembershipId }: { projectId: string; organizationId: string; actorMembershipId: string }) {
  const t = useTranslations("reports");
  const locale = useLocale();
  const client = useQueryClient();
  const [creating, setCreating] = useState(false);
  const [selected, setSelected] = useState("");
  const [page, setPage] = useState(1);
  const scope = ["work", organizationId, actorMembershipId, "reports", projectId];
  const reports = useQuery({ queryKey: [...scope, "list", page], queryFn: () => listReports(projectId, page) });
  const defaults = useQuery({ queryKey: [...scope, "defaults"], queryFn: () => getReportDefaults(projectId), enabled: creating });
  const id = selected || reports.data?.items[0]?.id;
  const detail = useQuery({ queryKey: [...scope, "detail", id], enabled: Boolean(id), queryFn: () => getReport(id!), refetchInterval: query => ["QUEUED", "RUNNING"].includes(query.state.data?.generation_state ?? "") ? 2000 : false });
  return <section className={styles.panel} aria-label={t("title")}>
    <div className={styles.header}><div><p className={styles.eyebrow}>{t("eyebrow")}</p><h2 className={styles.title}>{t("title")}</h2><p className={styles.description}>{t("description")}</p></div>
      <button className="primary-button" type="button" onClick={() => setCreating(true)}>{t("create")}</button></div>
    {creating && (defaults.isError ? <p className="error-message" role="alert">{t("error")} <button className="text-button" onClick={() => void defaults.refetch()}>{t("retry")}</button></p> : defaults.data ? <ReportForm projectId={projectId} defaults={defaults.data} onCancel={() => setCreating(false)} onCreated={data => {
      client.setQueryData([...scope, "detail", data.report.id], data);
      setSelected(data.report.id); setCreating(false);
      void client.invalidateQueries({ queryKey: [...scope, "list"] });
    }} /> : <p role="status">{t("loading")}</p>)}
    {reports.isError || detail.isError ? <p className="error-message" role="alert">{t("error")} <button className="text-button" type="button" onClick={() => { void reports.refetch(); void detail.refetch(); }}>{t("retry")}</button></p> : detail.data ? <ReportDetail data={detail.data} organizationId={organizationId} actorMembershipId={actorMembershipId} onPublished={data => {
      client.setQueryData([...scope, "detail", data.report.id], data);
      void client.invalidateQueries({queryKey: [...scope, "list"]});
    }} onStale={() => { void detail.refetch(); }} /> : reports.isPending || (id && detail.isPending) ? <p role="status">{t("loading")}</p> : <p className={styles.empty}>{t("empty")}</p>}
    {Boolean(reports.data?.items.length) && <div className={styles.history} aria-label={t("history")}>
      <h3 className="font-semibold">{t("history")}</h3>{reports.data?.items.map(report => <button className={styles.historyItem} key={report.id} type="button" aria-pressed={id === report.id} onClick={() => setSelected(report.id)}>
        <span>{t(report.kind === "WEEKLY" ? "weekly" : "daily")} · {new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeStyle: "short" }).format(new Date(report.created_at))}</span><span>{t("version", { version: report.version })}</span></button>)}
      <div className={styles.actions}><button className="secondary-button" disabled={page === 1} onClick={() => setPage(page - 1)}>{t("previous")}</button><button className="secondary-button" disabled={page * (reports.data?.page_size ?? 20) >= (reports.data?.total ?? 0)} onClick={() => setPage(page + 1)}>{t("next")}</button></div>
    </div>}
  </section>;
}
