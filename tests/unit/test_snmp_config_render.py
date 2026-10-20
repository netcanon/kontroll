"""snmp.yml is rendered whenever the exporter is deployed — with or without SNMP credentials.

`prometheus/exporters/snmp/snmp.yml` is a **file**-bind into snmp-exporter. The G9 rule is that a file-bind
source must exist before `compose up`, content or not: if it is missing, Docker auto-creates the source as a
`root:root` DIRECTORY and the container dies with *"not a directory: Are you trying to mount a directory onto a
file"*.

The render used to be gated `and (_snmp | length > 0)`. The reasoning was sound as far as it went — on a fresh
node the `snmp_observability` domain does not exist yet, and an exporter with no credentials has nothing to
scrape — but nothing gated the SERVICE. So a fresh box that listed `snmp-exporter` in `stack_services` skipped
the render and then hard-failed at `up` (live-caught 2026-07-28, on the first from-scratch floating-prod build).
Two decisions that disagreed: render-if-creds, deploy-always.

`deploy-stack` already solves this exact problem twice — the empty device-CA bundle and the empty
file_tail_ssh known_hosts are both written unconditionally so their binds resolve to a FILE. snmp.yml was the
one that was missed, so the fix is to match: always render, and let the template omit the credential block.

These evaluate the real template rather than string-matching it, because the property that matters is what the
rendered YAML actually contains in each case.

Note this file is NOT covered by `tests/check-storage-chown.py`: that guard deliberately excludes relative
repo-tree binds (`../`) and only sees storage-root-derived sources. Repo-tree file binds have no general guard —
which is why this one needed its own.
"""
import os

import jinja2
import pytest
import yaml

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SNMP_DIR = os.path.join(_ROOT, "prometheus", "exporters", "snmp")
_DEPLOY = os.path.join(_ROOT, "ansible", "playbooks", "deploy-stack.yml")

_CREDS = {
    "snmp_v3_username": "kontroll-mon",
    "snmp_v3_auth_password": "auth-secret",
    "snmp_v3_priv_password": "priv-secret",
}


def _render(snmp, trim_blocks=True):
    # The template module's own setting (`trim_blocks: true` is its default, and deploy-stack's render task leaves
    # it): rendering without it is how these rows passed while every deploy rendered `auths:` into a comment.
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(_SNMP_DIR), keep_trailing_newline=True,
                             trim_blocks=trim_blocks)
    return env.get_template("snmp.yml.j2").render(_snmp=snmp)


def _deploy_text():
    with open(_DEPLOY, encoding="utf-8") as fh:
        return fh.read()


@pytest.mark.parametrize("snmp,label", [({}, "no domain"), (_CREDS, "creds present")])
def test_the_rendered_config_is_always_valid_yaml_with_modules(snmp, label):
    """Both states must produce a usable config. A file that exists but is malformed trades the mount error for
    a parse error — no better for the operator, and harder to attribute."""
    doc = yaml.safe_load(_render(snmp))
    assert isinstance(doc, dict), "render (%s) did not produce a mapping" % label
    assert doc.get("modules"), "the generated modules block must survive in both states (%s)" % label
    assert "if_mib" in doc["modules"], "the if_mib module is the one the switch/AP targets scrape"


def test_without_the_domain_the_config_has_no_auths_block():
    """The fresh-node case, and the whole point of the fix: a modules-only config is valid, starts inert, and —
    critically — EXISTS, so the file-bind resolves. Nothing is scrapeable until a device is onboarded anyway."""
    doc = yaml.safe_load(_render({}))
    assert "auths" not in doc, "an auths block with unset credentials is worse than none: %r" % (doc.get("auths"),)


def test_with_the_domain_the_credentials_are_wired():
    """The fix must not quietly disable SNMP on a box that HAS credentials — that would trade a loud startup
    failure for a silent monitoring gap, which is the worse of the two."""
    doc = yaml.safe_load(_render(_CREDS))
    auth = doc["auths"]["v3_kontroll"]
    assert auth["version"] == 3 and auth["security_level"] == "authPriv"
    assert auth["username"] == _CREDS["snmp_v3_username"]
    assert auth["password"] == _CREDS["snmp_v3_auth_password"]
    assert auth["priv_password"] == _CREDS["snmp_v3_priv_password"]
    assert auth["auth_protocol"] == "SHA" and auth["priv_protocol"] == "AES"


