"use client";
import { useTranslations } from "next-intl";
import type { DraftAssessment } from "./reporting-contracts";

export function EvidenceAssessmentCard({assessment,acknowledged,onAcknowledge,disabled=false}:{assessment:DraftAssessment;acknowledged:boolean;onAcknowledge:(value:boolean)=>void;disabled?:boolean}){
 const t=useTranslations("dailyUpdate.assessment");
 const common=useTranslations("dailyUpdate");
 if(assessment.state==="STALE")return <p role="alert">{t("stale")}</p>;
 if(assessment.state==="UNAVAILABLE")return <div><p>{common("assessmentUnavailable")}</p>{t.has(`limitation.${assessment.limitation}`)&&<p>{t(`limitation.${assessment.limitation}`)}</p>}</div>;
 if(assessment.state==="NOT_ASSESSED_NO_EVIDENCE")return <p>{t("noEvidence")}</p>;
 if(assessment.state==="PENDING")return <p role="status">{t("pending")}</p>;
 const result=assessment.result;
 return <section className="rounded-xl border p-4 space-y-3" aria-label={t("title")}>
 <h4>{t("title")}</h4><p>{t("meaning")}</p>
 <p>{t(result?.scoring_method==="AI"?"aiOrigin":"legacyOrigin")}</p>
 <p>{result?.score==null?t("unknown"):`${Number(result?.score).toLocaleString(undefined,{maximumFractionDigits:2})}/100`}</p>
 <p>{t("claims",{assessed:result?.assessed_count??0,total:result?.total_count??0})}</p>
 <p>{t("sources",{processed:assessment.coverage.processed_count,total:assessment.coverage.total_count})}</p>
 {result?.rationale&&<div><h5>{t("rationale")}</h5><p>{result.rationale}</p></div>}
 {Boolean(result?.recommendations?.length)&&<div><h5>{t("recommendations")}</h5><p>{t("advisory")}</p><ul>{result?.recommendations?.map((advice,index)=><li key={index}>{advice}</li>)}</ul></div>}
 {assessment.findings.map(finding=><div key={finding.claim_id}>
 <p>{assessment.claims.find(claim=>claim.id===finding.claim_id)?.text}</p><p>{t(`finding.${finding.finding}`)}</p>
 {finding.limitation&&<p>{finding.limitation}</p>}
 {finding.source_refs.map(ref=><a className="block" key={`${ref.evidence_id}:${ref.version}`} href={`/api/v1/evidence/${ref.evidence_id}/versions/${ref.version}/content`} download>{t("original")} · {ref.evidence_id} · v{ref.version}</a>)}
 </div>)}
 {assessment.warnings.map(warning=><p role="alert" key={warning.id}>{t(`warning.${warning.code}`)}</p>)}
 {assessment.warnings.length>0&&<label><input type="checkbox" checked={acknowledged} disabled={disabled} onChange={event=>onAcknowledge(event.target.checked)}/>{t("acknowledge")}</label>}
 </section>;
}
