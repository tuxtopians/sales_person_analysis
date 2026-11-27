# Copyright (c) 2026, Savad Ibrahim and Contributors
# License: MIT

from unittest.mock import patch

from frappe.tests import UnitTestCase
from frappe.utils import flt

from sales_person_analysis.api import analysis as analysis_mod
from sales_person_analysis.api.analysis import DOCTYPE_META
from sales_person_analysis.api.period import week_of_month_sql
from sales_person_analysis.constants import MAX_EXPORT_FILENAME, MAX_EXPORT_ROWS, WEEK_START_DAYOFWEEK


class TestApiHelpers(UnitTestCase):
	def test_validate_doctype_allowlist(self):
		self.assertEqual(analysis_mod._validate_doctype("Sales Invoice"), "Sales Invoice")
		self.assertEqual(analysis_mod._validate_doctype(" Sales Order "), "Sales Order")
		with self.assertRaises(Exception):
			analysis_mod._validate_doctype("Purchase Invoice")
		with self.assertRaises(Exception):
			analysis_mod._validate_doctype("")

	def test_parse_names(self):
		self.assertEqual(analysis_mod._parse_names(None), [])
		self.assertEqual(analysis_mod._parse_names(""), [])
		self.assertEqual(analysis_mod._parse_names("Haroon"), ["Haroon"])
		self.assertEqual(analysis_mod._parse_names('["A","B"]'), ["A", "B"])
		self.assertEqual(analysis_mod._parse_names(["A", "", " B "]), ["A", "B"])
		self.assertEqual(analysis_mod._parse_names(123), [])

	def test_doctype_meta(self):
		self.assertEqual(DOCTYPE_META["Sales Invoice"]["date_field"], "posting_date")
		self.assertTrue(DOCTYPE_META["Sales Invoice"]["has_is_return"])
		self.assertEqual(DOCTYPE_META["Sales Order"]["date_field"], "transaction_date")
		self.assertFalse(DOCTYPE_META["Sales Order"]["has_is_return"])

	def test_limit_clause_pagination(self):
		sql, values = analysis_mod._limit_clause({"x": 1}, page=3, page_size=20, export=0, customer_count=100)
		self.assertIn("LIMIT", sql)
		self.assertEqual(values["limit"], 20)
		self.assertEqual(values["offset"], 40)

	def test_limit_clause_export_caps(self):
		_, values = analysis_mod._limit_clause({}, page=1, page_size=20, export=1, customer_count=50)
		self.assertEqual(values["limit"], 50)
		self.assertEqual(values["offset"], 0)

		_, values = analysis_mod._limit_clause({}, page=1, page_size=20, export=1, customer_count=999999)
		self.assertEqual(values["limit"], MAX_EXPORT_ROWS)

		_, values = analysis_mod._limit_clause({}, page=1, page_size=20, export=1, customer_count=0)
		self.assertEqual(values["limit"], MAX_EXPORT_ROWS)

	def test_validate_group_by(self):
		self.assertEqual(analysis_mod._validate_group_by(None), "Customer")
		self.assertEqual(analysis_mod._validate_group_by(" Invoice "), "Invoice")
		self.assertEqual(analysis_mod._validate_group_by("Item Group"), "Item Group")
		with self.assertRaises(Exception):
			analysis_mod._validate_group_by("Warehouse")

	def test_uses_item_level(self):
		self.assertFalse(analysis_mod._uses_item_level("Customer", []))
		self.assertFalse(analysis_mod._uses_item_level("Invoice", []))
		self.assertTrue(analysis_mod._uses_item_level("Item", []))
		self.assertTrue(analysis_mod._uses_item_level("Item Group", []))
		self.assertTrue(analysis_mod._uses_item_level("Customer", ["Products"]))
		self.assertTrue(analysis_mod._uses_item_level("Invoice", ["Products"]))

	def test_format_rows(self):
		rows = analysis_mod._format_rows(
			[
				frappe_dict(customer="C1", customer_name="Cust One", amount=10.5),
				frappe_dict(customer="C2", customer_name=None, amount=None),
			]
		)
		self.assertEqual(rows[0]["customer_name"], "Cust One")
		self.assertEqual(flt(rows[0]["amount"]), 10.5)
		self.assertEqual(rows[1]["customer_name"], "C2")
		self.assertEqual(flt(rows[1]["amount"]), 0.0)

	def test_format_rows_invoice_and_item(self):
		invoice_rows = analysis_mod._format_rows(
			[frappe_dict(invoice="SINV-1", customer="C1", customer_name="Alpha", amount=25)],
			"Invoice",
		)
		self.assertEqual(invoice_rows[0]["invoice"], "SINV-1")
		self.assertEqual(invoice_rows[0]["customer_name"], "Alpha")
		item_rows = analysis_mod._format_rows([frappe_dict(item="ITEM-1", item_name=None, amount=8)], "Item")
		self.assertEqual(item_rows[0]["item"], "ITEM-1")
		self.assertEqual(item_rows[0]["item_name"], "ITEM-1")
		group_rows = analysis_mod._format_rows([frappe_dict(item_group=None, amount=3)], "Item Group")
		self.assertEqual(group_rows[0]["item_group"], "(No Item Group)")

	def test_format_rows_sql_alias_fallbacks(self):
		"""MariaDB may return name/item_code instead of the AS aliases."""
		invoice_rows = analysis_mod._format_rows(
			[frappe_dict(name="ACC-SINV-2026-00001", customer="C1", customer_name="Alpha", amount=25)],
			"Invoice",
		)
		self.assertEqual(invoice_rows[0]["invoice"], "ACC-SINV-2026-00001")
		item_rows = analysis_mod._format_rows(
			[frappe_dict(item_code="SKU-1", item_name="Widget", amount=8)], "Item"
		)
		self.assertEqual(item_rows[0]["item"], "SKU-1")
		self.assertEqual(item_rows[0]["item_name"], "Widget")
		group_rows = analysis_mod._format_rows([frappe_dict(item_group="Products", amount=3)], "Item Group")
		self.assertEqual(group_rows[0]["item_group"], "Products")

	def test_export_filename_is_short_and_safe(self):
		name = analysis_mod._export_filename("Sales Invoice", 2026)
		self.assertEqual(name, "SPA_Sales_Invoice_2026.xlsx")
		self.assertLessEqual(len(name), MAX_EXPORT_FILENAME)
		for ch in '\\/:*?"<>|,':
			self.assertNotIn(ch, name)

		long_name = analysis_mod._export_filename("X" * 200, 2026)
		self.assertTrue(long_name.endswith(".xlsx"))
		self.assertLessEqual(len(long_name), MAX_EXPORT_FILENAME)

		# Customer rows used to become the download title. They must not.
		self.assertNotIn("Alpha Trading", analysis_mod._export_filename("Sales Invoice", 2026))

	def test_excel_rows_include_header_customers_and_total(self):
		rows = analysis_mod._excel_rows(
			{
				"currency": "AED",
				"rows": [
					{"customer": "C1", "customer_name": "Alpha", "amount": 10},
					{"customer": "C2", "customer_name": "", "amount": 2.5},
				],
				"grand_total": 12.5,
			}
		)
		self.assertEqual(rows[0][0], "Customer")
		self.assertIn("AED", rows[0][1])
		self.assertEqual(rows[1], ["Alpha", 10.0])
		self.assertEqual(rows[2][0], "C2")
		self.assertEqual(flt(rows[2][1]), 2.5)
		self.assertEqual(rows[-1][0], "Grand Total (incl. VAT)")
		self.assertEqual(flt(rows[-1][1]), 12.5)
		self.assertEqual(len(rows), 4)

	def test_excel_rows_invoice_item_and_item_group(self):
		invoice_rows = analysis_mod._excel_rows(
			{
				"group_by": "Invoice",
				"doctype": "Sales Invoice",
				"currency": "AED",
				"rows": [
					{"invoice": "SINV-1", "customer": "C1", "customer_name": "Alpha", "amount": 10},
				],
				"grand_total": 10,
			}
		)
		self.assertEqual(invoice_rows[0][0], "Invoice")
		self.assertEqual(invoice_rows[0][1], "Customer")
		self.assertEqual(invoice_rows[1], ["SINV-1", "Alpha", 10.0])
		self.assertEqual(invoice_rows[-1][0], "Grand Total (incl. VAT)")
		self.assertEqual(flt(invoice_rows[-1][-1]), 10.0)

		order_rows = analysis_mod._excel_rows(
			{
				"group_by": "Invoice",
				"doctype": "Sales Order",
				"rows": [{"invoice": "SO-1", "customer_name": "Beta", "amount": 4}],
				"grand_total": 4,
			}
		)
		self.assertEqual(order_rows[0][0], "Order")

		item_rows = analysis_mod._excel_rows(
			{
				"group_by": "Item",
				"rows": [{"item": "SKU", "item_name": "Widget", "amount": 7}],
				"grand_total": 7,
			}
		)
		self.assertEqual(item_rows[0][0], "Item")
		self.assertEqual(item_rows[1], ["Widget", 7.0])

		group_rows = analysis_mod._excel_rows(
			{
				"group_by": "Item Group",
				"rows": [{"item_group": "Products", "amount": 9}],
				"grand_total": 9,
			}
		)
		self.assertEqual(group_rows[0][0], "Item Group")
		self.assertEqual(group_rows[1], ["Products", 9.0])

	def test_base_conditions_include_submitted_returns(self):
		meta = DOCTYPE_META["Sales Invoice"]
		conditions, values = analysis_mod._base_conditions(
			"Sales Invoice",
			"Co",
			meta,
			year=2026,
			month_nums=[8, 9],
			weeks=[1, 5],
			sales_persons=["SP1"],
		)
		joined = " ".join(conditions)
		week_sql = week_of_month_sql("doc.`posting_date`")
		self.assertIn("doc.docstatus = 1", joined)
		self.assertNotIn("outstanding_amount", joined)
		# Credit notes stay in the query. Their stored amounts are negative.
		self.assertNotIn("is_return", joined)
		self.assertIn("MONTH", joined)
		self.assertIn(week_sql, joined)
		self.assertIn("DAYOFWEEK", joined)
		self.assertIn(f"- {WEEK_START_DAYOFWEEK} + 7", week_sql)
		self.assertNotIn("(DAY(doc.`posting_date`) - 1) / 7", joined)
		self.assertIn("EXISTS", joined)
		self.assertEqual(values["company"], "Co")
		self.assertEqual(values["year"], 2026)
		self.assertEqual(values["months"], [8, 9])
		self.assertEqual(values["weeks"], [1, 5])
		self.assertEqual(values["sales_persons"], ["SP1"])

	def test_payment_conditions(self):
		meta = DOCTYPE_META["Sales Invoice"]
		pending, _values = analysis_mod._base_conditions(
			"Sales Invoice",
			"Co",
			meta,
			year=2026,
			month_nums=None,
			weeks=[],
			sales_persons=[],
			status="Pending",
		)
		paid, _values = analysis_mod._base_conditions(
			"Sales Invoice",
			"Co",
			meta,
			year=2026,
			month_nums=None,
			weeks=[],
			sales_persons=[],
			status="Paid",
		)
		self.assertIn("outstanding_amount, 0) > 0", " ".join(pending))
		self.assertIn("outstanding_amount, 0) <= 0", " ".join(paid))
		self.assertIn("doc.docstatus = 1", " ".join(pending))

		so_pending = analysis_mod._payment_condition("Sales Order", "Pending")
		self.assertIn("advance_paid", so_pending)
		self.assertIn("sales_order", so_pending)
		self.assertIn("> 0", so_pending)
		so_paid = analysis_mod._payment_condition("Sales Order", "Paid")
		self.assertIn("<= 0", so_paid)
		self.assertIsNone(analysis_mod._payment_condition("Sales Invoice", "All"))

	def test_delivery_conditions(self):
		meta = DOCTYPE_META["Sales Invoice"]
		pending, _values = analysis_mod._base_conditions(
			"Sales Invoice",
			"Co",
			meta,
			year=2026,
			month_nums=None,
			weeks=[],
			sales_persons=[],
			delivery_status="Pending",
		)
		delivered, _values = analysis_mod._base_conditions(
			"Sales Invoice",
			"Co",
			meta,
			year=2026,
			month_nums=None,
			weeks=[],
			sales_persons=[],
			delivery_status="Delivered",
		)
		pending_sql = " ".join(pending)
		delivered_sql = " ".join(delivered)
		self.assertIn("update_stock", pending_sql)
		self.assertIn("is_stock_item", pending_sql)
		self.assertIn("delivered_qty", pending_sql)
		self.assertIn("dn_detail", pending_sql)
		self.assertIn("is_return", pending_sql)
		self.assertNotIn("outstanding_amount", pending_sql)
		self.assertTrue(delivered_sql.strip().count("NOT") >= 1)
		self.assertIn("update_stock", delivered_sql)

		so_pending = analysis_mod._delivery_condition("Sales Order", "Pending")
		self.assertIn("delivery_status", so_pending)
		self.assertIn("per_delivered", so_pending)
		self.assertIn("skip_delivery_note", so_pending)
		self.assertIn("NOT", so_pending)
		so_delivered = analysis_mod._delivery_condition("Sales Order", "Delivered")
		self.assertIn("Fully Delivered", so_delivered)
		self.assertNotIn("NOT", so_delivered)
		self.assertIsNone(analysis_mod._delivery_condition("Sales Invoice", "All"))

	def test_validate_delivery_status(self):
		self.assertEqual(analysis_mod._validate_delivery_status(None), "All")
		self.assertEqual(analysis_mod._validate_delivery_status(" Delivered "), "Delivered")
		self.assertEqual(analysis_mod._validate_delivery_status("Pending"), "Pending")
		with self.assertRaises(Exception):
			analysis_mod._validate_delivery_status("Partly Delivered")

	def test_validate_status(self):
		self.assertEqual(analysis_mod._validate_status(None), "All")
		self.assertEqual(analysis_mod._validate_status(" Pending "), "Pending")
		self.assertEqual(analysis_mod._validate_status("Paid"), "Paid")
		with self.assertRaises(Exception):
			analysis_mod._validate_status("Submitted")
		with self.assertRaises(Exception):
			analysis_mod._validate_status("Cancelled")

	def test_base_conditions_so_no_is_return(self):
		meta = DOCTYPE_META["Sales Order"]
		conditions, _values = analysis_mod._base_conditions(
			"Sales Order", "Co", meta, year=2026, month_nums=None, weeks=[], sales_persons=[]
		)
		joined = " ".join(conditions)
		self.assertNotIn("is_return", joined)
		self.assertIn("transaction_date", joined)
		self.assertNotIn("DAYOFWEEK", joined)

	def test_get_company_prefers_user_default(self):
		with (
			patch(
				"sales_person_analysis.api.analysis.frappe.defaults.get_user_default",
				return_value="User Co",
			) as get_default,
			patch(
				"sales_person_analysis.api.analysis.frappe.db.get_single_value",
			) as get_single,
		):
			self.assertEqual(analysis_mod._get_company(), "User Co")
		get_default.assert_called_once_with("Company")
		get_single.assert_not_called()

	def test_get_company_falls_back_to_global_defaults(self):
		with (
			patch(
				"sales_person_analysis.api.analysis.frappe.defaults.get_user_default",
				return_value=None,
			),
			patch(
				"sales_person_analysis.api.analysis.frappe.db.get_single_value",
				return_value="Global Co",
			) as get_single,
		):
			self.assertEqual(analysis_mod._get_company(), "Global Co")
		get_single.assert_called_once_with("Global Defaults", "default_company")

	def test_get_company_throws_when_unset(self):
		with (
			patch(
				"sales_person_analysis.api.analysis.frappe.defaults.get_user_default",
				return_value="",
			),
			patch(
				"sales_person_analysis.api.analysis.frappe.db.get_single_value",
				return_value=None,
			),
		):
			with self.assertRaises(Exception):
				analysis_mod._get_company()

	def test_get_years_falls_back_to_current(self):
		from frappe.utils import getdate

		with patch("sales_person_analysis.api.analysis.frappe.db.sql", return_value=[]):
			years = analysis_mod._get_years("Sales Invoice", "Any Co")
		self.assertEqual(years, [getdate().year])

	def test_inclusive_line_amount_keeps_mixed_rates(self):
		# Item A net 100 + 15 tax, item B net 200 + 0 tax, grand 315.
		# A net share of the invoice tax would be 105 and 210.
		taxed = analysis_mod.inclusive_line_amount(100, 15, 300, 15, 315)
		zero_rated = analysis_mod.inclusive_line_amount(200, 0, 300, 15, 315)
		self.assertAlmostEqual(taxed, 115)
		self.assertAlmostEqual(zero_rated, 200)
		self.assertAlmostEqual(taxed + zero_rated, 315)
		self.assertNotAlmostEqual(taxed, 105)
		self.assertNotAlmostEqual(zero_rated, 210)

	def test_inclusive_line_amount_scales_grand_total_discount(self):
		# Cash / non-trade discount leaves item nets and item tax unchanged
		# and reduces base_grand_total. 10% off 315 -> 283.5.
		taxed = analysis_mod.inclusive_line_amount(100, 15, 300, 15, 283.5)
		zero_rated = analysis_mod.inclusive_line_amount(200, 0, 300, 15, 283.5)
		self.assertAlmostEqual(taxed, 103.5)
		self.assertAlmostEqual(zero_rated, 180)
		self.assertAlmostEqual(taxed + zero_rated, 283.5)

	def test_inclusive_line_amount_keeps_credit_note_sign(self):
		# A full credit note is the positive case with every input negated.
		# Negative times negative over negative stays negative.
		taxed = analysis_mod.inclusive_line_amount(-100, -15, -300, -15, -315)
		zero_rated = analysis_mod.inclusive_line_amount(-200, 0, -300, -15, -315)
		self.assertAlmostEqual(taxed, -115)
		self.assertAlmostEqual(zero_rated, -200)
		self.assertAlmostEqual(taxed + zero_rated, -315)

		discounted = analysis_mod.inclusive_line_amount(-100, -15, -300, -15, -283.5)
		discounted_zero = analysis_mod.inclusive_line_amount(-200, 0, -300, -15, -283.5)
		self.assertAlmostEqual(discounted, -103.5)
		self.assertAlmostEqual(discounted_zero, -180)
		self.assertAlmostEqual(discounted + discounted_zero, -283.5)

	def test_return_amounts_are_not_negated_in_sql(self):
		sql = analysis_mod._parent_amount_sql() + analysis_mod._item_inclusive_amount_sql()
		self.assertIn("doc.base_grand_total", sql)
		self.assertNotIn("ABS(doc.base_grand_total)", sql)
		self.assertNotIn("-1 *", sql)
		self.assertNotIn("- doc.base_grand_total", sql)

	def test_inclusive_line_amount_no_tax_and_zero_base(self):
		self.assertAlmostEqual(analysis_mod.inclusive_line_amount(200, 0, 200, 0, 200), 200)
		self.assertAlmostEqual(analysis_mod.inclusive_line_amount(100, 15, 100, 15, 115), 115)
		self.assertAlmostEqual(analysis_mod.inclusive_line_amount(0, 0, 0, 0, 0), 0)
		self.assertAlmostEqual(analysis_mod.inclusive_line_amount(10, 0, 0, 0, 0), 10)

	def test_parent_amount_sql_is_vat_inclusive(self):
		sql = analysis_mod._parent_amount_sql()
		self.assertIn("base_grand_total", sql)
		self.assertNotIn("base_net_total", sql)
		self.assertNotIn("base_rounded_total", sql)

	def test_item_inclusive_sql_uses_item_wise_tax(self):
		joins = analysis_mod._item_tax_joins()
		expr = analysis_mod._item_inclusive_amount_sql()
		self.assertIn("tabItem Wise Tax Detail", joins)
		self.assertNotIn("category", joins)
		self.assertIn("doc_tax", joins)
		self.assertIn("item_tax", joins)
		self.assertIn("base_grand_total", expr)
		self.assertIn("base_net_amount", expr)
		self.assertIn("base_net_total", expr)

	def test_parent_query_sums_base_grand_total(self):
		captured = []

		def fake_sql(query, *args, **kwargs):
			captured.append(query)
			if "COUNT" in query:
				return [(0,)]
			if "COALESCE" in query:
				return [(0,)]
			return []

		with patch("sales_person_analysis.api.analysis.frappe.db.sql", side_effect=fake_sql):
			analysis_mod._query_parent_totals(
				"Sales Invoice",
				["doc.docstatus = 1"],
				{"parenttype": "Sales Invoice"},
				1,
				20,
				0,
			)
		joined = " ".join(captured)
		self.assertIn("doc.base_grand_total", joined)
		self.assertNotIn("base_net_total", joined)
		self.assertIn("GROUP BY doc.customer", joined)

	def test_parent_query_invoice_groups_by_document(self):
		captured = []

		def fake_sql(query, *args, **kwargs):
			captured.append(query)
			if "COUNT" in query:
				return [(0,)]
			if "COALESCE" in query:
				return [(0,)]
			return []

		with patch("sales_person_analysis.api.analysis.frappe.db.sql", side_effect=fake_sql):
			analysis_mod._query_parent_totals(
				"Sales Invoice",
				["doc.docstatus = 1"],
				{"parenttype": "Sales Invoice"},
				1,
				20,
				0,
				"Invoice",
			)
		joined = " ".join(captured)
		self.assertIn("doc.name AS invoice", joined)
		self.assertIn("GROUP BY doc.name, doc.customer", joined)
		self.assertIn("doc.base_grand_total", joined)
		self.assertNotIn("tabSales Invoice Item", joined)

	def test_parent_query_item_uses_line_amounts(self):
		captured = []

		def fake_sql(query, *args, **kwargs):
			captured.append(query)
			if "COUNT" in query:
				return [(0,)]
			if "COALESCE" in query:
				return [(0,)]
			return []

		with patch("sales_person_analysis.api.analysis.frappe.db.sql", side_effect=fake_sql):
			analysis_mod._query_parent_totals(
				"Sales Invoice",
				["doc.docstatus = 1"],
				{"parenttype": "Sales Invoice"},
				1,
				20,
				0,
				"Item",
			)
		joined = " ".join(captured)
		self.assertIn("item_row.item_code AS item", joined)
		self.assertIn("GROUP BY item_row.item_code", joined)
		self.assertIn("tabSales Invoice Item", joined)
		self.assertIn("tabItem Wise Tax Detail", joined)

	def test_item_group_query_sums_inclusive_lines(self):
		captured = []

		def fake_sql(query, *args, **kwargs):
			captured.append(query)
			if "COUNT" in query:
				return [(0,)]
			if "COALESCE" in query:
				return [(0,)]
			return []

		with patch("sales_person_analysis.api.analysis.frappe.db.sql", side_effect=fake_sql):
			analysis_mod._query_with_item_groups(
				"Sales Invoice",
				analysis_mod.DOCTYPE_META["Sales Invoice"],
				["doc.docstatus = 1"],
				{"parenttype": "Sales Invoice"},
				["Products"],
				1,
				20,
				0,
			)
		joined = " ".join(captured)
		self.assertIn("tabItem Wise Tax Detail", joined)
		self.assertNotIn("category", joined)
		self.assertIn("doc.base_grand_total", joined)
		self.assertNotIn("SUM(item_row.base_net_amount)", joined)
		self.assertIn("GROUP BY doc.customer", joined)

	def test_item_query_groups_by_item_group(self):
		captured = []

		def fake_sql(query, *args, **kwargs):
			captured.append(query)
			if "COUNT" in query:
				return [(0,)]
			if "COALESCE" in query:
				return [(0,)]
			return []

		with patch("sales_person_analysis.api.analysis.frappe.db.sql", side_effect=fake_sql):
			analysis_mod._query_with_item_groups(
				"Sales Invoice",
				analysis_mod.DOCTYPE_META["Sales Invoice"],
				["doc.docstatus = 1"],
				{"parenttype": "Sales Invoice"},
				["Products"],
				1,
				20,
				0,
				"Item Group",
			)
		joined = " ".join(captured)
		self.assertIn("item_row.item_group, item.item_group) AS item_group", joined)
		self.assertIn("GROUP BY IFNULL(item_row.item_group, item.item_group)", joined)
		self.assertIn("item.item_group IN %(item_groups)s", joined)


def frappe_dict(**kwargs):
	import frappe

	return frappe._dict(kwargs)