@pytest.mark.parametrize("snmp,label", [({}, "no domain"), (_CREDS, "creds present")])
def test_the_template_modules_trim_blocks_changes_nothing_the_exporter_reads(snmp, label):
    """The render the deploy makes (the template module's `trim_blocks: true`) parses to the same document as a
    render without it, and `auths:` starts its own line — it is never the tail of the comment above it.

    Why: the template opened its explanatory comment with `{#-`. Under trim_blocks the newline after `#}` goes
    too, so `auths:` was joined onto the comment line above: every deploy from 2026-07-28 to 2026-10-20 wrote an
    snmp.yml with the credentials commented out and an indented block YAML cannot parse, while these rows —
    rendering without trim_blocks — passed. A box running an exporter started before then kept working until
    its next restart (found on the production box's deploy, 2026-10-20)."""
    trimmed, plain = _render(snmp), _render(snmp, trim_blocks=False)
    assert yaml.safe_load(trimmed) == yaml.safe_load(plain), "trim_blocks changed the config (%s)" % label
    lines = trimmed.splitlines()
    assert not any(line.startswith("#") and line.rstrip().endswith("auths:") for line in lines), \
        "auths: was joined onto a comment line (%s)" % label
    assert ("auths:" in lines) is bool(snmp), "auths: must be a line of its own exactly when creds exist (%s)" % label


def test_the_render_is_not_gated_on_the_secret_existing():
    """THE REGRESSION. Re-adding a `_snmp`-dependent condition to the render's `when:` restores the exact
    fresh-box crash, and it would look like a tightening rather than a break."""
    block = _deploy_text().split("prometheus/exporters/snmp/snmp.yml —", 1)[1].split("- name:", 1)[0]
    when = [ln for ln in block.splitlines() if ln.strip().startswith("when:")]
    assert when, "the snmp.yml render task lost its when: — find it and update this test"
    assert "_snmp" not in when[0], \
        "the render must NOT depend on the credentials existing — that is the bug: %s" % when[0].strip()
    assert "snmp-exporter" in when[0], "it must be gated on the service that BINDS the file"


def _tasks(node):
    if isinstance(node, list):
        for item in node:
            yield from _tasks(item)
    elif isinstance(node, dict):
        yield node
        for key in ("block", "rescue", "always", "tasks", "pre_tasks", "post_tasks"):
            if key in node:
                yield from _tasks(node[key])


def _up_services_expr():
    """The two Jinja expressions off the real playbook, read STRUCTURALLY.

    `_requested_services` must be a task **var** and `_up_services` a set_fact key. That split is not cosmetic:
    sibling set_fact keys are all templated against the pre-task context, so a set_fact key referencing another
    key of the same task dies with "'_requested_services' is undefined" — which is exactly how this shipped the
    first time, past both `--syntax-check` and ansible-lint (they parse; they do not evaluate). Reading it
    structurally means that regression fails HERE rather than on a deploy."""
    for task in _tasks(yaml.safe_load(_deploy_text())):
        if str(task.get("name", "")).startswith("Compute the services to bring up"):
            fact = task.get("ansible.builtin.set_fact") or {}
            assert "_requested_services" in (task.get("vars") or {}), \
                "_requested_services must be a task VAR — as a sibling set_fact key it is undefined at render"
            assert "_requested_services" not in fact, \
                "_requested_services must NOT be a set_fact key alongside _up_services (undefined at render)"
            return task["vars"]["_requested_services"], fact["_up_services"]
    raise AssertionError("the up-services task is gone — if it moved, move this test with it")


