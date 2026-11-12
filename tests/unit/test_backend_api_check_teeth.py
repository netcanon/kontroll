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

  I-16        A basic recipe's username and password were looked up in the GET's own arguments, and only by the
              recipe's names: a name not in scope failed the GET under its `no_log` before `failed_when: false`
              applied, CENSORED, so the refusal was never reached; and the onboard wrote the pair as `ansible_user`
              and `ansible_password`, which the role never read. The pair now resolves as the token does (the
              recipe's var, else the onboarded per-host `<host>_api_username` and `_api_password`, under no_log
              with `default=''`), is refused by name before the request, and is what the request sends; the hint
              names each var with its per-host fallback. The two reads of I-16 found that `default=''` covers a name
              not in scope only: a defined name whose own value fails when read (an inline SOPS lookup whose domain
              will not open) still failed the no_log resolve, censored, and on 2.18 the per-host value was read even
              when the recipe's var held the credential, so one that failed failed a good host. Each resolve now sits
              in a block whose rescue refuses that host by name, and reads the per-host name only when the recipe's
              yields nothing; the token's resolve too.
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


def _chain(path, fragment):
    """The task whose name contains `fragment` and each block around it, outermost first."""
    def walk(node, above):
        if isinstance(node, list):
            for item in node:
                found = walk(item, above)
                if found:
                    return found
        elif isinstance(node, dict):
            if fragment.lower() in str(node.get("name", "")).lower():
                return above + [node]
            for key in ("block", "rescue", "always"):
                if key in node:
                    found = walk(node[key], above + [node])
                    if found:
                        return found
        return None

    with open(path, encoding="utf-8") as fh:
        found = walk(yaml.safe_load(fh), [])
    assert found, "no task in %s whose name contains %r" % (os.path.basename(path), fragment)
    return found


def _printed(path, fragment):
    """A refusal is printed: neither it nor a block around it sets `no_log`, unless to a literal false. A block's
    `no_log` reaches every task in it, a rescue's too, and any truthy value hides the message (`yes`, `1`, a
    template), not only `true`; the refusals were held to `is not True`, which those pass (the I-16 read's N4)."""
    for node in _chain(path, fragment):
        assert node.get("no_log", False) is False, (os.path.basename(path), node.get("name"), node.get("no_log"))


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
    block/rescue records this failure as CHECK FAILED, naming this task, and fails the run after the key cleanup
    (UNREACHABLE before I-84, which passed the run): the intended reporting path."""
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


TOKEN_HINT = ("The API rejected the credential: check that `fortios_api_token` (or, if that is unset or empty,"
              " `fw_1_api_token`) holds a current token with read permission.")
BASIC_HINT = ("The API rejected the credential: check that `probe_user` (or, if that is unset or empty,"
              " `fw_1_api_username`) and `probe_pass` (or, if that is unset or empty, `fw_1_api_password`) hold a"
              " current username and password with read permission.")
# A var the recipe does not name: its per-host name alone is named.
NO_PASS_HINT = BASIC_HINT.replace("`probe_pass` (or, if that is unset or empty, `fw_1_api_password`)",
                                  "`fw_1_api_password`")
TYPE_ONLY_HINT = NO_PASS_HINT.replace("`probe_user` (or, if that is unset or empty, `fw_1_api_username`)",
                                      "`fw_1_api_username`")
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
    (BASIC_NO_PASS, 401, "401", NO_PASS_HINT),
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
    password vars, and any recipe's that names them, since the GET sends them whatever the type, each else its
    onboarded per-host name, which alone is named for a var the recipe does not name (I-16); a token recipe's
    `token_var`, else the onboarded per-host name), never their values; a
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
    other assertion here would still pass (the I-7 read's S1, measured on both cores). Nor does the task hold any
    other key: a `loop:` prints each item with the refusal, so a loop over the response body would print the
    config (the I-17 read's N1, the same shape there)."""
    msg, hint = _refusal()
    task = _named(_CHECK, "Refuse unless the API actually answered")
    assert set(task.get("vars") or {}) == {"_api_auth_hint"}, \
        "a task var on the refusal outranks a host var, so it can rebind a name the message prints"
    assert set(task) <= {"name", "ansible.builtin.fail", "vars", "when"}, set(task)
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


