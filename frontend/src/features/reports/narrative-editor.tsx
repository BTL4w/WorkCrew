"use client";
import {useRef, useState} from "react";
import {useTranslations} from "next-intl";
import {isDefinitiveMutationRejection} from "@/shared/api/client";
import {editNarrative, publishReport, rejectNarrative} from "./api";
import type {ReportResult, NarrativeDocument} from "./contracts";
import styles from "./reports.module.css";

export function NarrativeEditor({data,onUpdated,onStale,edit=editNarrative,publish=publishReport,reject=rejectNarrative}: {data:ReportResult;onUpdated:(data:ReportResult)=>void;onStale:()=>void;edit?:typeof editNarrative;publish?:typeof publishReport;reject?:typeof rejectNarrative}) {
 const t=useTranslations("reports");
 const [editing,setEditing]=useState(false);
 const [draft,setDraft]=useState<NarrativeDocument|null>(data.selected_version.narrative??null);
 const [reason,setReason]=useState("");
 const [pending,setPending]=useState(false);
 const [error,setError]=useState(false);
 const [stale,setStale]=useState(false);
 const intent=useRef<{binding:string;key:string}|null>(null);
 if(!draft) return null;
 const blocked=pending||stale||data.review_state!=="PENDING";
 async function submit(action:"edit"|"publish"|"reject") {
  if(blocked||!draft||(action==="publish"&&data.verification_state!=="VERIFIED"))return;
  const body=action==="edit"?{parent_version_id:data.selected_version.id,snapshot_hash:data.snapshot.snapshot_hash,narrative:draft}:action==="reject"?{report_version_id:data.selected_version.id,snapshot_hash:data.snapshot.snapshot_hash,reason:reason.trim()}:{mode:"REVIEWED_NARRATIVE" as const,report_version_id:data.selected_version.id,snapshot_hash:data.snapshot.snapshot_hash};
  const binding=JSON.stringify([action,data.report.id,data.report.version,body]);
  if(intent.current?.binding!==binding)intent.current={binding,key:crypto.randomUUID()};
  setPending(true);setError(false);
  try {
   let result:ReportResult;
   if(action==="edit")result=await edit(data.report.id,{parent_version_id:data.selected_version.id,snapshot_hash:data.snapshot.snapshot_hash,narrative:draft},data.report.version,intent.current.key);
   else if(action==="reject")result=await reject(data.report.id,{report_version_id:data.selected_version.id,snapshot_hash:data.snapshot.snapshot_hash,reason:reason.trim()},data.report.version,intent.current.key);
   else result=await publish(data.report.id,{mode:"REVIEWED_NARRATIVE",report_version_id:data.selected_version.id,snapshot_hash:data.snapshot.snapshot_hash},data.report.version,intent.current.key);
   intent.current=null;setEditing(false);onUpdated(result);
  }catch(cause){setError(true);if(isDefinitiveMutationRejection(cause))intent.current=null;
   if(typeof cause==="object"&&cause!==null&&"status" in cause&&cause.status===412){setStale(true);onStale();}}
  finally{setPending(false);}
 }
 function originalText(id:string) {
  const block=data.selected_version.narrative?.blocks.find(b=>b.id===id);
  return block&&block.kind!=="FACT"?block.text:t("newNote");
 }
 return <section aria-label={t("reviewTitle")} className={styles.publications}>
  <h4>{t("reviewTitle")}</h4><p>{t(`verification.${data.verification_state}`)}</p>
  <p>{t(`review.${data.review_state}`)}</p>
  {data.selected_version.origin==="AI_EDITED"&&<p>{t("editedLineage")}</p>}
  {editing&&<div>
   <p>{t("editExplanation")}</p>
   {draft.blocks.filter(b=>b.kind!=="FACT").map(b=><label key={b.id} className={styles.field}>{t("editBlock",{id:b.id})}
    <textarea aria-label={t("editBlock",{id:b.id})} maxLength={2000} disabled={blocked} value={b.text} onChange={e=>setDraft({...draft,blocks:draft.blocks.map(current=>current.id===b.id&&current.kind!=="FACT"?{...current,text:e.target.value}:current)})}/>
    <p className={styles.description}>{t("before",{text:originalText(b.id)})}</p>
   </label>)}
   <button type="button" className="text-button" disabled={blocked||draft.blocks.length>=20} onClick={()=>setDraft({...draft,blocks:[...draft.blocks,{id:`note_${crypto.randomUUID().replaceAll("-","")}`,section:"limitations",kind:"LIMITATION",text:"",source_refs:[],assumptions:[]}]})}>{t("addNote")}</button>
   <button type="button" className="secondary-button" disabled={blocked||draft.blocks.some(b=>b.kind!=="FACT"&&!b.text.trim())} onClick={()=>void submit("edit")}>{t("saveVerify")}</button>
   <button type="button" className="text-button" disabled={pending} onClick={()=>{setEditing(false);setDraft(data.selected_version.narrative??null);}}>{t("cancelEdit")}</button>
  </div>}
  {!editing&&<div className={styles.actions}>
   <button type="button" className="secondary-button" disabled={blocked} onClick={()=>setEditing(true)}>{t("editNarrative")}</button>
   <button type="button" className="primary-button" disabled={blocked||data.verification_state!=="VERIFIED"} onClick={()=>void submit("publish")}>{t("reviewPublish")}</button>
  </div>}
  <label className={styles.field}>{t("rejectReason")}<textarea maxLength={2000} disabled={blocked} value={reason} onChange={e=>setReason(e.target.value)}/></label>
  <button type="button" className="secondary-button" disabled={blocked||!reason.trim()} onClick={()=>void submit("reject")}>{t("rejectNarrative")}</button>
  {error&&<p role="alert">{t(stale?"publishStale":"reviewError")}</p>}
 </section>;
}
