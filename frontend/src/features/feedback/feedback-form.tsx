"use client";
import {useRef,useState} from "react";
import {useTranslations} from "next-intl";
import {isDefinitiveMutationRejection} from "@/shared/api/client";
import {recordFeedback} from "./api";
import type {FeedbackInput} from "./contracts";
import styles from "../reports/reports.module.css";
export function FeedbackForm({reportId,versionId,record=recordFeedback}:{reportId:string;versionId:string;record?:typeof recordFeedback}){
 const t=useTranslations("feedback");
 const [reason,setReason]=useState("");const [decision,setDecision]=useState<FeedbackInput["decision"]>("ACCEPT");
 const [pending,setPending]=useState(false);const [error,setError]=useState(false);const [saved,setSaved]=useState(false);
 const intent=useRef<{binding:string;key:string}|null>(null);
 async function submit(){
  if(pending||!reason.trim())return;
  const body={report_id:reportId,report_version_id:versionId,decision,reason:reason.trim()};const binding=JSON.stringify(body);
  if(intent.current?.binding!==binding)intent.current={binding,key:crypto.randomUUID()};
  setPending(true);setError(false);setSaved(false);
  try{await record(body,intent.current.key);intent.current=null;setSaved(true);}
  catch(cause){setError(true);if(isDefinitiveMutationRejection(cause))intent.current=null;}
  finally{setPending(false);}
 }
 return <section aria-label={t("title")} className={styles.publications}><h4>{t("title")}</h4><p>{t("advisory")}</p>
  <label className={styles.field}>{t("decision")}<select value={decision} disabled={pending} onChange={e=>setDecision(e.target.value as FeedbackInput["decision"])}>{(["ACCEPT","EDIT","REJECT"] as const).map(value=><option key={value} value={value}>{t(value)}</option>)}</select></label>
  <label className={styles.field}>{t("reason")}<textarea value={reason} maxLength={2000} disabled={pending} onChange={e=>setReason(e.target.value)}/></label>
  <button type="button" className="secondary-button" disabled={pending||!reason.trim()} onClick={()=>void submit()}>{t("submit")}</button>
  {error&&<p role="alert">{t("error")}</p>}{saved&&<p role="status">{t("saved")}</p>}
 </section>;
}