def test_a_name_not_in_scope_cannot_raise_under_no_log():
    """`default=''` is load-bearing, not defensive styling: ansible/plugins/lookup/vars.py raises
    AnsibleUndefinedVariable when the name is absent, and these tasks carry no_log, so the fatal is
    CENSORED and surfaces as an unexplained UNREACHABLE. The default converts that into the explicit,
    readable refusal asserted below. It covers a name not in scope only: a defined name whose own value fails
    when it is read is the rescue's (test_a_lookup_that_fails_is_refused_by_name_and_not_censored)."""
    for path in (_CHECK, _BACKUP):
        task = _named(path, "Resolve the auth token")
        expr = str(task["ansible.builtin.set_fact"]["_api_token"])
        assert task.get("no_log") is True, "%s: the token resolve must stay no_log" % path
        assert expr.count("default='')") >= 2, \
            "%s: EVERY vars lookup needs default='' or a missing name raises inside a no_log task" % path


def test_an_empty_credential_is_refused_before_the_request():
    """Defined-but-empty is a real case, not a theoretical one: a drop-in's SOPS lookup yields an empty value
    where its domain holds the key empty (and, on 2.18, where the domain lacks the key), so the lookup succeeds and
    yields nothing. Sending that would authenticate as anonymous and report a device-side failure for a control-node
    cause. The refusal must name the VARIABLES (safe) and must NOT be no_log — silence was the original bug — nor
    sit in a block that is (`_printed`)."""
    for path in (_CHECK, _BACKUP):
        task = _named(path, "Refuse an unresolvable API credential")
        assert _module_of(task) == "ansible.builtin.assert"
        _printed(path, "Refuse an unresolvable API credential")
        that = str(task["ansible.builtin.assert"]["that"])
        assert "length > 0" in that, "%s: must reject an empty token, not merely an undefined one" % path
        fail_msg = str(task["ansible.builtin.assert"]["fail_msg"])
        assert "token_var" in fail_msg and "replace('-', '_')" in fail_msg, \
            "%s: the message must name BOTH candidate variables — that mismatch was invisible for months" % path


TOKEN_REFUSAL = ("fw-1: no API token is in scope, so this %s cannot authenticate. The probe recipe reads "
                 "`fortios_api_token`; the onboard writes `fw_1_api_token`. Neither is defined, or both are empty "
                 "(where a drop-in defines one, its SOPS domain may hold that key empty, or lack it).")


def test_the_token_refusal_names_both_vars_and_what_can_leave_them_empty():
    """The token's refusal, RENDERED in the check and in the backup: it names the recipe's var and the per-host one,
    and says what leaves a drop-in's name empty, never a value; the check's adds where to define one. Guards the
    message the I-16 reads found wrong: it said "Neither is defined" of a drop-in that does define the name, whose
    SOPS domain lacks the key, and never said so."""
    for path, what in ((_CHECK, "check"), (_BACKUP, "capture")):
        task = _named(path, "Refuse an unresolvable API credential")
        msg = jinja2.Environment(undefined=jinja2.StrictUndefined).from_string(
            str(task["ansible.builtin.assert"]["fail_msg"])).render(
            inventory_hostname="fw-1", backend_api_recipe="probe", _api_recipe={"auth": BEARER})
        expected = TOKEN_REFUSAL % what
        if path == _CHECK:
            expected += " Define one in the host's inventory drop-in as an inline community.sops lookup into its SOPS" \
                        " domain."
        assert msg == expected, path


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


# ------------------------------------------- I-16: the basic-auth pair resolves, and is refused, as the token is

PAIR = "Resolve the basic-auth pair"
PAIR_REFUSE = "Refuse an unresolvable basic-auth pair"
TOKEN_BLOCK = "Look up the auth token"
PAIR_BLOCK = "Look up the basic-auth pair"
TOKEN_RESCUE = "Refuse an API token whose lookup failed"
PAIR_RESCUE = "Refuse a basic-auth pair whose lookup failed"
GET = {_CHECK: "GET the check endpoint", _BACKUP: "Pull config via the recipe's backup endpoint"}
_MISSING = object()


class _Unreadable(Exception):
    """A defined var whose own value fails when it is read: an inline SOPS lookup over a domain that will not open."""


