"use client";

import { useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useEffect, useMemo, useRef, useState } from "react";

import { listPersonSkills, listSkills, listWeeklyWorkload } from "@/features/people-capacity/api";
import { listProjectWeeks } from "@/features/planning/api";
import { assignTask, listAllMembers } from "@/features/work/api";
import type { ExplicitAssignmentResponse, Task } from "@/features/work/contracts";
import { ApiError } from "@/shared/api/client";

import { getProjectTeam } from "./api";

function mutationKey() {
  return globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`;
}

export function TaskAssignmentControl({ task, onAssigned, onOpenTeam, onReloadTask }: {
  task: Task;
  onAssigned: (task: Task) => void;
  onOpenTeam: () => void;
  onReloadTask: () => void | Promise<void>;
}) {
  const t = useTranslations("projectTeam.assignment");
  const trigger = useRef<HTMLButtonElement>(null);
  const memberSelect = useRef<HTMLSelectElement>(null);
  const inFlight = useRef(false);
  const attempt = useRef<{ fingerprint: string; key: string } | null>(null);
  const [open, setOpen] = useState(false);
  const [selectedMemberId, setSelectedMemberId] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [successName, setSuccessName] = useState<string | null>(null);
  const [error, setError] = useState<{ kind: "stale" | "generic"; taskVersion: number } | null>(null);
  const [committedWorkload, setCommittedWorkload] = useState<ExplicitAssignmentResponse | null>(null);

  const team = useQuery({ queryKey: ["project-team", task.project_id], queryFn: () => getProjectTeam(task.project_id), enabled: open });
  const members = useQuery({ queryKey: ["active-members", task.project_id], queryFn: listAllMembers, enabled: open });
  const weeks = useQuery({ queryKey: ["project-weeks", task.project_id], queryFn: () => listProjectWeeks(task.project_id), enabled: open && task.project_week_id !== null });
  const weekStart = weeks.data?.find((week) => week.id === task.project_week_id)?.start_date;
  const workload = useQuery({ queryKey: ["assignment-workload", weekStart], queryFn: () => listWeeklyWorkload(weekStart!), enabled: open && Boolean(weekStart) });
  const skills = useQuery({ queryKey: ["skills", "assignment"], queryFn: listSkills, enabled: open && Boolean(selectedMemberId) });
  const personSkills = useQuery({ queryKey: ["person-skills", selectedMemberId, "assignment"], queryFn: () => listPersonSkills(selectedMemberId), enabled: open && Boolean(selectedMemberId) });

  const approvedMembers = useMemo(() => {
    const approvedIds = new Set(team.data?.memberships.filter((item) => item.active).map((item) => item.membership_id) ?? []);
    return members.data?.filter((member) => member.is_active && approvedIds.has(member.membership_id)) ?? [];
  }, [members.data, team.data]);
  const selectedMember = approvedMembers.find((member) => member.membership_id === selectedMemberId);
  const selectedWorkload = workload.data?.find((item) => item.membership_id === selectedMemberId && item.project_week_id === task.project_week_id);
  const projectedBeforeHours = selectedWorkload?.allocated_effort_hours ?? 0;
  const addedHours = task.status !== "DONE" && task.assignee?.membership_id !== selectedMemberId ? task.estimated_effort_hours ?? 0 : 0;
  const projectedAfterHours = projectedBeforeHours + addedHours;
  const hasProjectedWorkload = Boolean(weekStart && selectedWorkload);
  const beforeHours = committedWorkload?.workload_before_hours ?? projectedBeforeHours;
  const afterHours = committedWorkload?.workload_after_hours ?? projectedAfterHours;
  const effectiveCapacity = committedWorkload?.effective_capacity_hours ?? selectedWorkload?.effective_capacity_hours ?? 0;
  const hasWorkload = committedWorkload !== null || hasProjectedWorkload;
  const overloaded = committedWorkload
    ? committedWorkload.warnings.some((warning) => warning.code === "ASSIGNEE_OVER_CAPACITY")
    : hasProjectedWorkload && projectedAfterHours > effectiveCapacity;

  useEffect(() => {
    if (open && !team.isPending && !members.isPending) memberSelect.current?.focus();
  }, [members.isPending, open, team.isPending]);

  function close() {
    setOpen(false);
    setSelectedMemberId("");
    setError(null);
    queueMicrotask(() => trigger.current?.focus());
  }

  function keyForAssignment() {
    const fingerprint = `${task.id}:${task.version}:${selectedMemberId}`;
    if (attempt.current?.fingerprint !== fingerprint) attempt.current = { fingerprint, key: mutationKey() };
    return attempt.current.key;
  }

  async function submit() {
    if (!selectedMember || inFlight.current) return;
    inFlight.current = true;
    setSubmitting(true);
    setError(null);
    setSuccessName(null);
    try {
      const result = await assignTask(task.id, selectedMember.membership_id, task.version, keyForAssignment());
      setCommittedWorkload(result.data);
      setSuccessName(selectedMember.display_name);
      onAssigned(result.data.task);
    } catch (caught) {
      if (caught instanceof ApiError && caught.code === "RESOURCE_VERSION_MISMATCH") setError({ kind: "stale", taskVersion: task.version });
      else setError({ kind: "generic", taskVersion: task.version });
    } finally {
      inFlight.current = false;
      setSubmitting(false);
    }
  }

  const skillNames = new Map(skills.data?.map((skill) => [skill.id, skill.name]));
  const visibleError = error?.taskVersion === task.version ? error.kind : null;

  return (
    <section className="task-assignment mt-8" aria-label={t("title")} onKeyDown={(event) => { if (event.key === "Escape" && open) close(); }}>
      <button ref={trigger} aria-expanded={open} className="secondary-button" type="button" onClick={() => { setOpen(true); setSuccessName(null); }}>
        {t("action.open")}
      </button>
      {open ? <div className="task-assignment-panel mt-4 rounded-xl border border-slate-300 p-5">
        {team.isPending || members.isPending || (task.project_week_id !== null && weeks.isPending) ? <p role="status">{t("loading")}</p>
          : team.error || members.error || (task.project_week_id !== null && weeks.error) ? <p className="error-message" role="alert">{t("error.load")}</p>
          : approvedMembers.length === 0 ? <div><p>{t("emptyTeam")}</p><button className="text-button mt-3" type="button" onClick={onOpenTeam}>{t("action.openTeam")}</button></div>
          : <>
            <label className="block text-sm font-medium">{t("memberLabel")}<select ref={memberSelect} className="form-input mt-2" value={selectedMemberId} onChange={(event) => { setSelectedMemberId(event.target.value); setError(null); setSuccessName(null); setCommittedWorkload(null); attempt.current = null; }}><option value="">{t("selectMember")}</option>{approvedMembers.map((member) => <option key={member.membership_id} value={member.membership_id}>{member.display_name}</option>)}</select></label>
            {selectedMember ? <div className="mt-4 space-y-2">
              {weekStart && workload.isPending ? <p role="status">{t("loadingWorkload")}</p> : hasWorkload ? <p>{t("workload", { before: beforeHours, after: afterHours, capacity: effectiveCapacity })}</p> : <p>{t("workloadUnavailable")}</p>}
              {overloaded ? <p className="text-amber-800" role="status">{t("overloadWarning")}</p> : null}
              {personSkills.isPending || skills.isPending ? <p role="status">{t("loadingSkills")}</p> : personSkills.data?.filter((item) => item.active).length ? <ul>{personSkills.data.filter((item) => item.active).map((item) => <li key={item.id}>{t("skillSummary", { skill: skillNames.get(item.skill_id) ?? t("unknownSkill"), level: item.level, evidence: item.evidence.length })}</li>)}</ul> : <p>{t("noSkills")}</p>}
              <p className="text-sm text-slate-600">{t("confirmation", { name: selectedMember.display_name, task: task.title })}</p>
              <button className="primary-button" disabled={submitting} type="button" onClick={() => void submit()}>{submitting ? t("action.assigning") : t("action.confirm", { name: selectedMember.display_name })}</button>
            </div> : null}
          </>}
        {successName ? <p className="mt-4 text-emerald-800" role="status">{t("success", { name: successName })}</p> : null}
        {visibleError === "generic" ? <p className="error-message mt-4" role="alert">{t("error.mutation")}</p> : null}
        {visibleError === "stale" ? <div className="mt-4" role="alert"><p>{t("error.stale")}</p><button className="secondary-button mt-3" type="button" onClick={() => void onReloadTask()}>{t("action.reload")}</button></div> : null}
        <button className="text-button mt-4 ml-3" type="button" onClick={close}>{t("action.cancel")}</button>
      </div> : null}
    </section>
  );
}
