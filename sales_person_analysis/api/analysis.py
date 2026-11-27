# Copyright (c) 2026, Savad Ibrahim and Contributors
# License: MIT

"""Customer-wise sales APIs for Sales Person Wise Analysis.

Who can see rows is decided by Sales Person Analysis Settings (role scope),
not by where a Sales Person sits in the tree. See api.scope.

Amount rules
------------
- Amounts are VAT-inclusive and in company currency (base_*).
- No item-group filter: SUM(parent.base_grand_total) per customer.
  base_grand_total is the customer total after exclusive and inclusive tax.
  The rounded payable (base_rounded_total) is not used.
- With an item-group filter: each matching item row is base_net_amount plus
  that row's Item Wise Tax Detail amounts, scaled by
  base_grand_total / (base_net_total + the document's customer tax) so a
  document's rows sum to its base_grand_total. Mixed tax rates stay on the
  rows that were taxed. See inclusive_line_amount(). Sales Taxes and Charges
  has no category column in ERPNext v16, so sales tax rows are not filtered
  by tax category.
- Salesperson filter uses EXISTS on Sales Team so multi-person documents are
  counted once. Amounts are not weighted by allocated_percentage.
- SI and SO amounts are never combined in one result set.
- Submitted credit notes (Sales Invoice, is_return = 1, docstatus = 1) are
  included. ERPNext already stores their base_grand_total, base_net_amount,
  and item-wise tax as negative numbers, so the SUM subtracts the return.
  Do not negate those amounts again. Week and month use the credit note's
  own posting_date. Drafts and cancelled credit notes stay out.
- Payment status filters submitted documents only. Pending means money is
  still due: a Sales Invoice with outstanding_amount > 0, or a Sales Order
  whose order total is not covered by advance_paid plus payments on linked
  submitted invoices. Paid means nothing is left to collect. All is both,
  and it is the default. A credit note has no amount left to collect, so it
  is Paid, not Pending. Cancelled documents are never included.
- Delivery status is a separate filter on the same submitted documents.
  Pending means stock is still to deliver. Delivered means nothing is left
  to deliver. All is both, and it is the default. See _delivery_condition.
- Sales Order has no return document. A credit note does not change a
  submitted order's base_grand_total, so Sales Order mode is order totals only.
- Group by Customer or Invoice without an item-group filter uses each
  document's base_grand_total. Group by Item or Item Group, and any group
  once an item-group filter is on, uses inclusive_line_amount() so a
  document's lines still sum to its base_grand_total.
"""

from __future__ import annotations

import re

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate

from sales_person_analysis.api.period import (
	hierarchy_options,
	parse_week_list,
	resolve_months,
	week_of_month_sql,
)
from sales_person_analysis.api.scope import (
	enforce_sales_person_filter,
	public_restriction,
	restriction_context,
)
from sales_person_analysis.constants import (
	ALLOWED_DOCTYPES,
	DEFAULT_PAGE_SIZE,
	DELIVERY_ALL,
	DELIVERY_DELIVERED,
	DELIVERY_PENDING,
	DELIVERY_STATUSES,
	DOCUMENT_STATUSES,
	GROUP_BY_OPTIONS,
	GROUP_CUSTOMER,
	GROUP_INVOICE,
	GROUP_ITEM,
	GROUP_ITEM_GROUP,
	MAX_EXPORT_FILENAME,
	MAX_EXPORT_ROWS,
	STATUS_ALL,
	STATUS_PAID,
	STATUS_PENDING,
)

# Guard for a document whose net and customer tax are both zero.
_TAX_EPSILON = 1e-7

DOCTYPE_META = {
	"Sales Invoice": {
		"date_field": "posting_date",
		"item_doctype": "Sales Invoice Item",
		# Credit notes use this field. Submitted ones are included; the amounts
		# are already negative. Sales Order has no return document.
		"has_is_return": True,
	},
	"Sales Order": {
		"date_field": "transaction_date",
		"item_doctype": "Sales Order Item",
		"has_is_return": False,
	},
}


