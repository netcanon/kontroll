"""The API backup's failure says why, for every auth type and every kind of answer, and prints nothing secret.

I-17 (the I-7 read's N8): `backend_api`'s backup recorded "FAILED: HTTP <status> from the <recipe> backup endpoint
<path> — check the API token's permissions." for every pull that was not a 200: a basic recipe (no token), a 404 from
a wrong backup path, a 500, and a device that never answered (status -1) alike. That is the misdiagnosis I-7 fixed in
the check (test_backend_api_check_teeth.py). The record now names the cause as the check's refusal does: a 401 or 403
prints the check's auth hint, word for word, which names the variables the recipe's auth reads and never their values;
no status, or one below 0, is a device that did not answer; any other status is an API that answered, but not with
200. The summary prints this record, so what the message may read is held here as the check's is held there.
"""
import os
import re

import jinja2
import jinja2.meta
import pytest
import yaml

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TASKS = os.path.join(_ROOT, "ansible", "roles", "backend_api", "tasks")
FAILED = "Record capture failure (reported in summary, not a hard abort)"
REFUSE = "Refuse unless the API actually answered successfully"


def _task(entrypoint, name):
    """The task of tasks/<entrypoint> named `name`, found in its blocks too."""
    found = []

    def walk(node):
        if isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            if node.get("name") == name:
                found.append(node)
            for key in ("block", "rescue", "always"):
                walk(node.get(key))

    with open(os.path.join(_TASKS, entrypoint), encoding="utf-8") as fh:
        walk(yaml.safe_load(fh))
    assert len(found) == 1, (entrypoint, name, len(found))
    return found[0]


def _record():
    """The failure record's message and the auth hint (a task var) its 401/403 branch prints."""
    task = _task("backup.yml", FAILED)
    return (str(task["ansible.builtin.set_fact"]["kontroll_backup_result"]),
            str((task.get("vars") or {}).get("_api_auth_hint", "")))


def _render(auth, status, host="fw-1"):
    """The record as the task renders it for a recipe with `auth` and a pull that registered `status` (None: no status
    at all). Undefined is strict, as ansible-core's is for a missing attribute, so a message that names an attribute a
    recipe does not have (the I-7 fault) raises here."""
    msg, hint = _record()
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    ctx = {"inventory_hostname": host, "backend_api_recipe": "probe",
           "_api_recipe": {"auth": auth, "backup": {"path": "/api/backup"}},
           "_api_resp": {"changed": False} if status is None else {"changed": False, "status": status}}
    ctx["_api_auth_hint"] = env.from_string(hint).render(**ctx)
    return env.from_string(msg).render(**ctx)


TOKEN_HINT = ("The API rejected the credential: check that `fortios_api_token` (or, if it is empty, `fw_1_api_token`)"
              " holds a current token with read permission.")
BASIC_HINT = ("The API rejected the credential: check that `probe_user` and `probe_pass` hold a current username and"
              " password with read permission.")
NONE_HINT = "The API refused the request, and the recipe sends no credential: name one in its auth."
DOWN = ("The device did not answer the API port, or its TLS failed — wrong address, port, certificate, or the service"
        " is down.")
ANSWERED = "The API answered, but not with 200: check the recipe backup path and method, and the device API."
BEARER = {"type": "bearer", "token_var": "fortios_api_token"}
APIKEY = {"type": "apikey_header", "header_name": "X-Api-Key", "token_var": "fortios_api_token"}
BASIC = {"type": "basic", "username_var": "probe_user", "password_var": "probe_pass"}
NONE = {"type": "none"}
BASIC_NO_PASS = {"type": "basic", "username_var": "probe_user"}
BASIC_TYPE_ONLY = {"type": "basic"}
DIGEST = {"type": "digest", "username_var": "probe_user", "password_var": "probe_pass"}


@pytest.mark.parametrize("auth, status, http, remedy", [
    (BEARER, 401, "401", TOKEN_HINT),
    (BEARER, 403, "403", TOKEN_HINT),
    (APIKEY, 401, "401", TOKEN_HINT),
    (APIKEY, 403, "403", TOKEN_HINT),
    (BASIC, 401, "401", BASIC_HINT),     # before I-17: "check the API token's permissions", for a recipe with no token
    (BASIC, 403, "403", BASIC_HINT),
    (NONE, 401, "401", NONE_HINT),
    (NONE, 403, "403", NONE_HINT),
    (BASIC_NO_PASS, 401, "401", BASIC_HINT.replace("`probe_pass`", "`?`")),
    # The type alone picks the basic hint: a hand-written recipe that names neither var (derive_auth names both).
    (BASIC_TYPE_ONLY, 401, "401", BASIC_HINT.replace("`probe_user`", "`?`").replace("`probe_pass`", "`?`")),
    (DIGEST, 401, "401", BASIC_HINT),    # the pull sends the vars a recipe names, whatever its type
    (BEARER, 500, "500", ANSWERED),      # before I-17: the token blamed for the device's own error
    (BASIC, 404, "404", ANSWERED),       # a wrong backup path
    (BEARER, 201, "201", ANSWERED),      # the check takes a 201; the backup takes only a 200
    (BEARER, 302, "302", ANSWERED),
    (BEARER, -1, "-1", DOWN),            # uri's status for a refused connection, a timeout, a TLS failure
    (BASIC, -1, "-1", DOWN),
    (BEARER, None, "?", DOWN),
], ids=["bearer-401", "bearer-403", "apikey-401", "apikey-403", "basic-401", "basic-403", "none-401", "none-403",
        "basic-no-password-var-401", "basic-type-only-401", "digest-named-vars-401", "bearer-500", "basic-404",
        "bearer-201", "bearer-302", "bearer-refused", "basic-refused", "bearer-no-status"])
