import { useLocale, useTranslations } from "next-intl";
import type { AssistantBlock } from "../contracts";
import styles from "./retrieval-answer-block.module.css";

type Block = Extract<AssistantBlock, {kind: "retrieval_answer"}>;
const knownLimitations: Record<string, string> = {
  SOURCE_FRESHNESS_UNKNOWN: "freshnessUnknown",
  SYNTHESIS_UNAVAILABLE: "synthesisUnavailable",
  MODEL_UNAVAILABLE: "synthesisUnavailable",
  MODEL_TIMEOUT: "synthesisUnavailable",
  MODEL_RATE_LIMITED: "synthesisUnavailable",
  MODEL_INVALID_OUTPUT: "synthesisUnavailable",
  MODEL_USAGE_EXCEEDED: "synthesisUnavailable",
  REQUIRED_COVERAGE_INCOMPLETE: "partial",
};

export function RetrievalAnswerBlock({block}: {block: Block}) {
  const t = useTranslations("assistant.retrieval");
  const locale = useLocale();
  const date = (value: string) => new Intl.DateTimeFormat(locale, {dateStyle: "medium", timeStyle: "short", timeZone: "UTC"}).format(new Date(value));
  if (block.availability !== "AVAILABLE") return <section className={`assistant-block ${styles.card}`} aria-label={t("title")}>
    <p role="status">{t(block.availability === "EXPIRED" ? "expired" : block.outcome === "SOURCE_UNAVAILABLE" ? "outcome.SOURCE_UNAVAILABLE" : "unavailable")}</p>
  </section>;
  return <section className={`assistant-block ${styles.card}`} aria-label={t("title")}>
    <h3>{t(`outcome.${block.outcome}`)}</h3>
    {block.claims.length ? <ul className={styles.claims}>{block.claims.map((claim, index) => <li key={index}>
      <span>{claim.text}</span>{" "}
      {claim.evidence_ids.map(id => {
        const number = block.sources.findIndex(source => source.evidence_id === id) + 1;
        const source = block.sources[number - 1];
        return <a key={id} href={source.canonical_url} target="_blank" rel="noopener noreferrer" aria-label={t("citation", {number, title: source.title})}>[{number}]</a>;
      })}
    </li>)}</ul> : null}
    {block.limitations.length ? <ul className={styles.limitations}>{[...new Set(block.limitations.map(item => knownLimitations[item] ? t(knownLimitations[item]) : /^[A-Z][A-Z0-9_]+$/.test(item) ? t("limited") : item))].map(item => <li key={item}>{item}</li>)}</ul> : null}
    <ol className={styles.sources}>{block.sources.map(source => <li key={source.evidence_id}>
      <a href={source.canonical_url} target="_blank" rel="noopener noreferrer">{source.title}</a>
      <p className={styles.meta}>{t(source.method === "PAGE_CONTENT" ? "pageContent" : "searchExcerpt")}</p>
      <p className={styles.excerpt}>{source.excerpt}</p>
      <p className={styles.meta}>{source.published_at ? t("published", {date: date(source.published_at)}) : t("publishedUnknown")}</p>
      <p className={styles.meta}>{source.source_fetched_at ? t("fetched", {date: date(source.source_fetched_at)}) : t("fetchedUnknown")}</p>
    </li>)}</ol>
    <p className={styles.meta}>{t("observed", {date: date(block.observed_at)})} UTC</p>
  </section>;
}