def _ansible_bool(value):
    """Ansible's `bool` filter, which stock Jinja2 does not have. Matches the cases the playbook can actually
    produce: real booleans, and the 'True'/'False' STRINGS that `set_fact` hands back."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "yes", "on", "1")


def _resolve_up_services(stack, has_creds, runs_caddy=False):
    """Evaluate the playbook's own two expressions, so this tests the shipped Jinja rather than a paraphrase of
    it — the precedence bug below lived entirely in the punctuation, which a paraphrase would have silently
    fixed."""
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    env.filters["bool"] = _ansible_bool
    req_src, up_src = _up_services_expr()
    ctx = {
        "stack_services": stack,
        "_has_snmp_creds": has_creds,
        "_runs_caddy": {"stdout": "yes" if runs_caddy else "no"},
    }
    ctx["_requested_services"] = env.compile_expression(req_src.strip("{} \n"))(**ctx)
    return env.compile_expression(up_src.strip("{} \n"))(**ctx)


def test_snmp_exporter_is_dropped_from_the_up_list_without_credentials():
    """It cannot start without an `auths:` block, so bringing it up buys a crash-loop instead of a service.
    Dropping it is the only outcome that leaves the box healthy."""
    up = _resolve_up_services(["prometheus", "snmp-exporter", "api"], has_creds=False)
    assert "snmp-exporter" not in up, "a credential-less snmp-exporter must not be brought up: %r" % (up,)
    assert "prometheus" in up and "api" in up, "only snmp-exporter is affected"


def test_snmp_exporter_comes_up_once_credentials_exist():
    """The other half — the drop must be conditional, not a permanent removal. A fix that quietly disabled SNMP
    forever would trade a visible crash-loop for an invisible monitoring gap."""
    up = _resolve_up_services(["prometheus", "snmp-exporter", "api"], has_creds=True)
    assert "snmp-exporter" in up


def test_dropping_snmp_exporter_does_not_drop_caddy():
    """The bug written and caught before shipping: expressed as `(A if cond else B) + caddy`, the `+` binds to
    the `else` branch ALONE, so a creds-less box silently loses caddy — its TLS ingress — along with the exporter.
    Nothing else in the suite would have noticed: the stack simply comes up without the service that was never
    named in the failure. Evaluates the shipped Jinja, so the precedence is what is under test, not a paraphrase."""
    up = _resolve_up_services(["api", "snmp-exporter"], has_creds=False, runs_caddy=True)
    assert "caddy" in up, "caddy was collateral damage of the snmp drop"
    assert "snmp-exporter" not in up


def test_the_rendered_file_stays_untracked_and_secret_moded():
    """It carries SNMPv3 credentials whenever they exist, so the 0600 + gitignore pair has to survive any edit
    to the render task. `tests/validate` has a `snmp-config-untracked` check for the git half; this pins the
    mode next to the behaviour change that could disturb it."""
    block = _deploy_text().split("prometheus/exporters/snmp/snmp.yml —", 1)[1].split("- name:", 1)[0]
    assert 'mode: "0600"' in block, "the rendered config is creds-bearing whenever creds exist"
    assert "no_log: true" in block, "the render task must never echo the credentials"
    with open(os.path.join(_ROOT, ".gitignore"), encoding="utf-8") as fh:
        assert "prometheus/exporters/snmp/snmp.yml" in fh.read()


def test_a_changed_render_restarts_a_running_exporter_after_up():
    """When the render changed snmp.yml and snmp-exporter is in the up-list, the deploy restarts the exporter
    after `up`; it does nothing when the render changed nothing.

    Why: the render writes a new file under the same name, and a running container's file bind keeps the one it
    started with; `up` recreates only on a changed service definition. So a rotated credential, or the corrected
    template above, reached the exporter only at its next restart — on the production box, a reboot weeks after
    the deploy that broke it (2026-10-20)."""
    tasks = list(_tasks(yaml.safe_load(_deploy_text())))
    names = [str(t.get("name", "")) for t in tasks]
    render = tasks[next(i for i, n in enumerate(names) if n.startswith("Render the snmp_exporter config"))]
    assert render.get("register") == "_snmp_render", "the render's change is what the restart keys on"
    up = next(i for i, n in enumerate(names) if n.startswith("Bring up the selected stack"))
    hits = [i for i, n in enumerate(names) if n.startswith("Restart snmp-exporter when its rendered config")]
    assert len(hits) == 1 and hits[0] > up, "one restart, after `up` (so it acts on the container `up` left)"
    restart = tasks[hits[0]]
    assert restart.get("when") == ["_snmp_render is changed", "'snmp-exporter' in _up_services"]
    cmd = " ".join(restart["ansible.builtin.command"]["cmd"].split())
    assert cmd.endswith("restart snmp-exporter") and "--env-file {{ stack_dir }}/.env" in cmd
