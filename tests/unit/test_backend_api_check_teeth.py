"""The REST/API liveness check can actually fail, and says something useful when it does.

Three defects found by the 2026-07-28 fleet-restore review
(docs/reviews/2026-07-28-fleet-secret-restore/), all in the path aimed at the **edge firewall** — the
highest-blast-radius device in the fleet:

  F-CANTFAIL  roles/backend_api/tasks/check.yml registered `_api_check` with `failed_when: false` and then
              never read it again; the file ended one line later. A 200, a 401, a 403 and a refused
              connection were indistinguishable, so the check COULD NOT FAIL. It reported success against a
              device it had never authenticated to. A green check that means nothing is worse than no check,
              because a reclaim/cutover decision gets made on the strength of it.

  F-TOKENVAR  The recipe declares the var it reads (`token_var: fortios_api_token`) and
              ansible/backends/api/backend.yml claimed "the planner resolves it". Nothing ever did:
              service/onboard.py puts only `network_os` into `backend_params`, so `params.get("token_var")`
              is always None and an onboarded host gets `<host>_api_token` — a name the role never looked
              up. paths.py defines RECIPES_DIR and no Python reads it. The documented design was never
              implemented, so the check could not have authenticated even with a perfect credential.
              (That is the planner before S27. Since S27 the api backend's `api_token` entry names the
              per-host var as data, a `maps_to` template; still nothing resolves a recipe's `token_var`.)

  F-CENSORED  `lookup('vars', X)` raises AnsibleUndefinedVariable for a missing name, and the resolve task
              carries `no_log: true`, so that fatal arrived CENSORED. ping.yml's block/rescue turned it into
              a bare `hostname: UNREACHABLE` — a healthy firewall, reported down, with no way to tell that
              from a real outage.

Together these meant the edge-firewall check was incapable of either passing honestly or failing
informatively. These tests pin the fix and, just as importantly, pin that the fix did not print a secret to
get there: the response body is device configuration and the request carries the token.
"""
import os

import yaml

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_CHECK = os.path.join(_ROOT, "ansible", "roles", "backend_api", "tasks", "check.yml")
_BACKUP = os.path.join(_ROOT, "ansible", "roles", "backend_api", "tasks", "backup.yml")
_RECIPE = os.path.join(_ROOT, "ansible", "backends", "api", "recipes", "fortios.yml")


def _tasks(path):
    """Flatten the task list, descending into block/rescue/always."""
    out = []

    def walk(node):
        if isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            out.append(node)
            for key in ("block", "rescue", "always"):
                if key in node:
                    walk(node[key])

    with open(path, encoding="utf-8") as fh:
        walk(yaml.safe_load(fh))
    return out


def _named(path, fragment):
    for task in _tasks(path):
        if fragment.lower() in str(task.get("name", "")).lower():
            return task
    raise AssertionError("no task in %s whose name contains %r" % (os.path.basename(path), fragment))


def _module_of(task):
    for key in task:
        if key.startswith("ansible.builtin."):
            return key
    return None


# --------------------------------------------------------------- F-CANTFAIL: the check must be able to fail

def test_the_liveness_result_is_actually_evaluated():
    """The regression itself. `_api_check` must be READ by a later task, not merely registered. Without this
    the whole check is decoration."""
    body = open(_CHECK, encoding="utf-8").read()
    get = _named(_CHECK, "GET the check endpoint")
    assert get.get("failed_when") is False, \
        "failed_when:false is intentional (a censored uri fatal is useless) — but only if the result is read"
    # The registered var must appear somewhere OTHER than its own register: line.
    uses = [ln for ln in body.splitlines()
            if "_api_check" in ln and "register:" not in ln]
    assert uses, "_api_check is registered and never read — the check cannot fail (F-CANTFAIL)"


def test_a_non_success_status_fails_the_host():
    """The evaluation must key on HTTP status and treat only real successes as success. ping.yml's
    block/rescue converts this failure into UNREACHABLE, which is the intended reporting path."""
    task = _named(_CHECK, "Refuse unless the API actually answered")
    assert _module_of(task) == "ansible.builtin.fail", "must FAIL the host, not merely warn"
    cond = str(task.get("when", ""))
    assert "_api_check.status" in cond, "the gate must read the HTTP status"
    for ok in ("200", "201", "204"):
        assert ok in cond, "success statuses must be enumerated, so anything else fails closed"
    assert "not in" in cond, "the gate must be an allow-list of successes, not a deny-list of failures"


def test_the_failure_message_distinguishes_auth_from_unreachable():
    """A 401/403 and a dead port need different operator actions. Collapsing them into one message is how a
    credential problem gets misdiagnosed as an outage — which is exactly what the censored version did."""
    msg = str(_named(_CHECK, "Refuse unless the API actually answered")["ansible.builtin.fail"]["msg"])
    assert "401" in msg and "403" in msg, "the auth-rejection branch must be explicit"
    # Assert on the BRANCH TEXT, not on "token" or the status codes: those appear in the Jinja condition
    # itself, so a mutation that gutted the auth guidance while leaving `in [401, 403]` intact still passed.
    # The two branches must each carry their own distinguishable remedy.
    assert "rejected the credential" in msg, "the auth branch must say the credential was rejected"
    assert "did not answer the API port" in msg, "and the transport branch must say the device never answered"


