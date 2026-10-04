"use client";
import { useQuery } from "@tanstack/react-query";
import { useRef, useState, type FormEvent } from "react";
import { useTranslations, useLocale } from "next-intl";
import { isDefinitiveMutationRejection } from "@/shared/api/client";
import {
  getSchedule,
  previewSchedule,
  confirmSchedule,
  pauseSchedule,
} from "./api";
import { commandSchema, type ScheduleCommand, type ScheduleDraft, type ScheduleView } from "./contracts";

export function DailySummarySettings({
  projectId,
  organizationId,
  membershipId,
}: {
  projectId: string;
  organizationId: string;
  membershipId: string;
}) {
  const t = useTranslations("automations");
  const query = useQuery({
    queryKey: ["daily-summary", organizationId, membershipId, projectId],
    queryFn: () => getSchedule(projectId),
  });
  if (query.isPending) return <p role="status">{t("loading")}</p>;
  if (!query.data)
    return (
      <div role="alert">
        <p>{t("loadError")}</p>
        <button
          className="secondary-button"
          onClick={() => void query.refetch()}
        >
          {t("retry")}
        </button>
      </div>
    );
  return (
    <ScheduleEditor
      key={`${projectId}:${query.data.schedule?.version ?? 0}`}
      projectId={projectId}
      view={query.data}
      refresh={async () => {
        await query.refetch();
      }}
    />
  );
}