def _ansible_env(**hostvars):
    """Core Jinja with StrictUndefined, and `lookup('vars', name, default=...)` as ansible-core's vars lookup answers
    it: the host's var, else the default, else an error (AnsibleUndefinedVariable, the censored fatal I-16 found). A
    var given as an `_Unreadable` raises it, default or not, as a defined value whose own lookup fails does."""
    def lookup(kind, name, default=_MISSING):
        assert kind == "vars", kind
        if name in hostvars:
            if isinstance(hostvars[name], Exception):
                raise hostvars[name]
            return hostvars[name]
        if default is _MISSING:
            raise LookupError("No variable named %r was found" % name)
        return default
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    env.globals["lookup"] = lookup
    return env


def _resolve(path, auth, host="fw-1", **hostvars):
    """The pair as the resolve task sets it, for a recipe with `auth`, a host named `host` holding `hostvars`."""
    facts = _named(path, PAIR)["ansible.builtin.set_fact"]
    env = _ansible_env(**hostvars)
    ctx = {"inventory_hostname": host, "_api_recipe": {"auth": auth}}
    return tuple(env.from_string(str(facts[k])).render(**ctx) for k in ("_api_username", "_api_password"))


def _when(task, auth):
    """A task's `when` (one expression), evaluated for a recipe with `auth` as Ansible evaluates a conditional."""
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    cond = " ".join(str(task["when"]).split())
    return env.from_string("{%% if %s %%}yes{%% else %%}no{%% endif %%}" % cond).render(
        _api_recipe={"auth": auth}) == "yes"


def test_both_names_of_the_basic_pair_are_tried_and_a_name_not_in_scope_cannot_raise():
    """In the check and in the backup, the resolve task sets `_api_username` and `_api_password`, each from the
    recipe's var, else the onboarded per-host name (`<host with - as _>_api_username`, `_api_password`), with
    `default=''` on every lookup, under no_log. Guards I-16: the recipe's names alone were tried, in the request's
    own arguments, where a name not in scope raised under the request's no_log, censored. (A defined name whose value
    fails is the rescue's.)"""
    for path in (_CHECK, _BACKUP):
        task = _named(path, PAIR)
        facts = task["ansible.builtin.set_fact"]
        assert set(facts) == {"_api_username", "_api_password"}, (path, set(facts))
        assert task.get("no_log") is True, "%s: the pair's resolve must stay no_log" % path
        for fact, var in (("_api_username", "username_var"), ("_api_password", "password_var")):
            expr = str(facts[fact])
            assert "_api_recipe.auth.%s" % var in expr, (path, fact)
            assert "replace('-', '_') ~ '%s'" % fact in expr, (path, fact)
            assert expr.count("default='')") == 2 and expr.count("lookup('vars'") == 2, (path, fact)


@pytest.mark.parametrize("auth, hostvars, expected", [
    (BASIC, {"probe_user": "u-recipe", "probe_pass": "p-recipe"}, ("u-recipe", "p-recipe")),
    (BASIC, {"probe_user": "u-recipe", "probe_pass": "p-recipe",
             "fw_1_api_username": "u-host", "fw_1_api_password": "p-host"}, ("u-recipe", "p-recipe")),
    (BASIC, {"probe_user": "", "probe_pass": "", "fw_1_api_username": "u-host", "fw_1_api_password": "p-host"},
     ("u-host", "p-host")),
    (BASIC, {"fw_1_api_username": "u-host", "fw_1_api_password": "p-host"}, ("u-host", "p-host")),
    (BASIC_NO_PASS, {"probe_user": "u-recipe", "probe_pass": "p-unnamed", "fw_1_api_password": "p-host"},
     ("u-recipe", "p-host")),
    ({"type": "basic"}, {"fw_1_api_username": "u-host", "fw_1_api_password": "p-host"}, ("u-host", "p-host")),
    (BASIC, {}, ("", "")),
    (BASIC, {"probe_user": "", "fw_1_api_username": ""}, ("", "")),
], ids=["recipe-names", "recipe-names-first", "recipe-names-empty", "onboarded-names", "a-var-the-recipe-omits",
        "type-only", "none", "all-empty"])
def test_the_basic_pair_resolves_as_the_token_does(auth, hostvars, expected):
    """The resolve task's two expressions, EVALUATED for each case, in the check and in the backup: the recipe's var
    when it holds something, else the onboarded per-host name; a var the recipe does not name is the per-host one
    alone (a recipe's other names are not read); nothing in scope, or only empty values, resolves to empty, which the
    refusal takes. Guards the precedence (the recipe's first, as for the token), the empty-value fallback (a SOPS
    domain holding the key empty yields an empty value), and a name not in scope not raising."""
    for path in (_CHECK, _BACKUP):
        assert _resolve(path, auth, **hostvars) == expected, path


