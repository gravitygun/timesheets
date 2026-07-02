"""Tests for invoice.py - HTML invoice rendering."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import invoice
from models import InvoiceSettings
from storage import DeliverableBillingLine


def _line(
    del_id: str | None,
    wp_id: str,
    title: str,
    points: int,
    ex_vat: str,
) -> DeliverableBillingLine:
    ex = Decimal(ex_vat)
    return DeliverableBillingLine(
        deliverable_id=del_id,
        deliverable_title=title,
        work_package_id=wp_id,
        work_package_title="",
        hours=Decimal(points) * Decimal("2"),
        points=Decimal(points),
        amount_ex_vat=ex,
        amount_inc_vat=ex * Decimal("1.20"),
    )


def _render(lines: list[DeliverableBillingLine]) -> str:
    return invoice.render_invoice_html(
        InvoiceSettings(),
        lines,
        invoice_number=17,
        invoice_date=date(2026, 6, 6),
        due_date=date(2026, 7, 6),
        services_month=date(2026, 5, 1),
        point_rate=Decimal("200"),
        vat_rate=Decimal("0.20"),
    )


def test_renders_parties_and_meta() -> None:
    html = _render([_line("WP2d-D1", "WP2d", "Document methodology", 4, "800.00")])
    assert "BLITTERBYTE CONSULTING LIMITED" in html
    assert "LA International Accounts Payable" in html
    assert "INVOICE 017" in html
    assert "06 June 2026" in html
    assert "Payment due by 06 July 2026" in html
    assert "Services delivered in May 2026" in html


def test_line_details_and_subtotal() -> None:
    html = _render([_line("WP2d-D1", "WP2d", "Document methodology", 4, "800.00")])
    assert (
        "Work package WP2d, deliverable WP2d-D1: Document methodology" in html
    )
    assert "800.00" in html  # net subtotal
    assert "200.00" in html  # unit price
    assert "20%" in html


def test_totals_equal_summed_lines() -> None:
    lines = [
        _line("WP2d-D1", "WP2d", "Document methodology", 4, "800.00"),
        _line("WP5a-D1", "WP5a", "Deployed Code Changes", 40, "8000.00"),
    ]
    html = _render(lines)
    # Net 8,800.00, VAT 1,760.00, GBP Total 10,560.00
    assert "8,800.00" in html
    assert "1,760.00" in html
    assert "£10,560.00" in html


def test_unlinked_deliverable_renders() -> None:
    html = _render([_line(None, "WP5", "Loose work", 1, "200.00")])
    assert "deliverable UNLINKED: Loose work" in html