def test_the_failure_message_cannot_leak_the_token_or_the_config():
    """THE CONSTRAINT ON THE FIX. The GET carries a live API token and its response body can BE the device
    configuration (the fortios recipe's backup uses `extract: body`). Making the check informative must not
    turn it into a disclosure — so the message may reference status and the recipe path, never `content`,
    `json`, or the whole registered dict."""
    msg = str(_named(_CHECK, "Refuse unless the API actually answered")["ansible.builtin.fail"]["msg"])
    for leak in (".content", ".json", "_api_check }}", "_api_check}}", "_api_token", "_api_headers"):
        assert leak not in msg, "the failure message must not surface %r" % leak


# --------------------------------------------------- F-TOKENVAR / F-CENSORED: the credential must resolve

def test_both_token_var_names_are_tried():
    """The recipe's declared name AND the per-host name the onboard actually writes. Only accepting the
    recipe's name is what broke every onboarded api host; only accepting the convention would break a
    hand-written drop-in that followed the documented recipe field."""
    for path in (_CHECK, _BACKUP):
        expr = str(_named(path, "Resolve the auth token")["ansible.builtin.set_fact"]["_api_token"])
        assert "_api_recipe.auth.token_var" in expr, "%s: the recipe's declared token_var must be tried" % path
        assert "_api_token'" in expr and "replace('-', '_')" in expr, \
            "%s: the per-host <host>_api_token convention the onboard emits must also be tried" % path


def test_the_lookup_cannot_raise_under_no_log():
    """`default=''` is load-bearing, not defensive styling: ansible/plugins/lookup/vars.py raises
    AnsibleUndefinedVariable when the name is absent, and these tasks carry no_log, so the fatal is
    CENSORED and surfaces as an unexplained UNREACHABLE. The default converts that into the explicit,
    readable refusal asserted below."""
    for path in (_CHECK, _BACKUP):
        task = _named(path, "Resolve the auth token")
        expr = str(task["ansible.builtin.set_fact"]["_api_token"])
        assert task.get("no_log") is True, "%s: the token resolve must stay no_log" % path
        assert expr.count("default='')") >= 2, \
            "%s: EVERY vars lookup needs default='' or a missing name raises inside a no_log task" % path


def test_an_empty_credential_is_refused_before_the_request():
    """Defined-but-empty is a real case, not a theoretical one: `kontroll_sops_domain` returns `{}` for an
    absent or undecryptable domain, so the lookup succeeds and yields nothing. Sending that would
    authenticate as anonymous and report a device-side failure for a control-node cause. The refusal must
    name the VARIABLES (safe) and must NOT be no_log — silence was the original bug."""
    for path in (_CHECK, _BACKUP):
        task = _named(path, "Refuse an unresolvable API credential")
        assert _module_of(task) == "ansible.builtin.assert"
        assert task.get("no_log") is not True, \
            "%s: this refusal must be VISIBLE — an invisible failure is the defect being fixed" % path
        that = str(task["ansible.builtin.assert"]["that"])
        assert "length > 0" in that, "%s: must reject an empty token, not merely an undefined one" % path
        fail_msg = str(task["ansible.builtin.assert"]["fail_msg"])
        assert "token_var" in fail_msg and "replace('-', '_')" in fail_msg, \
            "%s: the message must name BOTH candidate variables — that mismatch was invisible for months" % path


def test_the_recipe_still_declares_the_name_the_role_reads():
    """Ties the fix to the data. If a recipe ever drops `auth.token_var`, the role's first candidate silently
    disappears and it would fall back to the convention alone — working by luck. Pin the contract."""
    recipe = yaml.safe_load(open(_RECIPE, encoding="utf-8"))
    assert recipe["auth"]["token_var"], "the fortios recipe must declare the var it reads"
    assert recipe["auth"]["type"] == "bearer", "a change here changes how the header is built"


def test_the_backend_doc_no_longer_claims_the_planner_resolves_it():
    """The false comment is part of the defect. `backend.yml` asserted "resolved by the planner" while
    onboard.py never did, and that claim is why nobody looked. A doc that describes unimplemented behaviour
    is a trap, so the corrected text must survive. Since S27 the per-host name is the descriptor's own data:
    its `api_token` entry's `maps_to` template is the name the role's second candidate reads
    (`inventory_hostname` with `-` as `_`, then `_api_token`), and the text says no recipe var is resolved."""
    text = open(os.path.join(_ROOT, "ansible", "backends", "api", "backend.yml"), encoding="utf-8").read()
    assert "resolved by the planner" not in text, "the false claim must not come back"
    assert "Nothing resolves a recipe's `token_var` at onboard time" in text, "the correction must stay"
    token = next(e for e in yaml.safe_load(text)["auth"] if e["field"] == "api_token")
    assert token["maps_to"] == "{cred_key}_api_token", "the per-host name the role reads, stated as data"
