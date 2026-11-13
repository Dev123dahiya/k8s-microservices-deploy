"""Checks on the rendered Helm chart: what `helm template` produces, not the template text.

Needs the `helm` binary. Without it these tests are skipped, unless REQUIRE_HELM=1
(set in CI), in which case a missing helm is a failure rather than a silent skip.
SHOP_RENDER may name an alternative command taking: <chart dir> <release> <values file or "">.
"""
import os
import shutil
import subprocess

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHART = os.path.join(ROOT, "chart")
RELEASE = "shop"
SELECTOR_KEYS = ("app.kubernetes.io/name", "app.kubernetes.io/instance")


def render(values_file=""):
    extra = os.path.join(CHART, values_file) if values_file else ""
    custom = os.environ.get("SHOP_RENDER")
    if custom:
        command = [custom, "chart", RELEASE, os.path.join("chart", values_file) if values_file else ""]
    elif shutil.which("helm"):
        command = ["helm", "template", RELEASE, CHART] + (["-f", extra] if extra else [])
    elif os.environ.get("REQUIRE_HELM") == "1":
        pytest.fail("helm is required but not installed")
    else:
        pytest.skip("helm is not installed")
    out = subprocess.run(command, capture_output=True, text=True, cwd=ROOT)
    assert out.returncode == 0, out.stderr
    return [doc for doc in yaml.safe_load_all(out.stdout) if doc]


def by_kind(docs, kind):
    return {d["metadata"]["name"]: d for d in docs if d["kind"] == kind}


@pytest.fixture(scope="module", params=["", "values-dev.yaml", "values-prod.yaml"], ids=["default", "dev", "prod"])
def docs(request):
    return render(request.param)


@pytest.fixture(scope="module")
def values():
    with open(os.path.join(CHART, "values.yaml")) as f:
        return yaml.safe_load(f)


def test_every_service_gets_a_deployment_service_budget_and_policy(docs, values):
    names = {f"{RELEASE}-{name}" for name in values["services"]}
    for kind in ("Deployment", "Service", "PodDisruptionBudget", "NetworkPolicy"):
        assert set(by_kind(docs, kind)) == names, kind


def test_selectors_match_pod_labels_everywhere(docs):
    """A Service, budget or policy whose selector misses the pods fails silently in a cluster."""
    deployments = by_kind(docs, "Deployment")
    for name, deployment in deployments.items():
        pod_labels = deployment["spec"]["template"]["metadata"]["labels"]
        selector = deployment["spec"]["selector"]["matchLabels"]
        assert selector.items() <= pod_labels.items()
        assert by_kind(docs, "Service")[name]["spec"]["selector"] == selector
        assert by_kind(docs, "PodDisruptionBudget")[name]["spec"]["selector"]["matchLabels"] == selector
        assert by_kind(docs, "NetworkPolicy")[name]["spec"]["podSelector"]["matchLabels"] == selector
    selectors = [tuple(d["spec"]["selector"]["matchLabels"][k] for k in SELECTOR_KEYS) for d in deployments.values()]
    assert len(set(selectors)) == len(selectors)                   # no two services select each other's pods


def test_pods_are_hardened(docs):
    for name, deployment in by_kind(docs, "Deployment").items():
        spec = deployment["spec"]["template"]["spec"]
        assert spec["securityContext"]["runAsNonRoot"] is True and spec["securityContext"]["runAsUser"] > 0
        assert spec["automountServiceAccountToken"] is False
        [container] = spec["containers"]
        ctx = container["securityContext"]
        assert ctx == {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}}
        assert set(container["resources"]["limits"]) == set(container["resources"]["requests"]) == {"cpu", "memory"}
        assert not container["image"].endswith(":latest") and ":" in container["image"]


