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

  I-7         The 401/403 remedy named `_api_recipe.auth.token_var`, which a `basic` recipe does not have, so a
              basic-auth 401 failed on its own message ("object of type 'dict' has no attribute 'token_var'")
              instead of saying the credential was refused; and any other answer (a 404, a 500) was reported as
              "did not answer the API port". The remedy is now a hint per auth type, and the message is rendered
              here for each type and each kind of failure.
"""
import os
import re

import jinja2
import jinja2.meta
import pytest
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


def _refusal():
    """The check's refusal: its message and the auth hint (a task var) the message's 401/403 branch prints."""
    task = _named(_CHECK, "Refuse unless the API actually answered")
    return str(task["ansible.builtin.fail"]["msg"]), str((task.get("vars") or {}).get("_api_auth_hint", ""))


def _render(auth, status, host="fw-1"):
    """The refusal's message as the task renders it for a recipe with `auth` and a GET that registered `status`
    (None: no status at all). Undefined is strict, as ansible-core's is for a missing attribute: the I-7 fault
    raises here as it did in the run."""
    msg, hint = _refusal()
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    ctx = {"inventory_hostname": host, "backend_api_recipe": "probe",
           "_api_recipe": {"auth": auth, "check": {"path": "/api/status"}},
           "_api_check": {"changed": False} if status is None else {"changed": False, "status": status}}
    ctx["_api_auth_hint"] = env.from_string(hint).render(**ctx)
    return env.from_string(msg).render(**ctx)


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
    credential problem gets misdiagnosed as an outage — which is exactly what the censored version did. Since
    I-7 the auth branch prints the hint, a task var, so both are read."""
    msg, hint = _refusal()
    assert "401" in msg and "403" in msg, "the auth-rejection branch must be explicit"
    assert "_api_auth_hint" in msg, "the auth branch prints the hint"
    # Assert on the BRANCH TEXT, not on "token" or the status codes: those appear in the Jinja condition
    # itself, so a mutation that gutted the auth guidance while leaving `in [401, 403]` intact still passed.
    # The two branches must each carry their own distinguishable remedy.
    assert "rejected the credential" in hint, "the auth branch must say the credential was rejected"
    assert "did not answer the API port" in msg, "and the transport branch must say the device never answered"


TOKEN_HINT = ("The API rejected the credential: check that `fortios_api_token` (or, if it is empty, `fw_1_api_token`)"
              " holds a current token with read permission.")
BASIC_HINT = ("The API rejected the credential: check that `probe_user` and `probe_pass` hold a current username and"
              " password with read permission.")
NONE_HINT = "The API refused the request, and the recipe sends no credential: name one in its auth."
DOWN = ("The device did not answer the API port, or its TLS failed — wrong address, port, certificate, or the service"
        " is down.")
ANSWERED = ("The API answered, but not with success (200/201/204): check the recipe check path and the device"
            " API.")
BEARER = {"type": "bearer", "token_var": "fortios_api_token"}
APIKEY = {"type": "apikey_header", "header_name": "X-Api-Key", "token_var": "fortios_api_token"}
BASIC = {"type": "basic", "username_var": "probe_user", "password_var": "probe_pass"}
NONE = {"type": "none"}
BASIC_NO_PASS = {"type": "basic", "username_var": "probe_user"}
DIGEST = {"type": "digest", "username_var": "probe_user", "password_var": "probe_pass"}


