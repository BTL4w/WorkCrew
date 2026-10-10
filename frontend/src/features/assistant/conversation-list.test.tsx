import { useState } from "react";
import { fireEvent, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { managerActor, renderWithAppProviders } from "@/test/render";
import { ConversationList } from "./conversation-list";

function Sidebar() {
  const [collapsed, setCollapsed] = useState(false);
  return <div><ConversationList actor={managerActor} conversations={[]} selectedId={null}
    collapsed={collapsed} onToggle={() => setCollapsed((value) => !value)}
    onNew={() => undefined} onSelect={() => undefined} /><main className="assistant-main-pane"><input aria-label="Message" /></main></div>;
}

describe("conversation navigation dismissal", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("keeps mobile keyboard navigation inside the drawer and restores the pane on Escape", () => {
    vi.stubGlobal("matchMedia", () => ({ matches: true, addEventListener() {}, removeEventListener() {} }));
    renderWithAppProviders(<Sidebar />);
    const toggle = screen.getByRole("button", { name: "Ẩn hoặc hiện danh sách cuộc trò chuyện" });
    expect(toggle).toHaveFocus();
    const pane = screen.getByRole("main");
    expect(pane.inert).toBe(true);
    fireEvent.keyDown(toggle, { key: "Tab", shiftKey: true });
    expect(screen.getByRole("button", { name: "en" })).toHaveFocus();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(pane.inert).toBe(false);
    expect(toggle).toHaveFocus();
  });

  it("closes with Escape and returns focus to the navigation toggle", () => {
    renderWithAppProviders(<Sidebar />);
    const toggle = screen.getByRole("button", { name: "Ẩn hoặc hiện danh sách cuộc trò chuyện" });
    const newConversation = screen.getByRole("button", { name: "Cuộc trò chuyện mới" });
    newConversation.focus();
    fireEvent.keyDown(newConversation, { key: "Escape" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(toggle).toHaveFocus();
  });

  it("dismisses the drawer through its backdrop without losing the reopen control", () => {
    renderWithAppProviders(<Sidebar />);
    fireEvent.click(screen.getByRole("button", { name: "Đóng danh sách cuộc trò chuyện" }));
    const toggle = screen.getByRole("button", { name: "Ẩn hoặc hiện danh sách cuộc trò chuyện" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("heading", { name: "Gần đây" })).toBeInTheDocument();
  });
});
