from contextlib import contextmanager

import click
import frappe
from frappe import _
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.utils import flt


def validate_integration_request(docname: str | None):
	if frappe.db.get_value("Integration Request", docname, "status") == "Cancelled":
		frappe.throw(_("Expired Token"))


# PR-Foundry fork patch — framework#149 / framework#150.
#
# A gateway checkout URL is a plain link with everything in the query string. It
# lives in Payment Request.payment_url, reaches people through the client's
# mirror Purchase Invoice, the native payment-request email and plain browser
# history, and upstream never checks whether the thing it settles is still owed.
# So a settled invoice stayed chargeable forever, and the amount charged was
# whatever the URL said.
#
# Stripe is the sharp case: its flow is synchronous and logs a NEW Integration
# Request per attempt, so there is no prior "Completed" request to test against
# (unlike payfast, whose own checkout page refuses a completed token). The only
# state that means anything is on the Payment Request itself.
#
# Deliberately CONSERVATIVE: it throws only when it can positively establish
# that a charge should not happen, and stays silent on anything it cannot
# evaluate. A guard that guessed would decline real money.
_PAYABLE_BLOCKING_STATUSES = ("Paid", "Cancelled")


def assert_payable(reference_doctype: str, reference_docname: str, amount=None):
	"""Refuse to charge for a reference document that is no longer payable.

	Also refuses an ``amount`` that disagrees with the Payment Request it
	claims to settle (framework#150) — the checkout template echoes the whole
	query string into the RPC body, so ``amount`` is caller-controlled all the
	way to ``stripe.Charge.create``.

	Note ``Failed`` is deliberately NOT blocking: a declined card must stay
	retryable, or a customer who mistyped their number is stranded.

	Raises:
	    frappe.ValidationError: when the reference is unpayable, or the amount
	        does not match it.
	"""
	if not reference_doctype or not reference_docname:
		return
	if not frappe.db.exists(reference_doctype, reference_docname):
		return
	if reference_doctype != "Payment Request":
		# Subscriptions and other reference shapes carry no comparable state;
		# see the conservative-by-design note above.
		return

	request = frappe.db.get_value(
		"Payment Request",
		reference_docname,
		["status", "docstatus", "grand_total", "reference_doctype", "reference_name"],
		as_dict=True,
	)
	if not request:
		return

	settled = _("This invoice has already been paid. No further payment is needed.")
	if request.docstatus == 2 or request.status in _PAYABLE_BLOCKING_STATUSES:
		frappe.throw(settled, title=_("Already Paid"))

	# The request can lag the document it settles — paid by EFT, by another
	# request, or a mirror that syncs daily — so check the source of truth too.
	if request.reference_doctype and request.reference_name:
		outstanding = frappe.db.get_value(
			request.reference_doctype, request.reference_name, "outstanding_amount"
		)
		if outstanding is not None and flt(outstanding) <= 0:
			frappe.throw(settled, title=_("Already Paid"))

	if amount is not None and request.grand_total is not None:
		if abs(flt(amount) - flt(request.grand_total)) > 0.005:
			frappe.throw(
				_("This payment link is not valid for that amount."),
				title=_("Invalid Payment"),
			)


def get_payment_gateway_controller(payment_gateway):
	"""Return payment gateway controller"""
	gateway = frappe.get_doc("Payment Gateway", payment_gateway)
	if gateway.gateway_controller is None:
		try:
			return frappe.get_doc(f"{payment_gateway} Settings")
		except Exception:
			frappe.throw(_("{0} Settings not found").format(payment_gateway))
	else:
		try:
			return frappe.get_doc(gateway.gateway_settings, gateway.gateway_controller)
		except Exception:
			frappe.throw(_("{0} Settings not found").format(payment_gateway))


@frappe.whitelist(allow_guest=True, xss_safe=True)
def get_checkout_url(**kwargs):
	try:
		if kwargs.get("payment_gateway"):
			doc = frappe.get_doc("{} Settings".format(kwargs.get("payment_gateway")))
			return doc.get_payment_url(**kwargs)
		else:
			raise Exception
	except Exception:
		frappe.respond_as_web_page(
			_("Something went wrong"),
			_(
				"Looks like something is wrong with this site's payment gateway configuration. No payment has been made."
			),
			indicator_color="red",
			http_status_code=frappe.ValidationError.http_status_code,
		)


