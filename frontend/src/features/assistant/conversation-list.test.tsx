import { useState } from "react";
import { fireEvent, screen, waitFor } from "@testing-library/react";
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

const chat = {
  id: "11111111-1111-4111-8111-111111111111", locale: "vi" as const,
  title: "Một tiêu đề rất dài cần xem đầy đủ trong sidebar", status: "ACTIVE",
  version: 1, is_pinned: false, last_message_sequence: 0, last_event_sequence: 0,
  created_at: "2026-10-10T00:00:00Z", updated_at: "2026-10-10T00:00:00Z",
};

function ManagedSidebar() {
  const [items, setItems] = useState([chat]);
  return <ConversationList actor={managerActor} conversations={items} selectedId={null}
    collapsed={false} onToggle={() => undefined} onNew={() => undefined} onSelect={() => undefined}
    onManage={async (conversation, change) => {
      setItems((current) => change === "delete" ? current.filter((item) => item.id !== conversation.id)
        : current.map((item) => item.id === conversation.id ? { ...item, ...change } : item));
    }} />;
}

it("scrolls an overflowing title within its row on hover and supports pinning", async () => {
  renderWithAppProviders(<ManagedSidebar />);
  const button = screen.getByRole("button", { name: chat.title });
  const window = button.querySelector(".assistant-conversation-title-window")!;
  const text = button.querySelector(".assistant-conversation-title-text")!;
  expect(window).not.toBeNull();
  Object.defineProperty(window, "clientWidth", { value: 120 });
  Object.defineProperty(text, "scrollWidth", { value: 360 });
  fireEvent.mouseEnter(button);
  expect(window).toHaveClass("is-scrolling");
  expect(window).toHaveStyle({ "--title-distance": "-240px", "--title-duration": `${Math.max(5, 240 / 28 + 3) / 2}s` });
  expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
  fireEvent.mouseLeave(button);
  expect(window).not.toHaveClass("is-scrolling");
  fireEvent.click(screen.getByRole("button", { name: "Ghim chat" }));
  expect(await screen.findByRole("button", { name: "Bỏ ghim chat" })).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "Đã ghim" })).toBeInTheDocument();
});

it("renames a chat through its menu and asks before deleting it", async () => {
  renderWithAppProviders(<ManagedSidebar />);
  fireEvent.click(screen.getByRole("button", { name: "Tùy chọn chat" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "Đổi tên" }));
  fireEvent.change(screen.getByLabelText("Tên cuộc trò chuyện"), { target: { value: "Tên mới" } });
  fireEvent.click(screen.getByRole("button", { name: "Lưu" }));
  expect(await screen.findByRole("button", { name: "Tên mới" })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Tùy chọn chat" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "Xóa chat" }));
  expect(screen.getByRole("button", { name: "Tên mới" })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Xác nhận xóa" }));
  expect(await screen.findByText("Chưa có cuộc trò chuyện.")).toBeInTheDocument();
});

it("keeps the chat and edit form when saving fails", async () => {
  renderWithAppProviders(<ConversationList actor={managerActor} conversations={[chat]} selectedId={chat.id}
    collapsed={false} onToggle={() => undefined} onNew={() => undefined} onSelect={() => undefined}
    onManage={async () => { throw new Error("Unavailable"); }} />);
  fireEvent.click(screen.getByRole("button", { name: "Tùy chọn chat" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "Đổi tên" }));
  fireEvent.change(screen.getByLabelText("Tên cuộc trò chuyện"), { target: { value: "Không mất bản nháp" } });
  fireEvent.click(screen.getByRole("button", { name: "Lưu" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Không thể cập nhật chat");
  expect(screen.getByLabelText("Tên cuộc trò chuyện")).toHaveValue("Không mất bản nháp");
  expect(screen.getByRole("button", { name: chat.title })).toBeInTheDocument();
});

it("places pinned chats first and navigates the menu with the keyboard", () => {
  renderWithAppProviders(<ConversationList actor={managerActor}
    conversations={[chat, { ...chat, id: "22222222-2222-4222-8222-222222222222", title: "Ghim ở đầu", is_pinned: true }]}
    selectedId={null} collapsed={false} onToggle={() => undefined} onNew={() => undefined} onSelect={() => undefined}
    onManage={async () => undefined} />);
  const history = document.querySelector(".assistant-conversation-items")!;
  expect(history.children[0]).toHaveTextContent("Đã ghim");
  expect(history.children[1]).toHaveTextContent("Ghim ở đầu");
  expect(history.children[2]).toHaveTextContent("Gần đây");
  expect(history.children[3]).toHaveTextContent(chat.title);
  fireEvent.click(screen.getAllByRole("button", { name: "Tùy chọn chat" })[0]);
  expect(screen.getByRole("menuitem", { name: "Đổi tên" })).toHaveFocus();
  fireEvent.keyDown(screen.getByRole("menuitem", { name: "Đổi tên" }), { key: "ArrowDown" });
  expect(screen.getByRole("menuitem", { name: "Xóa chat" })).toHaveFocus();
  fireEvent.keyDown(screen.getByRole("menuitem", { name: "Xóa chat" }), { key: "Escape" });
  expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  expect(screen.getAllByRole("button", { name: "Tùy chọn chat" })[0]).toHaveFocus();
});

it("focuses cancel when opening delete confirmation and returns to options after rename", async () => {
  renderWithAppProviders(<ManagedSidebar />);
  fireEvent.click(screen.getByRole("button", { name: "Tùy chọn chat" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "Xóa chat" }));
  expect(screen.getByRole("button", { name: "Hủy" })).toHaveFocus();
  fireEvent.keyDown(screen.getByRole("button", { name: "Hủy" }), { key: "Escape" });
  expect(screen.getByRole("button", { name: "Tùy chọn chat" })).toHaveFocus();
  fireEvent.click(screen.getByRole("button", { name: "Tùy chọn chat" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "Đổi tên" }));
  fireEvent.change(screen.getByLabelText("Tên cuộc trò chuyện"), { target: { value: "Đổi xong" } });
  fireEvent.click(screen.getByRole("button", { name: "Lưu" }));
  await screen.findByRole("button", { name: "Đổi xong" });
  await waitFor(() => expect(screen.getByRole("button", { name: "Tùy chọn chat" })).toHaveFocus());
});
