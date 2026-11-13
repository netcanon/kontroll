"""scripts/gen-secret-env.py — the UNIFIED secret->env manifest generator (telemetry secret_env_map + logging
secret_env_map -> ONE value-free manifest deploy-stack consumes).

Pins the fail-CLOSED config-injection guards (a malformed env name / template / a platform-core collision exits
non-zero, never reaching the manifest or the rendered .env), the value-free (NAMES-only) C12 contract, the
--check staleness contract, and the platform-core boundary (a device descriptor can never shadow the control
plane's own creds). The design + the must-fix bar: docs/reviews/2026-06-17-secret-injection/.
"""
import importlib.util
import os
import re

import pytest
from _deploy_walk import Walk, load, strings

from kontroll import paths

pytestmark = pytest.mark.unit


def _gen():
    spec = importlib.util.spec_from_file_location(
        "gen_secret_env", os.path.join(paths.ROOT, "scripts", "gen-secret-env.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_example_fleet_manifest_unifies_telemetry_and_logging_env_value_free():
    """The shipped example fleet's manifest carries BOTH the telemetry exporter env (proxmox declares `metrics: [pve]`, whose
    secret_env_map -> PVE_EXPORTER_*) AND the logging token (`logs: [proxmox_api]` -> KONTROLL_PVE_LOG_TOKEN), each
    keyed by env var with its secret_domain + a str.format template over SOPS field NAMES — and NO composed value /
    auth scheme (C12: names only). Guards (a) the telemetry env silently dropping out of the union (the smoking-gun
    gap this run closed), (b) the logging token going missing, (c) a credential leaking into the committed manifest."""
    gen = _gen()
    man = gen.secret_env_files(gen._load("instance.example/fleet.yml"))[gen.MANIFEST_REL]
    assert "PVE_EXPORTER_USER:" in man and "KONTROLL_PVE_LOG_TOKEN:" in man, "both axes in ONE manifest"
    assert "secret_domain: proxmox" in man and "{proxmox_api_user}" in man, "domain NAME + field-NAME template"
    assert "PVEAPIToken=" not in man and "root@pam" not in man, "NO composed token / auth scheme / value (C12)"


def test_manifest_always_emits_env_key_even_when_empty():
    """An empty fleet (no enabled module declares a secret_env_map) still emits `env: {}` — so --check stays honest
    when the last credentialed method is removed (a missing key would let a stale manifest pass silently)."""
    gen = _gen()
    man = gen.secret_env_files({"enabled_modules": []})[gen.MANIFEST_REL]
    assert "env:" in man, "the manifest ALWAYS emits an env: key (empty when nothing declares secret_env_map)"


def _method(name, sem, domain="proxmox"):
    return {"name": name, "secret_domain": domain, "secret_env_map": sem}


def test_secret_env_map_shape_is_fail_closed():
    """A malformed secret_env_map exits non-zero at GENERATE time (the config-injection guard): a non-UPPER_SNAKE
    env name, an empty/non-string template, an unsafe template field (format-spec / metacharacter), or a map with
    no secret_domain. Guards a bad descriptor smuggling an extra .env line or a broken token through the loop."""
    gen = _gen()
    with pytest.raises(SystemExit):                                          # lower-case env name
        gen._validate_or_exit(_method("m", {"pve_user": "{a}"}), "k", "telemetry")
    with pytest.raises(SystemExit):                                          # empty template
        gen._validate_or_exit(_method("m", {"PVE_USER": ""}), "k", "telemetry")
    with pytest.raises(SystemExit):                                          # format-spec field (injection)
        gen._validate_or_exit(_method("m", {"PVE_USER": "{a:>9}"}), "k", "telemetry")
    with pytest.raises(SystemExit):                                          # metacharacter field
        gen._validate_or_exit(_method("m", {"PVE_USER": "{a.b}"}), "k", "telemetry")
    with pytest.raises(SystemExit):                                          # secret_env_map but no secret_domain
        gen._validate_or_exit({"name": "m", "secret_env_map": {"PVE_USER": "{a}"}}, "k", "telemetry")
    assert gen._validate_or_exit(_method("m", {"PVE_USER": "{a}"}), "k", "telemetry") == {"PVE_USER": "{a}"}  # OK


def test_env_name_colliding_with_platform_core_is_rejected():
    """A device/consumer secret_env_map emitting a PLATFORM-CORE env name (SEMAPHORE_ADMIN_PASSWORD,
    KONTROLL_API_TOKEN, …) exits non-zero — the loop renders AFTER the platform-core literals, so a duplicate key
    could shadow the control plane's own admin password in a later-wins .env parser. The machine-checked form of
    the platform-core boundary (M2 / §5 of the design); without it a device drop-in could overwrite intrinsic creds."""
    gen = _gen()
    for reserved in ("SEMAPHORE_ADMIN_PASSWORD", "KONTROLL_API_TOKEN", "GRAFANA_ADMIN_PASSWORD"):
        with pytest.raises(SystemExit):
            gen._validate_or_exit(_method("evil", {reserved: "{a}"}), "k", "telemetry")


def test_a_secret_env_map_may_not_read_a_platform_core_domain():
    """A secret_env_map whose secret_domain is a platform-core one (`semaphore`, `dashboards`), a path or other spelling
    of one (`./dashboards`, `x/../dashboards`, `dashboards/`, `dashboards.x`, `Dashboards`) included, exits non-zero,
    naming the domain; and that set is exactly the domains the deploy decrypts fail-closed for its own .env lines, read
    by `_fail_closed_domains` from every `community.sops` use in a string of every file the deploy loads and in every
    template it renders or a var looks up (`_deploy_walk.Walk`: its task files, roles, vars files, each inventory file
    at any depth and the group_vars and host_vars as the tool ships them, imported playbooks and templates, each read
    in the places the walk lists; a YAML comment is none, a block scalar's `#` line is read; whitespace and quotes
    normalised), a use it cannot read, or a form the walk does not model, failing it. Why: the render loop writes
    `<env>=<template>` from the method's domain into docker/.env, so a drop-in naming `{grafana_admin_password}` or
    `{alert_notifier_url}` under a name of its own would hand the Grafana admin password or the alert URL to any
    fragment that reads that name; reserving the platform-core env NAMES did not stop it, and the tests that failed on
    such a drop-in did so only by chance (T32 fold 2 read B, N2: R8, R8b). An exact-name refusal let `./dashboards`
    through to a filter that decrypts the dashboards file for it, and a pin that read one spelling of the lookup passed
    a third platform-core domain written over two lines (fold 3 read B, S1: G1; N2); one written in a block scalar's `#`
    line, which the pin dropped as a comment, or in a task file the deploy imports, which it never read, passed too
    (fold 4's reads 90, S2 and 100, N4), as did one in a template a task renders, in the inventory's group_vars, or in a
    role's include_vars file Ansible loads from its `vars/` in place of the one the walk read (fold 5's reads 111, S2:
    X5, and 110, S2), and one in a template a play var looks up or in the inventory file's own vars, which the walk
    never read (fold 6's reads 120, S1, S2 and 121, S2, N1; the reader's self-test is
    test_the_drift_reader_reads_each_spelling); and one under a prefixed `with_template`, a spaced lookup, an inventory
    subdirectory's file, or a file Ansible loads from a place the walk did not search (fold 7's reads 130, S1, S2 and
    131, S2, S5, S6)."""
    gen = _gen()
    for domain in ("semaphore", "dashboards", "./dashboards", "x/../dashboards", "dashboards/", "dashboards.x",
                   "Dashboards"):
        with pytest.raises(SystemExit, match=re.escape(repr(domain))):
            gen._validate_or_exit(_method("m", {"PVE_EXPORTER_USER": "{a}"}, domain), "k", "telemetry")
    _assert_the_core_set_is_the_deploys(Walk(paths.ROOT), gen.PLATFORM_CORE_DOMAINS)


_SOPS_USE = re.compile(r"community\.sops\.[^)]*\)?")
_CLOSED = re.compile(r"community\.sops\.sops',_secrets_dir~'/([\w-]+)\.sops\.yml'\)")
_SOFT = re.compile(r"community\.sops\.sops',_secrets_dir~'/[\w-]+\.sops\.yml',errors='ignore'\)")


def _fail_closed_domains(w):
    """(the domains decrypted fail-closed, [(file, use)] for each use this cannot read), over every key and string
    of every file `w`, a `_deploy_walk.Walk`, opened, and the text of every template it read: each
    `community.sops` use, whitespace dropped and quotes made one, is a fail-closed lookup of
    `_secrets_dir ~ '/<domain>.sops.yml'`, the same with `errors='ignore'`, or unread."""
    closed, unread = set(), []
    texts = [(f, text) for f in dict.fromkeys(w.opened) for text in strings(load(f))]
    texts += [(f, w.template_text(f)) for f in w.template_files]
    for f, text in texts:
        for use in _SOPS_USE.findall(re.sub(r"\s+", "", text).replace('"', "'")):
            m = _CLOSED.fullmatch(use)
            if m:
                closed.add(m.group(1))
            elif not _SOFT.fullmatch(use):
                unread.append((f, use))
    return closed, unread


def _assert_the_core_set_is_the_deploys(w, core):
    """The pin itself: every `community.sops` use in `w` is read, and the fail-closed domains are exactly `core`."""
    closed, unread = _fail_closed_domains(w)
    assert unread == [], unread         # a use of the domain this pin cannot read fails here, not in silence
    assert closed == core, closed


_GUI = "lookup('community.sops.sops', _secrets_dir ~ '/gui.sops.yml')"
_DRIFT_BASE = """- hosts: localhost
  roles: [r]
  vars:
    _sem: "{{ lookup('community.sops.sops', _secrets_dir ~ '/semaphore.sops.yml') | from_yaml }}"
    _dash: "{{ lookup('community.sops.sops', _secrets_dir ~ '/dashboards.sops.yml') | from_yaml }}"
    # _old: "{{ %s | from_yaml }}"
    _soft: >-
      {{ lookup('community.sops.sops', _secrets_dir ~ '/snmp.sops.yml',
                errors='ignore') }}
%%s  tasks:
    - ansible.builtin.import_tasks: tasks/t.yml
%%s""" % _GUI
_DRIFT_SHAPES = {   # (play vars, more plays, {file under the tree: its text}, the third domain closed, or "unread")
    "T6-a-block-scalar-hash-line": ("    _note: |\n      # {{ %s | length }}\n" % _GUI, "", {}, "gui"),
    "a-domain-with-a-hyphen": (
        "    _h: \"{{ lookup('community.sops.sops', _secrets_dir ~ '/pve-lab.sops.yml') }}\"\n", "", {}, "pve-lab"),
    "a-double-quoted-lookup": (
        "    _d: '{{ lookup(\"community.sops.sops\", _secrets_dir ~ \"/gui.sops.yml\") }}'\n", "", {}, "gui"),
    "D1-an-imported-task-file": (
        "", "", {"ansible/playbooks/tasks/t.yml": '- ansible.builtin.debug:\n    msg: "{{ %s }}"\n' % _GUI}, "gui"),
    "a-role-default-in-yaml": ("", "", {"ansible/roles/r/defaults/main.yaml": '_g: "{{ %s }}"\n' % _GUI}, "gui"),
    "an-imported-playbook": (
        "", "- ansible.builtin.import_playbook: other.yml\n",
        {"ansible/playbooks/other.yml": '- hosts: localhost\n  vars:\n    _g: "{{ %s }}"\n' % _GUI}, "gui"),
    "a-strict-lookup": (
        "    _s: \"{{ lookup('community.sops.sops', _secrets_dir ~ '/gui.sops.yml', errors='strict') }}\"\n",
        "", {}, "unread"),
    "the-load-vars-module": (
        "", "", {"ansible/playbooks/tasks/t.yml": "- community.sops.load_vars:\n    file: x.sops.yml\n"}, "unread"),
    "X5-a-template-a-task-renders": (
        "", "", {"ansible/playbooks/tasks/t.yml": "- ansible.builtin.template: {src: g.j2, dest: /x}\n  no_log: true\n",
                 "ansible/playbooks/templates/g.j2": "x={{ %s }}\n" % _GUI}, "gui"),
    "the-inventory-group-vars": ("", "", {"instance.example/inventory/group_vars/all.yml": '_g: "{{ %s }}"\n' % _GUI},
                                 "gui"),
    "a-role-include-vars-from-its-vars": (
        "", "", {"ansible/roles/r/tasks/main.yml": "- ansible.builtin.include_vars: rv.yml\n",
                 "ansible/roles/r/tasks/rv.yml": "_x: plain\n",
                 "ansible/roles/r/vars/rv.yml": '_g: "{{ %s }}"\n' % _GUI},
        "gui"),
    "a-play-var-looks-up-a-template": ("    _t: \"{{ lookup('template', 'g.j2') }}\"\n", "",
                                       {"ansible/playbooks/templates/g.j2": "x={{ %s }}\n" % _GUI}, "gui"),
    "the-inventory-files-own-vars": (
        "", "", {"instance.example/inventory/hosts.yml": 'all:\n  vars:\n    _g: "{{ %s }}"\n' % _GUI}, "gui"),
    "a-template-under-action": ("", "", {
        "ansible/playbooks/tasks/t.yml": "- action: ansible.builtin.template src=g.j2 dest=/x\n  no_log: true\n",
        "ansible/playbooks/templates/g.j2": "x={{ %s }}\n" % _GUI}, "gui"),
    # fold 8, from fold 7's reads 130 (S1, S2: D1, D3-D6, D10) and 131 (S2, S5, S6: D3, D4, D5, D6)
    "with-a-builtin-template": ("", "", {
        "ansible/playbooks/tasks/t.yml": "- ansible.builtin.debug:\n    msg: \"{{ item }}\"\n"
                                         "  with_ansible.builtin.template: [g.j2]\n  no_log: true\n",
        "ansible/playbooks/templates/g.j2": "x={{ %s }}\n" % _GUI}, "gui"),
    "a-spaced-template-lookup": ("    _t: \"{{ lookup ('template', 'g.j2') }}\"\n", "",
                                 {"ansible/playbooks/templates/g.j2": "x={{ %s }}\n" % _GUI}, "gui"),
    "a-nested-playbooks-own-playbook-dir": ("", "- ansible.builtin.import_playbook: sub/other.yml\n", {
        "ansible/playbooks/sub/other.yml": "- hosts: localhost\n  tasks:\n    - ansible.builtin.template:\n"
                                           "        src: \"{{ playbook_dir }}/g.j2\"\n        dest: /x\n"
                                           "      no_log: true\n",
        "ansible/playbooks/sub/g.j2": "x={{ %s }}\n" % _GUI, "ansible/playbooks/g.j2": "plain\n"}, "gui"),
    "a-roles-include-in-the-working-directorys-tasks": ("", "", {
        "ansible/roles/r/tasks/main.yml": "- ansible.builtin.include_tasks: x.yml\n",
        "ansible/tasks/x.yml": "- ansible.builtin.debug:\n    msg: \"{{ %s }}\"\n  no_log: true\n" % _GUI,
        "ansible/playbooks/tasks/x.yml": "- ansible.builtin.debug:\n    msg: hi\n"}, "gui"),
    "a-nested-include-under-its-parents-directory": ("", "", {
        "ansible/playbooks/tasks/t.yml": "- ansible.builtin.include_tasks: in/m.yml\n",
        "ansible/playbooks/tasks/in/m.yml": "- ansible.builtin.include_tasks: l.yml\n",
        "ansible/playbooks/in/l.yml": "- ansible.builtin.debug:\n    msg: \"{{ %s }}\"\n  no_log: true\n" % _GUI,
        "ansible/playbooks/tasks/in/l.yml": "- ansible.builtin.debug:\n    msg: hi\n"}, "gui"),
    "a-roles-lookup-below-its-tasks": ("", "", {
        "ansible/roles/r/tasks/main.yml": "- ansible.builtin.include_tasks: sub/s.yml\n",
        "ansible/roles/r/tasks/sub/s.yml": "- ansible.builtin.debug:\n    msg: \"{{ lookup('template', 'x.j2') }}\"\n"
                                           "  no_log: true\n",
        "ansible/roles/r/tasks/x.j2": "x={{ %s }}\n" % _GUI, "ansible/playbooks/templates/x.j2": "plain\n"}, "gui"),
    "a-roles-handler-include-in-the-playbooks-handlers": ("", "", {
        "ansible/roles/r/handlers/main.yml": "- name: h\n  ansible.builtin.include_tasks: hx.yml\n",
        "ansible/playbooks/handlers/hx.yml": "- ansible.builtin.debug:\n    msg: \"{{ %s }}\"\n  no_log: true\n" % _GUI,
        "ansible/playbooks/hx.yml": "- ansible.builtin.debug:\n    msg: hi\n"}, "gui"),
    "an-inventory-subdirectorys-file": (
        "", "", {"instance.example/inventory/site/hosts.yml": "all:\n  vars:\n    _g: \"{{ %s }}\"\n" % _GUI}, "gui"),
}


def _drift_tree(root, shape):
    play_vars, plays, files, _ = _DRIFT_SHAPES[shape] if shape else ("", "", {}, None)
    files = dict({"ansible/playbooks/deploy-stack.yml": _DRIFT_BASE % (play_vars, plays),
                  "ansible/playbooks/tasks/t.yml": "- ansible.builtin.debug:\n    msg: hi\n",
                  "ansible/roles/r/tasks/main.yml": "- ansible.builtin.debug:\n    msg: hi\n"}, **files)
    for rel, text in files.items():
        path = root.joinpath(*rel.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return Walk(str(root))


@pytest.mark.parametrize("shape", sorted(_DRIFT_SHAPES))
def test_the_drift_reader_reads_each_spelling(tmp_path, shape):
    """`_fail_closed_domains` over a tree of its own reads the two fail-closed lookups and the `errors='ignore'` one
    (written over two lines) beside a YAML comment that names a third, and reports that third domain decrypted
    fail-closed where a block scalar's `#` line, a task file the play imports, a role's `defaults/main.yaml`, an
    imported playbook, a template a task renders (one under `action:` too) or a play var looks up, the inventory file's
    own vars or its group_vars, or a role's include_vars file in its `vars/` beside a plain one in the task's directory
    does it, or a lookup written with double quotes, or of a domain with a hyphen (`pve-lab`, a name the Secrets dialog
    writes), and a use it cannot read (an `errors='strict'` lookup, the `community.sops.load_vars` module) as unread;
    and the pin's own assertion, which the real-tree test makes, passes the base tree and fails each planted one. Why:
    the drift pin had no self-test, dropped a block scalar's `#` line as a comment and read deploy-stack.yml alone, so a
    third platform-core domain decrypted in any of those passed it (fold 4's reads 90, S2, N7 and 100, N4); and the
    real-tree assertion that an unread use fails was held by no row, as the real tree has none, so it could be dropped
    with the suite green (fold 4's read 90, mutant Dr1); a template, the inventory's group_vars and a role's
    include_vars file in its `vars/` passed the pin (fold 5's reads 111, S2: X5, and 110, S2:
    W-include-vars-role-shadow-drift); and a template a play var looks up, the inventory file's own vars and a template
    task under `action:` passed it too, the walk reading none of them (fold 6's reads 120, S1: C6, S2: C9 and 121, S2:
    Q5, Q14, N1); and one a task renders under `with_ansible.builtin.template`, a play var looks up with a space before
    the parenthesis, a nested playbook's `{{ playbook_dir }}` names, a role's include finds in the working directory's
    `tasks/`, a nested include finds under its parent's directory, a role's lookup finds in its `tasks/` from below it,
    a role handler's include finds in the playbook's `handlers/`, or an inventory subdirectory's file holds, passed it,
    each beside a decoy the walk read where there was one (fold 7's reads 130, S1, S2: D1, D3-D6, D10 and 131, S2, S5,
    S6: D3-D6)."""
    assert _fail_closed_domains(_drift_tree(tmp_path / "base", None)) == ({"semaphore", "dashboards"}, [])
    _assert_the_core_set_is_the_deploys(_drift_tree(tmp_path / "base2", None), {"semaphore", "dashboards"})
    with pytest.raises(AssertionError):
        _assert_the_core_set_is_the_deploys(_drift_tree(tmp_path / "planted2", shape), {"semaphore", "dashboards"})
    closed, unread = _fail_closed_domains(_drift_tree(tmp_path / "planted", shape))
    third = _DRIFT_SHAPES[shape][-1]
    if third != "unread":
        assert closed == {"semaphore", "dashboards", third} and unread == [], (closed, unread)
    else:
        assert closed == {"semaphore", "dashboards"} and unread != [], (closed, unread)


def test_a_secret_domain_is_a_name_the_secrets_dialog_takes():
    """gen-secret-env takes a secret_domain the Secrets dialog's own rule takes (`paths.component`: `pve-lab`,
    `9site`, `pve_lab`, 64 characters) and refuses any other, naming it: a lower-case-led path to another file
    (`x/../proxmox`, `proxmox/`, `proxmox.x`), a leading `-` or `_`, an upper-case letter, a space, 65 characters, a
    number. The decrypt filter's copy of the rule, and of the platform-core set, is the tool's. Why: the generator's
    stem rule refused `pve-lab`, a domain the dialog writes, and the filter read it as `{}` in silence; and both
    seats' tests passed a rule weakened to its first letter, every spelling they tried failing there (fold 4's reads
    90, N5, N8 and 100, N1, N2)."""
    gen = _gen()
    for domain in ("pve-lab", "9site", "pve_lab", "a" * 64):
        assert gen._validate_or_exit(_method("m", {"PVE_EXPORTER_USER": "{a}"}, domain), "k", "telemetry")
    for domain in ("x/../proxmox", "proxmox/", "proxmox.x", "-pve", "_pve", "Pve", "pve lab", "a" * 65, 7):
        with pytest.raises(SystemExit, match=re.escape(repr(domain))):
            gen._validate_or_exit(_method("m", {"PVE_EXPORTER_USER": "{a}"}, domain), "k", "telemetry")
    spec = importlib.util.spec_from_file_location(
        "kontroll_sops_seat", os.path.join(paths.ROOT, "ansible", "filter_plugins", "kontroll_sops.py"))
    seat = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(seat)
    assert seat._DOMAIN_RE.pattern == paths._COMPONENT_RE.pattern
    assert seat._PLATFORM_CORE == gen.PLATFORM_CORE_DOMAINS


def test_two_methods_claiming_same_env_with_different_spec_is_rejected(monkeypatch):
    """Two methods (here one telemetry + one logging) claiming the SAME env var with a DIFFERENT (domain, template)
    exits non-zero — a silent env-var collision would let one method's secret overwrite another's in the manifest.
    Guards a cross-axis collision shipping the wrong token. Monkeypatches the registries + the module loader so the
    collision is exercised deterministically (the real fleet has a single proxmox domain, no collision)."""
    gen = _gen()
    tele = {"name": "tele_a", "secret_domain": "d1", "secret_env_map": {"DUP_ENV": "{x}"}}
    logg = {"name": "log_b", "secret_domain": "d2", "secret_env_map": {"DUP_ENV": "{y}"}}
    monkeypatch.setattr(gen.catalog, "load_telemetry", lambda: [tele])
    monkeypatch.setattr(gen.catalog, "load_logging", lambda: [logg])
    monkeypatch.setattr(gen.os.path, "exists", lambda p: True)
    monkeypatch.setattr(gen, "_load",
                        lambda rel: {"metrics": [{"method": "tele_a"}], "logs": [{"method": "log_b"}]})
    with pytest.raises(SystemExit):
        gen.secret_env_files({"enabled_modules": ["k"]})


def test_check_mode_passes_when_committed_manifest_is_fresh():
    """gen-secret-env.py --check == 0 on the freshly-generated tree (the in-pytest twin of the tests/validate
    gate, which is skipped where the shell harness can't run). Guards a secret_env_map edit landing without the
    manifest regenerated — the generated-never-hand-maintained contract."""
    gen = _gen()
    assert gen.main(["--check"]) == 0, "the committed manifest must match a fresh generate (run gen-secret-env.py)"


def test_reserved_platform_core_matches_deploy_stack_env_lines():
    """Every LITERAL `NAME=` env line in deploy-stack.yml's .env render (the platform-core block — NOT the generic
    `{{ env }}=` loop) is in RESERVED_PLATFORM_CORE_ENV, so the collision guard above knows about ALL of them. A
    drift pin: adding a platform-core .env line without adding it to RESERVED would silently open a shadow hole
    (M2). Reads deploy-stack.yml as text and captures the literal env names the .env content block defines."""
    gen = _gen()
    text = open(os.path.join(paths.ROOT, "ansible", "playbooks", "deploy-stack.yml"), encoding="utf-8").read()
    # The .env content lines are indented literals `NAME={{...}}` / `NAME=1`; the generic loop line starts with
    # `{{ env }}=` (a `{`, not an UPPER letter) so it is excluded by the leading-[A-Z] anchor.
    literal_env = set(re.findall(r"^\s+([A-Z][A-Z0-9_]*)=", text, re.MULTILINE))
    missing = literal_env - gen.RESERVED_PLATFORM_CORE_ENV
    assert not missing, ("these literal deploy-stack .env names are not in RESERVED_PLATFORM_CORE_ENV "
                         "(the collision guard would miss them): %s" % sorted(missing))
