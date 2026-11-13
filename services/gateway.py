"""gateway: the only service exposed outside the cluster. Forwards order lookups to orders."""
from __future__ import annotations

import os

from .common import Service, call, env_port


def build(orders_url: str, port: int = 0, **kwargs) -> Service:
    def order(order_id: str, request_id: str):
        # The order itself is essential: if orders is down, call() raises and the client gets a 502.
        return call(f"{orders_url}/orders/{order_id}", request_id)

    def index(_: str, request_id: str):
        return 200, {"service": "gateway", "routes": ["/orders/<id>", "/healthz", "/readyz"]}

    return Service("gateway", {"/orders/": order, "/": index}, port, **kwargs)


if __name__ == "__main__":
    build(os.environ.get("ORDERS_URL", "http://orders:8080"), env_port()).run()