@frappe.whitelist()
def get_filter_options(
	doctype: str = "Sales Invoice",
	status: str | None = None,
	delivery_status: str | None = None,
):
	"""Return sales persons, item groups, years, and period hierarchy options."""
	doctype = _validate_doctype(doctype)
	status = _validate_status(status)
	delivery_status = _validate_delivery_status(delivery_status)
	ctx = _scope_or_throw()
	company = _get_company()
	if ctx["restricted"]:
		sales_persons = list(ctx["allowed_sales_persons"])
		year_sales_persons = sales_persons
	else:
		sales_persons = _get_sales_persons()
		year_sales_persons = None

	return {
		"company": company,
		"currency": frappe.get_cached_value("Company", company, "default_currency") or "",
		"sales_persons": sales_persons,
		"item_groups": _get_item_groups(),
		"years": _get_years(doctype, company, year_sales_persons, status, delivery_status),
		"status": status,
		"delivery_status": delivery_status,
		"period": hierarchy_options(year=getdate().year),
		"restriction": public_restriction(ctx),
	}


@frappe.whitelist()
def get_period_options(half=None, quarter=None, year=None):
	"""Cascading Half → Quarter → Month options, plus week labels for year."""
	return hierarchy_options(half=half, quarter=quarter, year=year)


@frappe.whitelist()
def get_customer_wise_sales(filters=None):
	"""Paginated customer-wise sales for the selected document type and filters."""
	filters = frappe._dict(frappe.parse_json(filters) if isinstance(filters, str) else (filters or {}))
	# Ignore a client-supplied allow-list. Export uses this same method.
	filters.pop("allowed_sales_persons", None)
	doctype = _validate_doctype(filters.get("doctype") or "Sales Invoice")
	status = _validate_status(filters.get("status"))
	delivery_status = _validate_delivery_status(filters.get("delivery_status"))
	group_by = _validate_group_by(filters.get("group_by"))

	page = max(cint(filters.get("page") or 1), 1)
	page_size = cint(filters.get("page_size") or DEFAULT_PAGE_SIZE)
	page_size = min(max(page_size, 1), 200)
	export = cint(filters.get("export"))

	year = cint(filters.get("year"))
	if not year:
		frappe.throw(_("Year is required"))

	# Apply the role scope before the empty-period shortcut, so a user with
	# no mapped role cannot hide behind an empty filter combination.
	sales_persons = enforce_sales_person_filter(_parse_names(filters.get("sales_persons")))
	company = _get_company()

	month_nums = resolve_months(filters.get("half"), filters.get("quarter"), filters.get("months"))
	if month_nums is not None and not month_nums:
		return _empty_result(company, doctype, page, page_size, status, delivery_status, group_by)

	weeks = parse_week_list(filters.get("weeks"))
	item_groups = _parse_names(filters.get("item_groups"))

	meta = DOCTYPE_META[doctype]
	conditions, values = _base_conditions(
		doctype,
		company,
		meta,
		year,
		month_nums,
		weeks,
		sales_persons,
		status,
		delivery_status,
	)

	if _uses_item_level(group_by, item_groups):
		rows, grand_total, group_count = _query_with_item_groups(
			doctype, meta, conditions, values, item_groups, page, page_size, export, group_by
		)
	else:
		rows, grand_total, group_count = _query_parent_totals(
			doctype, conditions, values, page, page_size, export, group_by
		)

	currency = frappe.get_cached_value("Company", company, "default_currency") or ""
	return {
		"company": company,
		"currency": currency,
		"doctype": doctype,
		"status": status,
		"delivery_status": delivery_status,
		"group_by": group_by,
		"rows": rows,
		"grand_total": flt(grand_total),
		"customer_count": cint(group_count),
		"page": page,
		"page_size": page_size if not export else len(rows),
		"total_pages": max(1, (cint(group_count) + page_size - 1) // page_size) if page_size else 1,
	}


@frappe.whitelist()
def export_customer_wise_sales(filters=None):
	"""Download the customer-wise report as a short-named Excel workbook.

	The desk page used to pass the result rows as downloadify's title, so the
	saved file was named after every customer and amount. Excel will not open
	that. This method keeps the name to SPA_<Doctype>_<Year>.xlsx and puts the
	rows in the workbook.
	"""
	parsed = frappe.parse_json(filters) if isinstance(filters, str) else (filters or {})
	parsed = frappe._dict(parsed)
	parsed["export"] = 1
	parsed["page"] = 1
	data = get_customer_wise_sales(parsed)

	from frappe.utils.xlsxutils import make_xlsx

	xlsx_file = make_xlsx(_excel_rows(data), _("Sales Person Analysis"))
	frappe.response["filename"] = _export_filename(data.get("doctype"), parsed.get("year"))
	frappe.response["filecontent"] = xlsx_file.getvalue()
	frappe.response["type"] = "binary"


def _export_filename(doctype: str | None = None, year=None) -> str:
	"""Short, filesystem-safe .xlsx name. Report data must never go in here."""
	parts = ["SPA"]
	slug = re.sub(r"[^A-Za-z0-9]+", "_", str(doctype or "").strip()).strip("_")
	if slug:
		parts.append(slug)
	year_num = cint(year)
	if year_num:
		parts.append(str(year_num))
	stem = "_".join(parts) or "SPA"
	max_stem = max(1, MAX_EXPORT_FILENAME - len(".xlsx"))
	stem = stem[:max_stem].rstrip("._") or "SPA"
	return f"{stem}.xlsx"


def _excel_rows(data) -> list:
	"""Grouped sheet: identity columns, VAT-inclusive amount, then grand total."""
	data = data or {}
	group_by = _validate_group_by(data.get("group_by"))
	doctype = data.get("doctype") or "Sales Invoice"
	currency = data.get("currency") or ""
	amount_header = _("Sales Amount incl. VAT")
	if currency:
		amount_header = f"{amount_header} ({currency})"
	headers = _excel_headers(group_by, doctype, amount_header)
	rows = [headers]
	for row in data.get("rows") or []:
		rows.append(_excel_data_row(group_by, row))
	blank = [""] * (len(headers) - 2)
	rows.append([_("Grand Total (incl. VAT)"), *blank, flt(data.get("grand_total"))])
	return rows


def _excel_headers(group_by: str, doctype: str, amount_header: str) -> list:
	if group_by == GROUP_INVOICE:
		doc_label = _("Invoice") if doctype == "Sales Invoice" else _("Order")
		return [doc_label, _("Customer"), amount_header]
	if group_by == GROUP_ITEM:
		return [_("Item"), amount_header]
	if group_by == GROUP_ITEM_GROUP:
		return [_("Item Group"), amount_header]
	return [_("Customer"), amount_header]


def _excel_data_row(group_by: str, row) -> list:
	amount = flt(row.get("amount"))
	if group_by == GROUP_INVOICE:
		return [
			row.get("invoice") or "",
			row.get("customer_name") or row.get("customer") or "",
			amount,
		]
	if group_by == GROUP_ITEM:
		return [row.get("item_name") or row.get("item") or "", amount]
	if group_by == GROUP_ITEM_GROUP:
		return [row.get("item_group") or "", amount]
	return [row.get("customer_name") or row.get("customer") or "", amount]


def _empty_result(
	company,
	doctype,
	page,
	page_size,
	status=STATUS_ALL,
	delivery_status=DELIVERY_ALL,
	group_by=GROUP_CUSTOMER,
):
	currency = frappe.get_cached_value("Company", company, "default_currency") or ""
	return {
		"company": company,
		"currency": currency,
		"doctype": doctype,
		"status": status,
		"delivery_status": delivery_status,
		"group_by": _validate_group_by(group_by),
		"rows": [],
		"grand_total": 0.0,
		"customer_count": 0,
		"page": page,
		"page_size": page_size,
		"total_pages": 1,
	}


def _validate_doctype(doctype: str) -> str:
	doctype = (doctype or "").strip()
	if doctype not in ALLOWED_DOCTYPES:
		frappe.throw(_("Document Type must be Sales Invoice or Sales Order"))
	return doctype


def _validate_status(status: str | None) -> str:
	status = (status or STATUS_ALL).strip()
	if status not in DOCUMENT_STATUSES:
		frappe.throw(_("Status must be Pending, Paid, or All"))
	return status


def _validate_delivery_status(delivery_status: str | None) -> str:
	delivery_status = (delivery_status or DELIVERY_ALL).strip()
	if delivery_status not in DELIVERY_STATUSES:
		frappe.throw(_("Delivery status must be All, Delivered, or Pending"))
	return delivery_status


def _validate_group_by(group_by: str | None) -> str:
	group_by = (group_by or GROUP_CUSTOMER).strip()
	if group_by not in GROUP_BY_OPTIONS:
		frappe.throw(_("Group by must be Invoice, Customer, Item, or Item Group"))
	return group_by


def _uses_item_level(group_by: str, item_groups) -> bool:
	"""Item and Item Group always need lines. An item-group chip does too."""
	group_by = _validate_group_by(group_by)
	return bool(item_groups) or group_by in (GROUP_ITEM, GROUP_ITEM_GROUP)


def _group_spec(group_by: str):
	"""SELECT / GROUP BY / ORDER BY for one grouping. {amount} is filled later."""
	group_by = _validate_group_by(group_by)
	if group_by == GROUP_INVOICE:
		return frappe._dict(
			select=(
				"doc.name AS invoice, doc.customer AS customer, "
				"MAX(doc.customer_name) AS customer_name, SUM({amount}) AS amount"
			),
			group="doc.name, doc.customer",
			order="amount DESC, invoice ASC",
			count_group="doc.name",
		)
	if group_by == GROUP_ITEM:
		return frappe._dict(
			select=(
				"item_row.item_code AS item, "
				"MAX(IFNULL(item_row.item_name, item.item_name)) AS item_name, "
				"SUM({amount}) AS amount"
			),
			group="item_row.item_code",
			order="amount DESC, item_name ASC",
			count_group="item_row.item_code",
		)
	if group_by == GROUP_ITEM_GROUP:
		return frappe._dict(
			select="item.item_group AS item_group, SUM({amount}) AS amount",
			group="item.item_group",
			order="amount DESC, item_group ASC",
			count_group="item.item_group",
		)
	return frappe._dict(
		select=("doc.customer AS customer, MAX(doc.customer_name) AS customer_name, SUM({amount}) AS amount"),
		group="doc.customer",
		order="amount DESC, customer_name ASC",
		count_group="doc.customer",
	)


def _payment_condition(doctype: str, status: str, alias: str = "doc") -> str | None:
	"""SQL predicate for the payment filter. None means every submitted document."""
	status = _validate_status(status)
	if status == STATUS_ALL:
		return None
	if doctype == "Sales Invoice":
		compare = ">" if status == STATUS_PENDING else "<="
		return f"IFNULL({alias}.outstanding_amount, 0) {compare} 0"
	due = _sales_order_amount_due_sql(alias)
	compare = ">" if status == STATUS_PENDING else "<="
	if status not in (STATUS_PENDING, STATUS_PAID):
		return None
	return f"{due} {compare} 0"


def _sales_order_amount_due_sql(alias: str = "doc") -> str:
	"""Order total still to collect, in the order's currency.

	advance_paid is what is still allocated to the order. After an advance
	moves onto a Sales Invoice, ERPNext drops it from advance_paid and reduces
	that invoice's outstanding_amount, so the two are added. A linked credit
	note reduces the collected amount because its grand_total is negative.
	"""
	order_total = (
		f"CASE WHEN IFNULL({alias}.rounded_total, 0) = 0 "
		f"THEN IFNULL({alias}.grand_total, 0) ELSE {alias}.rounded_total END"
	)
	collected = f"""IFNULL({alias}.advance_paid, 0) + IFNULL((
		SELECT SUM(IFNULL(si.grand_total, 0) - IFNULL(si.outstanding_amount, 0))
		FROM `tabSales Invoice` si
		WHERE si.docstatus = 1
			AND EXISTS (
				SELECT 1 FROM `tabSales Invoice Item` sii
				WHERE sii.parent = si.name
					AND sii.sales_order = {alias}.name
			)
	), 0)"""
	return f"(({order_total}) - ({collected}))"


def _delivery_condition(doctype: str, delivery_status: str, alias: str = "doc") -> str | None:
	"""SQL predicate for the delivery filter. None means every submitted document."""
	delivery_status = _validate_delivery_status(delivery_status)
	if delivery_status == DELIVERY_ALL:
		return None
	if doctype == "Sales Order":
		# Fully Delivered, closed, not applicable, or skip-delivery orders have
		# nothing left to ship. Partly and not delivered stay Pending.
		# per_delivered uses the same cutoff as ERPNext status_updater.
		delivered = (
			f"(IFNULL({alias}.skip_delivery_note, 0) = 1 "
			f"OR {alias}.delivery_status IN ('Fully Delivered', 'Not Applicable', 'Closed') "
			f"OR IFNULL({alias}.per_delivered, 0) >= 99.999999)"
		)
		if delivery_status == DELIVERY_DELIVERED:
			return delivered
		return f"NOT {delivered}"
	pending = _sales_invoice_pending_delivery_sql(alias)
	if delivery_status == DELIVERY_PENDING:
		return pending
	return f"NOT ({pending})"


def _sales_invoice_pending_delivery_sql(alias: str = "doc") -> str:
	"""Invoice still has company stock to deliver.

	update_stock and credit notes are not pending. A row created from a
	Delivery Note (dn_detail) is already delivered. Drop-ship rows are left
	out: ERPNext does not make a Delivery Note for them. Non-stock rows are
	not delivery work, so a service-only invoice is Delivered.
	"""
	return f"""IFNULL({alias}.update_stock, 0) = 0
		AND IFNULL({alias}.is_return, 0) = 0
		AND EXISTS (
			SELECT 1
			FROM `tabSales Invoice Item` sii
			INNER JOIN `tabItem` item ON item.name = sii.item_code
			WHERE sii.parent = {alias}.name
				AND item.is_stock_item = 1
				AND IFNULL(sii.delivered_by_supplier, 0) = 0
				AND IFNULL(sii.dn_detail, '') = ''
				AND IFNULL(sii.qty, 0) - IFNULL(sii.delivered_qty, 0) > 0.000001
		)"""


def _get_company() -> str:
	# get_user_default("Company") reads the scrubbed key "company" on Frappe v16.
	company = frappe.defaults.get_user_default("Company") or frappe.db.get_single_value(
		"Global Defaults", "default_company"
	)
	if not company:
		frappe.throw(_("Please set a default Company in your user defaults"))
	return company


def _scope_or_throw():
	ctx = restriction_context()
	if ctx["error"]:
		frappe.throw(ctx["error"], frappe.PermissionError)
	return ctx


def _get_sales_persons() -> list[str]:
	# db.get_all skips DocPerm. See All includes System Manager, who has no
	# Sales Person permission row in stock ERPNext.
	rows = frappe.db.get_all(
		"Sales Person",
		filters={"enabled": 1},
		fields=["name"],
		order_by="name asc",
		limit=500,
	)
	return [r.name for r in rows]


def _get_item_groups() -> list[str]:
	rows = frappe.get_all(
		"Item Group",
		fields=["name"],
		order_by="name asc",
		limit=1000,
	)
	return [r.name for r in rows]


def _get_years(
	doctype: str,
	company: str,
	sales_persons: list[str] | None = None,
	status: str = STATUS_ALL,
	delivery_status: str = DELIVERY_ALL,
) -> list[int]:
	date_field = DOCTYPE_META[doctype]["date_field"]
	current_year = getdate().year
	conditions = [
		"doc.docstatus = 1",
		"doc.company = %s",
		f"doc.`{date_field}` IS NOT NULL",
	]
	values: list = [company]
	payment = _payment_condition(doctype, status)
	if payment:
		conditions.append(payment)
	delivery = _delivery_condition(doctype, delivery_status)
	if delivery:
		conditions.append(delivery)
	if sales_persons is not None:
		# Restricted users only see years that contain their own documents.
		conditions.append(
			"""EXISTS (
				SELECT 1 FROM `tabSales Team` st
				WHERE st.parent = doc.name
					AND st.parenttype = %s
					AND st.sales_person IN %s
			)"""
		)
		values.extend([doctype, tuple(sales_persons) or ("",)])
	rows = frappe.db.sql(
		f"""
		SELECT DISTINCT YEAR(doc.`{date_field}`) AS y
		FROM `tab{doctype}` doc
		WHERE {" AND ".join(conditions)}
		ORDER BY y DESC
		""",
		tuple(values),
	)
	years = [cint(r[0]) for r in rows if r and r[0]]
	if current_year not in years:
		years.insert(0, current_year)
	return years or [current_year]


def _parse_names(value) -> list[str]:
	if not value:
		return []
	if isinstance(value, str):
		value = frappe.parse_json(value) if value.strip().startswith("[") else [value]
	if not isinstance(value, list | tuple):
		return []
	return [str(v).strip() for v in value if v and str(v).strip()]


def _base_conditions(
	doctype,
	company,
	meta,
	year,
	month_nums,
	weeks,
	sales_persons,
	status=STATUS_ALL,
	delivery_status=DELIVERY_ALL,
):
	date_field = meta["date_field"]
	status = _validate_status(status)
	delivery_status = _validate_delivery_status(delivery_status)
	conditions = [
		"doc.docstatus = 1",
		"doc.company = %(company)s",
		f"YEAR(doc.`{date_field}`) = %(year)s",
	]
	values = {"company": company, "year": year, "parenttype": doctype}

	# Submitted documents only. Credit notes stay in: their amounts are already
	# negative. Pending / Paid splits those submitted documents by what is
	# still due. All leaves the split off. Cancelled rows are excluded by
	# docstatus = 1. Dates use the document's own posting or transaction date.

	payment = _payment_condition(doctype, status)
	if payment:
		conditions.append(payment)

	delivery = _delivery_condition(doctype, delivery_status)
	if delivery:
		conditions.append(delivery)

	if month_nums is not None:
		conditions.append(f"MONTH(doc.`{date_field}`) IN %(months)s")
		values["months"] = month_nums

	if weeks:
		# Sunday-start week-of-month (WEEK_START_DAYOFWEEK). See period.week_of_month_sql.
		conditions.append(f"{week_of_month_sql(f'doc.`{date_field}`')} IN %(weeks)s")
		values["weeks"] = weeks

	if sales_persons:
		conditions.append(
			"""
			EXISTS (
				SELECT 1 FROM `tabSales Team` st
				WHERE st.parent = doc.name
					AND st.parenttype = %(parenttype)s
					AND st.sales_person IN %(sales_persons)s
			)
			"""
		)
		values["sales_persons"] = sales_persons

	return conditions, values


def inclusive_line_amount(
	base_net_amount,
	item_tax,
	base_net_total,
	invoice_tax,
	base_grand_total,
) -> float:
	"""Company-currency VAT-inclusive amount for one item row.

	Item rows have no tax-inclusive amount. Add that row's own Item Wise Tax
	Detail amounts (stored in company currency), then scale so the rows of
	one document sum to base_grand_total.

	A flat share of the invoice tax by net amount would put VAT on a
	zero-rated line when another line is taxed. Item-wise tax avoids that.
	The scale picks up cash / non-trade Grand Total discounts and inclusive-tax
	rounding dust, which change base_grand_total without being written back
	onto item nets. Net-total discounts are already inside base_net_amount.

	A credit note stores every one of these inputs as a negative number. The
	ratio stays negative, so the result must not be negated again.
	"""
	item_pre = flt(base_net_amount) + flt(item_tax)
	invoice_pre = flt(base_net_total) + flt(invoice_tax)
	if abs(invoice_pre) < _TAX_EPSILON:
		return item_pre
	return item_pre * flt(base_grand_total) / invoice_pre


def _parent_amount_sql() -> str:
	"""VAT-inclusive document total in company currency."""
	return "doc.base_grand_total"


def _item_tax_joins() -> str:
	"""Per-row and per-document sums of customer taxes, in company currency.

	Document tax is joined on the parent, not the item row: a zero-rated
	line may have no Item Wise Tax Detail row, and its scale still has to
	include the other lines' tax. Sales Taxes and Charges has no category
	column in ERPNext v16, so the join does not filter on one.
	"""
	return """
		LEFT JOIN (
			SELECT
				iwt.parent,
				iwt.item_row,
				SUM(iwt.amount) AS tax_amount
			FROM `tabItem Wise Tax Detail` iwt
			INNER JOIN `tabSales Taxes and Charges` stc
				ON stc.name = iwt.tax_row
				AND stc.parent = iwt.parent
				AND stc.parenttype = %(parenttype)s
			WHERE iwt.parenttype = %(parenttype)s
			GROUP BY iwt.parent, iwt.item_row
		) item_tax
			ON item_tax.parent = doc.name
			AND item_tax.item_row = item_row.name
		LEFT JOIN (
			SELECT
				iwt.parent,
				SUM(iwt.amount) AS tax_amount
			FROM `tabItem Wise Tax Detail` iwt
			INNER JOIN `tabSales Taxes and Charges` stc
				ON stc.name = iwt.tax_row
				AND stc.parent = iwt.parent
				AND stc.parenttype = %(parenttype)s
			WHERE iwt.parenttype = %(parenttype)s
			GROUP BY iwt.parent
		) doc_tax
			ON doc_tax.parent = doc.name
	"""


def _item_inclusive_amount_sql() -> str:
	"""SQL twin of inclusive_line_amount()."""
	item_pre = "(item_row.base_net_amount + IFNULL(item_tax.tax_amount, 0))"
	invoice_pre = "(doc.base_net_total + IFNULL(doc_tax.tax_amount, 0))"
	return f"""
		CASE
			WHEN ABS({invoice_pre}) < 0.0000001 THEN {item_pre}
			ELSE {item_pre} * doc.base_grand_total / {invoice_pre}
		END
	"""


def _query_parent_totals(doctype, conditions, values, page, page_size, export, group_by=GROUP_CUSTOMER):
	group_by = _validate_group_by(group_by)
	if _uses_item_level(group_by, []):
		return _query_item_level(
			doctype, DOCTYPE_META[doctype], conditions, values, [], page, page_size, export, group_by
		)
	return _query_grouped_sql(
		f"FROM `tab{doctype}` doc",
		conditions,
		values,
		_parent_amount_sql(),
		page,
		page_size,
		export,
		group_by,
	)


def _query_with_item_groups(
	doctype, meta, conditions, values, item_groups, page, page_size, export, group_by=GROUP_CUSTOMER
):
	return _query_item_level(
		doctype, meta, conditions, values, item_groups, page, page_size, export, group_by
	)


def _query_item_level(doctype, meta, conditions, values, item_groups, page, page_size, export, group_by):
	item_doctype = meta["item_doctype"]
	conditions = list(conditions)
	values = dict(values)
	if item_groups:
		conditions.append("item.item_group IN %(item_groups)s")
		values["item_groups"] = item_groups
	from_sql = f"""
		FROM `tab{doctype}` doc
		INNER JOIN `tab{item_doctype}` item_row
			ON item_row.parent = doc.name AND item_row.parenttype = %(parenttype)s
		INNER JOIN `tabItem` item ON item.name = item_row.item_code
		{_item_tax_joins()}
	"""
	return _query_grouped_sql(
		from_sql,
		conditions,
		values,
		_item_inclusive_amount_sql(),
		page,
		page_size,
		export,
		group_by,
	)


def _query_grouped_sql(from_sql, conditions, values, amount_sql, page, page_size, export, group_by):
	spec = _group_spec(group_by)
	where_sql = " AND ".join(conditions)
	select_sql = spec.select.replace("{amount}", amount_sql)

	count_row = frappe.db.sql(
		f"""
		SELECT COUNT(*) FROM (
			SELECT {spec.count_group}
			{from_sql}
			WHERE {where_sql}
			GROUP BY {spec.group}
		) t
		""",
		values,
	)
	group_count = cint(count_row[0][0]) if count_row else 0

	total_row = frappe.db.sql(
		f"""
		SELECT COALESCE(SUM({amount_sql}), 0)
		{from_sql}
		WHERE {where_sql}
		""",
		values,
	)
	grand_total = flt(total_row[0][0]) if total_row else 0.0

	limit_sql, values = _limit_clause(values, page, page_size, export, group_count)
	rows = frappe.db.sql(
		f"""
		SELECT {select_sql}
		{from_sql}
		WHERE {where_sql}
		GROUP BY {spec.group}
		ORDER BY {spec.order}
		{limit_sql}
		""",
		values,
		as_dict=True,
	)
	return _format_rows(rows), grand_total, group_count


def _limit_clause(values, page, page_size, export, customer_count):
	if export:
		values = {**values, "limit": min(customer_count or MAX_EXPORT_ROWS, MAX_EXPORT_ROWS), "offset": 0}
	else:
		values = {**values, "limit": page_size, "offset": (page - 1) * page_size}
	return "LIMIT %(limit)s OFFSET %(offset)s", values


def _format_rows(rows):
	formatted = []
	for row in rows:
		out = {"amount": flt(row.get("amount"))}
		if "invoice" in row:
			out["invoice"] = row.get("invoice")
		if "customer" in row or "customer_name" in row:
			out["customer"] = row.get("customer")
			out["customer_name"] = row.get("customer_name") or row.get("customer") or _("(No Customer)")
		if "item" in row:
			out["item"] = row.get("item")
			out["item_name"] = row.get("item_name") or row.get("item") or _("(No Item)")
		if "item_group" in row:
			out["item_group"] = row.get("item_group") or _("(No Item Group)")
		formatted.append(out)
	return formatted