def test_the_backup_failure_names_the_cause_for_each_auth_type_and_answer(auth, status, http, remedy):
    """The failure record, rendered as the task renders it, for each auth type the role builds a request for and each
    kind of failed pull: a 401 or a 403 names the variables that auth reads (a basic recipe's username and password
    vars, by its type alone too, and any recipe's that names them; a var the recipe does not name is `?`; a token
    recipe's `token_var`, else the onboarded per-host name), never their values; a status below 0 or none says the
    device did not answer or its TLS failed; any other status, a 201 too, says the API answered, but not with 200.
    Guards I-17: every one of these said "check the API token's permissions", which sends the operator of a basic
    recipe, a wrong path or a down device to a token that is not the cause."""
    expected = "FAILED: HTTP %s from the probe backup endpoint /api/backup. %s" % (http, remedy)
    assert _render(auth, status) == expected


def test_the_record_is_made_for_every_answer_but_a_200():
    """The record's `when` fails the pull for every status but 200, the 201 and the 204 the check takes included. The
    capture is written only for a 200, so a record keyed to the check's success set instead would leave a 201 or a 204
    with no result at all, neither a failure nor a capture: backup-configs.yml's summary would print it as "not run —
    unreachable or skipped" and the run would not fail. The rows above render what a 201 prints, but `_render` never
    evaluates the `when` (the I-17 read's N3: no suite held it). Guards the condition drifting to the check's set."""
    task = _task("backup.yml", FAILED)
    assert "(_api_resp.status | default(0)) != 200" in task["when"], task["when"]


def test_the_backup_hint_is_the_checks_word_for_word():
    """The backup's auth hint is the check's refusal's, character for character. Guards the two drifting apart: they
    name the same variables for the same recipe, and a fix to one (I-7 fixed the check's alone, which left the
    backup's message wrong as I-17) would otherwise leave the other stale with every other test passing."""
    check = str((_task("check.yml", REFUSE).get("vars") or {}).get("_api_auth_hint", ""))
    _, backup = _record()
    assert check and backup == check


def test_the_backup_failure_cannot_leak_the_token_or_the_config():
    """The pull carries the token and its answer is the device's configuration, and this record is printed by the
    backup summary (it is not `no_log`). So the message and its hint may read the status, the recipe's backup path and
    the NAMES its auth reads, never the response body, the whole registered result, the token or the headers: the
    names both read are held to a list, `_api_resp` to its status, and `_api_recipe` to its auth's names and its backup
    path, never whole. The task's only var is the hint, since a task var outranks a host var and a second one could
    rebind a name the message prints (`backend_api_recipe`) to the token (the I-7 read's S1, the same shape here); and
    the record sets only the result and the failed flag, so no other fact of it carries a value to a later print.
    The task holds no other key: a `loop:` prints each item at the default verbosity, so a loop over the response body
    would print the config with the record and every other assertion here would pass (the I-17 read's N1, run through
    the real backup-configs.yml)."""
    msg, hint = _record()
    task = _task("backup.yml", FAILED)
    assert task.get("no_log") is not True, "the record is what the operator reads"
    assert set(task.get("vars") or {}) == {"_api_auth_hint"}, \
        "a task var on the record outranks a host var, so it can rebind a name the message prints"
    assert set(task["ansible.builtin.set_fact"]) == {"kontroll_backup_result", "kontroll_backup_failed"}
    assert set(task) <= {"name", "ansible.builtin.set_fact", "vars", "when"}, set(task)
    env = jinja2.Environment()
    names = set().union(*(jinja2.meta.find_undeclared_variables(env.parse(t)) for t in (msg, hint)))
    assert names <= {"inventory_hostname", "backend_api_recipe", "_api_recipe", "_api_resp", "_api_auth_hint"}, names
    for leak in (".content", ".json", "_api_token", "_api_headers"):     # the hint names `<host>_api_token` in text
        assert leak not in msg, "the failure record must not surface %r" % leak
    for text in (msg, hint):
        assert "lookup(" not in text and "hostvars" not in text and "vars[" not in text, text
        assert all(m.group(1) == "status" for m in re.finditer(r"(?<!\w)_api_resp\.(\w+)", text)), text
        assert not re.search(r"(?<!\w)_api_resp(?![.\w])", text), "the registered result itself must not be printed"
        assert all(m.group(1) in ("auth", "backup") for m in re.finditer(r"(?<!\w)_api_recipe\.(\w+)", text)), text
        assert all(m.group(1) in ("type", "username_var", "password_var", "token_var")
                   for m in re.finditer(r"(?<!\w)_api_recipe\.auth\.(\w+)", text)), text
        assert all(m.group(1) == "path" for m in re.finditer(r"(?<!\w)_api_recipe\.backup\.(\w+)", text)), text
        assert not re.search(r"(?<!\w)_api_recipe(\.(auth|backup))?(?![.\w])", text), \
            "the recipe, its auth or its backup must not be printed whole"
