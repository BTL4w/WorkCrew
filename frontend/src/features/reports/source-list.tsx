"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { getReportSources } from "./api";
import styles from "./reports.module.css";

const detailFields: Record<string,string> = {reported_percent:"reported",remaining_hours:"remaining",spent_hours:"spent",reporting_date:"date",reporting_timezone:"timezone",timezone:"timezone",due_date:"due",estimated_effort_hours:"estimated",expected_count:"expected",reported_count:"reportedCount",score:"score",model_ref:"model",evaluated_at:"evaluated",hours:"capacity",unavailable_hours:"leave",scope_changed:"scopeChanged"};
export function SourceList({ reportId, organizationId, actorMembershipId }: { reportId: string; organizationId: string; actorMembershipId: string }) {
  const t = useTranslations("reports");
  const [cursors, setCursors] = useState<(string | undefined)[]>([undefined]);
  const cursor = cursors.at(-1);
  const query = useQuery({ queryKey: ["work", organizationId, actorMembershipId, "reports", reportId, "sources", cursor], queryFn: () => getReportSources(reportId, cursor) });
  return <section className={styles.sources} aria-label={t("sourcesTitle")}>
    <div className={styles.header}><h4>{t("sourcesTitle")}</h4>{query.data && <span className={styles.description}>{t("sourceTotal", { total: query.data.total })}</span>}</div>
    <p className={styles.description}>{t("sourcesExplanation")}</p>
    {query.isPending ? <p role="status">{t("loading")}</p> : query.isError ? <p role="alert">{t("error")} <button type="button" onClick={() => void query.refetch()}>{t("retry")}</button></p> : <>
      <ul className={styles.sourceRows}>{query.data.items.map(({ source, freshness }) => <li className={styles.sourceRow} key={`${source.resource_type}:${source.resource_id}`}>
        <div><strong>{source.label ?? source.resource_type}</strong><p className={styles.sourceId}>{source.resource_id} · v{source.version}</p></div>
        <span className={styles.description}>{t(`freshness.${freshness}`)}</span>
        {source.facts && Object.keys(source.facts).some(key => detailFields[key]) && <details className={styles.sourceFacts}>
          <summary>{t("sourceDetails")}</summary><dl>{Object.entries(source.facts).filter(([key,value]) => detailFields[key] && (value === null || typeof value !== "object")).map(([key,value]) => <div key={key}><dt>{t(`sourceFields.${detailFields[key]}`)}</dt><dd>{value === null ? t("unknown") : typeof value === "boolean" ? t(value ? "yes" : "no") : String(value)}{value !== null && key === "reported_percent" ? "%" : ""}</dd></div>)}</dl>
        </details>}
      </li>)}</ul>
      <div className={styles.actions}><button className="secondary-button" disabled={cursors.length === 1} onClick={() => setCursors(values => values.slice(0, -1))}>{t("previous")}</button><button className="secondary-button" disabled={!query.data.next_cursor} onClick={() => setCursors(values => [...values, query.data.next_cursor!])}>{t("next")}</button></div>
    </>}
  </section>;
}
