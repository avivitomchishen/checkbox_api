from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Union

import requests
from requests import Response

logger = logging.getLogger(__name__)

Number = Union[int, float]


@dataclass(frozen=True)
class CheckboxConfig:
    check_url: str = "https://check.checkbox.ua/"
    api_url: str = "https://api.checkbox.ua/api/"
    api_version: str = "v1"
    client_name: str = "Name"
    client_version: str = "v1"
    timeout_sec: int = 25


class CheckboxAPI:
    """Class for work with Checkbox API (clean & standalone)."""

    discount_value = "VALUE"
    discount_percent = "PERCENT"
    cash_payment_type = "CASH"
    no_cash_payment_type = "CASHLESS"
    full_payment = "FULL"
    prepayment = "PREPAYMENT"
    postpaid = "POSTPAID"

    def __init__(self, config: Optional[CheckboxConfig] = None):
        self.cfg = config or CheckboxConfig()
        self.session = requests.Session()
        self.session.headers.update(
            {
                "accept": "application/json",
                "Content-Type": "application/json",
                "X-Client-Name": self.cfg.client_name,
                "X-Client-Version": self.cfg.client_version,
            }
        )

    @staticmethod
    def _error(status_code: int, error: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        msg = (payload or {}).get("message") or error
        return {"success": False, "status": status_code, "error": error, "error_description": msg,
                "data": payload or {}}

    def _parse(self, r: Response) -> Dict[str, Any]:
        try:
            data = r.json()
        except ValueError:
            data = {"raw": r.text}
        ok = 200 <= r.status_code < 300
        return {"success": ok, "status": r.status_code, "data": data}

    def _request(
            self,
            method: str,
            endpoint: str,
            headers: Optional[Dict[str, str]] = None,
            params: Optional[Dict[str, Any]] = None,
            body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        url = f"{self.cfg.api_url}{self.cfg.api_version}/{endpoint.lstrip('/')}"
        try:
            r = self.session.request(
                method=method.upper(),
                url=url,
                headers={**self.session.headers, **(headers or {})},
                params=params or {},
                json=body or {},
                timeout=self.cfg.timeout_sec,
            )
            r.raise_for_status()
            return self._parse(r)
        except requests.exceptions.HTTPError as e:
            logger.error("Checkbox HTTP error: %s", e)
            status = getattr(e.response, "status_code", 400)
            try:
                payload = e.response.json()
            except Exception:
                payload = {"raw": getattr(e.response, "text", str(e))}
            return self._error(status, "HTTP error", payload)
        except requests.exceptions.RequestException as e:
            logger.error("Checkbox network error: %s", e)
            return self._error(400, "Network error", {"detail": str(e)})

    def cashier_signin(self, login: Optional[str] = None, password: Optional[str] = None) -> Dict[str, Any]:
        """
        Getting the cashier's token by credentials.
        """
        if not login or not password:
            return self._error(400, "Bad request", {"detail": "login and password are required"})
        return self._request("POST", "cashier/signin", body={"login": login, "password": password})

    def open_shift(self, license_key: str, access_token: str) -> Dict[str, Any]:
        """Create shift."""
        return self._request(
            "POST",
            "shifts",
            headers={"X-License-Key": license_key, "Authorization": f"Bearer {access_token}"},
            body={"id": str(uuid.uuid4())},
        )

    def status_shift(self, access_token: str, shift_id: str) -> Dict[str, Any]:
        """Get shift info."""
        return self._request(
            "GET",
            f"shifts/{shift_id}",
            headers={"Authorization": f"Bearer {access_token}"},
        )

    def close_shift(self, license_key: str, access_token: str, shift_id: str) -> Dict[str, Any]:
        """Close shift by Senior Cashier."""
        return self._request(
            "POST",
            f"shifts/{shift_id}/close",
            headers={"X-License-Key": license_key, "Authorization": f"Bearer {access_token}"},
        )

    def create_receipt(
            self,
            access_token: str,
            cashier: str,
            order_id: int,
            client: Dict[str, Any],
            products: List[Dict[str, Any]],
            discount: Optional[Dict[str, Any]],
            payment: Dict[str, Any],
            relation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Create a receipt in the system.
        """
        goods: List[Dict[str, Any]] = []
        for p in products:
            goods.append(
                {
                    "good": {
                        "code": f"order_product_{p.get('order_product')}",
                        "name": p.get("product_name"),
                        "price": int(round(float(p.get("product_price", 0)) * 100)),
                    },
                    "quantity": 1000,  # 1.000
                    "is_return": False,
                    "is_winnings_payout": False,
                }
            )

        receipt: Dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "cashier_name": cashier,
            "goods": goods,
            "delivery": {"phone": client.get("phone")},
            "payments": [
                {
                    "type": payment.get("payment_type"),
                    "value": int(round(float(payment.get("total_amount", 0)) * 100)),
                    "label": f"Order #{order_id}",
                    "payment_system": payment.get("payment_system"),
                    "rrn": payment.get("payment_system_invoice_id"),
                    "owner_name": client.get("full_name"),
                    "signature_required": False,
                }
            ],
            "rounding": False,
        }

        if discount:
            if discount.get("discount_type") == self.discount_value:
                discount_value = int(round(float(discount.get("discount_amount", 0)) * 100))
            else:
                discount_value = discount.get("discount_amount")
            receipt["discounts"] = [
                {
                    "type": "DISCOUNT",
                    "mode": discount.get("discount_type"),
                    "value": discount_value,
                    "name": "Знижка",
                }
            ]

        payment_method = payment.get("payment_method")
        headers = {"Authorization": f"Bearer {access_token}"}

        if payment_method == self.prepayment:
            return self._request("POST", "prepayment-receipts", headers=headers, body=receipt)

        if payment_method == self.postpaid:
            if not relation_id:
                return self._error(400, "Bad request", {"detail": "relation_id is required for POSTPAID"})
            rec = {k: v for k, v in receipt.items() if k != "goods"}
            return self._request("POST", f"prepayment-receipts/{relation_id}", headers=headers, body=rec)

        return self._request("POST", "receipts/sell", headers=headers, body=receipt)
