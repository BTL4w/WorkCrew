import {render, screen, fireEvent, waitFor} from "@testing-library/react";
import {it, expect, vi} from "vitest";
import {AppLocaleProvider} from "@/shared/i18n/locale-provider";
import {FeedbackForm} from "./feedback-form";
it("labels feedback as advisory and binds the exact version with stable retry",async()=>{
 const record=vi.fn().mockRejectedValueOnce(new TypeError("offline")).mockResolvedValueOnce({});
 render(<AppLocaleProvider initialLocale="en"><FeedbackForm reportId="report" versionId="version" record={record}/></AppLocaleProvider>);
 expect(screen.getByText("Advisory feedback does not approve or publish this report.")).toBeVisible();
 fireEvent.change(screen.getByLabelText("Feedback reason"),{target:{value:"Needs context"}});
 fireEvent.click(screen.getByRole("button",{name:"Send feedback"}));await screen.findByRole("alert");
 fireEvent.click(screen.getByRole("button",{name:"Send feedback"}));await waitFor(()=>expect(screen.getByRole("status")).toHaveTextContent("Feedback saved"));
 expect(record.mock.calls[0][0]).toEqual({report_id:"report",report_version_id:"version",decision:"ACCEPT",reason:"Needs context"});
 expect(record.mock.calls[0][1]).toBe(record.mock.calls[1][1]);
});
