import { useTranslations } from "next-intl";
import { useEffect, useRef } from "react";

import type { MeResponse } from "@/shared/api/contracts";
import { LocaleSwitcher } from "@/shared/i18n/locale-switcher";

import { ConversationRow, type ManageConversation } from "./conversation-row";

import type { AssistantConversation } from "./contracts";
import { AssistantIcon as SidebarIcon, type AssistantIconName } from "./assistant-icon";

export type AssistantNavigationSection = "assistant" | "projects" | "myTasks" | "peopleCapacity" | "evaluations" | "assignTask";

type IconName = AssistantIconName;

export function ConversationList({
  actor,
  conversations,
  selectedId,
  activeSection = "assistant",
  collapsed,
  onSelect,
  onManage,
  onNew,
  onToggle,
  onOpenProjects,
  onOpenMyTasks,
  onOpenPeopleCapacity,
  onOpenEvaluations,
  onAssignTask,
  isLoggingOut = false,
  logoutError = false,
  onLogout,
}: {
  actor: MeResponse;
  conversations: AssistantConversation[];
  selectedId: string | null;
  activeSection?: AssistantNavigationSection;
  collapsed: boolean;
  onSelect: (id: string) => void;
  onManage?: ManageConversation;
  onNew: () => void;
  onToggle: () => void;
  onOpenProjects?: () => void;
  onOpenMyTasks?: () => void;
  onOpenPeopleCapacity?: () => void;
  onOpenEvaluations?: () => void;
  onAssignTask?: () => void;
  isLoggingOut?: boolean;
  logoutError?: boolean;
  onLogout?: () => void | Promise<void>;
}) {
  const t = useTranslations("assistant");
  const work = useTranslations("work");
  const home = useTranslations("home");
  const sidebarRef = useRef<HTMLElement>(null);
  const toggleRef = useRef<HTMLButtonElement>(null);
  const ordered = conversations.toSorted((left, right) => Number(Boolean(right.is_pinned)) - Number(Boolean(left.is_pinned)));
  const hasPinned = ordered.some((item) => item.is_pinned);
  const hasRecent = ordered.some((item) => !item.is_pinned);
  function dismiss() {
    onToggle();
    toggleRef.current?.focus();
  }
  useEffect(() => {
    if (collapsed || !window.matchMedia) return;
    const media = window.matchMedia("(max-width: 767px)");
    const sidebar = sidebarRef.current;
    const pane = sidebar?.parentElement?.querySelector<HTMLElement>(".assistant-main-pane, .assistant-workspace-pane");
    if (!sidebar || !pane) return;
    const originalInert = Boolean(pane.inert);
    function syncViewport() {
      pane!.inert = media.matches || originalInert;
      if (media.matches) toggleRef.current?.focus();
    }
    function onKeyDown(event: KeyboardEvent) {
      if (!media.matches) return;
      if (event.key === "Escape") {
        if (event.target instanceof HTMLElement && event.target.closest('[data-conversation-controls="open"]')) return;
        event.preventDefault();
        event.stopPropagation();
        onToggle();
        toggleRef.current?.focus();
      } else if (event.key === "Tab") {
        const controls = sidebar!.querySelectorAll<HTMLElement>('button:not(:disabled), a[href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled)');
        const first = controls[0], last = controls[controls.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault(); last?.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault(); first?.focus();
        }
      }
    }
    syncViewport();
    media.addEventListener("change", syncViewport);
    document.addEventListener("keydown", onKeyDown, true);
    return () => {
      pane.inert = originalInert;
      media.removeEventListener("change", syncViewport);
      document.removeEventListener("keydown", onKeyDown, true);
    };
  }, [collapsed, onToggle, activeSection]);
  return <><aside ref={sidebarRef} className={`assistant-conversations ${collapsed ? "is-collapsed" : ""}`} aria-label={t("conversations.label")} onKeyDown={(event) => {
    if (event.key === "Escape" && !collapsed) {
      event.preventDefault();
      dismiss();
    }
  }}>
    <div className="assistant-sidebar-brand">
      <span className="assistant-brand-icon" aria-hidden="true">w</span>
      {!collapsed ? <span className="assistant-brand-name">{t("brand")}</span> : null}
      <button ref={toggleRef} className="assistant-sidebar-toggle" aria-expanded={!collapsed} aria-label={t("conversations.toggle")} type="button" onClick={onToggle}>
        <SidebarIcon name={collapsed ? "expand" : "collapse"} />
      </button>
    </div>

    <nav className="assistant-sidebar-navigation" aria-label={t("navigation.label")}>
      <SidebarAction icon="new" label={t("conversations.new")} collapsed={collapsed} active={activeSection === "assistant" && selectedId === null} onClick={onNew} />
      {onOpenProjects ? <SidebarAction icon="projects" label={t("navigation.projects")} collapsed={collapsed} active={activeSection === "projects"} onClick={onOpenProjects} /> : null}
      {onOpenMyTasks ? <SidebarAction icon="tasks" label={t("navigation.myTasks")} collapsed={collapsed} active={activeSection === "myTasks"} onClick={onOpenMyTasks} /> : null}
      {onOpenEvaluations ? <SidebarAction icon="evaluation" label={t("navigation.evaluations")} collapsed={collapsed} active={activeSection === "evaluations"} onClick={onOpenEvaluations} /> : null}
      {onOpenPeopleCapacity ? <SidebarAction icon="people" label={t("navigation.peopleCapacity")} collapsed={collapsed} active={activeSection === "peopleCapacity"} onClick={onOpenPeopleCapacity} /> : null}
      {onAssignTask ? <SidebarAction icon="assign" label={t("navigation.assignTask")} collapsed={collapsed} active={activeSection === "assignTask"} onClick={onAssignTask} /> : null}
    </nav>

    {!collapsed ? <section className="assistant-history" aria-labelledby={hasPinned ? "assistant-pinned-title" : "assistant-history-title"}>
      <div className="assistant-conversation-items">
        {ordered.flatMap((conversation, index) => [
          ...(index === 0 || Boolean(ordered[index - 1].is_pinned) !== Boolean(conversation.is_pinned) ? [
            <h2 key={conversation.is_pinned ? "pinned-heading" : "recent-heading"}
              id={conversation.is_pinned ? "assistant-pinned-title" : "assistant-history-title"}
              className="assistant-history-heading">{t(conversation.is_pinned ? "conversations.pinned" : "conversations.recent")}</h2>,
          ] : []),
          <ConversationRow key={conversation.id} conversation={conversation}
            active={activeSection === "assistant" && conversation.id === selectedId} onSelect={onSelect}
            onManage={onManage ? async (item, change) => {
              await onManage(item, change);
              if (change === "delete") sidebarRef.current?.querySelector<HTMLButtonElement>(".assistant-sidebar-navigation button")?.focus();
            } : undefined} />,
        ])}
        {!hasRecent ? <h2 key="recent-heading" id="assistant-history-title" className="assistant-history-heading">{t("conversations.recent")}</h2> : null}
        {conversations.length === 0 ? <p>{t("conversations.empty")}</p> : null}
      </div>
    </section> : null}

    <div className="assistant-sidebar-account">
      <div className="assistant-account-identity">
        <span className="assistant-account-avatar" aria-hidden="true">{initials(actor.user.display_name)}</span>
        {!collapsed ? <div className="assistant-account-copy"><p>{actor.user.display_name}</p><span>{actor.user.email}</span></div> : null}
      </div>
      {!collapsed ? <>
        <p className="assistant-account-context">{work(`role.${actor.membership.role}`)} · {actor.membership.organization_name}</p>
        <div className="assistant-account-actions">
          <LocaleSwitcher />
          {onLogout ? <button
            aria-label={isLoggingOut ? home("loggingOut") : home("logout")}
            className="assistant-account-logout"
            disabled={isLoggingOut}
            type="button"
            onClick={() => void onLogout()}
          ><SidebarIcon name="logout" /><span>{isLoggingOut ? home("loggingOut") : home("logout")}</span></button> : null}
        </div>
        {logoutError ? <p className="assistant-account-error" role="alert">{home("logoutError")}</p> : null}
      </> : null}
    </div>
  </aside>{!collapsed ? <button className="assistant-sidebar-backdrop" type="button" aria-label={t("conversations.close")} onClick={dismiss} /> : null}</>;
}

function SidebarAction({ icon, label, collapsed, active = false, onClick }: {
  icon: IconName;
  label: string;
  collapsed: boolean;
  active?: boolean;
  onClick: () => void;
}) {
  return <button
    aria-current={active ? "page" : undefined}
    aria-label={label}
    className={`assistant-sidebar-action ${active ? "is-active" : ""}`}
    title={collapsed ? label : undefined}
    type="button"
    onClick={onClick}
  ><SidebarIcon name={icon} />{!collapsed ? <span>{label}</span> : null}</button>;
}

function initials(name: string) {
  return name.split(/\s+/).filter(Boolean).slice(0, 2).map((part) => part[0]?.toUpperCase()).join("") || "U";
}
