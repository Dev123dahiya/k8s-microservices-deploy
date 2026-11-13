import io
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

from services import gateway, inventory, orders
from services.common import Service

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def get(port, path, headers=None):
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=5) as r:
            return r.status, json.loads(r.read()), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read()), dict(e.headers)


@pytest.fixture
def stack():
    """All three services on free local ports, wired together the way the chart wires them."""
    logs = {name: io.StringIO() for name in ("inventory", "orders", "gateway")}
    inv = inventory.build(log=logs["inventory"]).start()
    ords = orders.build(f"http://127.0.0.1:{inv.port}", log=logs["orders"]).start()
    gw = gateway.build(f"http://127.0.0.1:{ords.port}", log=logs["gateway"]).start()
    running = {"inventory": inv, "orders": ords, "gateway": gw, "logs": logs}
    yield running
    for name in ("gateway", "orders", "inventory"):
        if not running[name].draining.is_set():
            running[name].stop()


def test_request_flows_through_all_three_services(stack):
    status, body, _ = get(stack["gateway"].port, "/orders/1001")
    assert status == 200
    assert body == {"id": "1001", "customer": "asha", "degraded": False,
                    "items": [{"sku": "SKU-TEA", "in_stock": 120}, {"sku": "SKU-MUG", "in_stock": 14}]}


def test_not_found_is_passed_through_not_turned_into_an_error(stack):
    assert get(stack["gateway"].port, "/orders/9999")[0] == 404
    assert get(stack["gateway"].port, "/nope")[0] == 404
    assert get(stack["inventory"].port, "/stock/SKU-NOPE")[0] == 404


def test_request_id_is_carried_across_every_hop_and_into_every_log(stack):
    _, _, headers = get(stack["gateway"].port, "/orders/1002", {"X-Request-Id": "req-abc"})
    assert headers["X-Request-Id"] == "req-abc"
    time.sleep(0.05)
    for name in ("gateway", "orders", "inventory"):
        lines = [json.loads(line) for line in stack["logs"][name].getvalue().splitlines()]
        assert any(line.get("request_id") == "req-abc" and line["status"] == 200 for line in lines), name
    _, _, generated = get(stack["gateway"].port, "/orders/1002")
    assert len(generated["X-Request-Id"]) == 32                    # one is created when the caller sends none


def test_inventory_down_degrades_the_answer_instead_of_failing(stack):
    stack["inventory"].stop()
    status, body, _ = get(stack["gateway"].port, "/orders/1001")
    assert status == 200 and body["degraded"] is True
    assert body["items"] == [{"sku": "SKU-TEA", "in_stock": None}, {"sku": "SKU-MUG", "in_stock": None}]


def test_orders_down_is_a_502_from_the_gateway_and_the_gateway_stays_healthy(stack):
    stack["orders"].stop()
    status, body, _ = get(stack["gateway"].port, "/orders/1001")
    assert status == 502 and body["error"] == "upstream unavailable"
    assert get(stack["gateway"].port, "/healthz")[0] == 200        # liveness ignores dependencies
    assert get(stack["gateway"].port, "/readyz")[0] == 200


def test_health_checks_are_not_logged(stack):
    get(stack["gateway"].port, "/healthz"); get(stack["gateway"].port, "/readyz")
    assert stack["logs"]["gateway"].getvalue() == ""


def test_handler_bug_returns_500_and_the_server_keeps_working():
    def boom(_, request_id):
        raise RuntimeError("bug")
    service = Service("t", {"/boom": boom, "/ok": lambda _, r: (200, {"ok": True})}, log=io.StringIO()).start()
    try:
        assert get(service.port, "/boom")[0] == 500
        assert get(service.port, "/ok")[0] == 200
    finally:
        service.stop()


def test_readiness_fails_while_draining_but_liveness_does_not():
    service = Service("t", {}, log=io.StringIO()).start()
    try:
        assert get(service.port, "/readyz")[0] == 200
        service.draining.set()                                     # what SIGTERM does first
        assert get(service.port, "/readyz")[0] == 503
        assert get(service.port, "/healthz")[0] == 200             # draining is not a reason to be killed early
    finally:
        service.stop()


def test_stop_waits_for_a_request_in_flight():
    release = threading.Event()

    def slow(_, request_id):
        release.wait(5)
        return 200, {"done": True}

    service = Service("t", {"/slow": slow}, log=io.StringIO()).start()
    result = {}
    client = threading.Thread(target=lambda: result.update(response=get(service.port, "/slow")))
    client.start()
    while service.in_flight == 0:
        time.sleep(0.01)
    stopper = threading.Thread(target=service.stop)
    stopper.start()
    time.sleep(0.2)
    assert stopper.is_alive()                                      # still waiting for the open request
    release.set()
    client.join(5); stopper.join(5)
    assert result["response"][0] == 200 and not stopper.is_alive()


def test_sigterm_on_the_real_process_logs_and_exits_zero():
    env = dict(os.environ, PORT="0", PYTHONPATH=ROOT, PYTHONUNBUFFERED="1")
    proc = subprocess.Popen([sys.executable, "-m", "services.inventory"], env=env, stdout=subprocess.PIPE, text=True, cwd=ROOT)
    try:
        port = json.loads(proc.stdout.readline())["port"]
        assert get(port, "/readyz")[0] == 200
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=10) == 0
        events = [json.loads(line).get("event") for line in proc.stdout.read().splitlines()]
        assert events == ["shutdown requested", "stopped cleanly"]
    finally:
        proc.kill()