function ScheduleEditor({
  projectId,
  view,
  refresh,
}: {
  projectId: string;
  view: ScheduleView;
  refresh: () => Promise<void>;
}) {
  const t = useTranslations("automations"),
    locale = useLocale();
  const [command, setCommand] = useState<ScheduleCommand>(
    commandSchema.parse(view.schedule ?? {
      project_id: projectId,
      timezone:
        Intl.DateTimeFormat().resolvedOptions().timeZone || "Asia/Ho_Chi_Minh",
      weekdays: [1, 2, 3, 4, 5],
      cutoff: "17:00",
      recipients: [],
      send_when_complete: true,
      partial_at_cutoff: true,
    }),
  );
  const [draft, setDraft] = useState<ScheduleDraft | null>(null),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(false);
  const attempts = useRef(new Map<string, { payload: string; key: string }>());
  const instant = (value: string) =>
    new Intl.DateTimeFormat(locale, {
      dateStyle: "medium",
      timeStyle: "short",
      timeZone: command.timezone,
    }).format(new Date(value));
  const field = <K extends keyof ScheduleCommand>(
    key: K,
    value: ScheduleCommand[K],
  ) => setCommand((current) => ({ ...current, [key]: value }));
  async function mutate<T>(
    kind: string,
    payload: unknown,
    action: (key: string) => Promise<T>,
  ): Promise<T | null> {
    const serialized = JSON.stringify(payload);
    let attempt = attempts.current.get(kind);
    if (!attempt || attempt.payload !== serialized) {
      attempt = { payload: serialized, key: crypto.randomUUID() };
      attempts.current.set(kind, attempt);
    }
    setBusy(true);
    setError(false);
    try {
      const result = await action(attempt.key);
      attempts.current.delete(kind);
      return result;
    } catch (caught) {
      setError(true);
      if (isDefinitiveMutationRejection(caught)) attempts.current.delete(kind);
      return null;
    } finally {
      setBusy(false);
    }
  }
  async function preview(event: FormEvent) {
    event.preventDefault();
    const result = await mutate("preview", command, (key) =>
      previewSchedule(command, view.schedule?.version ?? 0, key),
    );
    if (result) setDraft(result);
  }
  async function confirm() {
    if (!draft) return;
    const result = await mutate("confirm", draft.id, (key) =>
      confirmSchedule(draft.id, draft.expected_version, key),
    );
    if (result) {
      setDraft(null);
      await refresh();
    }
  }
  async function pause() {
    const saved = view.schedule;
    if (!saved) return;
    const result = await mutate(
      "pause",
      { version: saved.version, paused: !saved.paused },
      (key) => pauseSchedule(saved.id, saved.version, !saved.paused, key),
    );
    if (result) await refresh();
  }
  return (
    <section
      className="mt-8 rounded-2xl border border-slate-200 bg-white p-6"
      aria-label={t("title")}
    >
      <header className="flex items-start justify-between gap-4">
        <div>
          <p className="eyebrow">{t("eyebrow")}</p>
          <h3 className="mt-2 text-xl font-semibold">{t("title")}</h3>
          <p className="mt-2 text-sm text-slate-600">{t("hint")}</p>
        </div>
        {view.schedule ? (
          <button
            type="button"
            className="secondary-button"
            disabled={busy}
            onClick={() => void pause()}
          >
            {t(view.schedule.paused ? "resume" : "pause")}
          </button>
        ) : null}
      </header>
      {view.window ? (
        <aside className="mt-5 rounded-xl bg-slate-50 p-4 text-sm">
          <p className="font-semibold">
            {t("window", {
              date: view.window.local_date,
              version: view.window.applied_version,
            })}
          </p>
          <p>
            {t("coverage", {
              received: view.window.reported_members.length,
              total: view.window.expected_reporters.length,
            })}
          </p>
          <p>
            {view.window.coverage_state === "NO_REPORTERS"
              ? t("noReporters")
              : t("cutoffAt", {
                  time: new Intl.DateTimeFormat(locale, {
                    dateStyle: "medium",
                    timeStyle: "short",
                    timeZone: view.window.timezone,
                  }).format(new Date(view.window.cutoff_at)),
                })}
          </p>
          {view.window.scope_changed ? (
            <p className="mt-2 text-amber-800">{t("scopeChanged")}</p>
          ) : null}
          {view.schedule?.paused ? <p className="mt-2">{t("paused")}</p> : null}
        </aside>
      ) : null}
      {error ? (
        <p className="mt-4 text-red-800" role="alert">
          {t("error")}
        </p>
      ) : null}
      {draft ? (
        <div className="mt-6 rounded-xl border border-blue-200 bg-blue-50 p-5">
          <h4 className="font-semibold">{t("confirmTitle")}</h4>
          <dl className="mt-3 grid gap-2">
            <div>
              <dt>{t("timezone")}</dt>
              <dd>{draft.command.timezone}</dd>
            </div>
            <div>
              <dt>{t("weekdays")}</dt>
              <dd>
                {draft.command.weekdays.map((day) => t(`day${day}`)).join(", ")}
              </dd>
            </div>
            <div>
              <dt>{t("cutoff")}</dt>
              <dd>{draft.command.cutoff}</dd>
            </div>
            <div>
              <dt>{t("recipients")}</dt>
              <dd>
                {draft.command.recipients
                  .map(
                    (id) =>
                      view.recipients.find((r) => r.membership_id === id)
                        ?.name ?? t("unavailableRecipient"),
                  )
                  .join(", ")}
              </dd>
            </div>
            <div>
              <dt>{t("complete")}</dt>
              <dd>
                {t(draft.command.send_when_complete ? "enabled" : "disabled")}
              </dd>
            </div>
            <div>
              <dt>{t("partial")}</dt>
              <dd>
                {t(draft.command.partial_at_cutoff ? "enabled" : "disabled")}
              </dd>
            </div>
            <div>
              <dt>{t("effective")}</dt>
              <dd>{instant(draft.effective_at)}</dd>
            </div>
          </dl>
          <p className="mt-3 text-sm">{t("nextWindow")}</p>
          <div className="mt-4 flex gap-3">
            <button
              className="secondary-button"
              disabled={busy}
              onClick={() => {
                setDraft(null);
                setError(false);
              }}
            >
              {t("cancel")}
            </button>
            <button
              className="primary-button"
              disabled={busy}
              onClick={() => void confirm()}
            >
              {t("confirm")}
            </button>
          </div>
        </div>
      ) : (
        <form
          onSubmit={(event) => void preview(event)}
          className="mt-6 grid gap-5"
        >
          <div className="grid gap-4 sm:grid-cols-2">
            <label className="grid gap-2 text-sm font-medium">
              {t("timezone")}
              <input
                className="rounded-lg border border-slate-300 p-3"
                required
                value={command.timezone}
                onChange={(event) => field("timezone", event.target.value)}
              />
            </label>
            <label className="grid gap-2 text-sm font-medium">
              {t("cutoff")}
              <input
                type="time"
                className="rounded-lg border border-slate-300 p-3"
                required
                value={command.cutoff}
                onChange={(event) => field("cutoff", event.target.value)}
              />
            </label>
          </div>
          <fieldset>
            <legend className="mb-2 text-sm font-medium">
              {t("weekdays")}
            </legend>
            <div className="flex flex-wrap gap-3">
              {[1, 2, 3, 4, 5, 6, 7].map((day) => (
                <label
                  className="flex gap-2 rounded-lg border border-slate-200 p-3 text-sm"
                  key={day}
                >
                  <input
                    type="checkbox"
                    checked={command.weekdays.includes(day)}
                    onChange={(event) =>
                      field(
                        "weekdays",
                        event.target.checked
                          ? [...command.weekdays, day].sort()
                          : command.weekdays.filter((value) => value !== day),
                      )
                    }
                  />
                  {t(`day${day}`)}
                </label>
              ))}
            </div>
          </fieldset>
          <fieldset>
            <legend className="mb-2 text-sm font-medium">
              {t("recipients")}
            </legend>
            <div className="flex flex-wrap gap-3">
              {command.recipients
                .filter((id) => !view.recipients.some((recipient) => recipient.membership_id === id))
                .map((id) => (
                  <div key={id} className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm">
                    <p>{t("unavailableRecipient")}</p>
                    <button type="button" className="secondary-button mt-2"
                      onClick={() => field("recipients", command.recipients.filter((value) => value !== id))}>
                      {t("removeUnavailableRecipient")}
                    </button>
                  </div>
                ))}
              {view.recipients.map((recipient) => (
                <label
                  key={recipient.membership_id}
                  className="flex gap-2 rounded-lg border border-slate-200 p-3 text-sm"
                >
                  <input
                    type="checkbox"
                    checked={command.recipients.includes(
                      recipient.membership_id,
                    )}
                    onChange={(event) =>
                      field(
                        "recipients",
                        event.target.checked
                          ? [...command.recipients, recipient.membership_id]
                          : command.recipients.filter(
                              (id) => id !== recipient.membership_id,
                            ),
                      )
                    }
                  />
                  {recipient.name}
                </label>
              ))}
            </div>
          </fieldset>
          <label className="flex gap-2 text-sm">
            <input
              type="checkbox"
              checked={command.send_when_complete}
              onChange={(event) =>
                field("send_when_complete", event.target.checked)
              }
            />
            {t("complete")}
          </label>
          <label className="flex gap-2 text-sm">
            <input
              type="checkbox"
              checked={command.partial_at_cutoff}
              onChange={(event) =>
                field("partial_at_cutoff", event.target.checked)
              }
            />
            {t("partial")}
          </label>
          <div>
            <button
              className="primary-button"
              disabled={
                busy ||
                command.weekdays.length === 0 ||
                command.recipients.length === 0
              }
            >
              {t("preview")}
            </button>
          </div>
        </form>
      )}
    </section>
  );
}