@pytest.mark.parametrize("auth, status, http, remedy", [
    (BEARER, 401, "401", TOKEN_HINT),
    (BEARER, 403, "403", TOKEN_HINT),
    (APIKEY, 401, "401", TOKEN_HINT),
    (APIKEY, 403, "403", TOKEN_HINT),
    (BASIC, 401, "401", BASIC_HINT),     # I-7: failed on its own message ('dict' has no attribute 'token_var')
    (BASIC, 403, "403", BASIC_HINT),
    (NONE, 401, "401", NONE_HINT),       # failed on its own message too: a `none` recipe has no token_var either
    (NONE, 403, "403", NONE_HINT),
    (BASIC_NO_PASS, 401, "401", BASIC_HINT.replace("`probe_pass`", "`?`")),
    (DIGEST, 401, "401", BASIC_HINT),    # the GET sends the vars a recipe names, whatever its type
    (BEARER, 500, "500", ANSWERED),      # before I-7: "did not answer the API port" for an answer
    (BASIC, 404, "404", ANSWERED),
    (BEARER, 302, "302", ANSWERED),
    (BEARER, -1, "-1", DOWN),            # uri's status for a refused connection, a timeout, a TLS failure
    (BASIC, -1, "-1", DOWN),
    (BEARER, None, "no response", DOWN),
], ids=["bearer-401", "bearer-403", "apikey-401", "apikey-403", "basic-401", "basic-403", "none-401", "none-403",
        "basic-no-password-var-401", "digest-named-vars-401", "bearer-500", "basic-404", "bearer-302",
        "bearer-refused", "basic-refused", "bearer-no-status"])
def test_the_failure_message_names_the_remedy_for_each_auth_type_and_answer(auth, status, http, remedy):
    """The refusal's message, rendered as the task renders it, for each auth type the role builds a request for
    and each kind of failure: a 401 or a 403 names the variables that auth reads (a basic recipe's username and
    password vars, and any recipe's that names them, since the GET sends them whatever the type; a var the recipe
    does not name is `?`; a token recipe's `token_var`, else the onboarded per-host name), never their values; a
    status below 0 or none says the device did not answer or its TLS failed; any other answer says the API
    answered, but not with success. The basic and none rows are I-7's fault: the message named `token_var`, which
    neither has, and failed itself."""
    expected = "fw-1: probe liveness check FAILED — HTTP %s from /api/status. %s" % (http, remedy)
    assert _render(auth, status) == expected


def test_the_failure_message_cannot_leak_the_token_or_the_config():
    """THE CONSTRAINT ON THE FIX. The GET carries a live API token and its response body can BE the device
    configuration (the fortios recipe's backup uses `extract: body`). Making the check informative must not
    turn it into a disclosure — so the message and its auth hint may reference status, the recipe's path and
    the NAMES its auth reads, never `content`, `json`, the whole registered dict, the token or the headers.
    The variables both read are held to a list, `_api_check` to its status and `_api_recipe` to the names its
    auth reads and its check path, so a new reference to a value (a credential var looked up, the response body,
    the recipe's auth printed whole) fails here. And the task's only var is the hint: a task var outranks a host
    var, so a second one could rebind a name the message prints (`backend_api_recipe`) to the token, and every
    other assertion here would still pass (the I-7 read's S1, measured on both cores)."""
    msg, hint = _refusal()
    task = _named(_CHECK, "Refuse unless the API actually answered")
    assert set(task.get("vars") or {}) == {"_api_auth_hint"}, \
        "a task var on the refusal outranks a host var, so it can rebind a name the message prints"
    for leak in (".content", ".json", "_api_check }}", "_api_check}}", "_api_token", "_api_headers"):
        assert leak not in msg, "the failure message must not surface %r" % leak
    env = jinja2.Environment()
    names = set().union(*(jinja2.meta.find_undeclared_variables(env.parse(t)) for t in (msg, hint)))
    assert names <= {"inventory_hostname", "backend_api_recipe", "_api_recipe", "_api_check", "_api_auth_hint"}, \
        names
    for text in (msg, hint):
        assert "lookup(" not in text and "hostvars" not in text and "vars[" not in text, text
        assert all(m.group(1) == "status" for m in re.finditer(r"_api_check\.(\w+)", text)), text
        assert not re.search(r"_api_check(?![.\w])", text), "the registered dict itself must not be printed"
        assert all(m.group(1) in ("auth", "check") for m in re.finditer(r"(?<!\w)_api_recipe\.(\w+)", text)), text
        assert all(m.group(1) in ("type", "username_var", "password_var", "token_var")
                   for m in re.finditer(r"(?<!\w)_api_recipe\.auth\.(\w+)", text)), text
        assert all(m.group(1) == "path" for m in re.finditer(r"(?<!\w)_api_recipe\.check\.(\w+)", text)), text
        assert not re.search(r"(?<!\w)_api_recipe(\.(auth|check))?(?![.\w])", text), \
            "the recipe, its auth or its check must not be printed whole"


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
