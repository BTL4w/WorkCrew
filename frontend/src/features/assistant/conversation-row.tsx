import { useTranslations } from "next-intl";
import { useEffect, useId, useRef, useState, type CSSProperties } from "react";

import { AssistantIcon } from "./assistant-icon";
import type { AssistantConversation } from "./contracts";

export type ConversationChange = { title: string } | { is_pinned: boolean } | "delete";
export type ManageConversation = (conversation: AssistantConversation, change: ConversationChange) => Promise<void>;

export function ConversationRow({ conversation, active, onSelect, onManage }: {
  conversation: AssistantConversation;
  active: boolean;
  onSelect: (id: string) => void;
  onManage?: ManageConversation;
}) {
  const t = useTranslations("assistant.conversations");
  const title = conversation.title ?? t("untitled");
  const [menu, setMenu] = useState(false);
  const [mode, setMode] = useState<"rename" | "delete" | null>(null);
  const [draft, setDraft] = useState(title);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  const restoreFocus = useRef(false);
  const [travel, setTravel] = useState(0);
  const titleWindow = useRef<HTMLSpanElement>(null);
  const titleText = useRef<HTMLSpanElement>(null);
  const row = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const input = useRef<HTMLInputElement>(null);
  const cancel = useRef<HTMLButtonElement>(null);
  const menuId = useId();
  useEffect(() => {
    if (!menu) return;
    row.current?.querySelector<HTMLElement>('[role="menuitem"]')?.focus();
    const dismiss = (event: PointerEvent) => {
      if (!row.current?.contains(event.target as Node)) setMenu(false);
    };
    document.addEventListener("pointerdown", dismiss);
    return () => document.removeEventListener("pointerdown", dismiss);
  }, [menu]);
  useEffect(() => { if (mode === "rename") { input.current?.focus(); input.current?.select(); } }, [mode]);
  useEffect(() => { if (mode === "delete") cancel.current?.focus(); }, [mode]);
  useEffect(() => {
    if (restoreFocus.current && !busy) { trigger.current?.focus(); restoreFocus.current = false; }
  }, [busy, mode, menu]);
  function close() { setMenu(false); setMode(null); setFailed(false); restoreFocus.current = true; }
  async function manage(change: ConversationChange) {
    if (!onManage || busy) return;
    setBusy(true); setFailed(false); setTravel(0);
    try { await onManage(conversation, change); close(); }
    catch { setFailed(true); }
    finally { setBusy(false); }
  }
  function reveal() {
    setTravel(Math.max(0, (titleText.current?.scrollWidth ?? 0) - (titleWindow.current?.clientWidth ?? 0)));
  }
  return <div ref={row} className={`assistant-conversation-row ${active ? "is-active" : ""}`}
    data-conversation-controls={menu || mode ? "open" : undefined}
    onKeyDown={(event) => {
      if (event.key === "Escape" && (menu || mode)) { event.preventDefault(); event.stopPropagation(); if (!busy) close(); }
    }}>
    <div className="assistant-conversation-line">
      <button className="assistant-conversation-select" type="button" aria-current={active ? "page" : undefined}
        aria-label={title}
        onMouseEnter={reveal} onMouseLeave={() => setTravel(0)} onFocus={reveal} onBlur={() => setTravel(0)}
        onClick={() => { setTravel(0); onSelect(conversation.id); }}>
        <span ref={titleWindow} className={`assistant-conversation-title-window ${travel > 0 ? "is-scrolling" : ""}`}
          style={{ "--title-distance": `${-travel}px`, "--title-duration": `${Math.max(5, travel / 28 + 3) / 2}s` } as CSSProperties}>
          <span ref={titleText} className="assistant-conversation-title-text">{title}</span>
        </span>
      </button>
      {onManage ? <div className={`assistant-conversation-actions ${conversation.is_pinned || menu || mode ? "is-visible" : ""}`}>
        <button aria-label={conversation.is_pinned ? t("unpin") : t("pin")} aria-pressed={conversation.is_pinned}
          title={conversation.is_pinned ? t("unpin") : t("pin")} type="button" disabled={busy}
          onClick={() => void manage({ is_pinned: !conversation.is_pinned })}><AssistantIcon name="pin" /></button>
        <button ref={trigger} aria-label={t("options")} title={t("options")} aria-haspopup="menu" aria-expanded={menu} aria-controls={menu ? menuId : undefined}
          type="button" disabled={busy} onClick={() => { setTravel(0); setMode(null); setMenu((value) => !value); }}><AssistantIcon name="more" /></button>
      </div> : null}
    </div>
    {menu ? <div id={menuId} role="menu" aria-label={t("options")} className="assistant-conversation-menu" onKeyDown={(event) => {
      if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
      event.preventDefault(); const buttons = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="menuitem"]'));
      const current = buttons.indexOf(document.activeElement as HTMLButtonElement);
      const next = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1 : (current + (event.key === "ArrowUp" ? -1 : 1) + buttons.length) % buttons.length;
      buttons[next]?.focus();
    }}>
      <button role="menuitem" type="button" onClick={() => { setDraft(title); setMenu(false); setMode("rename"); setFailed(false); }}><AssistantIcon name="edit" />{t("rename")}</button>
      <button role="menuitem" className="is-danger" type="button" onClick={() => { setMenu(false); setMode("delete"); setFailed(false); }}><AssistantIcon name="trash" />{t("delete")}</button>
    </div> : null}
    {mode === "rename" ? <form className="assistant-conversation-edit" onSubmit={(event) => { event.preventDefault(); if (draft.trim()) void manage({ title: draft.trim() }); }}>
      <label>{t("titleLabel")}<input ref={input} value={draft} maxLength={120} required disabled={busy} onChange={(event) => setDraft(event.target.value)} /></label>
      <div><button type="button" disabled={busy} onClick={close}>{t("cancel")}</button><button type="submit" disabled={busy || !draft.trim()}>{busy ? t("saving") : t("save")}</button></div>
    </form> : null}
    {mode === "delete" ? <div className="assistant-conversation-edit" role="group" aria-label={t("delete")}>
      <p>{t("deleteConfirm")}</p><div><button ref={cancel} type="button" disabled={busy} onClick={close}>{t("cancel")}</button><button className="is-danger" type="button" disabled={busy} onClick={() => void manage("delete")}>{busy ? t("saving") : t("confirmDelete")}</button></div>
    </div> : null}
    {failed ? <p className="assistant-conversation-error" role="alert">{t("mutationError")}</p> : null}
  </div>;
}
