// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { PayPoSummary } from "@/lib/pay/types";

import { renderWithIntl } from "../../../test/intl";
import { navigationMock, routerSpies } from "../../../test/navigation-mock";

vi.mock("@/i18n/navigation", () => navigationMock);
vi.mock("next/navigation", () => ({ useRouter: () => routerSpies }));
vi.mock("@/lib/actions/pay", () => ({ importPurchaseCsv: vi.fn(), endPurchaseOrder: vi.fn() }));

const pay = await import("@/lib/actions/pay");
const { PurchaseOrderList } = await import("../pay/purchase-orders");

function po(patch: Partial<PayPoSummary> = {}): PayPoSummary {
  return {
    id: "55555555-5555-4555-8555-555555555555",
    po_number: "PO-2026-0079",
    vendor_id: "66666666-6666-4666-8666-666666666666",
    vendor_name: "Synthetic Supplies LLC",
    vendor_reference: "VEN-7K2QX9",
    status: "open",
    match_type: "3-way",
    currency: "AED",
    order_date: "2026-09-20",
    department: "IT",
    subtotal: "6197.40",
    line_count: 2,
    receipt_count: 1,
    invoice_count: 0,
    source: "api",
    created_at: "2026-10-14T08:00:00Z",
    ...patch,
  };
}

describe("PurchaseOrderList", () => {
  beforeEach(() => {
    vi.mocked(pay.importPurchaseCsv).mockReset();
    vi.mocked(pay.endPurchaseOrder).mockReset();
  });

  it("lists purchase orders read only, in English and Arabic", () => {
    const { unmount } = renderWithIntl(<PurchaseOrderList items={[po()]} canImport={false} />);
    const row = document.querySelector('[data-po="PO-2026-0079"]');
    expect(row?.textContent).toContain("AED 6197.40");
    expect(row?.textContent).toContain("3-way");
    expect(document.querySelector("[data-po-import]")).toBeNull();
    unmount();
    renderWithIntl(<PurchaseOrderList items={[po({ match_type: "2-way", status: "cancelled" })]} canImport={false} />, "ar");
    expect(screen.getByText("ثنائية")).toBeTruthy();
    expect(screen.getByText("ملغى")).toBeTruthy();
  });

  it("shows the empty state", () => {
    renderWithIntl(<PurchaseOrderList items={[]} canImport={false} />);
    expect(screen.getByText("No purchase orders yet")).toBeTruthy();
  });

  it("imports a CSV and shows every problem with its row", async () => {
    vi.mocked(pay.importPurchaseCsv).mockResolvedValueOnce({
      ok: false,
      code: "invalid_purchase_data",
      problems: [
        { row: 3, field: "quantity", code: "invalid_quantity" },
        { row: 0, field: null, code: "no_rows" },
      ],
    });
    vi.mocked(pay.importPurchaseCsv).mockResolvedValueOnce({ ok: true, created: 2, numbers: ["PO-1", "PO-2"] });
    renderWithIntl(<PurchaseOrderList items={[]} canImport />);
    const file = new File(["po_number\nPO-1\n"], "pos.csv", { type: "text/csv" });
    await userEvent.upload(screen.getByLabelText("CSV file"), file);
    await userEvent.click(screen.getByRole("button", { name: "Import" }));
    await waitFor(() => expect(document.querySelector('[data-import-problem="3:invalid_quantity"]')).not.toBeNull());
    expect(screen.getByText(/Row 3:/)).toBeTruthy();
    expect(screen.getByRole("alert").textContent).toContain("Nothing was imported");
    expect(vi.mocked(pay.importPurchaseCsv)).toHaveBeenCalledWith("orders", "po_number\nPO-1\n");
    await userEvent.click(screen.getByRole("button", { name: "Import" }));
    await waitFor(() => expect(screen.getByText("2 purchase orders imported.")).toBeTruthy());
    expect(routerSpies.refresh).toHaveBeenCalled();
  });

  it("closes an open purchase order with a reason", async () => {
    vi.mocked(pay.endPurchaseOrder).mockResolvedValue({ ok: true, order: {} as never });
    renderWithIntl(<PurchaseOrderList items={[po(), po({ id: "77777777-7777-4777-8777-777777777777", po_number: "PO-2", status: "closed" })]} canImport={false} canClose />);
    // Only the open one can be closed, and cancelling needs the manage permission.
    expect(screen.getAllByRole("button", { name: "Close" })).toHaveLength(1);
    expect(screen.queryByRole("button", { name: "Cancel order" })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.getByRole("button", { name: "Close the purchase order" })).toBeDisabled();
    await userEvent.type(screen.getByLabelText("Reason"), "All delivered.");
    await userEvent.click(screen.getByRole("button", { name: "Close the purchase order" }));
    await waitFor(() => expect(routerSpies.refresh).toHaveBeenCalled());
    expect(vi.mocked(pay.endPurchaseOrder)).toHaveBeenCalledWith(po().id, "close", "All delivered.");
  });

  it("explains a refused cancel, in Arabic too", async () => {
    vi.mocked(pay.endPurchaseOrder).mockResolvedValue({ ok: false, code: "purchase_order_state" });
    renderWithIntl(<PurchaseOrderList items={[po()]} canImport={false} canCancel />, "ar");
    await userEvent.click(screen.getByRole("button", { name: "إلغاء الطلب" }));
    await userEvent.type(screen.getByLabelText("السبب"), "Withdrawn");
    await userEvent.click(screen.getAllByRole("button", { name: "إلغاء الطلب" })[0]!);
    await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("لا يمكن تغيير أمر الشراء"));
    expect(vi.mocked(pay.endPurchaseOrder)).toHaveBeenCalledWith(po().id, "cancel", "Withdrawn");
  });
});
