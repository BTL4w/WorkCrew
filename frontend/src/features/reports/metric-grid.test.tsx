import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import { AppLocaleProvider } from "@/shared/i18n/locale-provider";
import { snapshotSchema } from "./contracts";
import { MetricGrid } from "./metric-grid";
const id = "11111111-1111-4111-8111-111111111111";
const metric = (key: string, value: string | null, unit = "HOURS", state = value === null ? "UNKNOWN" : "KNOWN") => ({key,value,unit,state,time_basis:"AT_CAPTURE",policy_version:"report-metrics.v1",source_refs:[],limitations:[]});
it.each(["vi", "en"] as const)("renders catalog with explicit units, subset and unavailable states in %s", locale => {
  const snapshot = snapshotSchema.parse({ id, organization_id:id,project_id:id,report_id:id,captured_at:"2026-10-05T09:00:00Z",snapshot_hash:"a".repeat(64),catalog_version:"report-metrics.v1",query_version:"report-sql.v1",period:{kind:"WEEKLY",local_start:"2026-10-05",local_end:"2026-10-12",timezone:"UTC",start_utc:"2026-10-05T00:00:00Z",end_utc:"2026-10-12T00:00:00Z",observed_through:"2026-10-05T09:00:00Z",partial_period:true},sources:[],receipts:[],limitations:[],metrics:{
    "remaining.known_subtotal_hours":metric("remaining.known_subtotal_hours","14.5"),
    "remaining.total_hours":metric("remaining.total_hours",null),
    "progress.reported_percent":metric("progress.reported_percent","25","PERCENT","PARTIAL"),
    "reporter.coverage":metric("reporter.coverage",null,"FRACTION","NOT_APPLICABLE"),
  }});
  render(<AppLocaleProvider initialLocale={locale}><MetricGrid snapshot={snapshot}/></AppLocaleProvider>);
  expect(screen.getByText(locale === "en" ? "Known remaining subtotal" : "Tổng phụ giờ còn lại đã biết")).toBeVisible();
  expect(screen.getByText(locale === "en" ? "Not applicable" : "Không áp dụng")).toBeVisible();
  expect(screen.getByText(locale === "en" ? "Partial data" : "Dữ liệu một phần")).toBeVisible();
  expect(screen.queryByText("remaining.total_hours")).not.toBeInTheDocument();
  expect(screen.getByText(/14[.,]5/)).toHaveTextContent(locale === "en" ? "hours" : "giờ");
});
