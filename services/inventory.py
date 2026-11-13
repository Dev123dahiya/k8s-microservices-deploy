"""inventory: how many of each product are in stock. Sample data held in memory."""
from __future__ import annotations

from .common import Service, env_port

STOCK = {"SKU-TEA": 120, "SKU-MUG": 14, "SKU-KETTLE": 0}      # sample data, not a real catalogue


def build(port: int = 0, **kwargs) -> Service:
    def stock(sku: str, request_id: str):
        if sku not in STOCK:
            return 404, {"error": "unknown sku", "sku": sku}
        return 200, {"sku": sku, "in_stock": STOCK[sku]}

    return Service("inventory", {"/stock/": stock}, port, **kwargs)


if __name__ == "__main__":
    build(env_port()).run()
