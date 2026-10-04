import { useState } from "react";
import { useTranslations } from "next-intl";
import type { ProposalContent } from "@/features/ai-proposals/contracts";
import styles from "./risk-plan-card.module.css";

type Props = {
  content: ProposalContent;
  version: number;
  stale: boolean;
  canManage: boolean;
  canApprove: boolean;
  validationReasons?: string[];
  onEdit: (content: ProposalContent) => Promise<boolean>;
  onRevise: (instruction: string) => void;
  onApprove: () => void;
  onReject: () => void;
};
export function RiskPlanCard({
  content,
  version,
  stale,
  canManage,
  canApprove,
  validationReasons = [],
  onEdit,
  onRevise,
  onApprove,
  onReject,
}: Props) {
  const t = useTranslations("riskPlan");
  const [editing, setEditing] = useState(false),
    [saving, setSaving] = useState(false),
    [error, setError] = useState(false);
  const [draft, setDraft] = useState(content),
    [revising, setRevising] = useState(false),
    [instruction, setInstruction] = useState("");
  const source = content.risk_replan;
  if (!source) return null;
  const old = new Map(source.before.tasks.map((task) => [task.id, task]));
  const weeks = new Map(source.before.weeks.map((week) => [week.id, week]));
  const weekLabel = (id: string | null) => {
    const week = id ? weeks.get(id) : undefined;
    return week ? t("week", { number: week.week_number }) : t("unknown");
  };
  const rows = draft.tasks.filter((task) => {
    const before = old.get(task.ref);
    return (
      !before ||
      before.project_week_id !== task.project_week_ref ||
      before.due_date !== task.due_date ||
      before.estimated_effort_hours !== task.estimated_effort_hours
    );
  });
  const update = (
    ref: string,
    patch: Partial<ProposalContent["tasks"][number]>,
  ) =>
    setDraft((value) => ({
      ...value,
      tasks: value.tasks.map((task) =>
        task.ref === ref ? { ...task, ...patch } : task,
      ),
    }));
  return (
    <section className={styles.card} aria-label={t("title")}>
      <header className={styles.header}>
        <div>
          <p className={styles.kicker}>
            {t("title")} · {t("version", { version })}
          </p>
          <h3>{content.project.title}</h3>
          <p>{t("hint")}</p>
        </div>
        <span className={styles.badge}>
          {t(stale ? "historical" : "approvalHint")}
        </span>
      </header>
      {stale ? (
        <p className={styles.notice} role="status">
          {t("stale")}
        </p>
      ) : null}
      <div className={styles.diff}>
        <div className={styles.columns}>
          <span>{t("task")}</span>
          <span>{t("before")}</span>
          <span>{t("after")}</span>
        </div>
        {rows.map((task) => {
          const before = old.get(task.ref);
          return (
            <div className={styles.row} key={task.ref}>
              <div>
                <strong>{task.title}</strong>
                <p>
                  {before
                    ? t("taskVersion", { version: before.version })
                    : t("newTask")}
                </p>
                {!before ? (
                  <>
                    <p>{task.description ?? t("unknown")}</p>
                    <ul>
                      {task.acceptance_criteria.map((text, index) => (
                        <li key={index}>{text}</li>
                      ))}
                    </ul>
                  </>
                ) : null}
              </div>
              <div>
                {before ? (
                  <>
                    <strong>{weekLabel(before.project_week_id)}</strong>
                    <p>
                      {t("due")}: {before.due_date ?? t("unknown")}
                    </p>
                    <p>
                      {before.estimated_effort_hours === null
                        ? t("unknown")
                        : t("effort", { hours: before.estimated_effort_hours })}
                    </p>
                  </>
                ) : (
                  <p>{t("newTask")}</p>
                )}
              </div>
              <div className={styles.proposed}>
                {editing ? (
                  <>
                    {!before ? (
                      <label>
                        {t("titleField")}
                        <input
                          value={task.title}
                          onChange={(event) =>
                            update(task.ref, { title: event.target.value })
                          }
                        />
                      </label>
                    ) : null}
                    {!before ? (
                      <>
                        <label>
                          {t("descriptionField")}
                          <textarea
                            value={task.description ?? ""}
                            onChange={(event) =>
                              update(task.ref, {
                                description: event.target.value || null,
                              })
                            }
                          />
                        </label>
                        <label>
                          {t("criteriaField")}
                          <textarea
                            value={task.acceptance_criteria.join("\n")}
                            onChange={(event) =>
                              update(task.ref, {
                                acceptance_criteria:
                                  event.target.value.split("\n"),
                              })
                            }
                          />
                        </label>
                      </>
                    ) : null}
                    <label>
                      {t("weekField")}
                      <select
                        value={task.project_week_ref}
                        onChange={(event) =>
                          update(task.ref, {
                            project_week_ref: event.target.value,
                          })
                        }
                      >
                        {source.before.weeks
                          .filter((w) => w.status !== "COMPLETED")
                          .map((w) => (
                            <option key={w.id} value={w.id}>
                              {t("week", { number: w.week_number })}
                            </option>
                          ))}
                      </select>
                    </label>
                    <label>
                      {t("dueField")}
                      <input
                        type="date"
                        value={task.due_date ?? ""}
                        onChange={(event) =>
                          update(task.ref, {
                            due_date: event.target.value || null,
                          })
                        }
                      />
                    </label>
                    <label>
                      {t("effortField")}
                      <input
                        type="number"
                        min={1}
                        max={10000}
                        value={task.estimated_effort_hours}
                        onChange={(event) =>
                          update(task.ref, {
                            estimated_effort_hours: Number(event.target.value),
                          })
                        }
                      />
                    </label>
                  </>
                ) : (
                  <>
                    <strong>{weekLabel(task.project_week_ref)}</strong>
                    <p>
                      {t("due")}: {task.due_date ?? t("unknown")}
                    </p>
                    <p>{t("effort", { hours: task.estimated_effort_hours })}</p>
                  </>
                )}
              </div>
            </div>
          );
        })}
        {rows.length === 0 ? <p>{t("noChange")}</p> : null}
      </div>
      <p className={styles.policy}>
        {t("unchangedAssignment")} {t("completed")}
      </p>
      <details className={styles.sources}>
        <summary>{t("sources")}</summary>
        <p>{source.binding.risk_assessment_id}</p>
        <p>{t("projectVersion", { version: source.before.project_version })}</p>
        <p>{source.binding.observation_ids.join(", ")}</p>
      </details>
      <div role="status" className={styles.notice}>
        <strong>
          {t(canApprove ? "validationReady" : "validationFailed")}
        </strong>
        {validationReasons.length ? (
          <ul>
            {validationReasons.map((reason) => (
              <li key={reason}>{reason}</li>
            ))}
          </ul>
        ) : null}
      </div>
      {error ? <p role="alert">{t("editFailed")}</p> : null}
      {canManage && !stale ? (
        <footer className={styles.footer}>
          {editing ? (
            <>
              <button
                disabled={saving}
                onClick={() => {
                  setEditing(false);
                  setDraft(content);
                  setError(false);
                }}
              >
                {t("cancel")}
              </button>
              <button
                className={styles.primary}
                disabled={saving || rows.length === 0}
                onClick={() => {
                  setSaving(true);
                  void onEdit(draft)
                    .then((saved) => {
                      setError(!saved);
                      if (saved) setEditing(false);
                    })
                    .finally(() => setSaving(false));
                }}
              >
                {t("save")}
              </button>
            </>
          ) : (
            <>
              <button className={styles.reject} onClick={onReject}>
                {t("reject")}
              </button>
              <button onClick={() => setEditing(true)}>{t("edit")}</button>
              <button onClick={() => setRevising(true)}>{t("askAi")}</button>
              <button
                className={styles.primary}
                disabled={!canApprove}
                onClick={onApprove}
              >
                {t("approve")}
              </button>
            </>
          )}
        </footer>
      ) : null}
      {revising && !stale ? (
        <form
          className={styles.revise}
          onSubmit={(event) => {
            event.preventDefault();
            if (instruction.trim()) {
              onRevise(instruction.trim());
              setRevising(false);
            }
          }}
        >
          <label>
            {t("instruction")}
            <textarea
              value={instruction}
              onChange={(event) => setInstruction(event.target.value)}
              maxLength={8000}
              required
            />
          </label>
          <button type="button" onClick={() => setRevising(false)}>
            {t("cancel")}
          </button>
          <button className={styles.primary} type="submit">
            {t("send")}
          </button>
        </form>
      ) : null}
    </section>
  );
}