def create_payment_gateway(gateway, settings=None, controller=None):
	# NOTE: we don't translate Payment Gateway name because it is an internal doctype
	if not frappe.db.exists("Payment Gateway", gateway):
		payment_gateway = frappe.get_doc(
			{
				"doctype": "Payment Gateway",
				"gateway": gateway,
				"gateway_settings": settings,
				"gateway_controller": controller,
			}
		)
		payment_gateway.insert(ignore_permissions=True)


def make_custom_fields():
	if not frappe.get_meta("Web Form").has_field("payments_tab"):
		click.secho("* Installing Payment Custom Fields in Web Form")

		create_custom_fields(
			{
				"Web Form": [
					{
						"fieldname": "payments_tab",
						"fieldtype": "Tab Break",
						"label": "Payments",
						"insert_after": "custom_css",
					},
					{
						"default": "0",
						"fieldname": "accept_payment",
						"fieldtype": "Check",
						"label": "Accept Payment",
						"insert_after": "payments",
					},
					{
						"depends_on": "accept_payment",
						"fieldname": "payment_gateway",
						"fieldtype": "Link",
						"label": "Payment Gateway",
						"options": "Payment Gateway",
						"insert_after": "accept_payment",
					},
					{
						"default": "Buy Now",
						"depends_on": "accept_payment",
						"fieldname": "payment_button_label",
						"fieldtype": "Data",
						"label": "Button Label",
						"insert_after": "payment_gateway",
					},
					{
						"depends_on": "accept_payment",
						"fieldname": "payment_button_help",
						"fieldtype": "Text",
						"label": "Button Help",
						"insert_after": "payment_button_label",
					},
					{
						"fieldname": "payments_cb",
						"fieldtype": "Column Break",
						"insert_after": "payment_button_help",
					},
					{
						"default": "0",
						"depends_on": "accept_payment",
						"fieldname": "amount_based_on_field",
						"fieldtype": "Check",
						"label": "Amount Based On Field",
						"insert_after": "payments_cb",
					},
					{
						"depends_on": "eval:doc.accept_payment && doc.amount_based_on_field",
						"fieldname": "amount_field",
						"fieldtype": "Select",
						"label": "Amount Field",
						"insert_after": "amount_based_on_field",
					},
					{
						"depends_on": "eval:doc.accept_payment && !doc.amount_based_on_field",
						"fieldname": "amount",
						"fieldtype": "Currency",
						"label": "Amount",
						"insert_after": "amount_field",
					},
					{
						"depends_on": "accept_payment",
						"fieldname": "currency",
						"fieldtype": "Link",
						"label": "Currency",
						"options": "Currency",
						"insert_after": "amount",
					},
				]
			}
		)

		frappe.clear_cache(doctype="Web Form")

	if "erpnext" in frappe.get_installed_apps():
		custom_fields = {
			"GoCardless Mandate": [
				{
					"fieldname": "customer",
					"fieldtype": "Link",
					"in_list_view": 1,
					"label": "Customer",
					"options": "Customer",
					"reqd": 1,
					"insert_after": "disabled",
				}
			]
		}

		create_custom_fields(custom_fields)


def delete_custom_fields():
	if not frappe.get_meta("Web Form").has_field("payments_tab"):
		return

	click.secho("* Uninstalling Payment Custom Fields from Web Form")
	frappe.db.delete(
		"Custom Field",
		{
			"dt": "Web Form",
			"fieldname": (
				"in",
				(
					"payments_tab",
					"accept_payment",
					"payment_gateway",
					"payment_button_label",
					"payment_button_help",
					"payments_cb",
					"amount_field",
					"amount_based_on_field",
					"amount",
					"currency",
				),
			),
		},
	)

	frappe.clear_cache(doctype="Web Form")


def before_install():
	# TODO: remove this
	# This is done for erpnext CI patch test
	#
	# Since we follow a flow like install v14 -> restore v10 site
	# -> migrate to v12, v13 and then v14 again
	#
	# This app fails installing when the site is restored to v10 as
	# a lot of apis don;t exist in v10 and this is a (at the moment) required app for erpnext.
	if not frappe.get_meta("Module Def").has_field("custom"):
		return False


@contextmanager
def erpnext_app_import_guard():
	marketplace_link = '<a href="https://frappecloud.com/marketplace/apps/erpnext">Marketplace</a>'
	github_link = '<a href="https://github.com/frappe/erpnext">GitHub</a>'
	msg = _("erpnext app is not installed. Please install it from {} or {}").format(
		marketplace_link, github_link
	)
	try:
		yield
	except ImportError:
		frappe.throw(msg, title=_("Missing ERPNext App"))
