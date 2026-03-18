"""PayFast Payment Gateway Controller.

Implements the Frappe Payments gateway interface:
- validate() — registers PayFast in the Payment Gateway registry
- validate_transaction_currency() — ensures ZAR
- get_payment_url() — returns URL to redirect user to PayFast checkout
"""

import frappe
from frappe import _
from frappe.integrations.utils import create_request_log
from frappe.model.document import Document
from frappe.utils import call_hook_method, get_url

from payments.utils import create_payment_gateway


class PayFastSettings(Document):
    supported_currencies = ("ZAR",)

    def validate(self):
        create_payment_gateway("PayFast")
        call_hook_method("payment_gateway_enabled", gateway="PayFast")

    def validate_transaction_currency(self, currency):
        if currency not in self.supported_currencies:
            frappe.throw(
                _("PayFast does not support transactions in currency '{0}'").format(currency)
            )

    def get_payment_url(self, **kwargs):
        integration_request = create_request_log(kwargs, service_name="PayFast")
        return get_url(f"./payfast_checkout?token={integration_request.name}")

    def get_process_url(self):
        if self.process_url:
            return self.process_url
        if self.sandbox_mode:
            return "https://sandbox.payfast.co.za/eng/process"
        return "https://www.payfast.co.za/eng/process"

    def get_notify_url(self):
        return self.notify_url or get_url(
            "/api/method/icff_payfast.api.gateway.handle_itn"
        )

    def get_return_url(self):
        return self.return_url or get_url("/payment-success")

    def get_cancel_url(self):
        return self.cancel_url or get_url("/payment-cancelled")

    def get_passphrase(self):
        try:
            return self.get_password("passphrase") or ""
        except Exception:
            frappe.log_error("PayFast: failed to read passphrase")
            return ""
