"""scripts/gen-prometheus-jobs.py — proxy-exporter scrape JOBS generated from the telemetry registry.

Pins Phase 2a of the Option-A observability design (docs/observability/agent-less-monitoring.md): moving the
proxy-exporter relabel out of the hand-written prometheus.yml into telemetry/<name>.yml, so adding a proxy
exporter is a descriptor drop-in (closes gap-b). These guard that a proxy method (pve) generates its
prometheus job with the correct multi-target relabel + metrics_path, that a host-agent method generates NO
job (its inline file_sd job suffices), that two methods claiming one job fail loud, and that --check tracks
staleness. The generated file parses as valid scrape configs (yaml.safe_load).
"""
import importlib.util
import os

import pytest
import yaml

from kontroll import catalog

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
pytestmark = pytest.mark.unit


def _gen():
    spec = importlib.util.spec_from_file_location(
        "gen_prometheus_jobs", os.path.join(ROOT, "scripts", "gen-prometheus-jobs.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gen = _gen()
FLEET = gen._load("instance.example/fleet.yml")     # the shipped example, by name — never an operator's fleet


def test_pve_proxy_method_generates_the_proxmox_job():
    """The pve proxy-exporter method generates prometheus/jobs.d/proxmox.generated.yml with metrics_path
    /pve, a file_sd over targets/proxmox, and the multi-target relabel that proxies the device address onto
    pve-exporter:9221 — the relabel that used to be hand-written in prometheus.yml, now DERIVED from the
    descriptor (gap-b closed). Parsing the body proves it is valid scrape-config YAML, not just a string."""
    body = gen.jobs_files(FLEET)["prometheus/jobs.d/proxmox.generated.yml"]
    jobs = yaml.safe_load(body)["scrape_configs"]      # the file is a mapping with a scrape_configs: key
    assert len(jobs) == 1
    job = jobs[0]
    assert job["job_name"] == "proxmox" and job["metrics_path"] == "/pve"
    assert job["file_sd_configs"][0]["files"] == ["/etc/prometheus/targets/proxmox/*.yml"]
    rc = job["relabel_configs"]
    assert rc[0] == {"source_labels": ["__address__"], "target_label": "__param_target"}
    assert rc[1] == {"source_labels": ["__param_target"], "target_label": "instance"}
    assert rc[2] == {"target_label": "__address__", "replacement": "pve-exporter:9221"}


def test_proxy_methods_get_a_job_host_agents_do_not():
    """Each proxy-exporter method an enabled module uses gets ONE generated job (pve -> proxmox, snmp ->
    network, blackbox -> blackbox); host-agent methods (host_node) get NONE — their scrape comes from the
    inline `node` file_sd job. Guards that only proxy methods produce a job, that snmp and blackbox each get
    their OWN job (different exporters + metrics_paths, so they cannot share one), and that docker_host
    (host_node only) contributes none."""
    assert set(gen.jobs_files(FLEET)) == {"prometheus/jobs.d/proxmox.generated.yml",
                                          "prometheus/jobs.d/network.generated.yml",
                                          "prometheus/jobs.d/blackbox.generated.yml"}


def test_two_methods_claiming_one_job_fails_loud(monkeypatch):
    """If two telemetry methods resolve to the same prometheus job name, generation exits non-zero rather
    than silently letting one clobber the other — the job-name uniqueness guard across proxy methods (a
    promtool duplicate-job_name config error, caught at generate time instead)."""
    methods = {m["name"]: m for m in catalog.load_telemetry()}
    methods["pve2"] = dict(methods["pve"], name="pve2")             # a 2nd method on job 'proxmox'
    fake = {"inventory_group": "hypervisors", "metrics": [{"method": "pve"}, {"method": "pve2"}]}
    real_load = gen._load
    monkeypatch.setattr(gen, "_load", lambda rel: fake if rel.startswith("modules/") else real_load(rel))
    with pytest.raises(SystemExit):
        gen.jobs_files({"enabled_modules": ["proxmox"]}, methods=methods)


def test_check_mode_passes_when_committed_jobs_are_fresh():
    """`gen-prometheus-jobs.py --check` returns 0 when prometheus/jobs.d/*.generated.yml match what the
    registry + modules produce — the tests/validate guard that the committed job files cannot drift from the
    telemetry descriptors (generated-never-hand-maintained, machine-enforced, like gen-observability)."""
    assert gen.main(["--check"]) == 0
