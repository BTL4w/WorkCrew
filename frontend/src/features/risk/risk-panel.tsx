"use client";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useRef, useState } from "react";
import { isDefinitiveMutationRejection } from "@/shared/api/client";
import { currentRisk, refreshRisk, riskReviews, recordReview } from "./api";
import { confirmedWarningsSchema, type Review } from "./contracts";

type Props = { taskId: string; organizationId: string; actorMembershipId: string };
export function RiskPanel(props: Props) {
 const t = useTranslations("risk"); const client = useQueryClient();
 const scope = ["work", props.organizationId, props.actorMembershipId, "risk", props.taskId];
 const query = useQuery({ queryKey: scope, queryFn: () => currentRisk(props.taskId), refetchInterval: q => q.state.data?.state === "PENDING" ? 2000 : 30000 });
 const result = query.data;
 const reviews = useQuery({ queryKey: [...scope, "reviews", result?.id], queryFn: () => riskReviews(result!.id), enabled: Boolean(result && result.state !== "PENDING") });
 const [reason, setReason] = useState(""); const [disposition, setDisposition] = useState<Review["disposition"]>("NEEDS_FOLLOWUP");
 const [busy, setBusy] = useState(false); const [error, setError] = useState(false); const [uncertain, setUncertain] = useState(false);
 const pending = useRef<null | { key: string; action: "refresh" | "review"; riskId?: string; body?: Pick<Review, "reason" | "disposition"> }>(null);
 async function mutate(action: "refresh" | "review") {
  if (!pending.current) pending.current = { key: crypto.randomUUID(), action, riskId: result?.id, body: { reason, disposition } };
  const request = pending.current; setBusy(true); setError(false);
  try {
   if (request.action === "refresh") await refreshRisk(props.taskId, request.key);
   else await recordReview(request.riskId!, request.body!, request.key);
   pending.current = null; setReason(""); await client.invalidateQueries({ queryKey: scope });
   await client.invalidateQueries({ queryKey: ["work", props.organizationId, props.actorMembershipId, "notifications"] });
  } catch (failure) { setError(true); if (isDefinitiveMutationRejection(failure)) pending.current = null; }
  finally { setBusy(false); setUncertain(Boolean(pending.current)); }
 }
 const judgment = result?.state === "READY" ? result.judgment : null;
 const blocked = busy || uncertain;
 return <section className="mt-6 rounded-2xl border p-4" aria-label={t("title")}>
  <h3>{t("title")}</h3><p>{t("origin")}</p>
  {query.isPending ? <p role="status">{t("loading")}</p> : query.isError ? <p role="alert">{t("error")}<button onClick={() => void query.refetch()}>{t("reload")}</button></p> : <p role="status">{t(result ? `state.${result.state}` : "state.NONE")}</p>}
  {judgment?.score != null && <p><strong>{`${judgment.score} / 100`}</strong> · {t(`band.${result!.band}`)}</p>}
  {judgment && <><p>{judgment.rationale}</p><ul>{judgment.observations.map((o, i) => <li key={i}>{o.text}{o.source_ids.map(id => <details key={id}><summary>{id}</summary><dl>{Object.entries(result!.input_snapshot!.facts.find(f => f.id === id)?.values ?? {}).map(([key, value]) => <div key={key}><dt>{t.has(`field.${key}`) ? t(`field.${key}`) : t("fact")}</dt><dd>{value == null ? t("unknown") : typeof value === "boolean" ? t(value ? "yes" : "no") : typeof value === "object" ? JSON.stringify(value) : String(value)}</dd></div>)}</dl></details>)}</li>)}</ul>
   <h4>{t("recommendations")}</h4><ul>{judgment.recommendations.map((text, i) => <li key={i}>{text}</li>)}</ul><p>{t("advisory")}</p>
   {judgment.limitations.map((text, i) => <p key={i}>{text}</p>)}
  </>}
  {result?.input_snapshot?.facts.some(f => f.kind === "WARNING") && <section aria-label={t("confirmedWarnings")}>
   <h4>{t("confirmedWarnings")}</h4><p>{t("reportWarningScope")}</p>
   {result.input_snapshot.facts.filter(f => f.kind === "WARNING").map(fact => {
    const parsed = confirmedWarningsSchema.safeParse(fact.values);
    return parsed.success ? <article key={fact.id}>
     <ul>{parsed.data.warnings.map(warning => <li key={warning.id}>{t.has(`warningCode.${warning.code}`) ? t(`warningCode.${warning.code}`) : t("unknown")}</li>)}</ul>
     <p>{t("acknowledgedAt")}: <time dateTime={parsed.data.acknowledged_at}>{parsed.data.acknowledged_at}</time></p>
     <p>{t("reportReference")}: {parsed.data.update_id ?? parsed.data.assessment_id}</p>
    </article> : <p key={fact.id}>{t("unknown")}</p>;
   })}
  </section>}
  {result?.input_snapshot?.missing.length ? <p>{t("missing")}: {result.input_snapshot.missing.map(kind => t(`kind.${kind}`)).join(", ")}</p> : null}
  {result?.limitation && <p>{t.has(`limitation.${result.limitation}`) ? t(`limitation.${result.limitation}`) : t("unknown")}</p>}
  {result && <time dateTime={result.evaluated_at}>{t("evaluatedAt")}: {result.evaluated_at}</time>}
  {error && <p role="alert">{t("error")}</p>}
  {uncertain && <button disabled={busy} onClick={() => void mutate(pending.current!.action)}>{t("retry")}</button>}
  <button type="button" disabled={blocked || result?.state === "PENDING"} onClick={() => void mutate("refresh")}>{t("refresh")}</button>
  {result && result.state !== "PENDING" && <><label>{t("disposition")}<select aria-label={t("disposition")} value={disposition} disabled={blocked} onChange={e => setDisposition(e.target.value as Review["disposition"])}>{(["NEEDS_FOLLOWUP", "ACCEPTED_EXPLANATION", "RESOLVED"] as const).map(value => <option key={value} value={value}>{t(`dispositionValue.${value}`)}</option>)}</select></label>
   <label>{t("reason")}<textarea maxLength={2000} value={reason} disabled={blocked} onChange={e => setReason(e.target.value)} /></label>
   <button type="button" disabled={blocked || !reason.trim()} onClick={() => void mutate("review")}>{t("recordReview")}</button>
   {reviews.isError && <p role="alert">{t("error")}</p>}<ul>{reviews.data?.map(review => <li key={review.id}>{t(`dispositionValue.${review.disposition}`)} · {review.reason} · {review.at}</li>)}</ul>
  </>}
 </section>;
}
