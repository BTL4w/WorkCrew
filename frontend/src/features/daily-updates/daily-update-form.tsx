"use client";
import { useQuery,useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useRef,useState } from "react";
import { ApiError,isDefinitiveMutationRejection } from "@/shared/api/client";
import { assessDailyDraft,getDailyAssessment,createDailyDraft,getReportingContext,getUpdateHistory,submitDailyDraft } from "./reporting-api";
import { EvidenceAssessmentCard } from "./evidence-assessment-card";
import { EvidencePicker } from "./evidence-picker";
import { DailyUpdateHistory } from "./daily-update-history";
import type { DailyDraft,Observation,ReportingItem,SelectedEvidence } from "./reporting-contracts";

export function DailyUpdateForm({taskId,taskVersion,organizationId,actorMembershipId}:{taskId:string;taskVersion:number;organizationId:string;actorMembershipId:string}){
 const t=useTranslations("dailyUpdate");
 const queryClient=useQueryClient();
 const scope=["work",organizationId,actorMembershipId,"daily-update",taskId];
 const context=useQuery({queryKey:[...scope,"context",taskVersion],queryFn:()=>getReportingContext(taskId)});
 const history=useQuery({queryKey:[...scope,"history"],queryFn:()=>getUpdateHistory(taskId)});
 const [percent,setPercent]=useState("");const [remaining,setRemaining]=useState("");const [spent,setSpent]=useState("");
 const [date,setDate]=useState("");const [done,setDone]=useState("");const [next,setNext]=useState("");const [reason,setReason]=useState("");
 const [correction,setCorrection]=useState<Observation|null>(null);
 const [uploaded,setUploaded]=useState<SelectedEvidence[]>([]);const [selected,setSelected]=useState<string[]>([]);
 const [draft,setDraft]=useState<DailyDraft|null>(null);const [busy,setBusy]=useState(false);const [error,setError]=useState<string|null>(null);const [saved,setSaved]=useState(false);const [uncertain,setUncertain]=useState(false);
 const [assessing,setAssessing]=useState(false);
 const [assessmentRequested,setAssessmentRequested]=useState(false);const [acknowledgedId,setAcknowledgedId]=useState<string|null>(null);
 const assessmentKey=useRef<string|null>(null);
 const assessment=useQuery({queryKey:[...scope,"assessment",draft?.id,draft?.version],queryFn:()=>getDailyAssessment(draft!.id),enabled:Boolean(draft)&&assessmentRequested&&!uncertain,refetchInterval:query=>query.state.data?.state==="PENDING"?1000:false});
 const warnings=assessment.data?.warnings??[];
 const acknowledged=Boolean(assessment.data?.id)&&acknowledgedId===assessment.data?.id;
 function setAcknowledged(value:boolean){setAcknowledgedId(value?assessment.data?.id??null:null);}
 const attempt=useRef<{body:string;key:string}|null>(null);const confirmKey=useRef<string|null>(null);
 function showError(failure:unknown){const code=failure instanceof ApiError?failure.code:"UNKNOWN";setError(t.has(`error.${code}`)?t(`error.${code}`):t("error.UNKNOWN"));}
 if(context.isPending)return <p>{t("loading")}</p>;
 if(!context.data)return <p role="alert">{t("error.FORBIDDEN")} <button type="button" onClick={()=>void context.refetch()}>{t("reload")}</button></p>;
 const ctx=context.data;
 const options=Array.from(new Map([...ctx.evidence_refs,...uploaded].map(ref=>[ref.evidence_id,ref])).values());
 async function prepare(event:React.FormEvent){
  event.preventDefault();setError(null);setSaved(false);setBusy(true);
  const item:ReportingItem={task_id:taskId,expected_task_version:ctx.task_version,expected_progress_version:ctx.progress_version,reported_percent:percent,remaining_hours:remaining||null,spent_hours:spent||null,reporting_date:date||ctx.reporting_date,done_text:done,next_steps:next,evidence_refs:options.filter(ref=>selected.includes(ref.evidence_id)),corrects_observation_id:correction?.id??null,correction_reason:reason};
  const body=JSON.stringify(item);
  if(!attempt.current||attempt.current.body!==body)attempt.current={body,key:crypto.randomUUID()};
  try{setAssessmentRequested(false);setAcknowledged(false);assessmentKey.current=null;setDraft(await createDailyDraft([item],attempt.current.key));attempt.current=null;confirmKey.current=crypto.randomUUID();}
  catch(failure){showError(failure);if(isDefinitiveMutationRejection(failure))attempt.current=null;}
  finally{setBusy(false);}
 }
 async function confirm(){
  if(!draft||!confirmKey.current)return;
  setError(null);setBusy(true);
  try{await submitDailyDraft(draft,confirmKey.current,assessment.data,acknowledged);setSaved(true);setUncertain(false);setDraft(null);setCorrection(null);setReason("");setSelected([]);setUploaded([]);confirmKey.current=null;
   await queryClient.invalidateQueries({queryKey:scope});await queryClient.invalidateQueries({queryKey:["completion",organizationId,actorMembershipId,taskId]});await queryClient.invalidateQueries({queryKey:["work",organizationId,actorMembershipId,"weekly-progress"]});
  }catch(failure){showError(failure);const rejected=isDefinitiveMutationRejection(failure);setUncertain(!rejected);if(rejected){confirmKey.current=crypto.randomUUID();setAcknowledged(false);setAssessmentRequested(true);void assessment.refetch();}}
  finally{setBusy(false);}
 }
 async function requestAssessment(){if(!draft)return;setBusy(true);setAssessing(true);setAcknowledged(false);setError(null);if(!assessmentKey.current)assessmentKey.current=crypto.randomUUID();try{await assessDailyDraft(draft,assessmentKey.current);setAssessmentRequested(true);await assessment.refetch();assessmentKey.current=null;confirmKey.current=crypto.randomUUID();}catch(failure){showError(failure);if(isDefinitiveMutationRejection(failure))assessmentKey.current=null;setAssessmentRequested(true);}finally{setBusy(false);setAssessing(false);}}
 function correct(o:Observation){setCorrection(o);setPercent(o.item.reported_percent);setRemaining(o.item.remaining_hours??"");setSpent(o.item.spent_hours??"");setDate(o.item.reporting_date);setDone(o.item.done_text);setNext(o.item.next_steps??"");setSelected((o.item.evidence_refs??[]).map(r=>r.evidence_id));setReason("");setDraft(null);setSaved(false);setError(null);}
 return <section className="mt-8 rounded-2xl border p-5"><h3 className="text-lg font-semibold">{t("title")}</h3>
 <p>{t("independent")}</p>{ctx.project_week_state==="NO_PROJECT_WEEK"&&<p>{t("noWeek")}</p>}
 <p>{t("timezone",{timezone:ctx.reporting_timezone})}</p>
 {saved&&<p role="status">{t("saved")}</p>}{error&&<p role="alert">{error}</p>}
 {draft?<div className="mt-4 space-y-3">{assessing?<p role="status">{t("assessment.pending")}</p>:assessmentRequested?(assessment.data?<EvidenceAssessmentCard key={assessment.data.id??"unavailable"} assessment={assessment.data} acknowledged={acknowledged} onAcknowledge={setAcknowledged} disabled={busy||uncertain}/>:<p role="status">{assessment.isError?t("error.UNKNOWN"):t("loading")}</p>):<p>{t("assessmentUnavailable")}</p>}{!uncertain&&<button className="secondary-button" type="button" disabled={busy||assessment.data?.state==="PENDING"} onClick={()=>void requestAssessment()}>{t("assessment.request")}</button>}{!uncertain&&<button type="button" disabled={busy} onClick={()=>{setAssessmentRequested(true);setAcknowledged(false);confirmKey.current=crypto.randomUUID();void assessment.refetch();}}>{t("assessment.refresh")}</button>}<dl><dt>{t("percent")}</dt><dd>{draft.items[0].reported_percent}%</dd><dt>{t("remaining")}</dt><dd>{draft.items[0].remaining_hours??t("unknown")}</dd><dt>{t("spent")}</dt><dd>{draft.items[0].spent_hours??t("unknown")}</dd><dt>{t("date")}</dt><dd>{draft.items[0].reporting_date}</dd><dt>{t("done")}</dt><dd>{draft.items[0].done_text}</dd><dt>{t("next")}</dt><dd>{draft.items[0].next_steps}</dd><dt>{t("selectedEvidence")}</dt><dd>{draft.items[0].evidence_refs?.length??0}</dd></dl>
 <button className="primary-button" disabled={busy||(assessmentRequested&&!assessment.data)||(assessment.data?.state==="PENDING"||assessment.data?.state==="STALE")||(warnings.length>0&&!acknowledged)} type="button" onClick={()=>void confirm()}>{warnings.length>0?t("assessment.confirmDespiteWarning"):t("confirm")}</button>{!uncertain&&<button className="secondary-button" disabled={busy} type="button" onClick={()=>{setDraft(null);void context.refetch();}}>{t("edit")}</button>}
 {uncertain&&<p>{t("retry")}</p>}</div>:<form className="mt-4 grid gap-4" onSubmit={prepare}>
 <label>{t("percent")}<input type="number" required min="0" max="100" step="0.0001" value={percent} onChange={e=>setPercent(e.target.value)}/></label>
 <label>{t("remaining")}<input type="number" min="0" max="10000" step="0.0001" placeholder={t("unknown")} value={remaining} onChange={e=>setRemaining(e.target.value)}/></label>
 <label>{t("spent")}<input type="number" min="0" max="24" step="0.0001" placeholder={t("unknown")} value={spent} onChange={e=>setSpent(e.target.value)}/></label>
 <label>{t("date")}<input type="date" required max={ctx.reporting_date} value={date||ctx.reporting_date} onChange={e=>setDate(e.target.value)}/></label>
 <label>{t("done")}<textarea required maxLength={4000} value={done} onChange={e=>setDone(e.target.value)}/></label>
 <label>{t("next")}<textarea maxLength={4000} value={next} onChange={e=>setNext(e.target.value)}/></label>
 <label>{t("reason")}<input required={Boolean(correction)} maxLength={1000} value={reason} onChange={e=>setReason(e.target.value)}/></label>
 {correction&&<p>{t("correcting",{date:correction.item.reporting_date})}</p>}
 <fieldset><legend>{t("selectedEvidence")}</legend>{options.map(ref=><label className="block" key={ref.evidence_id}><input type="checkbox" checked={selected.includes(ref.evidence_id)} onChange={e=>setSelected(values=>e.target.checked?[...values,ref.evidence_id]:values.filter(id=>id!==ref.evidence_id))}/>{ref.evidence_id} · v{ref.version}</label>)}</fieldset>
 <button className="primary-button" disabled={busy} type="submit">{t("review")}</button></form>}
 {!draft&&<EvidencePicker onUploaded={proof=>{setUploaded(values=>[...values,{evidence_id:proof.evidence_id,version:proof.version}]);}}/>}
 {history.error?<p role="alert">{t("historyUnavailable")} <button type="button" onClick={()=>void history.refetch()}>{t("reload")}</button></p>:<DailyUpdateHistory observations={history.data??[]} onCorrect={correct} disabled={busy||Boolean(draft)}/>}
 </section>;
}
