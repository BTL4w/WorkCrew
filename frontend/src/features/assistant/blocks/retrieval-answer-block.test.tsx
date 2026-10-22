import { render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { describe, expect, it } from "vitest";
import en from "@/shared/i18n/messages/en.json";
import vi from "@/shared/i18n/messages/vi.json";
import { retrievalFixture } from "@/test/retrieval";
import { assistantBlockSchema } from "../contracts";
import { RetrievalAnswerBlock } from "./retrieval-answer-block";

function show(value = retrievalFixture(), locale: "vi" | "en" = "en") {
  const block = assistantBlockSchema.parse(value);
  if (block.kind !== "retrieval_answer") throw new Error("Wrong fixture");
  return render(<NextIntlClientProvider locale={locale} messages={locale === "en" ? en : vi}><RetrievalAnswerBlock block={block}/></NextIntlClientProvider>);
}

describe("Retrieval answer", () => {
  it.each(["vi", "en"] as const)("links verified claims and labels unknown source dates in %s", locale => {
    show(retrievalFixture(), locale);
    const citation = screen.getByRole("link", {name: `${locale === "vi" ? "Nguồn" : "Source"} 1: Public planning guide`});
    expect(citation).toHaveAttribute("href", "https://example.org/guide");
    expect(citation).toHaveAttribute("rel", "noopener noreferrer");
    expect(screen.getByText(locale === "vi" ? "Trích đoạn tìm kiếm" : "Search excerpt")).toBeVisible();
    expect(screen.getByText(locale === "vi" ? "Chưa rõ ngày xuất bản" : "Publication date unknown")).toBeVisible();
    expect(screen.queryByText(/just updated|vừa cập nhật/i)).not.toBeInTheDocument();
  });
  it("shows evidence and fallback reason when synthesis is unavailable", () => {
    show({...retrievalFixture(), outcome: "EVIDENCE_READY", claims: [], limitations: ["SYNTHESIS_UNAVAILABLE"]});
    expect(screen.getByText("Verified sources")).toBeVisible();
    expect(screen.getByText("A summary is unavailable. The verified excerpts are shown below.")).toBeVisible();
    expect(screen.getByText("Review the goal and constraints.")).toBeVisible();
  });
  it.each([["NOT_FOUND", "No matching sources found"], ["SOURCE_UNAVAILABLE", "Web sources are unavailable"]])("distinguishes %s", (outcome, text) => {
    show({...retrievalFixture(), outcome, claims: [], sources: [], limitations: []});
    expect(screen.getByText(text)).toBeVisible();
  });
  it("renders source titles and content as text without images or HTML", () => {
    const value = retrievalFixture();
    value.sources[0].title = '<img src="https://evil.example/tracker" onerror="alert(1)">';
    const view = show(value);
    expect(screen.getByRole("link", {name: value.sources[0].title})).toBeVisible();
    expect(view.container.querySelector("img")).toBeNull();
  });
  it("shows an expired placeholder without citations or stale content", () => {
    show({...retrievalFixture(), availability: "EXPIRED", claims: [], sources: [], limitations: []});
    expect(screen.getByText("These sources have expired. Search again for current evidence.")).toBeVisible();
    expect(screen.queryAllByRole("link")).toHaveLength(0);
    expect(screen.queryByText("Review the goal and constraints.")).not.toBeInTheDocument();
  });
});