@pytest.mark.parametrize("auth, resolved", [
    (BASIC, True), (BASIC_NO_PASS, True), ({"type": "basic"}, True), (DIGEST, True),
    ({"type": "bearer", "token_var": "t", "password_var": "p"}, True),
    (BEARER, False), (APIKEY, False), (NONE, False),
], ids=["basic", "basic-no-password-var", "type-only", "digest-named-vars", "bearer-naming-a-password-var",
        "bearer", "apikey", "none"])
def test_the_pair_is_resolved_and_refused_for_exactly_the_recipes_whose_request_sends_it(auth, resolved):
    """The resolve task and the refusal run for a `basic` recipe and for any that names either var, and for no
    other, in the check and in the backup: the request sends the pair for those (`force_basic_auth` for a basic
    one), and a token or `none` recipe must not be refused for a pair it never sends. The refusal's condition is
    the resolve's, so a pair is never sent unrefused."""
    for path in (_CHECK, _BACKUP):
        resolve, refuse = _named(path, PAIR), _named(path, PAIR_REFUSE)
        assert refuse["when"] == resolve["when"], path
        assert _when(resolve, auth) is resolved, (path, auth)


def _refusal_msg(path, auth, host="fw-1"):
    task = _named(path, PAIR_REFUSE)
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    return env.from_string(str(task["ansible.builtin.assert"]["fail_msg"])).render(
        inventory_hostname=host, backend_api_recipe="probe", _api_recipe={"auth": auth})


REFUSAL = ("fw-1: the API username or password (the failed assertion says which) is not in scope, so this %s cannot "
           "authenticate. The probe recipe reads %s and %s; the onboard writes `fw_1_api_username` and "
           "`fw_1_api_password`. Neither name is defined, or both are empty (where a drop-in defines one, its SOPS "
           "domain may hold that key empty, or lack it). Define them in the host's inventory drop-in as inline "
           "community.sops lookups into its SOPS domain.")


@pytest.mark.parametrize("auth, names", [
    (BASIC, ("`probe_user`", "`probe_pass`")),
    (BASIC_NO_PASS, ("`probe_user`", "no password var")),
    ({"type": "basic"}, ("no username var", "no password var")),
], ids=["basic", "basic-no-password-var", "type-only"])
def test_an_unresolvable_basic_pair_is_refused_by_name_before_the_request(auth, names):
    """The refusal is an `assert` that is not no_log, holds each of the pair to a non-empty value, and runs before
    the request (nor sits in a block that is no_log); its message, RENDERED for each shape of basic recipe, names the
    recipe's vars (or says it names none) and the onboarded per-host names, and what can leave a drop-in's name empty,
    never a value, in the check and in the backup. Guards I-16: a basic
    host with no pair in scope failed inside the no_log request, censored, where a token host is told which names
    to define. The message's names are held to the host's, the recipe's and its auth's: it reads no value."""
    for path, what in ((_CHECK, "check"), (_BACKUP, "capture")):
        task = _named(path, PAIR_REFUSE)
        assert _module_of(task) == "ansible.builtin.assert", path
        _printed(path, PAIR_REFUSE)
        assert task["ansible.builtin.assert"]["that"] == ["_api_username | default('') | length > 0",
                                                          "_api_password | default('') | length > 0"], path
        names_in = [t.get("name") for t in _tasks(path)]
        assert names_in.index(task["name"]) < names_in.index(_named(path, GET[path])["name"]), path
        assert names_in.index(_named(path, PAIR)["name"]) < names_in.index(task["name"]), path
        assert _refusal_msg(path, auth) == REFUSAL % ((what,) + names), path
        read = jinja2.meta.find_undeclared_variables(jinja2.Environment().parse(
            str(task["ansible.builtin.assert"]["fail_msg"])))
        assert read <= {"inventory_hostname", "backend_api_recipe", "_api_recipe"}, (path, read)


CAUSE = ("One of them is defined, and its value failed when it was read; for an inline community.sops lookup, its SOPS"
         " domain is missing, this box cannot decrypt it (no age key), or it lacks the key the lookup reads.")
TOKEN_FAILED = ("fw-1: the lookup of the API token failed, so this %s cannot authenticate. The probe recipe reads"
                " `fortios_api_token`; the onboard writes `fw_1_api_token`. " + CAUSE)
