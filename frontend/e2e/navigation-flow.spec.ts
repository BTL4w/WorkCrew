import { expect, test } from "@playwright/test";

const conversationId = "77777777-7777-4777-8777-777777777777";
const conversation = {
  id: conversationId, locale: "vi", title: "Chat trước đây", status: "ACTIVE",
  last_message_sequence: 0, last_event_sequence: 0,
  created_at: "2026-08-13T10:00:00Z", updated_at: "2026-08-13T10:01:00Z",
};

for (const role of ["MANAGER", "EMPLOYEE"] as const) {
  test(`fresh entry, menu URLs, refresh and conversation history (${role})`, async ({ page }) => {
    const snapshotRequests: string[] = [];
    await page.route("**/api/v1/**", async (route) => {
      const path = new URL(route.request().url()).pathname;
      if (path === "/api/v1/me") return route.fulfill({ json: {
        user: { id: "11111111-1111-4111-8111-111111111111", email: "demo@example.test", display_name: "Demo" },
        membership: { id: "22222222-2222-4222-8222-222222222222", organization_id: "33333333-3333-4333-8333-333333333333", organization_name: "Demo", role },
      } });
      if (path === "/api/v1/ai/conversations") return route.fulfill({ json: { items: [conversation] } });
      if (path === `/api/v1/ai/conversations/${conversationId}`) {
        snapshotRequests.push(path);
        return route.fulfill({ json: { conversation, messages: [] } });
      }
      if (path === "/api/v1/projects" || path === "/api/v1/my-tasks") {
        return route.fulfill({ json: { items: [], page: 1, page_size: 20, total: 0 } });
      }
      return route.fulfill({ status: 503, json: { error: { code: "SERVICE_UNAVAILABLE", message_key: "common.error.unavailable", request_id: "navigation-test", field_errors: [], details: {} } } });
    });

    await page.goto("/");
    await expect(page.getByRole("heading", { name: "Tôi có thể giúp gì cho bạn?" })).toBeVisible();
    await expect(page.getByRole("button", { name: conversation.title, exact: true })).toBeVisible();
    expect(snapshotRequests).toHaveLength(0);
    await page.getByRole("button", { name: "Projects", exact: true }).click();
    await expect(page).toHaveURL(/\?view=projects$/);
    await expect(page.getByRole("heading", { name: "Projects", exact: true })).toBeVisible();
    await page.reload();
    await expect(page.getByRole("heading", { name: "Projects", exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Task của tôi", exact: true }).click();
    await expect(page).toHaveURL(/\?view=myTasks$/);
    await page.goBack();
    await expect(page.getByRole("heading", { name: "Projects", exact: true })).toBeVisible();
    await page.goForward();
    await expect(page.getByRole("heading", { name: "Task của tôi", exact: true })).toBeVisible();
    await page.getByRole("button", { name: conversation.title, exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`\\?conversation=${conversationId}$`));
    await expect.poll(() => snapshotRequests.length).toBeGreaterThan(0);
    await page.reload();
    await expect(page.getByRole("button", { name: conversation.title, exact: true })).toHaveAttribute("aria-current", "page");
    await page.getByRole("button", { name: "Cuộc trò chuyện mới", exact: true }).click();
    await expect(page).toHaveURL(/\/$/);
    await expect(page.getByRole("button", { name: conversation.title, exact: true })).not.toHaveAttribute("aria-current");
    await page.goBack();
    await expect(page).toHaveURL(new RegExp(`\\?conversation=${conversationId}$`));
    await expect(page.getByRole("button", { name: conversation.title, exact: true })).toHaveAttribute("aria-current", "page");
    await page.goForward();
    await expect(page).toHaveURL(/\/$/);
    await expect(page.getByRole("button", { name: conversation.title, exact: true })).not.toHaveAttribute("aria-current");
  });
}
