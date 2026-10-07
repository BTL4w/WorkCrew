import {useLocale,useTranslations} from "next-intl";
import type {Feedback,FeedbackOutcome,ReviewRates} from "./contracts";

export function ReviewOutcomes({feedback,outcomes,rates}:{feedback:Feedback[];outcomes:FeedbackOutcome[];rates:ReviewRates|null}) {
 const t=useTranslations("feedback.outcomes");
 const locale=useLocale();
 const at=(value:string)=>new Intl.DateTimeFormat(locale,{dateStyle:"medium",timeStyle:"short"}).format(new Date(value));
 const percent=(value:string|null|undefined)=>value==null?t("unknown"):new Intl.NumberFormat(locale,{maximumFractionDigits:2}).format(Number(value))+"%";
 return <section aria-label={t("title")}>
  <h4>{t("title")}</h4><p>{t("meaning")}</p>
  <p>{t("rates",{count:rates?.reviewed_generation_count??0,accept:percent(rates?.accept_percent),edit:percent(rates?.edit_percent),reject:percent(rates?.reject_percent)})}</p>
  <p>{t("separate",{pending:rates?.pending_generation_count??0,failed:rates?.failed_generation_count??0,manual:rates?.manual_report_count??0})}</p>
  {outcomes.length===0&&<p>{t("noOutcome")}</p>}
  {feedback.map(review=><article key={review.id}>
   <p>{t(review.kind==="ADVISORY"?"advisory":review.decision==="EDIT"?"editedApproved":review.decision==="REJECT"?"rejected":"accepted")}</p>
   <p>{t("versions",{original:review.original_version_id,corrected:review.report_version_id})}</p>
   <p>{t("reviewedAt",{at:at(review.created_at)})}</p>
   {review.reason&&<p>{review.reason}</p>}
   <details><summary>{t("provenance")}</summary><pre>{JSON.stringify(review.provenance,null,2)}</pre></details>
   {outcomes.filter(outcome=>outcome.feedback_id===review.id).map(outcome=><div key={outcome.id}>
    <p>{t(outcome.state==="AVAILABLE"?"available":"noOutcome")}</p>
    <p>{t("source",{type:t(outcome.source.source_type),id:outcome.source.source_id,version:outcome.source.source_version})}</p>
    <p>{t("recordedAt",{at:at(outcome.recorded_at)})}{outcome.occurred_at&&` · ${t("occurredAt",{at:at(outcome.occurred_at)})}`}</p>
    {Object.entries(outcome.facts).map(([key,value])=><p key={key}>{t(`facts.${key}`)}: {key==="completion_state"?t("unknown"):key==="to_status"||key==="from_status"||key==="status"?t(`status.${value}`):String(value??t("unknown"))}</p>)}
   </div>)}
   {outcomes.every(outcome=>outcome.feedback_id!==review.id)&&outcomes.length>0&&<p>{t("noOutcome")}</p>}
  </article>)}
 </section>;
}
