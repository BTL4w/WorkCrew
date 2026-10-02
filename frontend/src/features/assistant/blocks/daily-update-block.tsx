"use client";

import { useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useState } from "react";
import { useAuth } from "@/features/auth/auth-provider";
import { DailyUpdateForm } from "@/features/daily-updates/daily-update-form";
import { getDailyDraft } from "@/features/daily-updates/reporting-api";
import type { AssistantBlock } from "../contracts";

type Block = Extract<AssistantBlock, { kind: "daily_update" }>;

export function DailyUpdateBlock({ block }: { block: Block }) {
  const t = useTranslations("dailyUpdate");
  const { actor } = useAuth();
  const [dismissed, setDismissed] = useState(false);
  const query = useQuery({
    queryKey: [
      "work", actor?.membership.organization_id, actor?.membership.id,
      "chat-daily-draft", block.draft_id, block.draft_version,
    ],
    queryFn: () => getDailyDraft(block.draft_id),
    enabled: Boolean(actor) && !dismissed,
    retry: false,
  });

  if (dismissed) return null;
  if (query.isPending) return <p role="status">{t("loading")}</p>;
  if (!query.data || !actor) return <p role="alert">{t("error.FORBIDDEN")}</p>;

  const draft = query.data;
  if (draft.confirmed_update_id) return <p role="status">{t("saved")}</p>;
  if (
    draft.version !== block.draft_version || draft.items.length !== 1 ||
    draft.items[0].task_id !== block.task_id
  ) {
    return <p role="alert">{t("chat.stale")}</p>;
  }

  return (
    <section className="assistant-block">
      <p>{t("chat.review")}</p>
      <DailyUpdateForm
        key={`${draft.id}:${draft.version}`}
        taskId={block.task_id}
        taskVersion={block.task_version}
        organizationId={actor.membership.organization_id}
        actorMembershipId={actor.membership.id}
        initialDraft={draft}
      />
      <button type="button" onClick={() => setDismissed(true)}>
        {t("chat.discard")}
      </button>
    </section>
  );
}