PAIR_FAILED = ("fw-1: the lookup of the API username or password failed, so this %s cannot authenticate. The probe"
               " recipe reads {} and {}; the onboard writes `fw_1_api_username` and `fw_1_api_password`. " + CAUSE)


@pytest.mark.parametrize("block, rescue, fact, refuse", [
    (TOKEN_BLOCK, TOKEN_RESCUE, "_api_token", "Refuse an unresolvable API credential"),
    (PAIR_BLOCK, PAIR_RESCUE, "_api_username", PAIR_REFUSE),
], ids=["token", "pair"])
def test_a_lookup_that_fails_is_refused_by_name_and_not_censored(block, rescue, fact, refuse):
    """Each resolve, the token's and the pair's, is the one task of a block whose rescue is one `fail`, printed (it
    and the blocks around it are not no_log), which reads only the host's, the recipe's and its auth's names, looks
    nothing up and reads nothing of the failed task, and which comes before the refusal of an empty credential and
    the request, in the check and in the backup. Guards the I-16 reads' S1: `default=''` catches a name not in scope
    only, and a defined name whose value fails when it is read (an inline SOPS lookup whose domain is missing, will
    not decrypt on the box, or on 2.19 lacks the key) failed the no_log resolve, censored: ping.yml then said
    UNREACHABLE and the backup dropped the host, with nothing on 2.18 to say why."""
    for path in (_CHECK, _BACKUP):
        node = _named(path, block)
        assert set(node) == {"name", "block", "rescue"}, (path, set(node))
        assert len(node["block"]) == 1 and fact in node["block"][0]["ansible.builtin.set_fact"], path
        assert node["block"][0].get("no_log") is True, "%s: the resolve stays no_log" % path
        assert len(node["rescue"]) == 1 and rescue.lower() in node["rescue"][0]["name"].lower(), path
        task = node["rescue"][0]
        assert set(task) == {"name", "ansible.builtin.fail"}, (path, set(task))
        _printed(path, rescue)
        msg = str(task["ansible.builtin.fail"]["msg"])
        read = jinja2.meta.find_undeclared_variables(jinja2.Environment().parse(msg))
        assert read <= {"inventory_hostname", "backend_api_recipe", "_api_recipe"}, (path, read)
        assert "lookup(" not in msg, path
        names_in = [t.get("name") for t in _tasks(path)]
        assert names_in.index(task["name"]) < names_in.index(_named(path, refuse)["name"]) \
            < names_in.index(_named(path, GET[path])["name"]), path


@pytest.mark.parametrize("rescue, auth, expected", [
    (TOKEN_RESCUE, BEARER, TOKEN_FAILED),
    (PAIR_RESCUE, BASIC, PAIR_FAILED.format("`probe_user`", "`probe_pass`")),
    (PAIR_RESCUE, BASIC_NO_PASS, PAIR_FAILED.format("`probe_user`", "no password var")),
    (PAIR_RESCUE, {"type": "basic"}, PAIR_FAILED.format("no username var", "no password var")),
], ids=["token", "basic", "basic-no-password-var", "type-only"])
def test_a_failed_lookup_is_refused_with_the_names_and_the_likely_cause(rescue, auth, expected):
    """The rescue's message, RENDERED for each shape of recipe, in the check and in the backup: it names the
    recipe's var (or says it names none) and the onboarded per-host one, says one of them is defined and failed when
    read, and gives the SOPS causes, never a value. Guards the operator being told which names to look at when the
    resolve itself fails, where before only the censored fatal was printed."""
    for path, what in ((_CHECK, "check"), (_BACKUP, "capture")):
        task = _named(path, rescue)
        msg = jinja2.Environment(undefined=jinja2.StrictUndefined).from_string(
            str(task["ansible.builtin.fail"]["msg"])).render(
            inventory_hostname="fw-1", backend_api_recipe="probe", _api_recipe={"auth": auth})
        assert msg == expected % what, path


def _resolve_token(path, auth, host="fw-1", **hostvars):
    """The token as the resolve task sets it, for a recipe with `auth`, a host named `host` holding `hostvars`."""
    expr = _named(path, "Resolve the auth token")["ansible.builtin.set_fact"]["_api_token"]
    return _ansible_env(**hostvars).from_string(str(expr)).render(inventory_hostname=host, _api_recipe={"auth": auth})


