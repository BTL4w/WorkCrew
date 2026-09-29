"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useLocale, useTranslations } from "next-intl";
import { useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";

import { ApiError, isDefinitiveMutationRejection } from "@/shared/api/client";
import type { Task } from "@/features/work/contracts";

import {
  createAcceptanceCriterion,
  createDependency,
  createGoal,
  createMilestone,
  createProjectWeek,
  deleteAcceptanceCriterion,
  deleteDependency,
  deleteGoal,
  deleteMilestone,
  deleteProjectWeek,
  getProjectPlanBundle,
  updateAcceptanceCriterion,
  updateDependency,
  updateGoal,
  updateMilestone,
  updateProjectWeek,
} from "./api";
import type {
  AcceptanceCriterion,
  Goal,
  Milestone,
  ProjectPlan,
  ProjectWeek,
  TaskDependency,
} from "./contracts";
import type { ProjectPlanBundle } from "./api";

type Editor =
  | { kind: "goal"; item: Goal | null }
  | { kind: "milestone"; item: Milestone | null }
  | { kind: "week"; item: ProjectWeek | null }
  | { kind: "dependency"; item: TaskDependency | null }
  | { kind: "criterion"; item: AcceptanceCriterion | null }
  | { kind: "delete"; resource: "goal" | "milestone" | "week" | "dependency" | "criterion"; item: Goal | Milestone | ProjectWeek | TaskDependency | AcceptanceCriterion };

type MutationAttempt = { fingerprint: string; key: string };

function useMutationAttempt() {
  const attempt = useRef<MutationAttempt | null>(null);
  return {
    keyFor(payload: unknown) {
      const fingerprint = JSON.stringify(payload);
      if (attempt.current?.fingerprint !== fingerprint) {
        attempt.current = {
          fingerprint,
          key: globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`,
        };
      }
      return attempt.current.key;
    },
    reset() { attempt.current = null; },
  };
}

export function ProjectPlanPanel({
  organizationId,
  actorMembershipId,
  projectId,
  tasks,
  canManage,
  taskId,
}: {
  organizationId: string;
  actorMembershipId: string;
  projectId: string;
  tasks: Task[];
  canManage: boolean;
  taskId?: string;
}) {
  const t = useTranslations("planning");
  const locale = useLocale();
  const queryClient = useQueryClient();
  const planKey = ["planning", organizationId, actorMembershipId, projectId] as const;
  const plan = useQuery({
    queryKey: planKey,
    queryFn: () => getProjectPlanBundle(projectId),
    select: (bundle) => ({ ...bundle.plan, taskOptions: bundle.tasks }),
  });
  const [editor, setEditor] = useState<Editor | null>(null);

  function updatePlan(update: (current: ProjectPlan) => ProjectPlan) {
    queryClient.setQueryData<ProjectPlanBundle>(planKey, (current) => current ? { ...current, plan: update(current.plan) } : current);
    void queryClient.invalidateQueries({ queryKey: ["work", organizationId, actorMembershipId, "weekly-progress", projectId] });
  }

  async function reload() {
    setEditor(null);
    await plan.refetch();
    await queryClient.invalidateQueries({ queryKey: ["work", organizationId, actorMembershipId, "weekly-progress", projectId] });
  }

  if (plan.isPending) return <p className="mt-6 text-sm text-slate-600" role="status">{t("common.loading")}</p>;
  if (plan.error) return <div className="error-message mt-6" role="alert"><p>{t("error.load")}</p><button className="mt-3 font-semibold underline" type="button" onClick={() => void plan.refetch()}>{t("action.retry")}</button></div>;
  if (!plan.data) return null;

  const criteria = taskId
    ? plan.data.acceptance_criteria.filter((criterion) => criterion.task_id === taskId)
    : plan.data.acceptance_criteria;
  const taskOptions = [...plan.data.taskOptions, ...tasks.filter((task) => !plan.data.taskOptions.some((option) => option.id === task.id))];

  return (
    <section className="mt-10 border-t border-[var(--border)] pt-8" aria-labelledby={taskId ? "criteria-title" : "plan-title"}>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h3 id={taskId ? "criteria-title" : "plan-title"} className="text-xl font-semibold">
          {taskId ? t("criteria.title") : t("title")}
        </h3>
        {!canManage ? <span className="status-pill">{t("readOnly")}</span> : null}
      </div>

      {taskId ? (
        <CriteriaSection
          canManage={canManage}
          criteria={criteria}
          onAdd={() => setEditor({ kind: "criterion", item: null })}
          onDelete={(item) => setEditor({ kind: "delete", resource: "criterion", item })}
          onEdit={(item) => setEditor({ kind: "criterion", item })}
        />
      ) : (
        <div className="mt-6 grid gap-6 xl:grid-cols-2">
          <PlanSection title={t("goal.title")} action={canManage ? { label: plan.data.goal ? t("goal.edit") : t("goal.add"), onClick: () => setEditor({ kind: "goal", item: plan.data.goal }) } : undefined}>
            {plan.data.goal ? <ResourceCard title={plan.data.goal.title} description={plan.data.goal.description} onDelete={canManage ? () => setEditor({ kind: "delete", resource: "goal", item: plan.data!.goal! }) : undefined} details={<><p className="mt-3 text-xs font-semibold text-slate-500 uppercase">{t("goal.fields.outcomes")}</p>{plan.data.goal.expected_outcomes.length ? <ul className="mt-1 list-disc pl-5 text-sm text-slate-700">{plan.data.goal.expected_outcomes.map((outcome) => <li key={outcome}>{outcome}</li>)}</ul> : <p className="mt-1 text-sm text-slate-500">{t("goal.noOutcomes")}</p>}<p className="mt-3 text-sm text-slate-600">{t("common.targetDate")}: {plan.data.goal.target_date ? formatPlanDate(plan.data.goal.target_date, locale) : t("common.noTargetDate")}</p></>} /> : <Empty text={t("goal.empty")} />}
          </PlanSection>
          <PlanSection title={t("milestone.title")} action={canManage ? { label: t("milestone.add"), onClick: () => setEditor({ kind: "milestone", item: null }) } : undefined}>
            {plan.data.milestones.length ? plan.data.milestones.toSorted((a, b) => a.position - b.position).map((item) => <ResourceCard key={item.id} title={item.name} description={item.description} onEdit={canManage ? () => setEditor({ kind: "milestone", item }) : undefined} onDelete={canManage ? () => setEditor({ kind: "delete", resource: "milestone", item }) : undefined} details={<><p className="mt-3 text-sm text-slate-600">{t("common.positionValue", { position: item.position })}</p><p className="mt-1 text-sm text-slate-600">{t("common.targetDate")}: {item.target_date ? formatPlanDate(item.target_date, locale) : t("common.noTargetDate")}</p></>} />) : <Empty text={t("milestone.empty")} />}
          </PlanSection>
          <PlanSection title={t("week.title")} action={canManage ? { label: t("week.add"), onClick: () => setEditor({ kind: "week", item: null }) } : undefined}>
            {plan.data.project_weeks.length ? plan.data.project_weeks.toSorted((a, b) => a.week_number - b.week_number).map((item) => <ResourceCard key={item.id} title={t("week.number", { number: item.week_number })} description={item.objective} onEdit={canManage && item.status !== "COMPLETED" ? () => setEditor({ kind: "week", item }) : undefined} onDelete={canManage && item.status !== "COMPLETED" ? () => setEditor({ kind: "delete", resource: "week", item }) : undefined} details={<><p className="mt-3 text-sm text-slate-600">{formatPlanDate(item.start_date, locale)} – {formatPlanDate(item.end_date, locale)}</p><p className="mt-1 text-sm text-slate-600">{t("week.status")}: {t(`week.statuses.${item.status}`)}</p></>} />) : <Empty text={t("week.empty")} />}
          </PlanSection>
          <PlanSection title={t("dependency.title")} action={canManage ? { label: t("dependency.add"), onClick: () => setEditor({ kind: "dependency", item: null }) } : undefined}>
            {plan.data.dependencies.length ? plan.data.dependencies.map((item) => <ResourceCard key={item.id} title={`${taskName(taskOptions, item.predecessor_task_id)} → ${taskName(taskOptions, item.successor_task_id)}`} onEdit={canManage ? () => setEditor({ kind: "dependency", item }) : undefined} onDelete={canManage ? () => setEditor({ kind: "delete", resource: "dependency", item }) : undefined} />) : <Empty text={t("dependency.empty")} />}
          </PlanSection>
          <PlanSection title={t("criteria.title")}>
            {criteria.length ? criteria.map((item) => <ResourceCard key={item.id} title={item.text} description={taskName(taskOptions, item.task_id)} />) : <Empty text={t("criteria.emptyProject")} />}
          </PlanSection>
        </div>
      )}

      {editor?.kind === "goal" ? <GoalDialog item={editor.item} projectId={projectId} onClose={() => setEditor(null)} onReload={reload} onSaved={(goal) => { updatePlan((current) => ({ ...current, goal })); setEditor(null); }} /> : null}
      {editor?.kind === "milestone" ? <MilestoneDialog item={editor.item} projectId={projectId} nextPosition={plan.data.milestones.length + 1} onClose={() => setEditor(null)} onReload={reload} onSaved={(saved) => { updatePlan((current) => ({ ...current, milestones: editor.item ? current.milestones.map((item) => item.id === saved.id ? saved : item) : [...current.milestones, saved] })); setEditor(null); }} /> : null}
      {editor?.kind === "week" ? <ProjectWeekDialog item={editor.item} projectId={projectId} nextWeekNumber={plan.data.project_weeks.length + 1} onClose={() => setEditor(null)} onReload={reload} onSaved={(saved) => { updatePlan((current) => ({ ...current, project_weeks: editor.item ? current.project_weeks.map((item) => item.id === saved.id ? saved : item) : [...current.project_weeks, saved] })); setEditor(null); }} /> : null}
      {editor?.kind === "dependency" ? <DependencyDialog item={editor.item} tasks={taskOptions} onClose={() => setEditor(null)} onReload={reload} onSaved={(saved) => { updatePlan((current) => ({ ...current, dependencies: editor.item ? current.dependencies.map((item) => item.id === saved.id ? saved : item) : [...current.dependencies, saved] })); setEditor(null); }} /> : null}
      {editor?.kind === "criterion" && taskId ? <CriterionDialog item={editor.item} taskId={taskId} nextPosition={criteria.length + 1} onClose={() => setEditor(null)} onReload={reload} onSaved={(saved) => { updatePlan((current) => ({ ...current, acceptance_criteria: editor.item ? current.acceptance_criteria.map((item) => item.id === saved.id ? saved : item) : [...current.acceptance_criteria, saved] })); setEditor(null); }} /> : null}
      {editor?.kind === "delete" ? <DeleteDialog projectId={projectId} editor={editor} onClose={() => setEditor(null)} onReload={reload} onDeleted={() => { const deleted = editor.item.id; updatePlan((current) => ({ ...current, goal: editor.resource === "goal" ? null : current.goal, milestones: current.milestones.filter((item) => item.id !== deleted), project_weeks: current.project_weeks.filter((item) => item.id !== deleted), dependencies: current.dependencies.filter((item) => item.id !== deleted), acceptance_criteria: current.acceptance_criteria.filter((item) => item.id !== deleted) })); setEditor(null); }} /> : null}
    </section>
  );
}

function PlanSection({ title, action, children }: { title: string; action?: { label: string; onClick: () => void }; children: ReactNode }) {
  return <section className="rounded-2xl border border-[var(--border)] p-5"><div className="flex items-center justify-between gap-3"><h4 className="font-semibold">{title}</h4>{action ? <button className="text-button" type="button" onClick={action.onClick}>{action.label}</button> : null}</div><div className="mt-4 space-y-3">{children}</div></section>;
}

function ResourceCard({ title, description, details, onEdit, onDelete }: { title: string; description?: string | null; details?: ReactNode; onEdit?: () => void; onDelete?: () => void }) {
  const t = useTranslations("planning");
  return <article className="rounded-xl bg-slate-50 p-4"><p className="font-medium">{title}</p>{description ? <p className="mt-1 text-sm text-slate-600">{description}</p> : null}{details}{onEdit || onDelete ? <div className="mt-3 flex gap-3">{onEdit ? <button className="text-button" type="button" onClick={onEdit}>{t("action.edit")}</button> : null}{onDelete ? <button className="text-button" type="button" onClick={onDelete}>{t("action.delete")}</button> : null}</div> : null}</article>;
}

function CriteriaSection({ criteria, canManage, onAdd, onEdit, onDelete }: { criteria: AcceptanceCriterion[]; canManage: boolean; onAdd: () => void; onEdit: (item: AcceptanceCriterion) => void; onDelete: (item: AcceptanceCriterion) => void }) {
  const t = useTranslations("planning");
  return <div className="mt-5">{canManage ? <button className="primary-button" type="button" onClick={onAdd}>{t("criteria.add")}</button> : null}<div className="mt-4 space-y-3">{criteria.length ? criteria.toSorted((a, b) => a.position - b.position).map((item) => <article key={item.id} className="rounded-xl border border-[var(--border)] p-4"><p>{item.text}</p>{canManage ? <div className="mt-3 flex gap-3"><button className="text-button" type="button" onClick={() => onEdit(item)}>{t("criteria.edit")}</button><button className="text-button" type="button" onClick={() => onDelete(item)}>{t("criteria.delete")}</button></div> : null}</article>) : <Empty text={t("criteria.empty")} />}</div></div>;
}

function Empty({ text }: { text: string }) { return <p className="text-sm text-slate-500">{text}</p>; }
function taskName(tasks: Task[], id: string) { return tasks.find((task) => task.id === id)?.title ?? id; }
function formatPlanDate(date: string, locale: string) { return new Intl.DateTimeFormat(locale, { timeZone: "UTC" }).format(new Date(`${date}T00:00:00.000Z`)); }

function GoalDialog({ item, projectId, onClose, onReload, onSaved }: { item: Goal | null; projectId: string; onClose: () => void; onReload: () => Promise<void>; onSaved: (item: Goal) => void }) {
  const t = useTranslations("planning");
  const attempt = useMutationAttempt();
  const [title, setTitle] = useState(item?.title ?? "");
  const [description, setDescription] = useState(item?.description ?? "");
  const [outcomes, setOutcomes] = useState(item?.expected_outcomes.join("\n") ?? "");
  const [targetDate, setTargetDate] = useState(item?.target_date ?? "");
  const [issue, setIssue] = useState<unknown>(null);
  const [submitting, setSubmitting] = useState(false);
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!title.trim()) { setIssue("required"); return; }
    const input = { title: title.trim(), description: description.trim() || null, expected_outcomes: outcomes.split("\n").map((value) => value.trim()).filter(Boolean), target_date: targetDate || null };
    setSubmitting(true); setIssue(null);
    try { onSaved((item ? await updateGoal(item.id, input, item.version, attempt.keyFor(input)) : await createGoal({ project_id: projectId, ...input }, attempt.keyFor(input))).data); }
    catch (error) { setIssue(error); if (isDefinitiveMutationRejection(error)) attempt.reset(); }
    finally { setSubmitting(false); }
  }
  return <Dialog title={item ? t("goal.edit") : t("goal.add")} onClose={onClose}><form onSubmit={submit}><Field label={t("goal.fields.title")}><input className="form-input" value={title} onChange={(event) => setTitle(event.target.value)} /></Field><Field label={t("goal.fields.description")}><textarea className="form-input" value={description} onChange={(event) => setDescription(event.target.value)} /></Field><Field label={t("goal.fields.outcomes")}><textarea className="form-input" value={outcomes} onChange={(event) => setOutcomes(event.target.value)} /></Field><Field label={t("common.targetDate")}><input className="form-input" type="date" value={targetDate} onChange={(event) => setTargetDate(event.target.value)} /></Field>{issue ? <Issue error={issue} onReload={onReload} /> : null}<Actions submitting={submitting} onCancel={onClose} save={t("goal.save")} /></form></Dialog>;
}

function MilestoneDialog({ item, projectId, nextPosition, onClose, onReload, onSaved }: { item: Milestone | null; projectId: string; nextPosition: number; onClose: () => void; onReload: () => Promise<void>; onSaved: (item: Milestone) => void }) {
  const t = useTranslations("planning"); const attempt = useMutationAttempt();
  const [name, setName] = useState(item?.name ?? ""); const [description, setDescription] = useState(item?.description ?? ""); const [targetDate, setTargetDate] = useState(item?.target_date ?? ""); const [position, setPosition] = useState(item?.position ?? nextPosition); const [issue, setIssue] = useState<unknown>(null); const [submitting, setSubmitting] = useState(false);
  async function submit(event: FormEvent) { event.preventDefault(); if (!name.trim()) { setIssue("required"); return; } const input = { name: name.trim(), description: description.trim() || null, target_date: targetDate || null, position }; setSubmitting(true); setIssue(null); try { onSaved((item ? await updateMilestone(item.id, input, item.version, attempt.keyFor(input)) : await createMilestone({ project_id: projectId, ...input }, attempt.keyFor(input))).data); } catch (error) { setIssue(error); if (isDefinitiveMutationRejection(error)) attempt.reset(); } finally { setSubmitting(false); } }
  return <Dialog title={item ? t("milestone.edit") : t("milestone.add")} onClose={onClose}><form onSubmit={submit}><Field label={t("milestone.fields.name")}><input className="form-input" value={name} onChange={(event) => setName(event.target.value)} /></Field><Field label={t("milestone.fields.description")}><textarea className="form-input" value={description} onChange={(event) => setDescription(event.target.value)} /></Field><Field label={t("common.targetDate")}><input className="form-input" type="date" value={targetDate} onChange={(event) => setTargetDate(event.target.value)} /></Field><Field label={t("common.position")}><input className="form-input" min={1} type="number" value={position} onChange={(event) => setPosition(Number(event.target.value))} /></Field>{issue ? <Issue error={issue} onReload={onReload} /> : null}<Actions submitting={submitting} onCancel={onClose} save={t("milestone.save")} /></form></Dialog>;
}

function ProjectWeekDialog({ item, projectId, nextWeekNumber, onClose, onReload, onSaved }: { item: ProjectWeek | null; projectId: string; nextWeekNumber: number; onClose: () => void; onReload: () => Promise<void>; onSaved: (item: ProjectWeek) => void }) {
  const t = useTranslations("planning"); const attempt = useMutationAttempt();
  const [weekNumber, setWeekNumber] = useState(item?.week_number ?? nextWeekNumber); const [startDate, setStartDate] = useState(item?.start_date ?? ""); const [endDate, setEndDate] = useState(item?.end_date ?? ""); const [objective, setObjective] = useState(item?.objective ?? ""); const [status, setStatus] = useState<ProjectWeek["status"]>(item?.status ?? "PLANNED"); const [issue, setIssue] = useState<unknown>(null); const [submitting, setSubmitting] = useState(false);
  async function submit(event: FormEvent) { event.preventDefault(); if (!objective.trim() || !startDate || !endDate) { setIssue("required"); return; } const input = { week_number: weekNumber, start_date: startDate, end_date: endDate, objective: objective.trim(), status }; setSubmitting(true); setIssue(null); try { onSaved((item ? await updateProjectWeek(projectId, item.id, input, item.version, attempt.keyFor(input)) : await createProjectWeek(projectId, input, attempt.keyFor(input))).data); } catch (error) { setIssue(error); if (isDefinitiveMutationRejection(error)) attempt.reset(); } finally { setSubmitting(false); } }
  return <Dialog title={item ? t("week.edit") : t("week.add")} onClose={onClose}><form onSubmit={submit}><Field label={t("week.fields.number")}><input className="form-input" min={1} type="number" value={weekNumber} onChange={(event) => setWeekNumber(Number(event.target.value))} /></Field><Field label={t("week.fields.startDate")}><input className="form-input" type="date" value={startDate} onChange={(event) => setStartDate(event.target.value)} /></Field><Field label={t("week.fields.endDate")}><input className="form-input" type="date" value={endDate} onChange={(event) => setEndDate(event.target.value)} /></Field><Field label={t("week.fields.objective")}><textarea className="form-input" value={objective} onChange={(event) => setObjective(event.target.value)} /></Field><Field label={t("week.status")}><select className="form-input" value={status} onChange={(event) => setStatus(event.target.value as ProjectWeek["status"])}>{(["PLANNED", "ACTIVE", "COMPLETED"] as const).map((value) => <option key={value} value={value}>{t(`week.statuses.${value}`)}</option>)}</select></Field>{issue ? <Issue error={issue} onReload={onReload} /> : null}<Actions submitting={submitting} onCancel={onClose} save={t("week.save")} /></form></Dialog>;
}

function DependencyDialog({ item, tasks, onClose, onReload, onSaved }: { item: TaskDependency | null; tasks: Task[]; onClose: () => void; onReload: () => Promise<void>; onSaved: (item: TaskDependency) => void }) {
  const t = useTranslations("planning"); const attempt = useMutationAttempt(); const [predecessor, setPredecessor] = useState(item?.predecessor_task_id ?? ""); const [successor, setSuccessor] = useState(item?.successor_task_id ?? ""); const [issue, setIssue] = useState<unknown>(null); const [submitting, setSubmitting] = useState(false);
  async function submit(event: FormEvent) { event.preventDefault(); if (!predecessor || !successor) { setIssue("required"); return; } const input = { predecessor_task_id: predecessor, successor_task_id: successor }; setSubmitting(true); setIssue(null); try { onSaved((item ? await updateDependency(item.id, input, item.version, attempt.keyFor(input)) : await createDependency(input, attempt.keyFor(input))).data); } catch (error) { setIssue(error); if (isDefinitiveMutationRejection(error)) attempt.reset(); } finally { setSubmitting(false); } }
  return <Dialog title={item ? t("dependency.edit") : t("dependency.add")} onClose={onClose}><form onSubmit={submit}><Field label={t("dependency.fields.predecessor")}><select className="form-input" value={predecessor} onChange={(event) => setPredecessor(event.target.value)}><option value="">{t("common.selectTask")}</option>{tasks.map((task) => <option key={task.id} value={task.id}>{task.title}</option>)}</select></Field><Field label={t("dependency.fields.successor")}><select className="form-input" value={successor} onChange={(event) => setSuccessor(event.target.value)}><option value="">{t("common.selectTask")}</option>{tasks.map((task) => <option key={task.id} value={task.id}>{task.title}</option>)}</select></Field>{issue ? <Issue dependency error={issue} onReload={onReload} /> : null}<Actions submitting={submitting} onCancel={onClose} save={t("dependency.save")} /></form></Dialog>;
}

function CriterionDialog({ item, taskId, nextPosition, onClose, onReload, onSaved }: { item: AcceptanceCriterion | null; taskId: string; nextPosition: number; onClose: () => void; onReload: () => Promise<void>; onSaved: (item: AcceptanceCriterion) => void }) {
  const t = useTranslations("planning"); const attempt = useMutationAttempt(); const [text, setText] = useState(item?.text ?? ""); const [position, setPosition] = useState(item?.position ?? nextPosition); const [issue, setIssue] = useState<unknown>(null); const [submitting, setSubmitting] = useState(false);
  async function submit(event: FormEvent) { event.preventDefault(); if (!text.trim()) { setIssue("required"); return; } const input = { text: text.trim(), position }; setSubmitting(true); setIssue(null); try { onSaved((item ? await updateAcceptanceCriterion(item.id, input, item.version, attempt.keyFor(input)) : await createAcceptanceCriterion({ task_id: taskId, ...input }, attempt.keyFor(input))).data); } catch (error) { setIssue(error); if (isDefinitiveMutationRejection(error)) attempt.reset(); } finally { setSubmitting(false); } }
  return <Dialog title={item ? t("criteria.edit") : t("criteria.add")} onClose={onClose}><form onSubmit={submit}><Field label={t("criteria.fields.text")}><textarea className="form-input" value={text} onChange={(event) => setText(event.target.value)} /></Field><Field label={t("common.position")}><input className="form-input" min={1} type="number" value={position} onChange={(event) => setPosition(Number(event.target.value))} /></Field>{issue ? <Issue error={issue} onReload={onReload} /> : null}<Actions submitting={submitting} onCancel={onClose} save={t("criteria.save")} /></form></Dialog>;
}

function DeleteDialog({ projectId, editor, onClose, onReload, onDeleted }: { projectId: string; editor: Extract<Editor, { kind: "delete" }>; onClose: () => void; onReload: () => Promise<void>; onDeleted: () => void }) {
  const t = useTranslations("planning"); const attempt = useMutationAttempt(); const [issue, setIssue] = useState<unknown>(null); const [submitting, setSubmitting] = useState(false);
  async function remove() { const key = attempt.keyFor({ resource: editor.resource, id: editor.item.id, version: editor.item.version }); setSubmitting(true); setIssue(null); try { if (editor.resource === "goal") await deleteGoal(editor.item.id, editor.item.version, key); if (editor.resource === "milestone") await deleteMilestone(editor.item.id, editor.item.version, key); if (editor.resource === "week") await deleteProjectWeek(projectId, editor.item.id, editor.item.version, key); if (editor.resource === "dependency") await deleteDependency(editor.item.id, editor.item.version, key); if (editor.resource === "criterion") await deleteAcceptanceCriterion(editor.item.id, editor.item.version, key); onDeleted(); } catch (error) { setIssue(error); if (isDefinitiveMutationRejection(error)) attempt.reset(); } finally { setSubmitting(false); } }
  return <Dialog title={t("delete.title")} onClose={onClose}><p>{t("delete.description")}</p>{issue ? <Issue error={issue} onReload={onReload} /> : null}<div className="mt-6 flex justify-end gap-3"><button className="secondary-button" type="button" onClick={onClose}>{t("action.cancel")}</button><button className="primary-button" disabled={submitting} type="button" onClick={() => void remove()}>{t("delete.confirm")}</button></div></Dialog>;
}

function Dialog({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  const t = useTranslations("planning"); const dialog = useRef<HTMLElement>(null);
  useEffect(() => { const previous = document.activeElement as HTMLElement | null; dialog.current?.focus(); const keydown = (event: KeyboardEvent) => { if (event.key === "Escape") onClose(); if (event.key !== "Tab" || !dialog.current) return; const focusable = Array.from(dialog.current.querySelectorAll<HTMLElement>("button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [href], [tabindex]:not([tabindex='-1'])")); if (!focusable.length) return; const first = focusable[0]; const last = focusable.at(-1)!; if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); } else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); } }; document.addEventListener("keydown", keydown); return () => { document.removeEventListener("keydown", keydown); previous?.focus(); }; }, [onClose]);
  return <div className="fixed inset-0 z-20 grid place-items-center bg-slate-950/50 p-4" role="presentation"><section ref={dialog} tabIndex={-1} role="dialog" aria-modal="true" aria-label={title} className="max-h-[90vh] w-full max-w-2xl overflow-auto rounded-3xl bg-white p-6 shadow-xl"><div className="flex items-center justify-between"><h3 className="text-xl font-semibold">{title}</h3><button className="text-button" aria-label={t("action.close")} type="button" onClick={onClose}>×</button></div><div className="mt-5">{children}</div></section></div>;
}
function Field({ label, children }: { label: string; children: ReactNode }) { return <label className="mb-4 block text-sm font-medium">{label}<span className="mt-2 block">{children}</span></label>; }
function Actions({ submitting, onCancel, save }: { submitting: boolean; onCancel: () => void; save: string }) { const t = useTranslations("planning"); return <div className="mt-6 flex justify-end gap-3"><button className="secondary-button" type="button" onClick={onCancel}>{t("action.cancel")}</button><button className="primary-button" disabled={submitting} type="submit">{submitting ? t("common.saving") : save}</button></div>; }
function Issue({ error, dependency = false, onReload }: { error: unknown; dependency?: boolean; onReload: () => Promise<void> }) { const t = useTranslations("planning"); const notice = useRef<HTMLDivElement>(null); useEffect(() => notice.current?.focus(), []); const stale = error instanceof ApiError && error.code === "RESOURCE_VERSION_MISMATCH"; return <div ref={notice} role="alert" tabIndex={-1} className="error-message"><p>{stale ? t("error.stale") : dependency ? t("error.dependency") : typeof error === "string" ? t("error.required") : t("error.mutation")}</p>{stale ? <button className="mt-3 font-semibold underline" type="button" onClick={() => void onReload()}>{t("action.reload")}</button> : null}</div>; }
