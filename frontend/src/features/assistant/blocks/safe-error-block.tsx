import { useTranslations } from "next-intl";

import type { AssistantBlock } from "../contracts";

type Block = Extract<AssistantBlock, { kind: "safe_error" }>;

export function SafeErrorBlock({ block, onContinueManually }: { block: Block; onContinueManually?: () => void }) {
  const t = useTranslations("assistant");
  const risk = useTranslations("risk.chat");
  return <section className="assistant-block assistant-safe-error" role="alert">
    <h3>{t("error.title")}</h3><p>{block.code === "RISK_CONTEXT_UNAVAILABLE" ? risk("contextUnavailable") : t("error.safe")}</p>
    {block.manual_fallback && block.code !== "RISK_CONTEXT_UNAVAILABLE" ? <p>{block.manual_fallback}</p> : null}
    {onContinueManually ? <button type="button" onClick={onContinueManually}>{t("error.manual")}</button> : null}
  </section>;
}