@pytest.mark.parametrize("which", ["token", "pair"])
def test_a_per_host_value_that_cannot_be_read_is_not_read_when_the_recipes_var_holds_one(which):
    """With the recipe's var holding the credential the per-host name is not read at all: a per-host value whose
    lookup fails (raised here, as ansible-core raises it) leaves the recipe's value resolved, in the check and in the
    backup; with the recipe's var empty the per-host value is read, and its failure raises, which is the rescue's
    case. Guards the I-16 reads' S2: the per-host lookup sat in `default()`'s argument, which Jinja evaluates whether
    or not the filter uses it, and 2.18's vars lookup renders what it returns, so on 2.18 a good recipe pair beside a
    per-host SOPS lookup over a domain that will not open failed the resolve, censored (base and 2.19 checked it
    OK); the token's resolve had the same eager fallback."""
    bad = _Unreadable("could not locate file in lookup: secrets/network.sops.yml")
    for path in (_CHECK, _BACKUP):
        if which == "token":
            assert _resolve_token(path, BEARER, fortios_api_token="t-recipe", fw_1_api_token=bad) == "t-recipe", path
            with pytest.raises(_Unreadable):
                _resolve_token(path, BEARER, fortios_api_token="", fw_1_api_token=bad)
        else:
            assert _resolve(path, BASIC, probe_user="u-recipe", probe_pass="p-recipe", fw_1_api_username=bad,
                            fw_1_api_password=bad) == ("u-recipe", "p-recipe"), path
            with pytest.raises(_Unreadable):
                _resolve(path, BASIC, probe_user="u-recipe", probe_pass="", fw_1_api_password=bad)


def test_the_request_sends_the_resolved_pair_and_looks_nothing_up():
    """The check's GET and the backup's pull send `_api_username` and `_api_password`, the resolved facts, or omit
    them, and template no lookup of their own. Guards the lookup moving back into the request's arguments, where a
    name not in scope fails the request under its no_log, censored, before `failed_when: false` applies (I-16)."""
    for path in (_CHECK, _BACKUP):
        uri = _named(path, GET[path])["ansible.builtin.uri"]
        assert uri["url_username"] == "{{ _api_username if _api_username is defined else omit }}", path
        assert uri["url_password"] == "{{ _api_password if _api_password is defined else omit }}", path
        assert "lookup(" not in str(uri), "%s: the request must look nothing up" % path


def _as_x(node, what):
    """A copy of `node` with the word naming what cannot authenticate made `X`, in a refusal's message (an assert's
    or a fail's), its own or its rescue's."""
    node = yaml.safe_load(yaml.safe_dump(node, allow_unicode=True))
    for task in [node] + list(node.get("rescue", [])):
        for module, key in (("ansible.builtin.assert", "fail_msg"), ("ansible.builtin.fail", "msg")):
            if module in task:
                task[module][key] = str(task[module][key]).replace("so this %s cannot" % what, "so this X cannot")
    return node


def test_the_check_and_the_backup_resolve_and_refuse_the_pair_alike():
    """The pair's block (its resolve and the rescue of a lookup that fails) and its refusal, and the token's block,
    are the same in both entrypoints but for the word naming what cannot authenticate. Guards one drifting from the
    other, as the check's hint once did from the backup's (I-17)."""
    for name in (PAIR_BLOCK, PAIR_REFUSE, TOKEN_BLOCK):
        check, backup = _as_x(_named(_CHECK, name), "check"), _as_x(_named(_BACKUP, name), "capture")
        assert check == backup, name


def test_the_onboard_writes_the_basic_pair_under_the_names_the_role_reads():
    """The api backend's `username` and `password` entries name the per-host vars the role's second candidates read
    (`<cred key>_api_username`, `<cred key>_api_password`, the cred key being the host name with `-` as `_`).
    Guards I-16's other half: the onboard wrote the pair as `ansible_user` and `ansible_password`, which the role
    never read, so an onboarded basic host could not authenticate; a fallback to those connection vars instead
    would send whatever a group sets for SSH to the API."""
    text = open(os.path.join(_ROOT, "ansible", "backends", "api", "backend.yml"), encoding="utf-8").read()
    entries = {e["field"]: e["maps_to"] for e in yaml.safe_load(text)["auth"]}
    assert entries["username"] == "{cred_key}_api_username" and entries["password"] == "{cred_key}_api_password"