def test_rollouts_are_zero_downtime_by_construction(docs):
    for deployment in by_kind(docs, "Deployment").values():
        spec = deployment["spec"]
        assert spec["strategy"]["rollingUpdate"] == {"maxUnavailable": 0, "maxSurge": 1}
        pod = spec["template"]["spec"]
        [container] = pod["containers"]
        assert container["readinessProbe"]["httpGet"]["path"] == "/readyz"
        assert container["livenessProbe"]["httpGet"]["path"] == "/healthz"       # liveness must not depend on readiness
        pre_stop = int(container["lifecycle"]["preStop"]["exec"]["command"][1])
        assert 0 < pre_stop < pod["terminationGracePeriodSeconds"]              # time left to drain after the sleep
        named_ports = {p["name"]: p["containerPort"] for p in container["ports"]}
        assert container["readinessProbe"]["httpGet"]["port"] in named_ports


def test_upstream_urls_point_at_services_that_exist(docs, values):
    services = by_kind(docs, "Service")
    for name, svc in values["services"].items():
        [container] = by_kind(docs, "Deployment")[f"{RELEASE}-{name}"]["spec"]["template"]["spec"]["containers"]
        env = {e["name"]: e["value"] for e in container["env"]}
        assert container["command"] == ["python", "-m", svc["module"]]
        assert env["PORT"] == str(values["port"])
        for variable, target in svc["upstreams"].items():
            host, port = env[variable].removeprefix("http://").split(":")
            assert host in services and int(port) == services[host]["spec"]["ports"][0]["port"]
        module_path = os.path.join(ROOT, *svc["module"].split(".")) + ".py"
        assert os.path.exists(module_path), module_path


def test_network_policies_allow_exactly_the_call_graph(docs, values):
    """Allowed (caller -> callee) pairs derived from the policies equal the pairs declared in values."""
    declared = {(caller, target) for caller, svc in values["services"].items() for target in svc["upstreams"].values()}
    allowed, open_to_all = set(), set()
    for policy in by_kind(docs, "NetworkPolicy").values():
        callee = policy["spec"]["podSelector"]["matchLabels"]["app.kubernetes.io/name"]
        assert policy["spec"]["policyTypes"] == ["Ingress"]
        for rule in policy["spec"].get("ingress") or []:
            assert rule["ports"] == [{"port": values["port"]}]
            if "from" not in rule:
                open_to_all.add(callee)
            for peer in rule.get("from", []):
                allowed.add((peer["podSelector"]["matchLabels"]["app.kubernetes.io/name"], callee))
    assert allowed == declared == {("gateway", "orders"), ("orders", "inventory")}
    assert open_to_all == {name for name, svc in values["services"].items() if svc["public"]} == {"gateway"}


def test_autoscaler_and_fixed_replicas_never_fight():
    """With the autoscaler on, the Deployment must not set replicas, or every `helm upgrade` resets the scale."""
    prod, dev = render("values-prod.yaml"), render("values-dev.yaml")
    assert all("replicas" not in d["spec"] for d in by_kind(prod, "Deployment").values())
    hpas = by_kind(prod, "HorizontalPodAutoscaler")
    assert set(hpas) == set(by_kind(prod, "Deployment"))
    for name, hpa in hpas.items():
        assert hpa["spec"]["scaleTargetRef"] == {"apiVersion": "apps/v1", "kind": "Deployment", "name": name}
        assert (hpa["spec"]["minReplicas"], hpa["spec"]["maxReplicas"]) == (3, 12)
    assert by_kind(dev, "HorizontalPodAutoscaler") == {}
    assert all(d["spec"]["replicas"] == 2 for d in by_kind(dev, "Deployment").values())


def test_budget_never_blocks_a_node_drain(docs):
    """minAvailable must be below the lowest replica count, or a drain can never evict a pod."""
    hpas = by_kind(docs, "HorizontalPodAutoscaler")
    for name, deployment in by_kind(docs, "Deployment").items():
        floor = hpas[name]["spec"]["minReplicas"] if name in hpas else deployment["spec"]["replicas"]
        assert by_kind(docs, "PodDisruptionBudget")[name]["spec"]["minAvailable"] < floor
