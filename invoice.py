"""Render a bill into a self-contained HTML invoice.

Pure formatting only: it takes the same per-deliverable bill lines the Billing
view already computes (``DeliverableBillingLine``) plus the fixed billing
details (``InvoiceSettings``) and lays them out like the paper invoices, so the
generated total always equals the Billing view for the same period. No database
or UI access here, which keeps it easy to unit test.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from html import escape
from typing import TYPE_CHECKING

from models import InvoiceSettings

if TYPE_CHECKING:
    from storage import DeliverableBillingLine


def _money(value: Decimal) -> str:
    """Format a Decimal as a thousands-separated 2dp string (no symbol)."""
    return f"{value:,.2f}"


def _address_html(address: str, postcode: str) -> str:
    """Render a newline-separated address block plus postcode as <br> lines."""
    lines = [line for line in address.splitlines() if line.strip()]
    if postcode.strip():
        lines.append(postcode)
    return "<br>".join(escape(line) for line in lines)


def render_invoice_html(
    settings: InvoiceSettings,
    lines: list[DeliverableBillingLine],
    *,
    invoice_number: int,
    invoice_date: date,
    due_date: date,
    services_month: date,
    point_rate: Decimal,
    vat_rate: Decimal,
) -> str:
    """Build a complete HTML invoice document.

    ``lines`` are the per-deliverable bill lines (quantity = points, net
    subtotal = amount_ex_vat). Totals are summed straight from those lines so
    they match the Billing view exactly.
    """
    net_total = sum((line.amount_ex_vat for line in lines), Decimal("0"))
    vat_total = sum(
        (line.amount_inc_vat - line.amount_ex_vat for line in lines),
        Decimal("0"),
    )
    grand_total = sum((line.amount_inc_vat for line in lines), Decimal("0"))

    vat_pct = (vat_rate * 100).quantize(Decimal("1"))

    row_html = []
    for line in lines:
        details = (
            f"Work package {line.work_package_id}, "
            f"deliverable {line.deliverable_id or 'UNLINKED'}: "
            f"{line.deliverable_title}"
        )
        row_html.append(
            "<tr>"
            f'<td class="qty">{int(line.points)}</td>'
            f"<td>{escape(details)}</td>"
            f'<td class="num">{_money(point_rate)}</td>'
            f'<td class="num">{vat_pct}%</td>'
            f'<td class="num">{_money(line.amount_ex_vat)}</td>'
            "</tr>"
        )

    inv_no = f"{invoice_number:03d}"

    return f"""<!DOCTYPE html>
<html lang="en-GB">
<head>
<meta charset="utf-8">
<title>Invoice {inv_no}</title>
<style>
  body {{ font-family: Arial, Helvetica, sans-serif; color: #222;
          max-width: 820px; margin: 2rem auto; padding: 0 1rem;
          font-size: 14px; line-height: 1.4; }}
  .top {{ display: flex; justify-content: space-between; }}
  .company {{ text-align: right; }}
  .company .name {{ font-weight: bold; }}
  hr {{ border: none; border-top: 2px solid #8b0000; margin: 1.2rem 0; }}
  h1 {{ color: #222; font-size: 1.6rem; margin: 0 0 0.2rem; }}
  .muted {{ color: #777; }}
  .parties {{ display: flex; justify-content: space-between; margin: 1rem 0; }}
  .invoice-meta {{ text-align: right; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 1rem; }}
  th {{ color: #8b0000; text-align: left; font-size: 0.75rem;
        text-transform: uppercase; border-bottom: 2px solid #8b0000;
        padding: 0.4rem 0.5rem; }}
  td {{ padding: 0.5rem; vertical-align: top; border-bottom: 1px solid #eee; }}
  th.num, td.num {{ text-align: right; }}
  td.qty {{ width: 3rem; }}
  .totals {{ width: 100%; margin-top: 1rem; }}
  .totals td {{ border: none; }}
  .totals td.label {{ text-align: right; color: #555; }}
  .totals td.value {{ text-align: right; width: 8rem; font-weight: bold; }}
  .totals tr.grand td {{ font-size: 1.2rem; color: #8b0000;
                         padding-top: 0.6rem; }}
  .foot {{ display: flex; justify-content: space-between; margin-top: 2rem; }}
  .foot .label {{ font-weight: bold; }}
  .delivered {{ color: #777; margin-top: 2rem; text-align: center; }}
</style>
</head>
<body>
  <div class="top">
    <div></div>
    <div class="company">
      <div class="name">{escape(settings.company_name)}</div>
      <div>{_address_html(settings.company_address, settings.company_postcode)}</div>
      <div style="margin-top:0.6rem">VAT: {escape(settings.company_vat_number)}</div>
    </div>
  </div>

  <hr>

  <div class="parties">
    <div class="customer">
      <div style="font-weight:bold">{escape(settings.customer_name)}</div>
      <div style="margin-top:0.4rem">
        {_address_html(settings.customer_address, settings.customer_postcode)}
      </div>
    </div>
    <div class="invoice-meta">
      <h1>INVOICE {inv_no}</h1>
      <div><strong>{invoice_date.strftime('%d %B %Y')}</strong></div>
      <div class="muted">Payment due by {due_date.strftime('%d %B %Y')}</div>
    </div>
  </div>

  <table>
    <thead>
      <tr>
        <th>Quantity</th>
        <th>Details</th>
        <th class="num">Unit Price (£)</th>
        <th class="num">VAT</th>
        <th class="num">Net Subtotal (£)</th>
      </tr>
    </thead>
    <tbody>
      {"".join(row_html)}
    </tbody>
  </table>

  <table class="totals">
    <tr>
      <td class="label">Net Total</td>
      <td class="value">{_money(net_total)}</td>
    </tr>
    <tr>
      <td class="label">VAT</td>
      <td class="value">{_money(vat_total)}</td>
    </tr>
    <tr class="grand">
      <td class="label">GBP TOTAL</td>
      <td class="value">£{_money(grand_total)}</td>
    </tr>
  </table>

  <hr>

  <div class="foot">
    <div class="payment">
      <div class="label">Payment Details</div>
      <div><strong>Bank/Sort Code:</strong> {escape(settings.bank_sort_code)}</div>
      <div><strong>Account Number:</strong> {escape(settings.bank_account_number)}</div>
      <div><strong>Payment Reference:</strong> {inv_no}</div>
    </div>
    <div class="other">
      <div class="label">Other Information</div>
      <div><strong>Company Registration Number:</strong><br>{escape(settings.company_reg_number)}</div>
      <div><strong>Contract/PO:</strong> {escape(settings.contract_po)}</div>
    </div>
  </div>

  <div class="delivered">Services delivered in {services_month.strftime('%B %Y')}</div>
</body>
</html>
"""
