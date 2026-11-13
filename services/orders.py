"""orders: returns an order and asks inventory for the stock level of each item."""
from __future__ import annotations

import os

from .common import DownstreamError, Service, call, env_port

ORDERS = {                                                     # sample data, not real orders
    "1001": {"customer": "asha", "items": ["SKU-TEA", "SKU-MUG"]},
    "1002": {"customer": "ravi", "items": ["SKU-KETTLE"]},
}


def build(inventory_url: str, port: int = 0, **kwargs) -> Service:
    def order(order_id: str, request_id: str):
        if order_id not in ORDERS:
            return 404, {"error": "unknown order", "id": order_id}
        record = ORDERS[order_id]
        items, degraded = [], False
        for sku in record["items"]:
            try:
                status, body = call(f"{inventory_url}/stock/{sku}", request_id)
                items.append({"sku": sku, "in_stock": body.get("in_stock") if status == 200 else None})
            except DownstreamError:
                # Stock is extra detail. If inventory is down, still return the order and say what is missing.
                items.append({"sku": sku, "in_stock": None})
                degraded = True
        return 200, {"id": order_id, "customer": record["customer"], "items": items, "degraded": degraded}

    return Service("orders", {"/orders/": order}, port, **kwargs)


if __name__ == "__main__":
    build(os.environ.get("INVENTORY_URL", "http://inventory:8080"), env_port()).run()
