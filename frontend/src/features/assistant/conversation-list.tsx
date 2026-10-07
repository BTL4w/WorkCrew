import { useTranslations } from "next-intl";
import type { ReactNode } from "react";

import type { MeResponse } from "@/shared/api/contracts";
import { LocaleSwitcher } from "@/shared/i18n/locale-switcher";

import type { AssistantConversation } from "./contracts";

export type AssistantNavigationSection = "assistant" | "projects" | "myTasks" | "peopleCapacity" | "evaluations" | "assignTask";

type IconName = "new" | "projects" | "tasks" | "people" | "assign" | "collapse" | "expand" | "chat" | "logout";

export function ConversationList({
  actor,
  conversations,
  selectedId,
  activeSection = "assistant",
  collapsed,
  onSelect,
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
  return <aside className={`assistant-conversations ${collapsed ? "is-collapsed" : ""}`} aria-label={t("conversations.label")}>
    <div className="assistant-sidebar-brand">
      <span className="assistant-brand-icon" aria-hidden="true"><SidebarIcon name="tasks" /></span>
      {!collapsed ? <span className="assistant-brand-name">{t("brand")}</span> : null}
      <button className="assistant-sidebar-toggle" aria-expanded={!collapsed} aria-label={t("conversations.toggle")} type="button" onClick={onToggle}>
        <SidebarIcon name={collapsed ? "expand" : "collapse"} />
      </button>
    </div>

    <nav className="assistant-sidebar-navigation" aria-label={t("navigation.label")}>
      <SidebarAction icon="new" label={t("conversations.new")} collapsed={collapsed} active={activeSection === "assistant" && selectedId === null} onClick={onNew} />
      {onOpenProjects ? <SidebarAction icon="projects" label={t("navigation.projects")} collapsed={collapsed} active={activeSection === "projects"} onClick={onOpenProjects} /> : null}
      {onOpenMyTasks ? <SidebarAction icon="tasks" label={t("navigation.myTasks")} collapsed={collapsed} active={activeSection === "myTasks"} onClick={onOpenMyTasks} /> : null}
      {onOpenEvaluations ? <SidebarAction icon="people" label={t("navigation.evaluations")} collapsed={collapsed} active={activeSection === "evaluations"} onClick={onOpenEvaluations} /> : null}
      {onOpenPeopleCapacity ? <SidebarAction icon="people" label={t("navigation.peopleCapacity")} collapsed={collapsed} active={activeSection === "peopleCapacity"} onClick={onOpenPeopleCapacity} /> : null}
      {onAssignTask ? <SidebarAction icon="assign" label={t("navigation.assignTask")} collapsed={collapsed} active={activeSection === "assignTask"} onClick={onAssignTask} /> : null}
    </nav>

    {!collapsed ? <section className="assistant-history" aria-labelledby="assistant-history-title">
      <h2 id="assistant-history-title">{t("conversations.recent")}</h2>
      <div className="assistant-conversation-items">
        {conversations.length === 0 ? <p>{t("conversations.empty")}</p> : conversations.map((conversation) =>
          <button
            aria-current={activeSection === "assistant" && conversation.id === selectedId ? "page" : undefined}
            className={activeSection === "assistant" && conversation.id === selectedId ? "is-active" : ""}
            key={conversation.id}
            type="button"
            onClick={() => onSelect(conversation.id)}
          ><SidebarIcon name="chat" /><span>{conversation.title ?? t("conversations.untitled")}</span></button>)}
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
  </aside>;
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

function SidebarIcon({ name }: { name: IconName }) {
  const paths: Record<IconName, ReactNode> = {
    new: <><path d="M12 5v14M5 12h14" /><rect x="3" y="3" width="18" height="18" rx="5" /></>,
    projects: <><rect x="3" y="4" width="18" height="16" rx="4" /><path d="M8 9h8M8 13h5" /></>,
    tasks: <><rect x="3" y="3" width="18" height="18" rx="5" /><path d="m8 12 2.2 2.2L16.5 8" /></>,
    people: <><circle cx="9" cy="8" r="3" /><circle cx="17" cy="9" r="2" /><path d="M3.5 20c.5-3.6 2.4-5.5 5.5-5.5s5 1.9 5.5 5.5M14 20c.2-1.8.9-3.1 2.4-3.8" /></>,
    assign: <><circle cx="9" cy="9" r="3" /><path d="M4 20c.5-3.2 2.2-5 5-5 1.2 0 2.2.3 3 .9M17 12v8M13 16h8" /></>,
    collapse: <path d="m14 7-5 5 5 5" />,
    expand: <path d="m10 7 5 5-5 5" />,
    chat: <><path d="M5 18.5 2.8 21v-4.8A8.2 8.2 0 1 1 5 18.5Z" /><path d="M8 11h.01M12 11h.01M16 11h.01" /></>,
    logout: <><path d="M10 5H6a2 2 0 0 0-2 2v10a2 2 0 0 0 2 2h4M14 8l4 4-4 4M9 12h9" /></>,
  };
  return <svg aria-hidden="true" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.8">{paths[name]}</svg>;
}

function initials(name: string) {
  return name.split(/\s+/).filter(Boolean).slice(0, 2).map((part) => part[0]?.toUpperCase()).join("") || "U";
}
