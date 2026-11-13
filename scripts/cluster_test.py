"""End-to-end checks against a running cluster. Used by CI after `helm install`.

usage: python scripts/cluster_test.py <namespace> <release>

1. A request through the gateway reaches all three services.
2. Network policy: a pod that is not a declared caller cannot reach inventory.
3. Zero downtime: while orders and inventory are each restarted, a steady
   stream of requests through the gateway must see no failures at all.
"""
import json
import subprocess
import sys
import threading
import time
import urllib.request

NAMESPACE, RELEASE = sys.argv[1], sys.argv[2]
LOCAL_PORT = 18080
URL = f"http://127.0.0.1:{LOCAL_PORT}/orders/1001"
EXPECTED_ITEMS = [{"sku": "SKU-TEA", "in_stock": 120}, {"sku": "SKU-MUG", "in_stock": 14}]


def kubectl(*args, check=True, timeout=240):
    return subprocess.run(["kubectl", "-n", NAMESPACE, *args], capture_output=True, text=True, check=check, timeout=timeout)


def fetch():
    with urllib.request.urlopen(URL, timeout=5) as response:
        return response.status, json.loads(response.read())


def main() -> int:
    forward = subprocess.Popen(["kubectl", "-n", NAMESPACE, "port-forward", f"svc/{RELEASE}-gateway", f"{LOCAL_PORT}:8080"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                fetch()
                break
            except Exception:
                time.sleep(0.2)

        print("1. request path")
        status, body = fetch()
        assert status == 200 and body["items"] == EXPECTED_ITEMS and body["degraded"] is False, body
        print(f"   gateway -> orders -> inventory: {body}")

        print("2. network policy")
        probe = ("import urllib.request,sys\n"
                 "try:\n urllib.request.urlopen(sys.argv[1], timeout=4); print('REACHED')\n"
                 "except Exception as e: print('BLOCKED', type(e).__name__)")
        blocked = kubectl("exec", f"deploy/{RELEASE}-gateway", "--", "python", "-c", probe,
                          f"http://{RELEASE}-inventory:8080/healthz").stdout.strip()
        allowed = kubectl("exec", f"deploy/{RELEASE}-orders", "--", "python", "-c", probe,
                          f"http://{RELEASE}-inventory:8080/healthz").stdout.strip()
        print(f"   gateway -> inventory (not declared): {blocked}")
        print(f"   orders  -> inventory (declared):     {allowed}")
        assert blocked.startswith("BLOCKED") and allowed == "REACHED", "network policy is not enforced as declared"

        print("3. rolling restarts under load")
        results = {"ok": 0, "failed": 0, "degraded": 0}
        errors, stop = [], threading.Event()

        def load():
            while not stop.is_set():
                try:
                    status, body = fetch()
                    if status != 200:
                        results["failed"] += 1
                        errors.append(f"status {status}")
                    elif body["degraded"]:
                        results["degraded"] += 1
                    else:
                        results["ok"] += 1
                except Exception as exc:
                    results["failed"] += 1
                    errors.append(repr(exc))
                time.sleep(0.1)

        worker = threading.Thread(target=load)
        worker.start()
        time.sleep(2)
        for service in ("orders", "inventory"):
            before = kubectl("get", "pods", "-l", f"app.kubernetes.io/name={service}", "-o", "jsonpath={.items[*].metadata.name}").stdout.split()
            kubectl("rollout", "restart", f"deployment/{RELEASE}-{service}")
            kubectl("rollout", "status", f"deployment/{RELEASE}-{service}", "--timeout=180s")
            kubectl("wait", "--for=delete", *[f"pod/{p}" for p in before], "--timeout=120s", check=False)
            after = kubectl("get", "pods", "-l", f"app.kubernetes.io/name={service}", "-o", "jsonpath={.items[*].metadata.name}").stdout.split()
            assert not set(before) & set(after), "pods were not replaced"
            print(f"   {service}: replaced {len(before)} pods with {len(after)} new ones")
        time.sleep(2)
        stop.set()
        worker.join()
        print(f"   requests during restarts: {results['ok']} ok, {results['degraded']} degraded, {results['failed']} failed")
        for error in errors[:5]:
            print(f"   error: {error}")
        assert results["ok"] > 50, "the load generator barely ran"
        assert results["failed"] == 0 and results["degraded"] == 0, "requests were lost during a rolling restart"
        print("all cluster checks passed")
        return 0
    finally:
        forward.terminate()


if __name__ == "__main__":
    sys.exit(main())
