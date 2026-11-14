"""A play that runs a role on every host of a group, whatever the host's own role, refuses a host whose connection is
not one that role is written for, before anything runs on it.

WHY. The fleet plays (ping.yml, backup-configs.yml) include `{{ device_role }}`, so a host runs only the role its own
`device_role` names, and each imports tasks/refuse-device-role-fact.yml first, which refuses a host whose
`device_role` is a fact (a module that returns facts set it); which roles may sit beside a controller-side
connection is a separate rule (the homelab tine's `local` rule). Two plays dispatch by group instead (the first
test holds that set). wire-logging.yml runs `-e role` on every host of a device class's
group, and install-node-exporter.yml runs node_exporter on `hypervisors:docker_hosts`.
A host in such a group whose connection executes on the controller would have that role's tasks run on the control
node, as the runner, with inputs (role defaults) that any host var outranks. Examples: an `authorized_keys` line under
a home the host names, a journald config edit, an apt install. Found by the read of the homelab catch-up's fold 4
(S1). The first cut refused two spellings of `local`; its read (2026-10-23-local-refusal, B1) measured ansible-core
running a host's tasks on the controller under four more spellings of the local plugin and under network_cli,
httpapi and netconf, so the refusal is an allowlist: the connections the role is written for.

The play imports ansible/playbooks/tasks/refuse-local-connection.yml as its first task, with `gather_facts: false`,
and passes the allowlist as `refusal_connections` on the import. The refusal is an `assert`, an action on the
controller, then a `set_fact` that freezes the connection it checked. It fails the refused host, and the play goes on
for the others.

The scan's scope is a play that runs a role. A play that runs inline tasks on a group, and no role, is outside it by
design (fold 1's read B, N3). Where it cannot tell, it errs toward holding a play to the refusal: a fleet play that
loads a vars file or an include_vars, one that runs a fixed role under a `when:`, and `hosts: localhost,` are held
too, on purpose (its N1), so that none is "fixed" into a hole.
"""
import ast
import codecs
import configparser
import hashlib
import json
import os
import re
import shutil
import subprocess

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PLAYBOOKS = os.path.join(ROOT, "ansible", "playbooks")
ROLES = os.path.join(ROOT, "ansible", "roles")
LOGGING = os.path.join(ROOT, "logging")
REFUSAL = "tasks/refuse-local-connection.yml"
_ROLE_KEYS = ("include_role", "import_role")
_TASKS_KEYS = ("include_tasks", "import_tasks")
_PLAYBOOK_KEYS = ("import_playbook",)
_SETUP_KEYS = ("setup",)
_ACTION_KEYS = ("action", "local_action")
_LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")
# ansible-core loads the local plugin under each of these (core's runtime.yml redirects redirected_local to local).
_LOCAL_FAMILY = ("local", "ansible.builtin.local", "ansible.legacy.local", "redirected_local",
                 "ansible.builtin.redirected_local", "ansible.legacy.redirected_local")
# The connections that execute a role's tasks on the host itself: the bound a play's list stays within.
_SSH_FAMILY = ("ssh", "ansible.builtin.ssh", "ansible.legacy.ssh", "paramiko_ssh", "ansible.builtin.paramiko_ssh",
               "ansible.legacy.paramiko_ssh")
# A task's own `when` condition that holds it to the ssh family: a literal list, so no var can widen it (I-92).
_SSH_GATE = re.compile(r"ansible_connection in (\[[^\]]*\])")
# NetworkConnectionBase plugins: a network module talks to the device; any other module runs on the controller.
_NETWORK_FAMILY = tuple(p + n for n in ("network_cli", "httpapi", "netconf", "grpc")
                        for p in ("", "ansible.netcommon."))
# What a role run over a network plugin may hold: actions that run on the controller and touch no host, and the
# netcommon CLI modules, which talk to the device over the plugin. Each named bare or under core's or netcommon's
# own collection only: another collection's `cli_config` is that collection's code (fold 6's read A, N4).
_NETWORK_SAFE = ("assert", "set_fact", "debug", "fail", "meta", "cli_config", "cli_command")
_NETWORK_SAFE_NAMES = tuple(p + n for n in _NETWORK_SAFE
                            for p in ("", "ansible.builtin.", "ansible.legacy.", "ansible.netcommon."))
_TASK_KEYWORDS = {"name", "when", "register", "vars", "no_log", "tags", "changed_when", "failed_when", "notify",
                  "check_mode", "diff", "become", "loop", "loop_control", "ignore_errors", "delegate_to", "run_once"}
_WIRE_ALLOWLIST = "{{ hostvars['localhost']._wire_connections | default([]) }}"
_FREEZE = ("{{ refusal_connections | default([]) | select('equalto', ansible_connection | default('')) | first"
           " | default('') }}")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
# ansible-core's own argument names: a templated `_variable_params` mapping is merged into the arguments on both
# cores (2.18 task_executor, 2.19 TaskArgsFinalizer), so the names it sets are opaque (fold 15's read A, S1).
_INTERNAL_ARGS = ("_variable_params", "_raw_params")
# A path ansible-core reads as written: anything else (a quote, a space, `$`, `~`, a template) it unquotes, strips,
# expands or renders first, so a file named that way is a decoy.
_PLAIN = re.compile(r"[A-Za-z0-9_./-]+\Z")
# Every directory ansible-core loads a plugin from beside a playbook or a legacy role (2.18 and 2.19) is a
# `*_plugins` one (action, become, cache, callback, cliconf, connection, filter, httpapi, inventory, lookup, netconf,
# shell, strategy, terminal, test, vars) or one of these three; ansible.cfg's key for a plugin path is the same name
# (`doc_fragment_plugins` for `doc_fragments`). A filter or test plugin is looked up ahead of the Jinja builtin of its
# name, so a `first` or a `default` there rewrites the freeze itself (fold 5's read A, S1).
_PLUGIN_DIRS = ("library", "module_utils", "doc_fragments")
# The shipped `[ssh_connection]`: an `ssh_executable` or a `ProxyCommand` there runs a command on the controller with
# the freeze untouched (fold 5's read A, N1).
_SSH_CONNECTION = {"pipelining": "True", "ssh_args": "-o ControlMaster=auto -o ControlPersist=60s"}
# The shipped ansible/ansible.cfg's sections, and its `[defaults]` and `[inventory]` keys with their values. Any other
# section or key is a setting the scan does not reason about: `[persistent_connection] ansible_connection_path` names
# the program the controller runs for a network_cli, httpapi or netconf host, 2.19's `[connection]
# ssh_agent_executable` one it starts for the run, `[paramiko_connection]` a `proxy_command`, and `[defaults]
# jinja2_extensions` code the templar imports (fold 6's read A, N1). A held key's value is held too (a
# `collections_path`, a `stdout_callback`, a `vars_plugins_enabled` or an `enable_plugins` names code ansible-core
# loads on the controller), and so is its presence: without one ansible-core's own default stands in, and with no
# `[inventory]` that default enables `script` and `auto` (fold 7's read B, N2; read A, N1). So the file is held to its
# shipped sections, keys and values; a change to it changes these in the same diff.
_CFG_SECTIONS = ("defaults", "inventory", "ssh_connection")
_CFG_DEFAULTS = {"callback_result_format": "yaml", "collections_path": "collections",
                 "filter_plugins": "filter_plugins", "host_key_checking": "False",
                 "interpreter_python": "auto_silent", "inventory": "../instance/inventory",
                 "log_path": "./.ansible/ansible.log", "nocows": "True",
                 "retry_files_enabled": "False", "roles_path": "roles", "stdout_callback": "default",
                 "vars_plugins_enabled": "host_group_vars,community.sops.sops"}
_CFG_INVENTORY = {"enable_plugins": "yaml, ini, constructed"}
# The reviewed filter files ansible/ansible.cfg's `filter_plugins` loads, by their sha256 (line endings as LF): a
# filter file is code ansible-core imports on the controller, so a change to one changes this pin in the same
# diff, where a reviewer reads it (fold 6's read B, N1).
_FILTER_FILES = {
    "kontroll_sops.py": "b17e653c6b6afd4548e04b4a3e81a132398a7d81d7d53ea9a8f5a29f7e883f6f",
    "kontroll_token.py": "503e3d20ed57425fdc839154e14b0a867ea947d1108d203b11fb46f656811153",  # gitleaks:allow (a hash)
}


def _plugin_dir(name):
    """Whether ansible-core loads plugins from a directory of this name beside a playbook or a legacy role."""
    return name in _PLUGIN_DIRS or name.endswith("_plugins")


def _names(module, names):
    """Whether `module` is one of `names`, bare or under a collection (`ansible.builtin.x`, `ansible.legacy.x`)."""
    return isinstance(module, str) and any(module == n or module.endswith("." + n) for n in names)


def _key(task, names):
    """The key of `task` that runs one of `names`: the module as a key, or `action:` / `local_action:` naming it
    (`action: ansible.builtin.include_role name=x`, or a mapping with `module:`); else None."""
    for key in task:
        if _names(key, names):
            return key
    for key in _ACTION_KEYS:
        value = task.get(key)
        words = str((value.get("module") if isinstance(value, dict) else value) or "").split()
        if words and _names(words[0], names):
            return key
    return None


def _network_safe(task):
    """Whether `task` runs a `_NETWORK_SAFE` module named bare or under `ansible.builtin`, `ansible.legacy` or
    `ansible.netcommon`, as a key or in `action:` form (`_names` takes any collection's)."""
    key = _key(task, _NETWORK_SAFE)
    if key in _ACTION_KEYS:
        value = task[key]
        key = str((value.get("module") if isinstance(value, dict) else value) or "").split()[0]
    return key in _NETWORK_SAFE_NAMES


def _folds_args(task):
    """Whether ansible-core folds into `task`'s module arguments the scan does not read where it reads the module's:
    the task's `args:` keyword, or an `action:` / `local_action:` mapping at all, whose own `args` (fold 7's read B,
    S1) and the `k=v` words after its `module:` name (fold 8's reads A and B, S1: `module: include_role name=x` beside
    `name: "{{ device_role }}"` ran `x`) ansible-core merges OVER the mapping's own keys, so no mapping form is read.
    An `action:` string is never read as a path or a role name."""
    return "args" in task or any(isinstance(task.get(k), dict) for k in _ACTION_KEYS)


def _ref(task, names):
    """The path a task-file or playbook include names, or None: as written, or a mapping's `_raw_params` or `file`.
    A templated path is returned as written, and an `action:` form, an include that carries `args:` (ansible-core
    merges it under the key's own arguments, so a null key's path, or a `_raw_params` beside a `file`, comes from there:
    fold 6's read B, S1) or a mapping that names neither as a marker that names no file, so none of them is ever
    followed."""
    key = _key(task, names)
    if key is None:
        return None
    ref = task[key]
    if key in _ACTION_KEYS or "args" in task:
        return "{{ action form or args: %s }}" % (ref,)
    if isinstance(ref, dict):   # ansible-core takes `_raw_params` ahead of `file` (fold 5's read B, S1)
        return ref.get("_raw_params") or ref.get("file") or "{{ no path: %s }}" % (ref,)
    return ref


def _set_fact_names(task, key):
    """(names, opaque) for a `set_fact` task: the names it sets as written (the module's mapping or `k=v` words, and
    the task's `args:`), and whether it holds one the scan cannot read. ansible-core templates a set_fact key (under
    `{{`, `{%` or `{#`), so a name that is not a plain identifier (template syntax before a `k=v` word's `=` leaves
    one) or an `args:` that is not a mapping (ansible-core renders a templated one into the arguments) is opaque. A
    name `args` counts as `ansible_connection`: in the `action:` form ansible-core folds the module's own `args` (a
    mapping or `k=v` words) into its arguments (fold 4's read A, S1). A name that is one of ansible-core's own
    arguments (`_INTERNAL_ARGS`) is opaque: both cores merge a templated `_variable_params` into the task's arguments,
    so it sets any name (fold 15's read A, S1)."""
    sets, extra = task[key], task.get("args")
    names, opaque = [], extra is not None and not isinstance(extra, dict)
    words = [str(sets.get("module") or "")] if key in _ACTION_KEYS and isinstance(sets, dict) else []
    for part in [sets, extra] + words:
        if isinstance(part, dict):
            names += [str(k) for k in part]
        elif isinstance(part, str):
            names += [w.split("=", 1)[0] for w in part.split() if "=" in w]
    return names, opaque or any(not _IDENT.match(n) or n in _INTERNAL_ARGS for n in names)


def _unsafe_hook(pairs):
    """2.18's JSON reader takes a mapping with an `__ansible_unsafe` key as that key's value, anywhere in a file."""
    return pairs["__ansible_unsafe"] if "__ansible_unsafe" in pairs else pairs


class _Unreadable(AssertionError):
    """A playbook or task file `_read` cannot parse: the test that reads it fails naming the file, not in a traceback
    (fold 12's read B, N1)."""


def _read(path):
    """The document a playbook or task file holds, as 2.18 reads it: its bytes decoded as 2.18 decodes them
    (`surrogateescape`), JSON first (2.18 reads `{"__ansible_unsafe": x}` as `x`, so a play or a task wrapped so ran
    with the scan blind to it: fold 8's read B, S2), then YAML. 2.19, and the `from_yaml` filter, keep a wrapper's
    siblings instead; the tree rule refuses any `__ansible` key (`_ansible_keyed`), so on every file the tool ships the
    three readings agree. A file neither reading parses (a NUL, several documents, a tag the safe loader does not know,
    a byte that is not UTF-8 outside a JSON string, a nesting deeper than PyYAML reads) raises `_Unreadable`, which
    names it: ansible-core reads none of those but a tagged one (`!unsafe`), which it runs, so the test fails closed
    there, and is never skipped (fold 12's read B, N1: two tests ended in a traceback on each)."""
    with open(path, "rb") as fh:
        text = fh.read().decode("utf-8", "surrogateescape")
    try:
        try:
            return json.loads(text, object_hook=_unsafe_hook)
        except ValueError:
            return yaml.safe_load(text)
    except (yaml.YAMLError, ValueError, RecursionError) as e:
        raise _Unreadable("%s: a file the scan cannot read (%s)" % (path, type(e).__name__)) from None


def _readings(path):
    """Every document the file reads to, its bytes decoded as 2.18 decodes them (`surrogateescape`): as JSON with every
    key as written, as JSON the way 2.18 reads it (`_unsafe_hook`), and as YAML by libyaml's reader, which ansible-core
    uses, and by PyYAML's own, which it uses where libyaml is not installed, a tag read as the node it tags. A reading
    that fails is left out; on some file each of the four gives a document the other three do not."""
    with open(path, "rb") as fh:
        text = fh.read().decode("utf-8", "surrogateescape")
    docs = []
    for hook in (None, _unsafe_hook):
        try:
            docs.append(json.loads(text, object_hook=hook))
        except ValueError:
            pass
    for loader in (_AnyTagC, _AnyTag):
        try:
            docs.append(yaml.load(text, Loader=loader))   # noqa: S506 (SafeLoader subclasses)
        except (yaml.YAMLError, ValueError):   # libyaml cannot encode a surrogate, so it raises UnicodeEncodeError
            pass
    return docs


def _keys(node):
    """Every mapping key in a document, at any depth, as text."""
    if isinstance(node, dict):
        for k, v in node.items():
            yield str(k)
            yield from _keys(v)
    elif isinstance(node, list):
        for v in node:
            yield from _keys(v)


def _ansible_keyed(path):
    """Whether any reading of the file (`_readings`) holds a mapping key that starts `__ansible`. ansible-core reads
    such a key three ways: 2.18 reads `{"__ansible_unsafe": x}` as `x` and drops its siblings, 2.19 does so for a
    string `x` and otherwise reads the whole file as YAML, siblings kept, and `from_yaml` (wire-logging.yml's reader
    of logging/) keeps them on both cores. The scan reads 2.18's way; no shipped file holds such a key, so with none
    allowed the three readings agree on every file the tool ships (fold 9's read A, S1, and read B, S1: on 2.19 a
    wrapped `set_fact` of `ansible_connection: local` with a sibling ran after the refusal, scan green). A key tagged
    `!vault` reads as one too (`_untagged`): ansible-core decrypts it at run time, to a name the scan never read."""
    return any(k.startswith("__ansible") for doc in _readings(path) for k in _keys(doc))


_MODULE_NAME = re.compile(r"[A-Za-z0-9_.]+\Z")   # a module named as written; ansible-core renders any other


def _templated_module(task):
    """Whether `task` has an `action:` or `local_action:` whose module (a string's first word, or a mapping's
    `module`'s, split as ansible-core splits it: at a space or a line break, not a tab) is not a name as written:
    ansible-core renders `{{ 'set_fact' }}`, `set_fact{{ '' }}` or `set<tab>{{- '_fact' }}` (whose `-` strips the
    tab) at run time and runs the module it names, so the scan cannot read which module runs (fold 12's read B, S1
    and S2; a Python `split()` took `set` as the word of the last, which ran a task on the controller after the
    refusal and set `device_role` with the scan green, on both cores: fold 13's read B, S1). A word split so that
    matches `_MODULE_NAME` holds no Jinja delimiter, so ansible-core cannot render it into another name; one that
    leads with a space reads as empty, and is refused."""
    return any(k in task and not _MODULE_NAME.match(re.split(r"[ \n]", str((task[k].get("module") if isinstance(
        task[k], dict) else task[k]) or ""))[0].strip()) for k in _ACTION_KEYS)


_KV_ROLE = re.compile(r"(?<![A-Za-z0-9_.])device_role\s*=(?!=)")
_ESCAPES = re.compile(r"\\U[a-fA-F0-9]{8}|\\u[a-fA-F0-9]{4}|\\x[a-fA-F0-9]{2}|\\N\{[^}]+\}|\\[\\'\"abfnrtv]")


def _kv_texts(text):
    """`text` as written and as ansible-core's `parse_kv` reads each `k=v` word of it: `split_args` splits the text on
    spaces and line breaks, and each word's escapes are decoded (its `_decode_escapes`: `\\x72`, `\\u0065`,
    `\\N{SPACE}`, `\\t`) before it takes the key, which it then strips, so `device_\\x72ole=x` or `device_role\\t=x`
    sets `device_role` (fold 14's read A, S1). Each word is decoded alone: a `\\N{` read across the words to a later
    one's `}` hid the escapes inside it, and `a=\\N{X device_\\x72ole=x b=}` set `device_role` on both cores, split by
    spaces or by line breaks (fold 15's own probe; the tree rule refuses such a `set_fact` for its `{` too, so
    `test_the_kv_reading_is_ansible_cores_own` holds the reading). An escape that does not decode is kept as written:
    ansible-core ends the run on it."""
    def decode(m):
        try:
            return codecs.decode(m.group(0), "unicode-escape")
        except (UnicodeDecodeError, ValueError):
            return m.group(0)
    return text, "".join(_ESCAPES.sub(decode, w) for w in re.split("([ \n])", text))


_VAR_SETTERS = ("set_fact", "include_vars", "add_host", "load_vars", "cli_parse", "update_fact", "group_by")


def _module_of(value):
    """The short name of the module an `action:`/`local_action:` value names: its first word, a mapping's `module`'s
    too (ansible-core splits `module: include_vars file=v.yml` into the module and its arguments, so a mapping whose
    `module` held its arguments set `device_role` unseen: fold 11's read B, S1)."""
    raw = value.get("module", "") if isinstance(value, dict) else value if isinstance(value, str) else ""
    return str((str(raw).split() or [""])[0]).rsplit(".", 1)[-1]


def _sets_device_role(node):
    """Whether a document, read as data, sets `device_role` anywhere in it: a mapping key `device_role` (a play's, a
    task's, a role entry's, an import's or a set_fact's vars, role vars or defaults, group_vars or host_vars beside the
    playbooks), a `register`, `loop_var` or `index_var` that names it or is templated, an `add_host` (it writes a host's
    vars), a `group_by` (it moves a host into a group whose vars may name another device_role; same-depth groups merge
    by name: fold 14's read A, N2), a `load_vars` (community.sops: each key of a file becomes a fact), a `cli_parse`
    (ansible.utils: its `set_fact` names one; either set it with the scan green: fold
    11's read A, S2) or an `update_fact` (ansible.utils: the first part of each `updates`
    path becomes a fact; fold 12's read A, S3), an `include_vars` without a `name` that
    is a plain string other than device_role (a null or false one loads the file unscoped: fold 11's read B, S2), a
    `set_fact` whose key is templated, an `action:` form of any of those (a mapping's `module` read by its first word)
    or of a `set_fact` written as a mapping or with a templated word, an `action:` or `local_action:` whose module is
    not a name as written, split as ansible-core splits it (`_templated_module`: ansible-core renders `{{
    'include_vars' }}` or `include<tab>{{- '_vars' }}` there at run time, and a fixed role ran on a `local` host with
    the rule green: fold 12's read B, S2; fold 13's read B, S1), an `args:` that is templated, a `vars_prompt` that
    names it, or a `device_role=` word in a string (a `k=v` form), read as written and as ansible-core's `parse_kv`
    reads it, each escape decoded and the key stripped (`_kv_texts`: `device_\\x72ole=x` and `device_role<tab>=x` set it
    past the rule, on both cores: fold 14's read A, S1). A read (`when: device_role == 'x'`) is not one. The tool never
    sets the role a host dispatches by; only the instance's inventory does, so a play's `{{ device_role }}` is the
    host's own whatever play or role code ran before it. Read per play, a `register`, a loop variable, an `add_host` or
    a `set_fact` in an earlier play or in a role's own code set it, and backup-configs.yml ran a fixed role on a `local`
    host on 2.18 and 2.19 with the scan green (fold 9's read A, S2; one in group_vars beside the playbooks, its N1).
    A key `_variable_params` (ansible-core's own: a templated one sets any name), a `group_by` in `action:` form and
    a set_fact's own `device_role==` word (`_set_fact_eq`: it sets a value that begins with `=`, the name of a role
    whose directory name begins with `=`) set it too (fold 15's read A, S1, N1 and S3)."""
    if isinstance(node, list):
        return any(_sets_device_role(v) for v in node)
    if isinstance(node, str):
        return any(_KV_ROLE.search(t) for t in _kv_texts(node))
    if not isinstance(node, dict):
        return False
    for k, v in node.items():
        k = str(k)
        short = k.rsplit(".", 1)[-1]
        if _set_fact_eq(k, v, "device_role"):
            return True
        if k in ("device_role", "_variable_params") or \
                short in ("add_host", "load_vars", "cli_parse", "update_fact", "group_by"):
            return True
        if k in ("register", "loop_var", "index_var") and isinstance(v, str) and \
                (v.strip() == "device_role" or "{" in v):
            return True
        if short == "include_vars":
            name = v.get("name", "") if isinstance(v, dict) else ""
            if not isinstance(name, str) or not _IDENT.match(name.strip()) or name.strip() == "device_role":
                return True
        if short == "set_fact" and ("{" in v if isinstance(v, str) else
                                    isinstance(v, dict) and any("{" in str(x) for x in v)):
            return True
        if k in _ACTION_KEYS and _templated_module(node):
            return True
        if k in _ACTION_KEYS and _module_of(v) in _VAR_SETTERS and (_module_of(v) != "set_fact" or "{" in str(v)):
            return True
        if k == "args" and isinstance(v, str) and "{" in v:
            return True
        if k == "vars_prompt" and any(isinstance(p, dict) and str(p.get("name", "")).strip() == "device_role"
                                      for p in (v if isinstance(v, list) else [v])):
            return True
        if _sets_device_role(v):
            return True
    return False


def _sets_device_role_file(path):
    """Whether any reading of the file (`_readings`) sets `device_role` (`_sets_device_role`)."""
    return any(_sets_device_role(doc) for doc in _readings(path))


_KV_FACTS = re.compile(r"(?<![A-Za-z0-9_.])ansible_facts\s*=(?!=)")

# in a set_fact's own k=v string a key's `==` sets a value that begins with `=` (fold 15's read A, S3): the
# lookahead that lets a condition's `device_role == 'x'` pass reads past it there, so read that word alone
_KV_EQ = {"device_role": re.compile(r"(?<![A-Za-z0-9_.])device_role\s*=="),
          "ansible_facts": re.compile(r"(?<![A-Za-z0-9_.])ansible_facts\s*==")}


def _set_fact_eq(k, v, name):
    """Whether `k: v` is a set_fact whose own k=v string (the module as a key, or an `action:` or `local_action:` string
    naming set_fact) sets `name` by a `name==` word, read as `parse_kv` reads it (`_kv_texts`)."""
    if not isinstance(v, str) or not (k.rsplit(".", 1)[-1] == "set_fact" or
                                      k in _ACTION_KEYS and _module_of(v) == "set_fact"):
        return False
    return any(_KV_EQ[name].search(x) for x in _kv_texts(v))


def _names_ansible_facts(node):
    """Whether a document, read as data, names `ansible_facts` as a var: a mapping key (a play's or a task's `vars`, a
    set_fact's), a `register`, `loop_var` or `index_var`, an `include_vars` `name`, a `vars_prompt` name, or an
    `ansible_facts=` word in a string, read as `parse_kv` reads it (`_kv_texts`; fold 14's read A, S1). Such a var
    hides the facts from the device_role guard, which reads `ansible_facts`: after an
    earlier `update_fact`, a play's `vars: {ansible_facts: {}}` passed the guard and a fixed
    role ran on a `local` host, on both cores (fold 13's reads A and B, N2). A key `_variable_params` and a set_fact's
    own `ansible_facts==` word name it too (fold 15's read A, S1 and S3)."""
    if isinstance(node, list):
        return any(_names_ansible_facts(v) for v in node)
    if isinstance(node, str):
        return any(_KV_FACTS.search(t) for t in _kv_texts(node))
    if not isinstance(node, dict):
        return False
    for k, v in node.items():
        k = str(k)
        if _set_fact_eq(k, v, "ansible_facts"):
            return True
        if k in ("ansible_facts", "_variable_params") or \
                k in ("register", "loop_var", "index_var") and str(v).strip() == "ansible_facts":
            return True
        if k.rsplit(".", 1)[-1] == "include_vars" and isinstance(v, dict) and \
                str(v.get("name", "")).strip() == "ansible_facts":
            return True
        if k == "vars_prompt" and any(isinstance(p, dict) and str(p.get("name", "")).strip() == "ansible_facts"
                                      for p in (v if isinstance(v, list) else [v])):
            return True
        if _names_ansible_facts(v):
            return True
    return False


def _revives_anywhere(node):
    """Whether a document, read as data, holds a task that runs `meta: clear_host_errors` (`_revives`) anywhere: one
    in a role a passing host runs revives a host the guard refused, which then ran its fact's role (fold 14's read
    A, N1)."""
    if isinstance(node, list):
        return any(_revives_anywhere(v) for v in node)
    if not isinstance(node, dict):
        return False
    return _revives(node) or any(_revives_anywhere(v) for v in node.values())


# the one templated include the tree may name: node_exporter's OS-family dispatch, pinned to its role's
# `tasks/main.yml` in the roles path (`rel` is a file's path below the roles path, so a copy under
# `<playbooks>/roles`, which ansible-core searches first, is refused: fold 18's read B, S4), with the families its
# `when` must hold it to: ansible-core finds the name it templates in the role's `tasks/` only while that `when`
# keeps the family to these and a file for each is there, and tries the working directory next (fold 19's read A, S5)
_INCLUDE_PINS = {("roles/node_exporter/tasks/main.yml", "{{ ansible_os_family }}.yml"): ("Debian", "Alpine")}
_OUT = "includes a task file or role the scan cannot follow into the tree"
# the dispatch seams, each with the one playbook it is pinned to: `{{ role }}` is the operator's `-e role`, which
# wire-logging.yml checks against its allowlist, and a set_fact or vars elsewhere named any role with it (fold 17's
# read B, S2); `{{ device_role }}`, which only the inventory sets (`_sets_device_role`), is excepted in any file
_ROLE_SEAMS = {"{{ device_role }}": None, "{{ role }}": "wire-logging.yml"}


def _where(path, tree):
    """Where `path` sits in the tree `(base, roles)` that `_tree_faults` walks, for `_reaches_outside`. First, its
    path below `base` (a seam's pin names one). Then the directories ansible-core searches for a role named plainly,
    each before the working directory: `base/roles`, the roles path and `base`. `_tree_faults` refuses a play
    anywhere below the top of the playbooks, `files/` and `templates/` too, so `base` is every play's loader basedir.
    Then the directory a task file it includes must be in. That is its own directory, and only for an entry file: a
    file at the top of the playbooks, or a role's `tasks/main.yml` or `handlers/main.yml` (`.yaml` too) under the
    roles path. There ansible-core's first candidate is that directory however the include is loaded. Any other file
    gets none, so an include in it is refused. Further down a chain, or in a role found elsewhere, ansible-core's
    first place depends on the chain and on where it found the role (fold 19's read B, S1 and S2). Last, the roles path,
    the one directory a role may be found in first, and only as a directory (ansible-core stops where the name first
    exists, a file too, and runs nothing from a file: fold 20's read A, N1): a role only `base/roles` or `base` holds,
    or one `base/roles` shadows, has files ansible-core reads as a role's and the scan as playbook-level ones (fold 19's
    read A, S3). `_tree_faults` refuses every symlink, so each file sits where ansible-core loads it. None only when
    no tree is given: every seam and plain name is then refused (fold 17's read B, S2 and S3)."""
    if not tree:
        return None
    base, roles = (os.path.realpath(d) if d else None for d in tree)
    real = os.path.realpath(path)
    inside = os.path.relpath(real, roles).replace(os.sep, "/").split("/") if roles else [".."]
    entry = os.path.dirname(real) == base or (
        inside[0] != ".." and len(inside) == 3 and inside[1] in ("tasks", "handlers")
        and inside[2] in ("main.yml", "main.yaml"))
    tasks = (os.path.dirname(real),) if entry else ()
    return (os.path.relpath(real, base).replace(os.sep, "/"),
            tuple(d for d in (os.path.join(base, "roles"), roles, base) if d), tasks, roles)


def _includes_tasks(node):
    """Whether a document holds a task-file include or import anywhere: a task file an include names may hold
    none, since ansible-core's first place for its own includes depends on the chain (fold 19's read B, S2)."""
    if isinstance(node, list):
        return any(_includes_tasks(v) for v in node)
    if not isinstance(node, dict):
        return False
    return _key(node, _TASKS_KEYS) is not None or any(_includes_tasks(v) for v in node.values())


def _reaches_outside(node, rel="", where=None):
    """Whether a document holds a task-file include or import, or a role include, import, `roles:` entry or `meta/`
    dependency, whose path or name the scan cannot follow into the tree: a path that is not plain (`_ref` marks
    an `action:` form or an include carrying `args:`), absolute, or with a `..` part; a role named by a path, by a
    collection's name (two dots: ansible-core looks in a collection first) or by a role include that is not a
    mapping; a `*_from` path of those kinds; a dependency that has only a `src` (ansible-core names it from that
    `src`); a seam outside the playbook it is pinned to (`_ROLE_SEAMS`, matched as written); a role named `.` (the
    search directory itself); and a plain name, read as written, that the tree does not hold where ansible-core
    looks before the working directory (`_where`: it then looks there, which the scan never reads).
    ansible-core runs such a file or role from wherever it names, and nothing reads it: from a `hosts: localhost`
    play, or from a role's own code, a file outside the tree set device_role, hid ansible_facts or revived a refused
    host, on both cores (fold 15's read A, S2). A role's `meta/` dependency was read as a `roles:` entry is, by its
    `role` or `name` (fold 15's read B, S3); one by its `src` alone, a `{{ role }}` set in a role's own task and a
    plain name the tree does not hold each revived a refused host on both cores (fold 17's read B, S1 to S3); a
    role named `.`, a name written with a space or a tab, and a name the tree holds only after the working directory
    each passed the rule (fold 18's read B, S1 to S3). An include in a file that is not an entry, and one naming a
    task file that itself includes one, are refused (`_where`; fold 19's read B, S2); so are a role the roles path is
    not the first to hold, whose files ansible-core reads as a role's and the scan as playbook-level ones (read A,
    S3), a `collections` keyword anywhere, since ansible-core looks a role up in each listed collection first (S6),
    and the pinned include with a `when` other than its pin's, or with a family's file missing (S5). Fold 20's read A: a
    role is found where its name first exists, a file too, from which ansible-core runs nothing, so a role the roles
    path does not hold as a directory where it is found first is refused (N1); and a role's `meta/` `dependencies` are
    refused whatever they name, since ansible-core looks each one up as `ansible.legacy.<name>` in every collection root
    before any roles directory (N2)."""
    if isinstance(node, list):
        return any(_reaches_outside(v, rel, where) for v in node)
    if not isinstance(node, dict):
        return False
    here, role_dirs, task_dirs, roles_path = where or ("", (), (), None)
    if "collections" in node:   # ansible-core looks a role up in each listed collection first (fold 19's read A, S6)
        return True
    def bad_path(ref):
        ref = str(ref)
        return not _PLAIN.match(ref) or ref.startswith("/") or ".." in ref.split("/")
    def bad_role(name):
        name = str(name)
        if name in _ROLE_SEAMS:
            return _ROLE_SEAMS[name] not in (None, here) or not where
        # ansible-core takes the first place where the name exists, a file too, and runs nothing from a file
        # (fold 20's read A, N1); found first anywhere but the roles path is refused (fold 19's read A, S3)
        first = next((d for d in role_dirs if os.path.exists(os.path.join(d, name))), None)
        return bad_path(name) or "/" in name or name.count(".") >= 2 or name == "." or first is None or \
            first != roles_path or not os.path.isdir(os.path.join(first, name))
    if _key(node, _TASKS_KEYS) is not None:
        ref = _ref(node, _TASKS_KEYS)
        found = [os.path.join(d, str(ref)) for d in task_dirs if os.path.isfile(os.path.join(d, str(ref)))]
        pin = _INCLUDE_PINS.get((rel, str(ref)))
        if pin is not None:   # its `when` and a file for each family it allows (fold 19's read A, S5)
            if node.get("when") != ["ansible_os_family in %r" % list(pin)] or \
                    not all(os.path.isfile(os.path.join(task_dirs[0], n + ".yml")) for n in pin):
                return True
        elif bad_path(ref) or not found or any(_includes_tasks(doc) for doc in _readings(found[0])):
            return True
    key = _key(node, _ROLE_KEYS)
    if key is not None:
        value = node[key]
        if key in _ACTION_KEYS or "args" in node or not isinstance(value, dict):
            return True
        if bad_role(value.get("name", "")) or any(bad_path(value[k]) for k in
                                                   ("tasks_from", "vars_from", "defaults_from", "handlers_from")
                                                   if k in value):
            return True
    if node.get("dependencies") and rel.split("/")[2:3] == ["meta"]:
        return True   # looked up as ansible.legacy.<name> in each collection root first (fold 20's read A, N2)
    for entry in [e for k in ("roles", "dependencies") if isinstance(node.get(k), list) for e in node[k]]:
        if isinstance(entry, dict) and "role" not in entry and "name" not in entry:
            return True   # ansible-core names such a dependency from its `src` (fold 17's read B, S1)
        name = entry if isinstance(entry, str) else entry.get("role") or entry.get("name") \
            if isinstance(entry, dict) else ""
        if bad_role(name):
            return True
    return any(_reaches_outside(v, rel, where) for v in node.values())


def _file_faults(path, device_role=True, tree=None):
    """[why] the tree rule refuses a file for: an `__ansible` key in any reading (`_ansible_keyed`) and, unless
    `device_role` is false, a `device_role` it sets (`_sets_device_role_file`), an `ansible_facts` var it names
    (`_names_ansible_facts`) or a `meta: clear_host_errors` it runs anywhere (`_revives_anywhere`: a role's own one,
    or a role another host runs, revived a host the guard refused, which then ran its fact's role: fold 14's reads A
    and B, N1 and N2). A file nested deeper than the scan reads is refused as that, where it ended the test in a
    traceback: PyYAML's own reader stops near 500 levels, as ansible-core's file loader does (it copies what it reads),
    and the scan's walks near 1000; libyaml's reader reads deeper, as 2.18's include_vars and the `from_yaml` filter do
    (fold 11's read B, N2; fold 12's read B, N3). It refuses, too, an include or a role the scan cannot follow into
    the tree (`_reaches_outside`, read where the file sits in `tree`, the `(base, roles)` `_tree_faults` walks; fold
    15's read A, S2; fold 17's read B, S2 and S3)."""
    try:
        out = ["an __ansible key, which ansible-core reads three ways"] if _ansible_keyed(path) else []
        out += ["sets device_role, which only the instance's inventory may"] if device_role and \
            _sets_device_role_file(path) else []
        out += ["runs `meta: clear_host_errors`, which revives a refused host"] if device_role and \
            any(_revives_anywhere(doc) for doc in _readings(path)) else []
        below = os.path.relpath(os.path.realpath(path), os.path.realpath(tree[1])).replace(os.sep, "/") \
            if tree and tree[1] else ".."
        rel = "" if below.split("/")[0] == ".." else "roles/" + below
        out += [_OUT] if device_role and \
            any(_reaches_outside(doc, rel, _where(path, tree)) for doc in _readings(path)) else []
        return out + (["names `ansible_facts` as a var, which hides a fact from the device_role guard"]
                      if device_role and any(_names_ansible_facts(doc) for doc in _readings(path)) else [])
    except RecursionError:
        return ["nested deeper than the scan reads"]


def _docs(base):
    """[(relative path, directory, the document `_read` reads)] of every YAML file under `base`."""
    found = []
    for directory, _dirs, files in os.walk(base):
        _dirs.sort()
        for name in sorted(files):
            if name.endswith((".yml", ".yaml")):
                path = os.path.join(directory, name)
                found.append((os.path.relpath(path, base).replace(os.sep, "/"), directory, _read(path)))
    return found


def _plays(base):
    """[(relative path, the playbook's directory, play)] of every play in every playbook under `base`. A task file
    (a list with no `hosts`) and a vars file (a mapping) hold no play."""
    return [(rel, directory, play) for rel, directory, doc in _docs(base) if isinstance(doc, list)
            for play in doc if isinstance(play, dict) and "hosts" in play]


def _imports_outside(base):
    """["path: …"] for every `import_playbook` that names no `.yml`/`.yaml` file under `base`: one outside it, a
    templated path or one that is not plain (ansible-core unquotes, strips or expands it: fold 4's read B, N3, and fold
    5's read B, N3), a collection playbook (`ns.coll.pb`), or a file `_docs` does not parse (any but `.yml`/`.yaml`),
    which ansible-core runs all the same. A play there is not scanned."""
    out, real = [], os.path.realpath(base)
    for rel, directory, doc in _docs(base):
        for entry in doc if isinstance(doc, list) else []:
            ref = _ref(entry, _PLAYBOOK_KEYS) if isinstance(entry, dict) else None
            if ref is None:
                continue
            path = os.path.realpath(os.path.join(directory, str(ref)))
            lexical = os.path.normpath(os.path.join(os.path.realpath(directory), str(ref)))
            if not _PLAIN.match(str(ref)) or os.path.commonpath([path, real]) != real or \
                    not os.path.isfile(path) or \
                    not path.endswith((".yml", ".yaml")) or path != lexical:
                out.append("%s: imports a playbook the scan does not read (%s)" % (rel, ref))
    return out


def _walk(tasks, base, roles, seen=(), chain=()):
    """Every task in `tasks`, inside a block's `block`, `rescue` and `always` too, and inside every task file a task
    imports or includes. A task file the play includes directly is resolved from `base`, the playbook's directory, as
    ansible-core resolves it. An include inside an included file is read from `base` joined with the directory of each
    include above it as written (`chain`), and from `base` alone, but is also counted as a file the scan cannot read:
    ansible-core tries the working directory, and `<base>/tasks/` when `base` looks like a role, among its candidates,
    which the scan cannot list from the tree (fold 3's read B, S1). Every candidate that exists is read (lexically and
    through symlinks), once (a cycle ends there). In place of an include it does not read it yields a marker that
    `_roles` counts as a role run: `{"_role_file": ref}` for a role's own task file (under `roles`), and `{"_unread":
    ref}` for a path that names no file (a templated path, an `action:` form, a mapping that names no path, a missing
    file), a path that is not plain (ansible-core unquotes, strips or expands it, so a file named as written is a decoy:
    fold 4's read B, N3, and fold 5's read B, N3), or an include inside an included file."""
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        yield task
        for key in ("block", "rescue", "always"):
            yield from _walk(task.get(key), base, roles, seen, chain)
        ref = _ref(task, _TASKS_KEYS)
        if ref is None:
            continue
        ref = str(ref)
        if not _PLAIN.match(ref):   # ansible-core unquotes, strips and expands it
            yield {"_unread": ref}
            continue
        cands = [os.path.join(base, *chain[k:], ref) for k in range(len(chain) - 1, -1, -1)] + [os.path.join(base, ref)]
        paths = list(dict.fromkeys(os.path.realpath(n) for c in cands for n in (os.path.normpath(c), c)
                                   if os.path.isfile(n)))
        if not paths or chain:
            yield {"_unread": ref}
        for path in paths:
            if os.path.commonpath([path, roles]) == roles:
                yield {"_role_file": ref}
            elif path not in seen:
                yield from _walk(_read(path), base, roles, seen + (path,), chain + (os.path.dirname(ref),))


def _roles_dir(base):
    """The roles directory beside the scanned playbooks directory (ansible/roles for ansible/playbooks)."""
    return os.path.realpath(os.path.join(os.path.dirname(os.path.realpath(base)), "roles"))


def _all_tasks(play, base, roles):
    """Every task the play's task lists hold (pre_tasks, tasks, post_tasks, handlers), followed as `_walk` does."""
    for key in ("pre_tasks", "tasks", "post_tasks", "handlers"):
        yield from _walk(play.get(key), base, roles)


def _roles(play, base, roles):
    """The names of every role the play runs: its `roles:`, every include_role/import_role (under any collection, or
    in `action:` form), and for each include `_walk` does not read, `tasks of <path>` (a role's own task file) or
    `tasks the scan cannot read (<path>)`."""
    names = [r if isinstance(r, str) else r.get("role") or r.get("name") for r in play.get("roles") or []]
    for task in _all_tasks(play, base, roles):
        key = _key(task, _ROLE_KEYS)
        if key is not None:   # a role named where `args` can override it is one the scan cannot read
            names.append("{{ args: %s }}" % (task,) if _folds_args(task) else
                         task[key].get("name") if isinstance(task[key], dict) else task[key])
        if "_role_file" in task:
            names.append("tasks of %s" % task["_role_file"])
        if "_unread" in task:
            names.append("tasks the scan cannot read (%s)" % (task["_unread"],))
    return names


def _defines_device_role(play, base, roles):
    """Whether the play sets `device_role` itself, so that `{{ device_role }}` is not the host's own role: in its
    vars or a vars_prompt, a vars file, a role entry's vars, or a task's `vars`, `set_fact` (by any name
    `_set_fact_names` reads, or one it cannot: fold 4's read A, S1) or `include_vars`. A vars file and an
    include_vars count whatever they hold: their files are not read."""
    if "device_role" in (play.get("vars") or {}) or play.get("vars_files"):
        return True
    prompts = play.get("vars_prompt") or []
    if any(isinstance(p, dict) and p.get("name") == "device_role"
           for p in ([prompts] if isinstance(prompts, dict) else prompts)):
        return True
    if any(isinstance(r, dict) and "device_role" in (r.get("vars") or {}) for r in play.get("roles") or []):
        return True
    for task in _all_tasks(play, base, roles):
        key = _key(task, ("set_fact",))
        if "device_role" in (task.get("vars") or {}) or _key(task, ("include_vars",)):
            return True
        names, opaque = _set_fact_names(task, key) if key else ([], False)
        if opaque or any(n in ("device_role", "args") for n in names):
            return True
    return False


def _dispatches_by_group(play, base, roles):
    """Whether the play runs, on a host that is not the control node, a role other than the host's own. A play
    keyword `connection: local` exempts nothing: a host var outranks it. Only a play whose `hosts` is the control
    node alone is exempt (`localhost,` is not: the scan does not parse a pattern)."""
    if str(play.get("hosts", "")).strip() in _LOCAL_HOSTS:
        return False
    own = not _defines_device_role(play, base, roles)
    return any(not (own and " ".join(str(n).split()) == "{{ device_role }}") for n in _roles(play, base, roles))


def _revives(task):
    """Whether `task` runs `meta: clear_host_errors`, as a key or in `action:` form (`_key`), the word in the key's
    value or in the task's `args:`, which ansible-core merges under it (`meta:` null or empty, `action: meta`, a
    mapping's `module: meta`, each with `args: {_raw_params: clear_host_errors}`, revived a host: fold 14's read B,
    N1): it revives a host a refusal or the device_role guard failed, and the revived host runs the play's tasks after
    it (fold 13's read A, N1: a revived host ran a fixed role on a `local` host past the guard, on both cores)."""
    key = _key(task, ("meta",))
    return bool(key) and "clear_host_errors" in str(task[key]) + str(task.get("args", ""))


def _refusal_faults(play, directory, refusal, roles):
    """[why the play does not refuse first]: its first task must be a static import of `refusal` (resolved from the
    play's own directory; one through `$` or `~` is a file `_walk` cannot read) with nothing but a name and `vars:
    {refusal_connections: …}`; the play must not gather facts implicitly (that runs before the refusal) or ignore
    errors; and no task may gather with `setup` (as a key or in `action:` form), which reports the controller's facts
    for a network-OS host where `gather_facts` fails closed. With `roles:`, the roles run before `tasks`, so the refusal
    must open `pre_tasks`. After it, nothing may name `ansible_connection` in a task's `vars`, in a `set_fact` other
    than the freeze (as a key, an `args:` key, a `k=v` word, a name that is not a plain identifier, which ansible-core
    templates under `{{`, `{%` or `{#`, or the `args` an `action:` form folds into the module's arguments:
    `_set_fact_names`), in a role entry, or as a loop or index variable (any `ansible_` name, or one that is not a plain
    identifier), each of which outranks the freeze or may; carry `module_defaults` (on the play, a role entry or a task:
    a later `set_fact` takes its arguments from them); run `meta: clear_host_errors` (it revives a refused host, which
    then skips the freeze); delegate a block, a static import, an include's tasks (`apply:`) or a role entry, or import
    with `local_action:` (their tasks run over the delegate's connection); include a task file the scan cannot read;
    give a task but a `set_fact` an `args:` (ansible-core merges it under the module's own arguments, so an include's
    path, an `apply:`, a `tasks_from` or `meta`'s `clear_host_errors` can sit there, unread: fold 6's read B, S1), or
    write one as an `action:` mapping (whose own `args` and `module:` words ansible-core merges over the mapping's keys:
    fold 7's read B, S1; fold 8's reads A and B, S1); or tag a task or role entry (`--tags` would run it and skip the
    untagged refusal)."""
    faults = []
    if play.get("gather_facts") is not False:
        faults.append("gathers facts before the refusal (set `gather_facts: false`)")
    if "ignore_errors" in play:
        faults.append("sets ignore_errors at the play level")
    first = (play.get("pre_tasks") or []) or ([] if play.get("roles") else play.get("tasks") or [])
    task = first[0] if first and isinstance(first[0], dict) else {}
    key = _key(task, ("import_tasks",))
    ref = task.get(key) if key else None
    if not isinstance(ref, str) or os.path.realpath(os.path.join(directory, ref)) != os.path.realpath(refusal):
        faults.append("does not open with an import of %s" % REFUSAL)
    elif set(task) - {"name", key, "vars"} or set(task.get("vars") or {}) != {"refusal_connections"}:
        faults.append("the refusal's import carries more than a name and `vars: {refusal_connections: …}`")
    for t in _all_tasks(play, directory, roles):
        key = _key(t, ("set_fact",))
        sets = t[key] if key else None
        names, opaque = _set_fact_names(t, key) if key else ([], False)
        freeze = isinstance(sets, dict) and list(sets) == ["ansible_connection"] and t.get("args") is None and \
            " ".join(str(sets["ansible_connection"]).split()) == _FREEZE
        if "ansible_connection" in (t.get("vars") or {}) and t is not task or \
                (key and not freeze and (opaque or any(n in ("ansible_connection", "args") for n in names))):
            faults.append("sets ansible_connection after the refusal (an include's vars and a later set_fact outrank "
                          "its freeze)")
            break
    every = list(_all_tasks(play, directory, roles))
    entries = [r for r in play.get("roles") or [] if isinstance(r, dict)]
    if "module_defaults" in play or any("module_defaults" in t for t in every + entries):
        faults.append("sets module_defaults (a later set_fact takes ansible_connection from them)")
    if any(isinstance(r, dict) and ("ansible_connection" in r or "ansible_connection" in (r.get("vars") or {}))
           for r in play.get("roles") or []):
        faults.append("a role entry sets ansible_connection (a role param outranks the refusal's freeze)")
    if any("_unread" in t for t in _all_tasks(play, directory, roles)):
        faults.append("includes a task file the scan cannot read (it could set ansible_connection or a tag)")
    if any(any(k in lc and (str(lc[k]).strip().startswith("ansible_") or not _IDENT.match(str(lc[k]).strip()))
               for k in ("loop_var", "index_var"))
           for lc in (t.get("loop_control") for t in every) if isinstance(lc, dict)):
        faults.append("names an `ansible_` variable, or one it cannot read, as a loop variable (a loop variable "
                      "outranks the freeze)")
    if any(_revives(t) for t in every):
        faults.append("runs `meta: clear_host_errors` (it revives a refused host, which then skips the freeze)")
    if any(_delegates(t) for t in every) or any("delegate_to" in r for r in entries):
        faults.append("delegates a block, an import (`local_action:` among them), an include's tasks or a role entry "
                      "(its tasks run over the delegate's connection)")
    if any(_folds_args(t) and not _key(t, ("set_fact",)) for t in every):
        faults.append("gives a task `args:`, or writes one as an `action:` mapping (ansible-core merges the "
                      "mapping's `args` and its `module:` words into the module's arguments; the scan reads the "
                      "key's or the form's alone: a path, an `apply: {delegate_to: …}` or a `clear_host_errors` there "
                      "is not read)")
    if any(_templated_module(t) for t in every):
        faults.append("names a module in an `action:` form by a template (ansible-core renders it at run "
                      "time)")
    if any("tags" in t for t in _all_tasks(play, directory, roles)) or \
            any(isinstance(r, dict) and "tags" in r for r in play.get("roles") or []):
        faults.append("tags a task or role (`--tags` would run it and skip the untagged refusal)")
    if any(_key(t, _SETUP_KEYS) for t in _all_tasks(play, directory, roles)):
        faults.append("gathers facts with `setup` (use `ansible.builtin.gather_facts:`)")
    return faults


def _delegates(task):
    """Whether `task` delegates other tasks: `delegate_to` on a block, or on a static import (`import_role`,
    `import_tasks`), whose keywords every imported task takes, or on an include (which ansible-core refuses), or an
    include's `apply: {delegate_to: …}`, or a `local_action:` that imports or includes (it is `delegate_to:
    localhost`, and an `import_role`'s tasks take it; on an include_role ansible-core refuses it). A delegated task of
    its own is not counted: it runs where its author put it. Measured on 2.18 and 2.19: a role reached by each of the
    others ran on the controller for an ssh host (fold 3's read A, S1; fold 4's reads A and B, S1)."""
    key = _key(task, _ROLE_KEYS + _TASKS_KEYS)
    applied = task[key].get("apply") if key and isinstance(task[key], dict) else None
    return key == "local_action" or ("delegate_to" in task and ("block" in task or key is not None)) or \
        (isinstance(applied, dict) and "delegate_to" in applied)


def group_dispatch_without_refusal(base=PLAYBOOKS):
    """(["path: play name: why"] of every group-dispatching play that does not refuse first, and of every
    `import_playbook` the scan cannot follow; ["path: play name"] of every group-dispatching play)."""
    missing, dispatching, roles = _imports_outside(base), [], _roles_dir(base)
    for rel, directory, play in _plays(base):
        if _dispatches_by_group(play, directory, roles):
            label = "%s: %s" % (rel, play.get("name"))
            dispatching.append(label)
            missing += ["%s: %s" % (label, f)
                        for f in _refusal_faults(play, directory, os.path.join(base, REFUSAL), roles)]
    return missing, dispatching


def _tree_faults(base, cfg, roles=None, others=()):
    """["what: why"] for what in the tree would make the scan's reading of it untrue. Under `base`: a symlink, a file or
    a directory (`os.walk` does not enter a linked directory, and ansible-core runs a playbook through it; a linked file
    is read at its target's place: fold 19's read B, S3), a `.yml`/`.yaml` play anywhere below the top, `files/` and
    `templates/` too (its loader basedir is then that directory: fold 19's read B, S1), a file that is not
    `.yml`/`.yaml` outside a top-level `files/` or `templates/` (a playbook ansible-core runs that `_docs` does not
    parse: fold 4's read B, N4; fold 5's read B, N2), one inside them that holds a play (`_holds_a_play`: run by its
    path, it ran on the controller: fold 6's read B, N2), and a plugin directory (`_plugin_dir`: ansible-core loads a
    plugin beside a playbook ahead of the builtin of its name: on 2.18 and 2.19 a `connection_plugins/ssh.py` ran the
    role on the controller for an ssh host the refusal passed, fold 4's read A, N3). Under `roles`: a symlink at its
    top, to a role or to a file, and one anywhere in a role (fold 19's read A, S9; its read B, S4; fold 20's read B,
    N3); a role with a plugin directory's name; and a plugin directory at the top of a role (ansible-core loads every
    one a legacy role has; a file beside the roles is not one). A link is refused and never read, so a dangling or
    looping one is reported, not a traceback (fold 20's read B, N6). Each of `others` that exists (an ansible.cfg in a
    directory ansible-core is run from: the repo root, where Semaphore runs). In `cfg` (ansible/ansible.cfg): a
    `roles_path` other than `roles`, the directory the scan walks (fold 4's read B,
    N5), a plugin path other than its own `filter_plugins`, a filter there that is not a `kontroll_` name
    (`_filter_faults`), an `[ssh_connection]` other than the shipped one, a section or `[defaults]` key the shipped file
    does not have (`_CFG_SECTIONS`, `_CFG_DEFAULTS`: a `[paramiko_connection]`, a `[persistent_connection]`, a
    `[connection]`, a `jinja2_extensions`; fold 5's read A, S1 and N1; fold 6's read A, N1), a held key with another
    value than the shipped one, and a held key or section that is missing (an `[inventory]` other than `_CFG_INVENTORY`,
    none included: ansible-core's default then enables `script` and `auto`; fold 7's read B, N2). The cfg is read as
    ansible-core reads it, its bytes through `read_string`, but for a `;` comment after a value, which ansible-core
    takes and the scan does not (`roles_path = roles ; c` is `roles` there and refused here, which fails closed: fold
    12's read B, N3): a file whose lines end in a bare CR is one comment line there, so it holds none of the shipped
    keys (fold 8's read A, S2), and a byte that is not UTF-8 is read, not a traceback (fold 9's read B, N2); a cfg it
    refuses to read (a duplicate key or section) is reported as such, not a traceback (fold 11's read B, N2). Under
    `base` and under every role, any file `_file_faults` refuses: one that holds an `__ansible` key in any reading, one
    that sets `device_role`, one that names `ansible_facts` as a var, one that runs `meta: clear_host_errors`, one
    that includes a task file or role the scan cannot follow into the tree, and one nested deeper than the scan
    reads. And a `collections` directory at the top of `base`: a collection root ansible-core searches before the
    roles path, for a role's meta dependency even with no `collections` keyword (fold 19's read A, S8)."""
    out = []
    for directory, dirs, files in os.walk(base):
        dirs.sort()    # the walk's order, and so the faults', is the same on every filesystem
        files.sort()
        rel = os.path.relpath(directory, base).replace(os.sep, "/")
        out += ["%s/%s: a symlinked directory" % (rel, d) for d in dirs if os.path.islink(os.path.join(directory, d))]
        out += ["%s/%s: a symlink" % (rel, f) for f in files if os.path.islink(os.path.join(directory, f))]
        files = [f for f in files if not os.path.islink(os.path.join(directory, f))]   # refused, never read
        out += ["%s/%s: a plugin directory" % (rel, d) for d in dirs if _plugin_dir(d)]
        out += ["%s/%s: a play below the top of the playbooks" % (rel, f) for f in files if rel != "."
                and f.endswith((".yml", ".yaml"))
                and _holds_a_play(os.path.join(directory, f))]
        out += ["%s/%s: a file the scan does not parse" % (rel, f) for f in files
                if not f.endswith((".yml", ".yaml")) and rel.split("/")[0] not in ("files", "templates")]
        out += ["%s/%s: a play under the top-level files/ or templates/" % (rel, f) for f in files
                if not f.endswith((".yml", ".yaml")) and rel.split("/")[0] in ("files", "templates")
                and _holds_a_play(os.path.join(directory, f))]
        out += ["%s/%s: %s" % (rel, f, why) for f in files
                for why in _file_faults(os.path.join(directory, f), tree=(base, roles))]
    out += ["roles/%s: a symlink" % r for r in sorted(os.listdir(roles)) if os.path.islink(os.path.join(roles, r))] \
        if roles and os.path.isdir(roles) else []   # a role that is one, or a file there
    for role in sorted(r for r in os.listdir(roles) if os.path.isdir(os.path.join(roles, r))) \
            if roles and os.path.isdir(roles) else []:   # a README.md beside the roles is not one
        out += ["roles/%s: a plugin directory" % role] if _plugin_dir(role) else []
        out += ["roles/%s/%s: a plugin directory" % (role, d) for d in sorted(os.listdir(os.path.join(roles, role)))
                if _plugin_dir(d) and os.path.isdir(os.path.join(roles, role, d))]
        for directory, _dirs, files in os.walk(os.path.join(roles, role)):
            _dirs.sort()
            rel = os.path.relpath(directory, roles).replace(os.sep, "/")
            out += ["roles/%s/%s: a symlink" % (rel, f) for f in sorted(_dirs + files)
                    if os.path.islink(os.path.join(directory, f))]
            out += ["roles/%s/%s: %s" % (rel, f, why) for f in sorted(files)
                    if not os.path.islink(os.path.join(directory, f))
                    for why in _file_faults(os.path.join(directory, f), tree=(base, roles))]
    out += ["collections: a collection root ansible-core searches before the roles path"] \
        if os.path.isdir(os.path.join(base, "collections")) else []   # fold 19's read A, S8
    out += ["%s: an ansible.cfg ansible-core reads from that directory" % c for c in others if os.path.exists(c)]
    parser = configparser.ConfigParser(interpolation=None)
    if os.path.isfile(cfg):   # as ansible-core reads it, bytes through `read_string`: a bare CR ends no line there
        with open(cfg, "rb") as fh:   # a byte that is not UTF-8 is read, as ansible-core reads it (fold 9's read B, N2)
            try:   # strict, as ansible-core reads it: a duplicate key or section ends its run (fold 11's read B, N2)
                parser.read_string(fh.read().decode("utf-8", "surrogateescape"), source=cfg)
            except configparser.Error as e:
                return out + ["ansible.cfg: a file ansible-core refuses to read (%s)" % type(e).__name__]
    defaults = dict(parser["defaults"]) if parser.has_section("defaults") else {}
    if defaults.get("roles_path") != "roles":
        out.append("ansible.cfg: roles_path is %r, not the roles the scan walks" % defaults.get("roles_path"))
    out += ["ansible.cfg: names %s" % k for k in sorted(defaults)
            if _plugin_dir(k) and (k, defaults[k].strip()) != ("filter_plugins", "filter_plugins")]
    if defaults.get("filter_plugins", "").strip() == "filter_plugins":
        out += _filter_faults(os.path.join(os.path.dirname(cfg), "filter_plugins"))
    ssh = dict(parser["ssh_connection"]) if parser.has_section("ssh_connection") else {}
    if ssh != _SSH_CONNECTION:
        out.append("ansible.cfg: [ssh_connection] is %r, not the shipped options" % ssh)
    out += ["ansible.cfg: a [%s] section" % s for s in parser.sections() if s not in _CFG_SECTIONS]
    out += ["ansible.cfg: [defaults] %s, a key the scan does not reason about" % k for k in sorted(defaults)
            if k not in _CFG_DEFAULTS and not _plugin_dir(k)]
    out += ["ansible.cfg: [defaults] %s is %r, not the shipped value" % (k, defaults[k]) for k in sorted(defaults)
            if k in _CFG_DEFAULTS and k != "roles_path" and not _plugin_dir(k)
            and defaults[k].strip() != _CFG_DEFAULTS[k]]   # roles_path and the plugin paths have their own rules
    out += ["ansible.cfg: [defaults] %s is missing (ansible-core's own default stands in)" % k
            for k in sorted(set(_CFG_DEFAULTS) - set(defaults)) if k != "roles_path"]   # roles_path has its own rule
    inventory = dict(parser["inventory"]) if parser.has_section("inventory") else {}
    if inventory != _CFG_INVENTORY:   # absent, ansible-core enables `script` and `auto`
        out.append("ansible.cfg: [inventory] is %r, not the shipped options" % inventory)
    return out


class _AnyTag(yaml.SafeLoader):
    """A safe loader that reads any tag (ansible-core's `!unsafe`, `!vault`) as the plain node it tags: a tagged play
    or list is still a play or a list (2.19 runs a `- !unsafe` play: fold 7's read B, N1)."""


class _AnyTagC(getattr(yaml, "CSafeLoader", yaml.SafeLoader)):
    """`_AnyTag` on libyaml's parser, which ansible-core reads with: it takes a tab PyYAML's own reader refuses (a
    flow play with a tab, `hosts:<tab>g`, a BOM before JSON with a tab: fold 8's read B, N1)."""


def _untagged(loader, suffix, node):
    """A tagged node read as the plain node it tags, and a `!vault` or `!vault-encrypted` scalar as its text after
    `__ansible_vault `: ansible-core decrypts a vaulted mapping key at run time (to `device_role`, say) where the scan
    reads only its ciphertext, so such a key is refused as an `__ansible` key (fold 11's read A, N1)."""
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    value = loader.construct_scalar(node)
    return "__ansible_vault " + value if suffix in ("!vault", "!vault-encrypted") else value


_AnyTag.add_multi_constructor("", _untagged)
_AnyTagC.add_multi_constructor("", _untagged)


def _holds_a_play(path):
    """Whether the file reads (JSON first, an `__ansible_unsafe` mapping read as its value, as 2.18 does; then YAML by
    libyaml's reader, which ansible-core uses, and by PyYAML's own; a tag read as the node it tags) to a list holding a
    play (`hosts`) or a playbook import, as ansible-core would run it if named directly (fold 6's read B, N2; fold 8's
    read B, N1: a flow play with a tab, a BOM before JSON with a tab and a wrapped play each ran past the sniff). A file
    no reading parses is not one; a file that is not UTF-8 is counted as one (ansible-core 2.18 decodes it with
    `surrogateescape`, and ran a JSON play holding such a byte with the sniff blind to it: fold 9's read B, N1)."""
    try:
        with open(path, encoding="utf-8") as fh:
            fh.read()
    except UnicodeDecodeError:   # 2.18 decodes it with surrogateescape, and ran a JSON play so (fold 9's read B, N1)
        return True
    try:
        return any(isinstance(doc, list) and any(isinstance(d, dict) and ("hosts" in d or _key(d, _PLAYBOOK_KEYS))
                                                 for d in doc) for doc in _readings(path))
    except RecursionError:   # nested deeper than the scan reads: counted as one (fold 11's read B, N2)
        return True


def _filter_faults(directory):
    """["what: why"] for each filter the cfg's own `filter_plugins` directory defines that is not a `kontroll_` name in
    a literal `filters()` dict: ansible-core looks a filter up there ahead of the Jinja builtin of its name, so a
    `first` or a `default` there rewrites the freeze (fold 5's read A, S1). A `.py` there with no `filters` function (a
    lambda, a `FilterModule` it imports) is one whose names the scan cannot read. It reads names, not code: what a
    shipped filter file runs is held by its pin (`_FILTER_FILES`) and reviewed as code."""
    out = []
    for here, _dirs, files in os.walk(directory):
        _dirs.sort()
        for f in sorted(x for x in files if x.endswith(".py")):
            rel = os.path.relpath(os.path.join(here, f), os.path.dirname(directory)).replace(os.sep, "/")
            with open(os.path.join(here, f), encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
            defs = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "filters"]
            if not defs:
                out.append("%s: a filter file whose filters the scan cannot read" % rel)
            rets = [r.value for n in defs for r in ast.walk(n) if isinstance(r, ast.Return)]
            for r in rets:
                keys = r.keys if isinstance(r, ast.Dict) else [None]
                out += ["%s: a filter not named kontroll_*" % rel for k in keys
                        if not (isinstance(k, ast.Constant) and str(k.value).startswith("kontroll_"))]
    return out


def _allowlists(base=PLAYBOOKS):
    """{"path: play name": the `refusal_connections` its refusal import passes} for every group-dispatching play."""
    out = {}
    for rel, directory, play in _plays(base):
        if _dispatches_by_group(play, directory, _roles_dir(base)):
            first = (play.get("pre_tasks") or []) or ([] if play.get("roles") else play.get("tasks") or [])
            out["%s: %s" % (rel, play.get("name"))] = (first[0].get("vars") or {}).get("refusal_connections")
    return out


def _methods(directory=LOGGING):
    """[(file name, method)] of every `.yml`/`.yaml` file at the top of `directory`, read as YAML alone by libyaml's
    reader, as wire-logging.yml reads each one (`lookup('file') | from_yaml` over `logging/*.yml` and `*.yaml`), a tag
    read as the node it tags (more permissively than `from_yaml`, which refuses a tag). `_read` would read a JSON
    method 2.18's way, as an `__ansible_unsafe` wrapper's value, where `from_yaml` keeps the wrapper's siblings: a
    method so wrapped gave its ssh-only wiring role network_cli with the allowlist test green (fold 9's read A, S1). A
    file the reader cannot parse, a byte that is not UTF-8 among them, is read as None, which `_logging_faults`
    reports, where it ended the test in a traceback (fold 11's read B, N2)."""
    out = []
    for name in sorted(os.listdir(directory)):
        if name.endswith((".yml", ".yaml")) and os.path.isfile(os.path.join(directory, name)):
            with open(os.path.join(directory, name), "rb") as fh:
                text = fh.read().decode("utf-8", "surrogateescape")
            try:
                method = yaml.load(text, Loader=_AnyTagC)   # noqa: S506 (a SafeLoader subclass)
            except (yaml.YAMLError, ValueError, RecursionError):   # libyaml cannot encode a surrogate: a ValueError
                method = None
            out.append((name, method))
    return out


def _logging_faults(directory=LOGGING):
    """["file: why"] for every file under `directory` that holds an `__ansible` key in any reading, or is nested
    deeper than the scan reads (`_file_faults`): wire-logging.yml (`from_yaml`) and the catalog (`yaml.safe_load`) read
    a method as YAML, where 2.18's JSON reader, which the scan's `_read` follows, reads a wrapper as its value, so no
    method may hold a key those read apart (fold 9's read A, S1). And every method (`_methods`) that does not read to a
    mapping: one the reader cannot parse, or that is not UTF-8, ended the test in a traceback (fold 11's read B, N2)."""
    out = ["%s: %s" % (os.path.relpath(os.path.join(d, f), directory).replace(os.sep, "/"), why)
           for d, _dirs, files in sorted(os.walk(directory)) for f in sorted(files)
           for why in _file_faults(os.path.join(d, f), device_role=False)]
    return out + ["%s: a method that does not read to a mapping" % name for name, method in _methods(directory)
                  if not isinstance(method, dict)]


def _network_unsafe(role, tasks_from="main", roles=ROLES, seen=(), playbooks=PLAYBOOKS):
    """["role/file: task"] for every task that `<roles>/<role>/tasks/<tasks_from>` runs, through a block's `block`,
    `rescue` and `always` and every include_role/import_role (with its `tasks_from`), whose module is not
    `_network_safe` and that is not delegated to the control node (a delegated task runs there over any connection, so a
    network plugin adds nothing to it). ansible-core reads the first of `<name>`, `<name>.yml`, `<name>.yaml` and
    `<name>.json` that exists (the bare name first for a `tasks_from`, last for the default `main`), so every one that
    exists is read. Reported, not read: a templated or missing role or task file (a role named in `action:` words names
    none), a task-file include, a role's `meta/` dependencies and `collections:` (in `main.yml`, `main.yaml`,
    `main.json` or `main`), a task that sets `collections:` (a role or module named bare is looked up there before the
    roles path: fold 4's read B, S2), an include_role that carries `args:` or is written as an `action:` mapping (a
    `tasks_from` in the `args`, or in the mapping's `module:` words: fold 6's read B, S1; fold 8's reads A and B, S1),
    its `handlers/`, and a role that `<playbooks>/roles/<role>` shadows (ansible-core searches the playbook's own
    `roles/` before the roles path: fold 3's read B, S3)."""
    label = "%s/%s" % (role, tasks_from)
    paths = [p for p in (os.path.join(roles, str(role), "tasks", str(tasks_from) + ext)
                         for ext in ("", ".yml", ".yaml", ".json")) if os.path.isfile(p)]
    if "{{" in label or not paths:
        return ["%s: not read" % label]
    if os.path.isdir(os.path.join(playbooks, "roles", str(role))):
        return ["%s: shadowed by <playbooks>/roles/%s, which ansible-core finds first" % (label, role)]
    if label in seen:
        return []
    out, seen = [], seen + (label,)
    for meta in (os.path.join(roles, str(role), "meta", m) for m in ("main.yml", "main.yaml", "main.json", "main")):
        if os.path.isfile(meta):
            doc = _read(meta) or {}
            if doc.get("dependencies"):
                out.append("%s: its meta/ dependencies are not read" % role)
            if doc.get("collections"):
                out.append("%s: its meta/ `collections:` is searched before the roles path" % role)
    if os.path.isdir(os.path.join(roles, str(role), "handlers")):
        out.append("%s: its handlers/ are not read" % role)

    def walk(tasks):
        for task in tasks or []:
            if not isinstance(task, dict):
                continue
            if "collections" in task:
                out.append("%s: a task sets `collections:`" % label)
            if "block" in task:
                for key in ("block", "rescue", "always"):
                    walk(task.get(key))
                continue
            key = _key(task, _ROLE_KEYS)
            if key is not None and _folds_args(task):
                out.append("%s: an include whose `args:` is not read" % label)
            elif key is not None:
                args = task[key] if isinstance(task[key], dict) else {"name": task[key]}
                out.extend(_network_unsafe(args.get("name"), args.get("tasks_from", "main"), roles, seen, playbooks))
            elif not _network_safe(task) and task.get("delegate_to") not in _LOCAL_HOSTS:
                out.append("%s: %s" % (label, task.get("name") or sorted(set(task) - _TASK_KEYWORDS)))

    for path in paths:
        walk(_read(path))
    return out


def _ssh_only(task):
    """Whether one of `task`'s own `when` conditions (the string, or an item of the list, which ansible-core ANDs) is
    exactly `ansible_connection in [...]` with a literal, non-empty list of names all within `_SSH_FAMILY`. Read after
    the refusal's freeze, which `test_every_group_dispatched_play_refuses_first` holds, such a task runs over the ssh
    family only, never over a network plugin. A list named by a variable is not read (a block's, a task's or a fact's
    value outranks a play's), nor a gate on an enclosing block, nor a condition in any other form."""
    when = task.get("when")
    for item in when if isinstance(when, list) else [when]:
        match = _SSH_GATE.fullmatch(item) if isinstance(item, str) else None
        if match is None:
            continue
        try:
            names = ast.literal_eval(match.group(1))
        except (ValueError, SyntaxError):
            continue
        if names and all(isinstance(n, str) for n in names) and set(names) <= set(_SSH_FAMILY):
            return True
    return False


def _play_network_unsafe(play, directory, roles=ROLES):
    """[task] for every task a dispatching play runs on its hosts besides the role it dispatches (`{{ role }}`, which
    each method's check covers), through its includes and handlers, that a network plugin would run on the controller,
    judged as `_network_unsafe` judges a role's task, save one whose own `when` holds it to the ssh family (`_ssh_only`:
    wire-logging.yml's probe and re-check, I-92); a fixed role it includes is walked by `_network_unsafe`, with the
    play's own directory as `<playbooks>` (its `roles/` shadows: fold 4's read B, N5). A play, a block or a task that
    sets `collections:` is reported: a role named without a collection is looked up there before the roles path, so the
    role walked would not be the role run (fold 3's read B, S3; fold 4's read B, S2). So is an include_role that carries
    `args:` or is written as an `action:` mapping (a `tasks_from` in the `args`, or in the mapping's `module:` words, is
    not read: fold 6's read B, S1; fold 8's reads A and B, S1)."""
    out = ["sets `collections:`"] if "collections" in play or \
        any("collections" in t for t in _all_tasks(play, directory, roles)) else []
    for t in _all_tasks(play, directory, roles):
        if "_unread" in t or "_role_file" in t:
            out.append("not read: %s" % (t,))
            continue
        key = _key(t, _ROLE_KEYS)
        if key is not None and _folds_args(t):
            out.append("not read: %s" % (t,))
        elif key is not None:
            args = t[key] if isinstance(t[key], dict) else {"name": t[key]}
            if args.get("name") != "{{ role }}":
                out.extend(_network_unsafe(args.get("name"), args.get("tasks_from", "main"), roles,
                                           playbooks=directory))
        elif "block" in t or _key(t, _TASKS_KEYS):
            continue
        elif not _network_safe(t) and t.get("delegate_to") not in _LOCAL_HOSTS and not _ssh_only(t):
            out.append(t.get("name") or sorted(set(t) - _TASK_KEYWORDS))
    for r in play.get("roles") or []:
        out.extend(_network_unsafe(r if isinstance(r, str) else r.get("role") or r.get("name"), roles=roles,
                                   playbooks=directory))
    return out


def test_every_group_dispatched_play_refuses_first():
    """Every play that runs a role other than the host's own `{{ device_role }}` on hosts other than the control node
    opens with a static import of tasks/refuse-local-connection.yml that carries only its allowlist, gathers no facts
    before it, ignores no errors and never gathers with `setup`; after it, nothing names `ansible_connection` in a form
    that outranks the freeze (an include's vars, a role entry's parameter, a later `set_fact`, a loop variable,
    `module_defaults`) or in one the scan cannot read, revives a refused host, delegates the role's tasks (a
    `local_action:` import among the ways), includes a task file the scan cannot read or gives a task but a `set_fact`
    an `args:` or writes one as an `action:` mapping, and no task or role entry is tagged (`--tags` would run it and
    skip the untagged refusal); and no `import_playbook` names a file the scan does not read (a file other than
    `.yml`/`.yaml`, which ansible-core runs, counts as one). The plays the scan finds are exactly wire-logging.yml's and
    install-node-exporter.yml's, so it is not passing over nothing, and the docs' "two plays" stays true (fold 1's read
    B, N8). Guards a new group-dispatched play, an explicit task or implicit fact gathering placed above the refusal, a
    refusal switched off by a keyword on its import or its play (its read, S1), and a connection passed into the role or
    a tag that skips the refusal (fold 2's read B, S3, S4), running a role's tasks on the control node, as an `action:`
    mapping's own `args` (fold 7's read B, S1) and its `module:` words (fold 8's reads A and B, S1) did."""
    missing, dispatching = group_dispatch_without_refusal()
    assert missing == [], missing
    assert {d.split(":")[0] for d in dispatching} == {"wire-logging.yml", "install-node-exporter.yml"}, dispatching


def test_every_play_that_dispatches_by_device_role_refuses_a_device_role_fact_first():
    """Every play under ansible/playbooks/ that includes `{{ device_role }}` runs no `pre_tasks` and no `roles:`,
    and its first task is a static import of tasks/refuse-device-role-fact.yml with nothing but a name; that file is
    one `assert` that `device_role` is not in `ansible_facts`; and the play sets no `ignore_errors`, tags no task
    (`--tags` would skip the untagged guard) and runs no `meta: clear_host_errors` (`_revives`, the word in a task's
    `args:` too: fold 14's read B, N1), each of which lets a refused host run the play's
    role. The plays found are exactly ping.yml's and backup-configs.yml's, so the test is
    not passing over nothing. Guards a module that returns facts setting the role a host dispatches by, which outranks
    the inventory's: an earlier play's ansible.utils.update_fact did so, plainly, in `action:` form, by
    `module_defaults`, with a templated or a `!vault` path, and backup-configs.yml ran a fixed role on a `local` host on
    2.18 and 2.19 with the scan green (fold 12's read A, S3); with the guard in place, a play's `ignore_errors`, a
    tagged include run under `--tags` and a `clear_host_errors` after the guard each ran it again, with this test green
    (fold 13's read B, N1, and read A, N1). The scan names each setter it knows; the guard refuses the class
    (tests/integration/test_device_role_fact_guard_runtime.py runs it)."""
    with open(os.path.join(PLAYBOOKS, "tasks", "refuse-device-role-fact.yml"), encoding="utf-8") as fh:
        guard = yaml.safe_load(fh)
    assert len(guard) == 1 and set(guard[0]) == {"name", "ansible.builtin.assert"}, guard
    assert guard[0]["ansible.builtin.assert"]["that"] == ["('device_role' not in ansible_facts)"], guard
    found = []
    for rel, directory, play in _plays(PLAYBOOKS):
        if not any(" ".join(str(n).split()) == "{{ device_role }}" for n in _roles(play, directory, ROLES)):
            continue
        found.append(rel)
        assert not play.get("pre_tasks") and not play.get("roles"), rel
        every = list(_all_tasks(play, directory, ROLES))
        assert "ignore_errors" not in play and not any("tags" in t for t in every), rel
        assert not any(_revives(t) for t in every), rel
        first = (play.get("tasks") or [{}])[0]
        assert set(first) == {"name", "ansible.builtin.import_tasks"}, (rel, first)
        assert first["ansible.builtin.import_tasks"] == "tasks/refuse-device-role-fact.yml", (rel, first)
    assert sorted(found) == ["backup-configs.yml", "ping.yml"], found


@pytest.mark.parametrize("word,sets", [
    ("device_role=x", True), ("device_\\x72ole=x", True), ("device_rol\\u0065=x", True),
    ("device_rol\\U00000065=x", True), ("device_role\\N{SPACE}=x", True), ("device_role\\t=x", True),
    ("device_role\\x3dx", True), ("a=\\N{X device_\\x72ole=x b=}", True),
    ("a=\\N{X\ndevice_\\x72ole=x\nb=}", True), ("device_rol\\N{LATIN SMALL LETTER E}=x", False),
    ("device_role\\N{NOSUCHNAME}=x", False), ("my_device_\\x72ole=x", False), ("device_role == 'x'", False),
], ids=["plain", "hex", "unicode", "long-unicode", "named", "written-tab", "escaped-equals", "a-span-split-by-a-space",
        "a-span-split-by-a-line-break", "a-name-split-by-a-space", "undecodable", "another-name", "a-condition"])
def test_the_kv_reading_is_ansible_cores_own(word, sets):
    """The scan reads a `device_role=` word (`_KV_ROLE` over `_kv_texts`) as ansible-core's own `parse_kv` reads it:
    both set `device_role` from the same words, each escape decoded in its own word, and neither from a word that does
    not decode (ansible-core ends the run on it). Why: the scan decoded the whole text, where `parse_kv` decodes each
    word alone, so a `\\N{` read across the words to a later one's `}` hid the escapes inside it, and `a=\\N{X
    device_\\x72ole=x b=}` set `device_role` on both cores while the scan's reading said not (fold 15's own probe).
    The tree test cannot hold this: its rule refuses a `set_fact` string for any `{`, so this test reads each word
    alone. The readings differ on a class of words, not one: a word's key is what comes before its first `=`, so
    `device_role==x` sets `device_role` to `=x`, the name of a role whose directory name begins with `=`, and `_KV_ROLE`
    keeps the lookahead a condition's `==` needs; the tree rule reads such a word in a set_fact's own string
    (`_set_fact_eq`, which the next test holds; fold 15's reads A and B, S3 and S2). The scan also refuses some words
    ansible-core reads no `device_role` from (`x=device_role=y`), which fails closed."""
    splitter = pytest.importorskip("ansible.parsing.splitter")
    try:
        parsed = "device_role" in splitter.parse_kv(word)
    except UnicodeDecodeError:   # ansible-core ends the run on an escape that does not decode
        parsed = False
    assert (parsed, any(_KV_ROLE.search(t) for t in _kv_texts(word))) == (sets, sets)


@pytest.mark.parametrize("name", ["device_role", "ansible_facts"])
@pytest.mark.parametrize("word", ["%s==x", "%s\t==x", "%s\\t==x", "%s=\\x3dx", "%s\\N{SPACE}==x"],
                         ids=["equals-equals", "a-tab", "a-written-tab", "an-escaped-equals", "an-escaped-space"])
def test_a_set_fact_word_parse_kv_reads_a_held_name_from_is_refused(name, word):
    """Five words of the class from which ansible-core's `parse_kv` reads `device_role` or `ansible_facts` as a
    set_fact's key, its value beginning with `=` (`name==x`; a tab before the `==`, real or written; an escaped `=`; an
    escaped space; the class itself is `_set_fact_eq`'s), are each refused by the tree rule as a set_fact's string, the
    module as a key, in an `action:` or `local_action:` string and in a `local_action:` mapping's `module`. Why:
    `_KV_ROLE` and `_KV_FACTS` keep the lookahead a condition's `==` needs, so they read none of these, and
    `device_role==x` set `=x`, the name of a role whose directory name begins with `=`, while `ansible_facts==x` hid a
    cached `device_role` fact from the guard whatever its value, each running a fixed role on a `local` host with the
    scan green (fold 15's reads A and B, S3 and S2); the tree test held the plain word alone, so `_set_fact_eq`'s
    reading of the rest (`_KV_EQ`, through `_kv_texts`) was held by no row (fold 15's read B, S2)."""
    splitter = pytest.importorskip("ansible.parsing.splitter")
    text = word % name
    assert str(splitter.parse_kv(text).get(name, "")).startswith("="), text
    for doc in ({"ansible.builtin.set_fact": text}, {"action": "ansible.builtin.set_fact " + text},
                {"local_action": "set_fact " + text}, {"local_action": {"module": "set_fact " + text}}):
        assert _sets_device_role(doc) or _names_ansible_facts(doc), doc


def test_the_tree_holds_nothing_the_scan_reads_past(tmp_path):
    """The shipped ansible/playbooks/ holds no symlinked directory, no file but a `.yml`/`.yaml` one outside its
    top-level `files/` and `templates/`, and no plugin directory ansible-core loads beside a playbook; no role under
    ansible/roles/ has one at its top; there is no ansible.cfg at the repo root; and ansible/ansible.cfg keeps
    `roles_path = roles`, names no plugin path but its own `filter_plugins` (whose every file has a `filters` function
    returning a literal dict of `kontroll_` names), keeps the shipped `[ssh_connection]` (its keys, and their values)
    and has no `[paramiko_connection]`, nor any other section or `[defaults]` key the shipped file does not have, keeps
    the shipped value of each key it has, and has every held key and the shipped `[inventory]`, read as ansible-core
    reads the file (its bytes: a file whose lines end in a bare CR holds none, and a byte that is not UTF-8 is read);
    and no file under ansible/playbooks/ or a role holds an `__ansible` key in any reading, sets `device_role`, names
    `ansible_facts` as a var or runs `meta: clear_host_errors`. On a planted tree `_tree_faults` names each of them (a
    play in the top-level `files/` or `templates/` too: JSON with a tab, a tagged play, a flow play or a key with a tab
    libyaml reads, a tagged one with a tab, one with an escape libyaml refuses, a BOM before JSON with a tab, a play in
    an `__ansible_unsafe` wrapper, a file that is not UTF-8; an `__ansible` key as written, as JSON escapes it, in YAML,
    past YAML's key limit (JSON alone reads it) and beside an escape libyaml refuses (PyYAML alone reads it), and a key
    tagged `!vault` or `!vault-encrypted`; each form `_sets_device_role` reads, in a playbook, in group_vars and in a
    role's own task, a `k=v` word with an escape or a key `parse_kv` strips among them; each way `_names_ansible_facts`
    reads to name `ansible_facts` as a var; a `clear_host_errors` as a key's value, in `args:` under a null, an empty or
    an `action:` meta, in a handler and in a role's own handler; a file nested past PyYAML's reader, under `files/` too
    (the sniff counts it a play); a cfg with a duplicate key), passes a file beside the roles, a read of `device_role`
    or of `ansible_facts`, a scoped include_vars, a plain `action:` set_fact and another name's `k=v`, escaped or not,
    and passes a clean tree. Guards the scan's own premises: a play in a symlinked directory or a `.json` playbook, run
    directly, ran a role on the controller while every scan test passed (fold 4's read B, N4), as a `.YML` or `.txt` one
    did (fold 5's read B, N2); a `connection_plugins/ssh.py` beside the playbooks ran the role on the controller for a
    host the refusal passed as `ssh` (fold 4's read A, N3), as a role's own `action_plugins/command.py` and a repo-root
    ansible.cfg naming a connection plugin path did (fold 5's read B, N2); a `first` filter beside the playbooks or in
    ansible/filter_plugins/ rewrote the freeze to `local` (as a `default` one would), and an `ssh_executable` in
    `[ssh_connection]` ran the role on the controller with the freeze untouched (fold 5's read A, S1 and N1), as the
    program `[persistent_connection] ansible_connection_path` names would for a network_cli host the refusal passed
    (fold 6's read A, N1); a play in the top-level `files/`, run by its path, ran a role on the controller (fold 6's
    read B, N2), the ssh rule had no row for a value (its N3), and a README beside the roles crashed the walk (its N4);
    a `roles_path` other than the directory the scan walks would have it judge roles that do not run (fold 4's read B,
    N5); a removed held key or `[inventory]` let ansible-core's own default stand in (fold 7's read B, N2, and read A,
    N1), as a cfg with bare CRs did for every key with the rule green (fold 8's read A, S2); and a play under `files/`
    that only ansible-core's readers parse ran by its path (fold 7's read B, N1; fold 8's read B, N1), as a JSON one
    holding a byte that is not UTF-8 did on 2.18 (fold 9's read B, N1), with the libyaml reader's tag constructor held
    by no row (its N4), and PyYAML's by none once libyaml's read every tagged row (fold 10); a wrapper's sibling, which
    2.19 keeps and 2.18 drops, ran a play's `device_role` or a `set_fact` of `ansible_connection: local` after the
    refusal on 2.19 with the scan green (fold 9's read B, S1, and read A, S1); a `register`, a loop variable, an
    `add_host` or a `set_fact` that set `device_role` in an earlier play or a role's own code, or a `device_role` in
    group_vars beside the playbooks, ran a fixed role on a `local` host from backup-configs.yml (fold 9's read A, S2 and
    N1), as a `load_vars` or a `cli_parse`, each also in its `action:` form, set it with the rule green (fold 11's read
    A, S2), as an `update_fact` did, plainly, in `action:` form or through a play's `module_defaults` (fold 12's read A,
    S3), and an `action:` mapping whose `module` held its arguments or an include_vars with a null or false `name` (its
    read B, S1 and S2), as an `action:` or `local_action:` naming include_vars by a template or glued to a Jinja comment
    did, and the rule held no row for a `module` split from its words by a line break (fold 12's read B, S2 and N2), as
    a tab before a `{{-`, `{%-` or `{#-` tag did, in a string, a mapping's `module` or a `local_action:` (fold 13's read
    B, S1); a var named `ansible_facts` hid an `update_fact`'s fact from the guard and a fixed role ran on a `local`
    host (fold 13's reads A and B, N2); a `k=v` word whose key an escape (of each kind ansible-core decodes) or a
    stripped tab hid set `device_role`, a `group_by` moved a host into a group naming another, and a `clear_host_errors`
    in a role's own code, in the role another host runs or in a task's `args:` revived a host the guard refused, each
    running a fixed role on a `local` host with the rule green (fold 14's read A, S1, N1 and N2; its read B, N1 and N2);
    a vaulted key decrypts at run time to a name the scan never read (fold 12's read A, N1); and a byte that is not
    UTF-8 in the cfg ended the test in a traceback (fold 9's read B, N2), as a duplicate cfg key and a file nested past
    PyYAML's reader did (fold 11's read B, N2). Fold 15's read A: ansible-core's own `_variable_params` key set any
    name, a `device_role==` or `ansible_facts==` word in a set_fact's own string set a value that begins with `=`, a
    `group_by` in `action:` form moved a host with the rule green, and an include or a role outside the tree (an
    absolute path, a `..` part, a role by its path) set device_role, hid ansible_facts or revived a refused host, each
    on both cores (S1, S3, N1 and S2). So an include of each of these shapes is refused (a templated path, a role entry
    by its path, a role's `tasks_from`, `vars_from`, `defaults_from` or `handlers_from`, an `action:` form, a `k=v`
    string, one carrying `args:` too), the planted tree holds each in a playbook (and an absolute path in a role's own
    task too), and it passes a plain include, a role by its name (a `roles:` entry's mapping too: fold 16's own
    mutant, which read a mapping's role as no name and so refused every one), the dispatch seam, a debug's `==` word
    and another name's. A role's `meta/` dependency named by a path, as a string or as a mapping, is refused as a
    `roles:` entry is, and one named by its name passed, until every one was refused (fold 20's read A, N2: ansible-core
    looks a dependency up in each collection root first): a role named by a path in a role's own code ran a fixed role
    on a `local` host on both cores, and the rule read no dependency (fold 15's read B, S3). Fold 17's read B: a
    dependency by its `src` alone, a `{{ role }}` include outside wire-logging.yml (a set_fact beside it named the
    role) and a plain role or task-file name the tree does not hold (ansible-core then looks for it at the working
    directory) each revived a refused host on both cores (S1 to S3), and five arms of the rule had no row (N1); so the
    planted tree holds each, and a role by a collection's name. Fold 18's read B: a role named `.` (the roles path
    itself, whose plugin directories ansible-core then loads), a name written with a trailing space or a tab
    (ansible-core reads it as written), a name the tree holds only where ansible-core looks after the working
    directory (`base` from a role's file or a subdirectory's, a role file's own subdirectory, the playbooks directory
    from a play below their top) and a copy of node_exporter's pinned include under `<playbooks>/roles` each passed the
    rule (S1 to S4), and F18-src-unread's equivalence rested on no role being named `None` (N3); so the planted tree
    holds each, a plugin directory at the top of the roles path, a play below the top of the playbooks and a role named
    `None`, and passes the `{{ role }}` seam in wire-logging.yml, a plain name where ansible-core looks before the
    working directory and a role handler's include of a file in its own `handlers/`. Fold 19's reads: a role
    ansible-core finds first in `<playbooks>/roles` or in the playbooks directory (it reads the role's files as a
    role's, the scan as playbook-level ones), a `collections` keyword on a play or a block, a `collections` directory at
    the top of the playbooks (ansible-core looks a meta dependency up in `ansible.legacy` first), a play under `files/`
    or `templates/` (its loader basedir is then that directory), an include in a file that is not an entry or of a task
    file that itself includes one, and a symlink each let ansible-core run a file the scan read elsewhere or not at all
    (read A, S3, S6 and S8; read B, S1 to S4); so the planted tree holds each, a role the roles path holds and
    `<playbooks>/roles` shadows among them, and the pin's include, with its `when` and a file for each family, at the
    top of the playbooks (the pin is a place, not a name: fold 19's read A, S5)."""
    assert _tree_faults(PLAYBOOKS, os.path.join(ROOT, "ansible", "ansible.cfg"), ROLES,
                        [os.path.join(ROOT, "ansible.cfg")]) == []
    base = tmp_path / "playbooks"
    (base / "sub" / "real").mkdir(parents=True)
    os.symlink("real", str(base / "sub" / "link"))
    plugins = ("connection_plugins", "action_plugins", "library", "module_utils", "doc_fragments", "filter_plugins",
               "test_plugins", "lookup_plugins")   # as written, not the tuple
    for d in plugins:
        (base / d).mkdir()
    for name in ("plays.json", "plays", "ok.yml", "plays.txt", "plays.YML"):
        (base / name).write_text("[]\n")
    os.symlink("ok.yml", str(base / "file-link.yml"))   # a file read at its target's place (fold 19's read B, S3)
    (base / "files").mkdir()
    (base / "files" / "hook.sh").write_text("[]\n")
    (base / "files" / "play.txt").write_text(   # a play there, run by its path, with a tag only ansible-core reads
        "- hosts: g\n  vars: {x: !unsafe '{{ y }}'}\n  tasks: []\n")
    (base / "files" / "json.txt").write_text(   # JSON with a tab: ansible-core reads it, PyYAML does not
        '[{"hosts": "g",\t"tasks": []}]\n')
    (base / "files" / "unsafe.txt").write_text("- !unsafe\n  hosts: g\n  tasks: []\n")   # 2.19 runs it
    sniffed = {   # each ran under ansible-core and passed fold 8's sniff (fold 8's read B, N1)
        "flow-tab.txt": "[{hosts: g,\ttasks: []}]\n",                      # libyaml reads it, PyYAML does not
        "key-tab.txt": "- hosts:\tg\n  tasks: []\n",                       # the same
        "bom-tab.txt": chr(0xFEFF) + '[{"hosts": "g",\t"tasks": []}]\n',   # a BOM before JSON with a tab
        "wrapped.txt": '[{"__ansible_unsafe": {"hosts": "g", "tasks": []}}]\n',   # 2.18 reads it as the play
        "tag-tab.txt": "- !unsafe\n  hosts:\tg\n  tasks: []\n"}   # libyaml alone, with a tag (fold 9's read B, N4)
    for name, text in sniffed.items():
        (base / "files" / name).write_text(text, encoding="utf-8")
    (base / "files" / "latin.txt").write_bytes(   # not UTF-8: 2.18 decodes it and runs it (fold 9's read B, N1)
        b'[{"hosts": "g", "tasks": [], "x": "' + bytes([0xFF]) + b'"}]\n')
    (base / "files" / "tag-escape.txt").write_text(   # PyYAML alone, with a tag: libyaml refuses the escape (fold 10)
        '- !unsafe\n  hosts: g\n  vars: {x: "%sud800"}\n  tasks: []\n' % chr(92))
    keyed = {   # an `__ansible` key, read three ways by 2.18, 2.19 and `from_yaml` (fold 9's reads A and B, S1)
        "keyed.yml": '[{"__ansible_unsafe": {}, "ansible.builtin.set_fact": {"ansible_connection": "local"}}]\n',
        "escaped.yml": '[{"%su005f_ansible_unsafe": "x"}]\n' % chr(92),   # the key as JSON escapes it
        "yaml-keyed.yml": "- __ansible_vault: x\n",
        "long-keyed.yml": '[{"__ansible_unsafe": {}, "%s": 1}]\n' % ("k" * 1100),   # JSON alone: past YAML's key limit
        "surrogate-keyed.yml": '- __ansible_vault: "%sud800"\n' % chr(92),   # PyYAML alone: libyaml refuses the escape
        "vault-keyed.yml": '- {!vault "$ANSIBLE_VAULT;1.1;AES256 6162": x}\n',   # decrypted at run time (read A, N1)
        "vault-encrypted-keyed.yml": '- {!vault-encrypted "$ANSIBLE_VAULT;1.1;AES256 6162": x}\n'}
    for name, text in keyed.items():
        (base / name).write_text(text)
    setters = {   # each sets device_role, which only the instance's inventory may (fold 9's read A, S2 and N1)
        "group_vars/all.yml": {"device_role": "x"},
        "register.yml": [{"ansible.builtin.command": "true", "register": "device_role"}],
        "loop-var.yml": [{"ansible.builtin.debug": {}, "loop": [1], "loop_control": {"loop_var": "device_role"}}],
        "templated-index-var.yml": [{"ansible.builtin.debug": {}, "loop": [1],
                                     "loop_control": {"index_var": "{{ v }}"}}],
        "add-host.yml": [{"ansible.builtin.add_host": {"name": "h"}}],
        "include-vars.yml": [{"ansible.builtin.include_vars": "v.yml"}],
        "include-vars-named-it.yml": [{"include_vars": {"file": "v.yml", "name": "device_role"}}],
        "templated-key.yml": [{"ansible.builtin.set_fact": {"{{ k }}": "x"}}],
        "templated-kv.yml": [{"set_fact": "{{ k }}=x"}],
        "kv.yml": [{"ansible.builtin.set_fact": "device_role=x"}],
        "kv-escape.yml": [{"ansible.builtin.set_fact": "device_%sx72ole=x" % chr(92)}],   # fold 14's read A, S1
        "kv-unicode-escape.yml": [{"set_fact": "device_rol%su0065=x" % chr(92)}],
        "kv-key-tab.yml": [{"set_fact": "device_role\t=x"}],
        "kv-key-ideographic.yml": [{"set_fact": "device_role\u3000=x"}],
        "kv-escaped-equals.yml": [{"set_fact": "device_role%sx3dx" % chr(92)}],
        "action-kv-escape.yml": [{"action": "set_fact device_%sx72ole=x" % chr(92)}],
        "kv-long-escape.yml": [{"set_fact": "device_rol%sU00000065=x" % chr(92)}],   # fold 15's own rows
        "kv-written-tab.yml": [{"set_fact": "device_role%st=x" % chr(92)}],
        "action.yml": [{"action": "include_vars v.yml"}],
        "action-set-fact.yml": [{"local_action": {"module": "set_fact", "{{ k }}": "x"}}],
        "templated-args.yml": [{"ansible.builtin.set_fact": {}, "args": "{{ a }}"}],
        "prompt.yml": [{"hosts": "g", "vars_prompt": [{"name": "device_role", "prompt": "?"}]}],
        "load-vars.yml": [{"community.sops.load_vars": {"file": "v.sops.yml"}}],   # fold 11's read A, S2
        "action-load-vars.yml": [{"action": "community.sops.load_vars file=v.sops.yml"}],
        "cli-parse.yml": [{"ansible.utils.cli_parse": {"command": "show version", "set_fact": "device_role",
                                                       "parser": {"name": "ansible.netcommon.native"}}}],
        "action-cli-parse.yml": [{"action": {"module": "ansible.utils.cli_parse", "command": "show version",
                                             "set_fact": "device_role"}}],
        "group-by.yml": [{"ansible.builtin.group_by": {"key": "g"}}],   # fold 14's read A, N2
        "update-fact.yml": [{"ansible.utils.update_fact": {"updates": [{"path": "device_role", "value": "x"}]}}],
        "action-update-fact.yml": [{"action": {"module": "ansible.utils.update_fact",   # fold 12's read A, S3
                                               "updates": [{"path": "device_role", "value": "x"}]}}],
        "module-defaults-update-fact.yml": [{"hosts": "g", "module_defaults": {"ansible.utils.update_fact": {
            "updates": [{"path": "device_role", "value": "x"}]}}, "tasks": [{"ansible.utils.update_fact": {}}]}],
        "action-module-words.yml": [{"action": {"module": "include_vars file=v.yml"}}],   # its read B, S1
        "local-action-module-words.yml": [{"local_action": {"module": "ansible.builtin.set_fact {{ k }}=1"}}],
        "include-vars-null-name.yml": [{"include_vars": {"file": "v.yml", "name": None}}],   # its read B, S2
        "include-vars-false-name.yml": [{"include_vars": {"file": "v.yml", "name": False}}],
        "action-template.yml": [{"action": "{{ 'include_vars' }} file=v.yml"}],   # fold 12's read B, S2
        "local-action-template.yml": [{"local_action": {"module": "{{ m }}", "file": "v.yml"}}],
        "action-comment.yml": [{"action": {"module": "include_vars{# c #} file=v.yml"}}],
        "action-module-newline.yml": [{"action": {"module": "include_vars\nfile=v.yml"}}],
        "action-tab-template.yml": [{"action": "include\t{{- '_vars' }} file=v.yml"}],   # fold 13's read B, S1
        "action-tab-template-module.yml": [{"action": {"module": "include\t{%- if true %}_vars{% endif %}",
                                                       "file": "v.yml"}}],
        "local-action-tab-comment.yml": [{"local_action": "include\t{#- c #}_vars file=v.yml"}]}
    setters.update({   # a `group_by` in each `action:` form, a set_fact's own `device_role==` word (fold 15's read
        "action-group-by.yml": [{"action": "group_by key=g"}],                                         # A, N1 and S3)
        "local-action-group-by.yml": [{"local_action": {"module": "ansible.builtin.group_by", "key": "g"}}],
        "kv-eqeq.yml": [{"ansible.builtin.set_fact": "device_role==x"}],
        "action-eqeq.yml": [{"action": "set_fact device_role==x"}],
        "local-action-eqeq.yml": [{"local_action": "set_fact device_role==x"}]})   # fold 17's read B, N1
    internal = {   # ansible-core's own `_variable_params` sets any name: device_role, ansible_facts (read A, S1)
        "variable-params.yml": [{"ansible.builtin.set_fact": {"_variable_params": "{{ {'device_' ~ 'role': 'x'} }}"}}],
        "args-variable-params.yml": [{"set_fact": None, "args": {"_variable_params": "{{ v }}"}}]}
    outside = {   # an include or a role the scan cannot follow into the tree (fold 15's read A, S2)
        "include-absolute.yml": [{"ansible.builtin.include_tasks": "/etc/kontroll/site.yml"}],
        "include-dotdot.yml": [{"include_tasks": "../../site.yml"}],
        "include-templated.yml": [{"ansible.builtin.import_tasks": "{{ f }}.yml"}],
        "role-by-path.yml": [{"hosts": "localhost", "tasks": [{"ansible.builtin.include_role":
                                                                {"name": "/etc/kontroll/site_role"}}]}],
        "role-by-relative-path.yml": [{"ansible.builtin.include_role": {"name": "sub/r"}}],
        "role-entry-by-path.yml": [{"hosts": "g", "roles": ["../elsewhere/r"]}],
        "role-entry-mapping-by-path.yml": [{"hosts": "g", "roles": [{"role": "/etc/kontroll/site_role"}]}],
        "role-tasks-from.yml": [{"ansible.builtin.import_role": {"name": "r", "tasks_from": "/etc/x.yml"}}],
        "role-action.yml": [{"action": {"module": "ansible.builtin.include_role", "name": "r"}}],
        "role-kv.yml": [{"ansible.builtin.include_role": "name=r"}],
        "role-args.yml": [{"ansible.builtin.include_role": {"name": "r"}, "args": {"tasks_from": "/etc/x.yml"}}]}
    # each arm of the reach rule a row (fold 17's read B, N1), and its S2 and S3: the `{{ role }}` seam outside
    # wire-logging.yml, a plain name the tree does not hold, a collection's name (roles `h` and `i` below too)
    outside.update({
        "role-entry-name-by-path.yml": [{"hosts": "g", "roles": [{"name": "../elsewhere/r"}]}],
        "role-vars-from.yml": [{"ansible.builtin.include_role": {"name": "r", "vars_from": "../../x"}}],
        "role-defaults-from.yml": [{"ansible.builtin.include_role": {"name": "r", "defaults_from": "/etc/x"}}],
        "role-handlers-from.yml": [{"ansible.builtin.include_role": {"name": "r", "handlers_from": "{{ x }}"}}],
        "role-alias-by-path.yml": [{"ansible.builtin.include_role": {"role": "/etc/kontroll/site_role"}}],
        "os-family-elsewhere.yml": [{"ansible.builtin.include_tasks": "{{ ansible_os_family }}.yml",   # its `when`
                                     "when": ["ansible_os_family in ['Debian', 'Alpine']"]}],   # too (S5)
        "role-seam-elsewhere.yml": [{"ansible.builtin.include_role": {"name": "{{ role }}"}}],
        "role-absent.yml": [{"ansible.builtin.include_role": {"name": "zzabsent"}}],
        "role-entry-absent.yml": [{"hosts": "localhost", "roles": ["zzabsent"]}],
        "include-absent.yml": [{"ansible.builtin.include_tasks": "zzabsent.yml"}],
        "role-collection.yml": [{"ansible.builtin.include_role": {"name": "ns.coll.r"}}],
        "role-dot.yml": [{"ansible.builtin.include_role": {"name": "."}}],
        "role-entry-dot.yml": [{"hosts": "localhost", "roles": ["."]}],
        "role-trailing-space.yml": [{"ansible.builtin.include_role": {"name": "r "}}],
        "role-entry-tab.yml": [{"hosts": "localhost", "roles": ["r\t"]}],
        "include-file-trailing-space.yml": [{"ansible.builtin.include_tasks": {"file": "t.yml "}}],
        "device-role-seam-space.yml": [{"ansible.builtin.include_role": {"name": "{{ device_role }} "}}]})
    hides = {   # a var named ansible_facts hides a fact from the device_role guard (fold 13's reads A and B, N2)
        "vars-ansible-facts.yml": [{"hosts": "g", "vars": {"ansible_facts": {}}, "tasks": []}],
        "set-fact-ansible-facts.yml": [{"ansible.builtin.set_fact": {"ansible_facts": {}}}],
        "register-ansible-facts.yml": [{"ansible.builtin.command": "true", "register": "ansible_facts"}],
        "kv-ansible-facts.yml": [{"set_fact": "ansible_facts=x"}],
        "kv-escape-ansible-facts.yml": [{"set_fact": "ansible_f%sx61cts=x" % chr(92)}],   # fold 14's read A, S1
        "kv-tab-ansible-facts.yml": [{"set_fact": "ansible_facts\t=x"}],
        "loop-var-ansible-facts.yml": [{"debug": {"msg": "x"}, "loop": [1],
                                        "loop_control": {"loop_var": "ansible_facts"}}],
        "index-var-ansible-facts.yml": [{"debug": {"msg": "x"}, "loop": [1],
                                         "loop_control": {"index_var": "ansible_facts"}}],
        "include-vars-ansible-facts.yml": [{"ansible.builtin.include_vars": {"file": "v.yml",
                                                                             "name": "ansible_facts"}}],
        "prompt-ansible-facts.yml": [{"hosts": "g", "vars_prompt": [{"name": "ansible_facts"}], "tasks": []}]}
    hides.update({   # a set_fact's own `ansible_facts==` word (fold 15's read A, S3)
        "kv-eqeq-ansible-facts.yml": [{"set_fact": "ansible_facts==x"}],
        "action-eqeq-ansible-facts.yml": [{"action": "set_fact ansible_facts==x"}],
        "local-action-eqeq-ansible-facts.yml": [{"local_action": "set_fact ansible_facts==x"}]})   # read B, N1
    revives = {   # a clear_host_errors anywhere revives a host the guard refused (fold 14's read A, N1), the word in
        "revive.yml": [{"ansible.builtin.meta": "clear_host_errors"}],   # the key's value or in `args:` (read B, N1)
        "revive-null-args.yml": [{"ansible.builtin.meta": None, "args": {"_raw_params": "clear_host_errors"}}],
        "revive-empty-args.yml": [{"meta": "", "args": {"_raw_params": "clear_host_errors"}}],
        "revive-action-args.yml": [{"action": "ansible.builtin.meta", "args": {"_raw_params": "clear_host_errors"}}],
        "revive-action-module-args.yml": [{"action": {"module": "ansible.builtin.meta"},
                                           "args": {"_raw_params": "clear_host_errors"}}],
        "revive-handler.yml": [{"hosts": "g", "tasks": [], "handlers": [{"name": "h", "meta": "clear_host_errors"}]}]}
    passing = {   # reads, a scoped include_vars, a plain action set_fact and another name's k=v pass
        "reads.yml": [{"ansible.builtin.debug": {"msg": "{{ device_role }}"}, "when": "device_role == 'x'"}],
        "named.yml": [{"ansible.builtin.include_vars": {"file": "v.yml", "name": "fleet"}}],
        "action-plain.yml": [{"action": "set_fact x=1"}],
        "other-var.yml": [{"set_fact": "my_device_role=x"}],
        "other-escaped-var.yml": [{"set_fact": "my_device_%sx72ole=x" % chr(92)}],
        "reads-facts.yml": [{"debug": {"msg": "{{ ansible_facts.x }}"}, "when": "ansible_facts == {}"}],
        "other-facts-var.yml": [{"set_fact": "my_ansible_facts=x"}]}
    passing.update({   # a plain include, a role by its name (a mapping entry too), the seam and a debug's `==` word
        "include-plain.yml": [{"ansible.builtin.include_tasks": "t.yml"},
                              {"ansible.builtin.include_role": {"name": "r", "tasks_from": "main.yml"}}],
        "seam.yml": [{"hosts": "g", "roles": ["{{ device_role }}"]}],
        "role-entry-mapping.yml": [{"hosts": "g", "roles": [{"role": "r"}, {"name": "r", "tags": ["t"]}]}],
        "debug-eqeq.yml": [{"ansible.builtin.debug": "msg=device_role==x"}],
        "other-eqeq.yml": [{"set_fact": "my_device_role==x my_ansible_facts==x"}]})
    # the `{{ role }}` seam in wire-logging.yml, and a plain name the tree holds where ansible-core looks first: a
    # role in the roles path (`r`), a task file beside a top-level file or a role's own entry (role `f`'s handler
    # below, `h2` in its `handlers/`) (fold 17's read B, S2 and S3; fold 18's, S3; fold 19's, S2)
    passing.update({
        "wire-logging.yml": [{"ansible.builtin.include_role": {"name": "{{ role }}"}}]})
    # a role ansible-core finds first in `<playbooks>/roles` (`pr`, and `sr`, which the roles path holds too) or in
    # the playbooks directory (`br`): it reads the role's files as a role's, the scan as playbook-level ones (fold
    # 19's read A, S3); a `collections` keyword on a play or a block, whose collections it searches first (its S6)
    outside.update({
        "role-beside.yml": [{"ansible.builtin.include_role": {"name": "pr"}}],
        "role-in-base.yml": [{"hosts": "g", "roles": ["br"]}],
        "role-shadowed.yml": [{"ansible.builtin.include_role": {"name": "sr"}}],
        "collections-play.yml": [{"hosts": "localhost", "collections": ["ns.coll"], "roles": ["r"]}],
        "collections-block.yml": [{"block": [{"ansible.builtin.include_role": {"name": "r"}}],
                                   "collections": ["ns.coll"]}]})
    (base / "sub" / "real").mkdir(parents=True, exist_ok=True)
    (base / "sub" / "real" / "include-beside.yml").write_text(yaml.safe_dump([   # an included file's include
        {"ansible.builtin.import_tasks": "beside.yml"}]))
    (base / "group_vars").mkdir()
    (base / "sub" / "real" / "include-from-base.yml").write_text(yaml.safe_dump([   # base after the working directory
        {"ansible.builtin.include_tasks": "t.yml"}]))
    (base / "sub" / "play.yml").write_text(yaml.safe_dump([{"hosts": "localhost", "tasks": []}]))
    (base / "sub" / "play.yaml").write_text(yaml.safe_dump([{"hosts": "localhost", "tasks": []}]))   # `.yaml` too
    (base / "roles" / "node_exporter" / "tasks").mkdir(parents=True)   # the pin's name, not its place
    (base / "roles" / "node_exporter" / "tasks" / "main.yml").write_text(yaml.safe_dump([
        {"ansible.builtin.include_tasks": "{{ ansible_os_family }}.yml"}]))
    for name, doc in {**setters, **internal, **outside, **hides, **revives, **passing}.items():
        (base / name).write_text(yaml.safe_dump(doc))
    for target in ("t.yml", "sub/real/beside.yml", "roles/pr/tasks/main.yml", "br/tasks/main.yml",
                   "roles/sr/tasks/main.yml", "Debian.yml", "Alpine.yml"):   # the pin's files, at the top
        (base / target).parent.mkdir(parents=True, exist_ok=True)
        (base / target).write_text("[]\n")
    (base / "sub" / "real" / "wire-logging.yml").write_text(yaml.safe_dump(   # the seam's pin is a path, not a name
        [{"ansible.builtin.include_role": {"name": "{{ role }}"}}]))
    (base / "deep.yml").write_text("[" * 600 + "]" * 600 + "\n")   # past PyYAML's reader: a traceback (read B, N2)
    (base / "files" / "deep.txt").write_text("[" * 600 + "]" * 600 + "\n")   # the sniff too: counted as a play
    (base / "templates").mkdir()
    (base / "templates" / "pb.j2").write_text("- ansible.builtin.import_playbook: x.yml\n")
    for d in ("files", "templates"):   # its loader basedir is that directory (fold 19's read B, S1)
        (base / d / "zz-play.yml").write_text(yaml.safe_dump([{"hosts": "localhost", "tasks": []}]))
    (base / "roles" / "pr" / "tasks" / "sub").mkdir(parents=True)   # a role in `<playbooks>/roles`: its subdirectory
    (base / "roles" / "pr" / "tasks" / "sub" / "a.yml").write_text(yaml.safe_dump([
        {"ansible.builtin.include_tasks": "b.yml"}]))
    (base / "roles" / "pr" / "tasks" / "sub" / "b.yml").write_text("[]\n")
    (base / "sub" / "files").mkdir()
    (base / "sub" / "files" / "hook.sh").write_text("[]\n")   # only the top-level files/ is passed over
    (tmp_path / "roles" / "r" / "action_plugins").mkdir(parents=True)
    (tmp_path / "roles" / "README.md").write_text("x\n")   # a file beside the roles
    (tmp_path / "roles" / "r" / "tasks").mkdir()   # a role's own task sets it on every host (fold 9's read A, S2: V14)
    (tmp_path / "roles" / "r" / "tasks" / "set.yml").write_text(yaml.safe_dump([{
        "ansible.builtin.set_fact": {"device_role": "node_exporter"}, "delegate_to": "{{ item }}",
        "delegate_facts": True, "loop": "{{ groups.all }}"}]))
    (tmp_path / "roles" / "r" / "tasks" / "outside.yml").write_text(yaml.safe_dump([   # fold 15's read A, S2
        {"ansible.builtin.include_tasks": "/etc/kontroll/site.yml"}]))
    # a dependency by a path, as a string and as a mapping, and by name (fold 15's read B, S3); by a path under
    # `name:`, by its `src` alone and by a name the tree does not hold, and a role's own include of a task file the
    # tree does not hold (fold 17's read B, N1, S1 and S3); a role named by a collection's name or by a path is
    # refused though the tree holds a directory of that name; a role's handler includes its role's own task file
    # (every `meta/` dependency is refused, `f`'s by name too, though the roles path holds `r`: fold 20's read A, N2)
    for role, deps in (("d", ["../elsewhere/r"]), ("e", [{"role": "sub/r"}]), ("f", ["r", {"role": "r"}]),
                       ("g", [{"name": "sub/r"}]), ("h", [{"src": "../elsewhere/r"}]), ("i", ["zzabsent"]),
                       ("j", ["."]), ("k", ["r "])):
        (tmp_path / "roles" / role / "meta").mkdir(parents=True)
        (tmp_path / "roles" / role / "meta" / "main.yml").write_text(yaml.safe_dump({"dependencies": deps}))
    (tmp_path / "roles" / "r" / "tasks" / "include-absent.yml").write_text(yaml.safe_dump([
        {"ansible.builtin.include_tasks": "zzabsent.yml"}]))
    (tmp_path / "roles" / "r" / "tasks" / "include-base.yml").write_text(yaml.safe_dump([   # base: after the cwd
        {"ansible.builtin.include_tasks": "t.yml"}]))
    (tmp_path / "roles" / "r" / "tasks" / "sub").mkdir()   # its own subdirectory: after the working directory
    (tmp_path / "roles" / "r" / "tasks" / "sub" / "a.yml").write_text(yaml.safe_dump([
        {"ansible.builtin.include_tasks": "b.yml"}]))
    (tmp_path / "roles" / "r" / "tasks" / "sub" / "b.yml").write_text("[]\n")
    (tmp_path / "roles" / "connection_plugins").mkdir()   # a plugin directory of role `.`
    (tmp_path / "roles" / "connection_plugins" / "c.yml").write_text("[]\n")
    (tmp_path / "roles" / "library").mkdir()   # each kind `_plugin_dir` names, not only a `*_plugins` one
    (tmp_path / "roles" / "library" / "x.py").write_text("x = 1\n")
    for held in ("ns.coll.r", "sub/r", "None"):   # `None`: h's `src` alone is not read as a name None
        (tmp_path / "roles" / held / "tasks").mkdir(parents=True)
        (tmp_path / "roles" / held / "tasks" / "main.yml").write_text("[]\n")
    (tmp_path / "roles" / "sr" / "tasks").mkdir(parents=True)   # the roles path's `sr`, which `<playbooks>/roles`
    (tmp_path / "roles" / "sr" / "tasks" / "main.yml").write_text("[]\n")   # shadows
    (tmp_path / "roles" / "f" / "tasks").mkdir()
    (tmp_path / "roles" / "f" / "tasks" / "t2.yml").write_text("[]\n")
    os.symlink("t2.yml", str(tmp_path / "roles" / "f" / "tasks" / "linked.yml"))   # in a role: a file, a directory
    (tmp_path / "outside").mkdir()
    os.symlink("../../outside", str(tmp_path / "roles" / "f" / "linked-dir"))   # os.walk does not enter it
    os.symlink("../outside", str(tmp_path / "roles" / "linked-role"))   # a role that is one
    (tmp_path / "roles" / "f" / "handlers").mkdir()
    (tmp_path / "roles" / "f" / "handlers" / "main.yml").write_text(yaml.safe_dump([{
        "name": "h2", "ansible.builtin.include_tasks": "h2.yml"}]))
    (tmp_path / "roles" / "f" / "handlers" / "h2.yml").write_text("[]\n")
    (tmp_path / "roles" / "r" / "handlers").mkdir()   # a role another host runs revives the refused one (N2)
    (tmp_path / "roles" / "r" / "handlers" / "main.yml").write_text(yaml.safe_dump([{
        "name": "h", "ansible.builtin.meta": "clear_host_errors"}]))
    (tmp_path / "roles" / "r" / "meta").mkdir()
    (tmp_path / "roles" / "r" / "meta" / "main.json").write_text('{"__ansible_unsafe": {"dependencies": []}}\n')
    (tmp_path / "roles" / "r" / "tasks").mkdir(parents=True, exist_ok=True)   # an import in an imported file
    (tmp_path / "roles" / "r" / "tasks" / "nest-a.yml").write_text(yaml.safe_dump([
        {"ansible.builtin.import_tasks": "nest-b.yml"}]))
    (tmp_path / "roles" / "r" / "tasks" / "nest-b.yml").write_text("[]\n")
    for role, doc in (("s", [{"ansible.builtin.include_tasks": "main.yml"}]),   # a main that includes itself
                      ("m", [{"ansible.builtin.include_tasks": "x.yml"}])):     # a main's include beside it passes
        (tmp_path / "roles" / role / "tasks").mkdir(parents=True)
        (tmp_path / "roles" / role / "tasks" / "main.yml").write_text(yaml.safe_dump(doc))
    (tmp_path / "roles" / "m" / "tasks" / "x.yml").write_text("[]\n")
    (tmp_path / "roles" / "l" / "tasks").mkdir(parents=True)   # a handler's include of a tasks/ file (3rd candidate)
    (tmp_path / "roles" / "l" / "tasks" / "t2.yml").write_text("[]\n")
    (tmp_path / "roles" / "l" / "handlers").mkdir()
    (tmp_path / "roles" / "l" / "handlers" / "main.yml").write_text(yaml.safe_dump([{
        "name": "h", "ansible.builtin.include_tasks": "t2.yml"}]))
    legacy = base / "collections" / "ansible_collections" / "ansible" / "legacy" / "roles" / "r" / "tasks"
    legacy.mkdir(parents=True)   # a meta dependency is looked up in ansible.legacy first (fold 19's read A, S8)
    (legacy / "main.yml").write_text("[]\n")
    (tmp_path / "root.cfg").write_text("[defaults]\n")
    cfg = tmp_path / "ansible.cfg"
    cfg.write_text("[defaults]\nroles_path = elsewhere\n" + "".join("%s = p\n" % d for d in plugins)
                   + "doc_fragment_plugins = p\n"
                   + "jinja2_extensions = x\ncollections_path = elsewhere\n[inventory]\nenable_plugins = script\n"
                   + "[ssh_connection]\nssh_executable = x\n[paramiko_connection]\nproxy_command = x\n"
                   + "[persistent_connection]\nansible_connection_path = x\n[connection]\nssh_agent_executable = x\n")
    found = _tree_faults(str(base), str(cfg), str(tmp_path / "roles"), [str(tmp_path / "root.cfg")])
    assert sorted(found) == sorted(["sub/link: a symlinked directory", "./file-link.yml: a symlink",
                                    "roles/f/tasks/linked.yml: a symlink", "roles/f/linked-dir: a symlink",
                                    "roles/linked-role: a symlink", "./plays.json: a file the scan does not parse",
                                    "sub/files/hook.sh: a file the scan does not parse",
                                    "files/play.txt: a play under the top-level files/ or templates/",
                                    "files/json.txt: a play under the top-level files/ or templates/",
                                    "files/unsafe.txt: a play under the top-level files/ or templates/",
                                    *["files/%s: a play under the top-level files/ or templates/" % n
                                      for n in list(sniffed) + ["latin.txt", "tag-escape.txt"]],
                                    "files/wrapped.txt: an __ansible key, which ansible-core reads three ways",
                                    *["./%s: an __ansible key, which ansible-core reads three ways" % n for n in keyed],
                                    "roles/r/meta/main.json: an __ansible key, which ansible-core reads three ways",
                                    *["%s: sets device_role, which only the instance's inventory may"
                                      % (n if "/" in n else "./" + n) for n in setters],
                                    *["./%s: names `ansible_facts` as a var, which hides a fact from the device_role "
                                      "guard" % n for n in hides],
                                    *["./%s: runs `meta: clear_host_errors`, which revives a refused host" % n
                                      for n in revives],
                                    "roles/r/handlers/main.yml: runs `meta: clear_host_errors`, which revives a "
                                    "refused host",
                                    "roles/r/tasks/set.yml: sets device_role, which only the instance's inventory may",
                                    *["./%s: %s" % (n, why) for n in internal for why in (
                                        "sets device_role, which only the instance's inventory may",
                                        "names `ansible_facts` as a var, which hides a fact from the device_role "
                                        "guard")],
                                    *["./%s: %s" % (n, _OUT) for n in outside],
                                    "roles/r/tasks/outside.yml: " + _OUT,
                                    "roles/d/meta/main.yml: " + _OUT, "roles/e/meta/main.yml: " + _OUT,
                                    "roles/f/meta/main.yml: " + _OUT, "roles/g/meta/main.yml: " + _OUT,
                                    "roles/h/meta/main.yml: " + _OUT,
                                    "roles/i/meta/main.yml: " + _OUT, "roles/r/tasks/include-absent.yml: " + _OUT,
                                    "sub/real/wire-logging.yml: " + _OUT,
                                    "roles/j/meta/main.yml: " + _OUT, "roles/k/meta/main.yml: " + _OUT,
                                    "roles/r/tasks/include-base.yml: " + _OUT, "roles/r/tasks/sub/a.yml: " + _OUT,
                                    "sub/real/include-from-base.yml: " + _OUT,
                                    "roles/node_exporter/tasks/main.yml: " + _OUT,
                                    "sub/play.yml: a play below the top of the playbooks",
                                    "sub/play.yaml: a play below the top of the playbooks",
                                    "roles/library: a plugin directory",
                                    "roles/connection_plugins: a plugin directory",
                                    "sub/real/include-beside.yml: " + _OUT, "roles/pr/tasks/sub/a.yml: " + _OUT,
                                    "roles/r/tasks/nest-a.yml: " + _OUT, "roles/s/tasks/main.yml: " + _OUT,
                                    "roles/l/handlers/main.yml: " + _OUT,
                                    "collections: a collection root ansible-core searches before the roles path",
                                    "files/zz-play.yml: a play below the top of the playbooks",
                                    "templates/zz-play.yml: a play below the top of the playbooks",
                                    "./deep.yml: nested deeper than the scan reads",
                                    "files/deep.txt: nested deeper than the scan reads",
                                    "files/deep.txt: a play under the top-level files/ or templates/",
                                    "templates/pb.j2: a play under the top-level files/ or templates/",
                                    "./plays: a file the scan does not parse",
                                    "./plays.txt: a file the scan does not parse",
                                    "./plays.YML: a file the scan does not parse",
                                    "roles/r/action_plugins: a plugin directory",
                                    "%s: an ansible.cfg ansible-core reads from that directory"
                                    % (tmp_path / "root.cfg"),
                                    "ansible.cfg: roles_path is 'elsewhere', not the roles the scan walks",
                                    "ansible.cfg: [ssh_connection] is {'ssh_executable': 'x'}, not the shipped "
                                    "options", "ansible.cfg: a [paramiko_connection] section",
                                    "ansible.cfg: a [persistent_connection] section",
                                    "ansible.cfg: a [connection] section",
                                    "ansible.cfg: [defaults] jinja2_extensions, a key the scan does not reason about",
                                    "ansible.cfg: [defaults] collections_path is 'elsewhere', not the shipped value",
                                    "ansible.cfg: [inventory] is {'enable_plugins': 'script'}, not the shipped "
                                    "options"]
                                   + ["./%s: a plugin directory" % d for d in plugins]
                                   + ["ansible.cfg: names %s" % d for d in plugins + ("doc_fragment_plugins",)]
                                   + ["ansible.cfg: [defaults] %s is missing (ansible-core's own default stands in)" % k
                                      for k in sorted(set(_CFG_DEFAULTS) - {"roles_path", "filter_plugins",
                                                                             "collections_path"})]), found
    shipped = "[defaults]\n" + "".join("%s = %s\n" % kv for kv in sorted(_CFG_DEFAULTS.items()))
    inv = "[inventory]\nenable_plugins = yaml, ini, constructed\n"
    ok = "[ssh_connection]\npipelining = True\nssh_args = -o ControlMaster=auto -o ControlPersist=60s\n" + inv
    cfg.write_text(shipped + ok.replace(inv, ""))
    assert _tree_faults(str(tmp_path / "none"), str(cfg)) == [   # no [inventory]: `script` and `auto` load
        "ansible.cfg: [inventory] is {}, not the shipped options"]
    cfg.write_text(shipped.replace("vars_plugins_enabled", "#") + ok)
    assert _tree_faults(str(tmp_path / "none"), str(cfg)) == [   # a held key removed: the engine's default stands in
        "ansible.cfg: [defaults] vars_plugins_enabled is missing (ansible-core's own default stands in)"]
    with open(os.path.join(ROOT, "ansible", "ansible.cfg"), "rb") as fh:   # bare CRs: ansible-core reads none
        cfg.write_bytes(fh.read().replace(b"\n", b"\r"))                   # (fold 8's read A, S2)
    assert "ansible.cfg: roles_path is None, not the roles the scan walks" in _tree_faults(str(tmp_path / "none"),
                                                                                          str(cfg))
    cfg.write_bytes(shipped.encode() + b"# " + bytes([0xFF]) + b"\n" + ok.encode())   # a byte that is not UTF-8: read,
    assert _tree_faults(str(tmp_path / "none"), str(cfg)) == []                       # not a traceback (read B, N2)
    cfg.write_text(shipped + "roles_path = roles\n" + ok)   # a duplicate key: ansible-core refuses the file, and the
    assert _tree_faults(str(tmp_path / "none"), str(cfg)) == [   # test ended in a traceback (fold 11's read B, N2)
        "ansible.cfg: a file ansible-core refuses to read (DuplicateOptionError)"]
    cfg.write_text(shipped + inv + "[ssh_connection]\npipelining = True\nssh_args = -o ProxyCommand=x\n")
    assert _tree_faults(str(tmp_path / "none"), str(cfg)) == [   # the shipped keys, another value
        "ansible.cfg: [ssh_connection] is {'pipelining': 'True', 'ssh_args': '-o ProxyCommand=x'}, not the shipped "
        "options"]
    (tmp_path / "filter_plugins").mkdir()
    (tmp_path / "filter_plugins" / "f.py").write_text(
        "class FilterModule(object):\n    def filters(self):\n        return {'kontroll_x': len, 'first': len}\n")
    (tmp_path / "filter_plugins" / "g.py").write_text(
        "class FilterModule(object):\n    def filters(self):\n        return dict(kontroll_y=len)\n")
    (tmp_path / "filter_plugins" / "h.py").write_text("class FilterModule(object):\n    filters = lambda self: {}\n")
    (tmp_path / "filter_plugins" / "i.py").write_text(
        "class FilterModule(object):\n    def filters(self):\n        return {'default': len}\n")
    cfg.write_text(shipped + ok)
    assert _tree_faults(str(tmp_path / "none"), str(cfg)) == ["filter_plugins/f.py: a filter not named kontroll_*",
                                                              "filter_plugins/g.py: a filter not named kontroll_*",
                                                              "filter_plugins/h.py: a filter file whose filters the "
                                                              "scan cannot read",
                                                              "filter_plugins/i.py: a filter not named kontroll_*"]
    clean = tmp_path / "clean"
    (clean / "tasks").mkdir(parents=True)
    (clean / "ok.yml").write_text("[]\n")
    (tmp_path / "filter_plugins" / "f.py").write_text(
        "class FilterModule(object):\n    def filters(self):\n        return {'kontroll_x': len}\n")
    (tmp_path / "filter_plugins" / "g.py").unlink()
    (tmp_path / "filter_plugins" / "h.py").unlink()
    (tmp_path / "filter_plugins" / "i.py").unlink()
    assert _tree_faults(str(clean), str(cfg)) == []


@pytest.mark.parametrize("when, files, nest, refused", [
    (["ansible_os_family in ['Debian', 'Alpine']"], ("Debian", "Alpine"), False, None),
    (["ansible_os_family in ['Debian', 'Alpine', 'RedHat']"], ("Debian", "Alpine"), False, "main.yml"),
    (None, ("Debian", "Alpine"), False, "main.yml"),
    (["ansible_os_family in ['Debian', 'Alpine']"], ("Debian",), False, "main.yml"),
    (["ansible_os_family in ['Debian', 'Alpine']"], ("Debian", "Alpine"), True, "Debian.yml")],
    ids=["as-shipped", "a-wider-when", "no-when", "a-file-gone", "a-file-that-includes"])
def test_the_pin_holds_its_when_and_its_files(tmp_path, when, files, nest, refused):
    """node_exporter's pinned include, `{{ ansible_os_family }}.yml` in its role's `tasks/main.yml`, passes only with
    the `when` that holds the family to the two files its role's `tasks/` has, and with both of them there: with a
    wider `when`, with none, or with one of the files gone, ansible-core tries the working directory next for the name
    it templates, which the scan never reads, so the include is refused (fold 19's read A, S5: the pin was held by its
    name alone, and a wider `when` or a removed file passed). A file the pin names is not an entry file, so an include
    in it is refused on its own (fold 19's read B, S2)."""
    base = tmp_path / "playbooks"
    base.mkdir()
    tasks = tmp_path / "roles" / "node_exporter" / "tasks"
    tasks.mkdir(parents=True)
    task = {"ansible.builtin.include_tasks": "{{ ansible_os_family }}.yml"}
    if when is not None:
        task["when"] = when
    (tasks / "main.yml").write_text(yaml.safe_dump([task]))
    for n in files:
        (tasks / (n + ".yml")).write_text("[]\n")
    if nest:
        (tasks / "Debian.yml").write_text(yaml.safe_dump([{"ansible.builtin.import_tasks": "Alpine.yml"}]))
    found = [f for f in _tree_faults(str(base), os.path.join(ROOT, "ansible", "ansible.cfg"), str(tmp_path / "roles"))
             if "ansible.cfg" not in f]
    assert found == (["roles/node_exporter/tasks/%s: %s" % (refused, _OUT)] if refused else []), found

def _small_tree(tmp_path):
    """A playbooks directory with no `<playbooks>/roles` (the shipped layout) and an empty roles path beside it."""
    base, roles = tmp_path / "playbooks", tmp_path / "roles"
    base.mkdir()
    roles.mkdir()
    return base, roles


def _small_faults(base, roles):
    return [f for f in _tree_faults(str(base), os.path.join(ROOT, "ansible", "ansible.cfg"), str(roles))
            if "ansible.cfg" not in f]


def test_a_role_named_dot_is_refused_where_the_roles_path_comes_first(tmp_path):
    """With no `<playbooks>/roles`, as the shipped tree has none, a role named `.`, in an include or in a play's
    `roles:`, is refused, while a role the roles path holds passes beside it. ansible-core takes `.` from the first
    directory it searches that exists, here the roles path itself, and reads that directory's `tasks/main.yml`, a file
    the scan reads only as a file of a role named `tasks`, never as a role's entry. Guards the refusal of `.` by name,
    which the big fixture cannot hold: there `<playbooks>/roles` exists and is found first, and that alone refuses `.`
    (fold 20's mutant MB18-dot-accepted survived it on both cores)."""
    base, roles = _small_tree(tmp_path)
    (roles / "r" / "tasks").mkdir(parents=True)
    (roles / "r" / "tasks" / "main.yml").write_text("[]\n")
    (base / "role-dot.yml").write_text(yaml.safe_dump([{"ansible.builtin.include_role": {"name": "."}}]))
    (base / "role-entry-dot.yml").write_text(yaml.safe_dump([{"hosts": "localhost", "roles": ["."]}]))
    (base / "role-r.yml").write_text(yaml.safe_dump([{"ansible.builtin.include_role": {"name": "r"}}]))
    found = _small_faults(base, roles)
    assert found == ["./role-dot.yml: " + _OUT, "./role-entry-dot.yml: " + _OUT], found


def test_the_scan_names_its_faults_in_one_order_whatever_order_a_directory_lists(tmp_path, monkeypatch):
    """The scan names its faults in path order, and in the same order when every directory lists its entries the
    other way round: a tree with a fault in two files at the top, in two directories below it and in two directories
    of a role. Guards a walk that takes the filesystem's order: the public CI's runners list a directory in another
    order than the gate's image does, and the dot-role row above, which compares two faults as a list, failed there
    (PR #125's first CI run)."""
    base, roles = _small_tree(tmp_path)
    for name in ("role-dot.yml", "role-entry-dot.yml"):
        (base / name).write_text(yaml.safe_dump([{"ansible.builtin.include_role": {"name": "."}}]))
    for sub in ("a", "b"):
        (base / sub).mkdir()
        (base / sub / "play.yml").write_text(yaml.safe_dump([{"hosts": "localhost", "tasks": []}]))
    (roles / "r" / "tasks").mkdir(parents=True)
    task = [{"ansible.builtin.set_fact": {"device_role": "x"}}]
    (roles / "r" / "tasks" / "main.yml").write_text(yaml.safe_dump(task))
    (roles / "r" / "vars").mkdir()
    (roles / "r" / "vars" / "main.yml").write_text(yaml.safe_dump({"device_role": "x"}))
    found = _small_faults(base, roles)
    paths = [f.split(": ", 1)[0] for f in found]
    assert sorted(set(paths)) == ["./role-dot.yml", "./role-entry-dot.yml", "a/play.yml", "b/play.yml",
                                  "roles/r/tasks/main.yml", "roles/r/vars/main.yml"], found
    assert paths == sorted(paths), found
    walk = os.walk

    def reversed_walk(top, *args, **kwargs):   # the same lists, turned round before the scan sees them
        for directory, dirs, files in walk(top, *args, **kwargs):
            dirs.reverse()
            files.reverse()
            yield directory, dirs, files

    monkeypatch.setattr(os, "walk", reversed_walk)
    assert _small_faults(base, roles) == found, found


def test_the_pin_is_held_by_its_place_not_its_name(tmp_path):
    """A copy of node_exporter's pinned include as shipped (its `when`, and both family files beside it) in a
    `node_exporter` role under `<playbooks>/roles`, not the roles path, is refused: the pin names the one file in the
    roles path, and a role found anywhere else is not it. Guards a pin matched by the role's name or by the file's
    last path parts. The big fixture's copy carries no `when`, so the pin's own `when` check refused it either way,
    and fold 20's mutant MB18-pin-by-name survived there on both cores; here that mutant meets the pin's file check
    in a file with no task directory, and the scan stops."""
    base, roles = _small_tree(tmp_path)
    tasks = base / "roles" / "node_exporter" / "tasks"
    tasks.mkdir(parents=True)
    (tasks / "main.yml").write_text(yaml.safe_dump([{"ansible.builtin.include_tasks": "{{ ansible_os_family }}.yml",
                                                     "when": ["ansible_os_family in ['Debian', 'Alpine']"]}]))
    for n in ("Debian", "Alpine"):
        (tasks / (n + ".yml")).write_text("[]\n")
    found = _small_faults(base, roles)
    assert found == ["roles/node_exporter/tasks/main.yml: " + _OUT], found


def test_a_roles_main_yaml_is_an_entry_file_too(tmp_path):
    """A role in the roles path whose entry file is `tasks/main.yaml`, which ansible-core loads as it does
    `main.yml`, may include a task file beside it, as a `main.yml` may. Guards the entry rule losing `.yaml`, which
    would refuse a role that ansible-core runs as written (fold 20's mutant MBT-entry-main-yaml-off, which no row
    held)."""
    base, roles = _small_tree(tmp_path)
    (roles / "y" / "tasks").mkdir(parents=True)
    (roles / "y" / "tasks" / "main.yaml").write_text(yaml.safe_dump([{"ansible.builtin.include_tasks": "more.yml"}]))
    (roles / "y" / "tasks" / "more.yml").write_text("[]\n")
    (base / "role-y.yml").write_text(yaml.safe_dump([{"ansible.builtin.include_role": {"name": "y"}}]))
    assert _small_faults(base, roles) == []



@pytest.mark.parametrize("inner", [
    [{"block": [{"ansible.builtin.include_tasks": "z.yml"}]}],
    [{"ansible.builtin.import_tasks": "z.yml"}],
    [{"block": [], "rescue": [{"block": [{"ansible.builtin.import_tasks": "z.yml"}]}]}]],
    ids=["in-a-block", "an-import", "an-import-deep-in-a-rescue"])
def test_an_entry_file_that_includes_is_not_a_task_file_an_entry_may_name(tmp_path, inner):
    """A role's `handlers/main.yml` that includes the `main.yaml` beside it, and a playbook at the top that includes
    another at the top, are refused when the named file itself includes a task file, however that file writes it: in
    a block, as a static import, or deep in a rescue. The named file is an entry file, so its own include passes
    beside it; only the check on the file an include names (`_includes_tasks`) refuses the chain. Guards that check
    losing its walk into a task's values (a block, a rescue) or its import key, which no other row held (read 229's
    mutants on fold 20)."""
    base, roles = _small_tree(tmp_path)
    (roles / "q" / "handlers").mkdir(parents=True)
    (roles / "q" / "handlers" / "main.yml").write_text(yaml.safe_dump([
        {"name": "h", "ansible.builtin.include_tasks": "main.yaml"}]))
    (roles / "q" / "handlers" / "main.yaml").write_text(yaml.safe_dump(inner))
    (roles / "q" / "handlers" / "z.yml").write_text("[]\n")
    (base / "a.yml").write_text(yaml.safe_dump([{"ansible.builtin.include_tasks": "b.yml"}]))
    (base / "b.yml").write_text(yaml.safe_dump(inner))
    (base / "z.yml").write_text("[]\n")
    found = _small_faults(base, roles)
    assert found == ["./a.yml: " + _OUT, "roles/q/handlers/main.yml: " + _OUT], found


def test_a_main_file_below_a_roles_tasks_is_not_an_entry_file(tmp_path):
    """A role's `tasks/sub/main.yml`, which `tasks_from: sub/main` runs, is not an entry file: ansible-core looks for
    a task file it names in the role's `tasks/`, then in the working directory, never in `tasks/sub/` (fold 20's
    read A, `E-tasks_from-sub-main`, on both cores). Guards the entry rule taking any `main.yml` below a role's
    `tasks/` as an entry, which holds an include that ansible-core resolves to the working directory once the
    role's `tasks/` lacks the name (fold 20's read A, MA20-entry-any-depth, which no row held)."""
    base, roles = _small_tree(tmp_path)
    (roles / "y" / "tasks" / "sub").mkdir(parents=True)
    (roles / "y" / "tasks" / "main.yml").write_text("[]\n")
    (roles / "y" / "tasks" / "sub" / "main.yml").write_text(yaml.safe_dump([
        {"ansible.builtin.include_tasks": "more.yml"}]))
    (roles / "y" / "tasks" / "sub" / "more.yml").write_text("[]\n")
    (base / "role-y.yml").write_text(yaml.safe_dump([{"ansible.builtin.include_role":
                                                       {"name": "y", "tasks_from": "sub/main"}}]))
    assert _small_faults(base, roles) == ["roles/y/tasks/sub/main.yml: " + _OUT]


@pytest.mark.parametrize("shadow, role_dir, refused", [(True, True, True), (False, False, True), (False, True, False)],
                         ids=["a-file-in-the-playbooks-roles", "a-file-in-the-roles-path", "a-role-in-the-roles-path"])
def test_a_role_is_found_where_its_name_first_exists_a_file_too(tmp_path, shadow, role_dir, refused):
    """ansible-core finds a role where its name first exists, a file too (`path_exists`), and runs nothing from a
    file: a file `<playbooks>/roles/zz.yml` ahead of a role `zz.yml` in the roles path, and a file `zz.yml` in the
    roles path with no role of the name, each made an `include_role` of `zz.yml` run nothing, rc 0, on both cores
    (fold 20's read A, N1, `R2-file-shadow-*` and `R2-file-in-rp`). Both are refused, and the role alone passes.
    Guards the scan's first place taking only a directory, which stepped past the file to the roles path and held a
    role that never runs (read A's PA20-file-shadow survived fold 20), and that place taken where the name exists
    without its being a directory there (read A's FX20-role-first-exists, alone, holds the file in the roles
    path)."""
    base, roles = _small_tree(tmp_path)
    if shadow:
        (base / "roles").mkdir()
        (base / "roles" / "zz.yml").write_text("[]\n")
    if role_dir:
        (roles / "zz.yml" / "tasks").mkdir(parents=True)
        (roles / "zz.yml" / "tasks" / "main.yml").write_text("[]\n")
    else:
        (roles / "zz.yml").write_text("[]\n")
    (roles / "zz-o" / "tasks").mkdir(parents=True)
    (roles / "zz-o" / "tasks" / "main.yml").write_text(yaml.safe_dump([
        {"ansible.builtin.include_role": {"name": "zz.yml"}}]))
    assert _small_faults(base, roles) == (["roles/zz-o/tasks/main.yml: " + _OUT] if refused else [])


def test_a_role_meta_dependency_is_refused_whatever_it_names(tmp_path):
    """A role's `meta/` dependency is refused even by a plain name the roles path holds, while the same role in a
    play's `roles:` passes: ansible-core looks a dependency up as `ansible.legacy.<name>` in every collection root,
    the cfg's `collections_path` among them, before any roles directory, so the role it runs need not be the one the
    scan read (fold 20's read A, N2, `CX-cfg-root-dep`, on both cores); a play's `roles:` and an `include_role` do not
    look there. An empty list passes, and so does a task's var named `dependencies`. Guards the outright refusal of
    a dependency list, which no row but the big tree's role `f` holds, and its limit to a role's `meta/`."""
    base, roles = _small_tree(tmp_path)
    for role, meta in (("x", None), ("o", {"dependencies": ["x"]}), ("e", {"dependencies": []})):
        (roles / role / "tasks").mkdir(parents=True)
        (roles / role / "tasks" / "main.yml").write_text(yaml.safe_dump([
            {"ansible.builtin.set_fact": {"dependencies": ["x"]}}]) if role == "e" else "[]\n")
        if meta is not None:
            (roles / role / "meta").mkdir()
            (roles / role / "meta" / "main.yml").write_text(yaml.safe_dump(meta))
    (base / "play.yml").write_text(yaml.safe_dump([{"hosts": "all", "roles": ["x", "o", "e"]}]))
    assert _small_faults(base, roles) == ["roles/o/meta/main.yml: " + _OUT]


def test_a_link_is_refused_and_never_read_a_dangling_one_too(tmp_path):
    """A symlink is refused wherever the scan meets one, and never read: a link at the top of the roles path to a
    file (ansible-core takes it as a role of that name: fold 20's read B, N3), and a dangling link and a looping one
    at the top of the playbooks and in a role, each reported as a symlink, not a traceback (read B, N6). Guards the
    link arms: on fold 20 a file link at the top of the roles path passed, and a dangling or looping link ended
    `_tree_faults` in a traceback, since the walk then read the file it names."""
    base, roles = _small_tree(tmp_path)
    (roles / "y" / "tasks").mkdir(parents=True)
    (roles / "y" / "tasks" / "main.yml").write_text("[]\n")
    (tmp_path / "t.yml").write_text("[]\n")
    os.symlink(str(tmp_path / "t.yml"), str(roles / "z.yml"))
    os.symlink(str(tmp_path / "absent.yml"), str(base / "gone.yml"))
    os.symlink("loop.yml", str(base / "loop.yml"))
    os.symlink(str(tmp_path / "absent.yml"), str(roles / "y" / "tasks" / "gone.yml"))
    assert sorted(_small_faults(base, roles)) == ["./gone.yml: a symlink", "./loop.yml: a symlink",
                                                  "roles/y/tasks/gone.yml: a symlink", "roles/z.yml: a symlink"]


def test_no_logging_method_holds_a_key_its_readers_read_apart(tmp_path):
    """No file under logging/ holds an `__ansible` key in any reading (`_logging_faults`), and `_methods` reads each
    method as wire-logging.yml does, YAML alone. On a planted directory, a method written as JSON whose
    `__ansible_unsafe` wrapper names the ssh family and whose siblings add network_cli is reported, a plain method and a
    README are not, and `_methods` gives the siblings' list, as `from_yaml` does; a method that is not UTF-8, one the
    reader cannot parse, one that reads to a list and one nested past what both readers build (tagged) are reported,
    each where the test ended in a traceback (fold 11's read B, N2); and a method in a subdirectory, which neither
    reader reads today, is reported too, so a reader that comes to read one is covered (a walk cut to the top level
    passed every row: fold 12's own mutant run). Guards the allowlist test reading a list the run does not use: read
    2.18's way, a wrapped JSON method (a new drop-in, or a shipped one rewritten) named the ssh family while
    wire-logging.yml gave its ssh-only wiring role network_cli and ran its `command` on the controller, on 2.18 and
    2.19, with the whole fast suite green (fold 9's read A, S1)."""
    assert _logging_faults() == []
    (tmp_path / "m.yml").write_text(json.dumps({"__ansible_unsafe": {"wiring_role": "r", "wiring_connections": ["ssh"]},
                                                "wiring_role": "r", "wiring_connections": ["ssh", "network_cli"]}))
    (tmp_path / "plain.yml").write_text("wiring_role: p\nwiring_connections: [ssh]\n")
    (tmp_path / "README.md").write_text("x\n")
    assert _logging_faults(str(tmp_path)) == ["m.yml: an __ansible key, which ansible-core reads three ways"]
    assert [(n, m["wiring_connections"]) for n, m in _methods(str(tmp_path))] == [
        ("m.yml", ["ssh", "network_cli"]), ("plain.yml", ["ssh"])]
    (tmp_path / "latin.yml").write_bytes(b"wiring_role: p\nwiring_connections: [ssh]\nx: " + bytes([0xFF]) + b"\n")
    (tmp_path / "broken.yml").write_text("wiring_role: [p\n")
    (tmp_path / "deep.yml").write_text("!t " + "[" * 600 + "]" * 600 + "\n")   # tagged: libyaml's reader too
    (tmp_path / "list.yml").write_text("- wiring_role: p\n  wiring_connections: [ssh]\n")   # reads, but to a list
    (tmp_path / "sub").mkdir()   # read by neither reader today (each lists the top level), reported all the same
    (tmp_path / "sub" / "n.yml").write_text(json.dumps({"__ansible_unsafe": {"wiring_role": "r"}}))
    assert sorted(_logging_faults(str(tmp_path))) == [   # each was a traceback (fold 11's read B, N2)
        "broken.yml: a method that does not read to a mapping", "deep.yml: a method that does not read to a mapping",
        "deep.yml: nested deeper than the scan reads", "latin.yml: a method that does not read to a mapping",
        "list.yml: a method that does not read to a mapping",
        "m.yml: an __ansible key, which ansible-core reads three ways",
        "sub/n.yml: an __ansible key, which ansible-core reads three ways"]


def test_the_filter_plugins_are_the_reviewed_ones():
    """ansible/filter_plugins/ holds exactly the pinned files, each with its pinned content. Guards the one plugin
    path the tree rule lets ansible-core load: a filter file is controller code, so no reading of the names it
    registers holds what its body does. A line in `filters()` that patched Jinja's `first` ran a passing host's role
    on the controller with every other test green, on 2.18 and 2.19 (fold 6's read B, N1)."""
    directory = os.path.join(ROOT, "ansible", "filter_plugins")
    found = {}
    for name in sorted(os.listdir(directory)):
        if name != "__pycache__":
            with open(os.path.join(directory, name), "rb") as fh:
                found[name] = hashlib.sha256(fh.read().replace(b"\r\n", b"\n")).hexdigest()
    assert found == _FILTER_FILES, found


@pytest.mark.requires_checkout  # reads the tree through git: skipped outside a checkout (I-51)
def test_no_tracked_file_is_one_the_repo_ignores():
    """No tracked file matches the repo's own `.gitignore` rules (`git ls-files -ci --exclude-per-directory=.gitignore`
    names none: a developer's global excludes or `.git/info/exclude` are not the repo's), git lists this file (the test
    needs a git checkout, and fails where git lists none of the repo's files), and the files tracked under
    ansible/filter_plugins/ and ansible/collections/ansible_collections/ (the paths the held cfg's `filter_plugins`
    and `collections_path` name, read from `_CFG_DEFAULTS`: literal prefixes could drift from the cfg, fold 9's read
    B, N3) are exactly the filter pin's. Guards the code ansible-core loads from the paths the filter pin and the tree
    rule do not read, which the ignore rules keep out of every commit but a forced one: an
    unchecked hash-based `filter_plugins/__pycache__/*.pyc` ran in place of the pinned
    source with the pin green, and a vars plugin under ansible/collections/ (the shipped cfg's `collections_path`) ran
    on a CLI run, on 2.18 and 2.19 (fold 7's read B, N6); a nested `.gitignore` negation committed either with a plain
    `git add` and the rule green (fold 8's read B, N2)."""
    def git(*args):
        return subprocess.run(("git",) + args + ("-z",), cwd=ROOT, capture_output=True, text=True,
                              check=True).stdout.split(chr(0))[:-1]
    tracked = git("ls-files")
    assert "tests/unit/test_group_dispatch_refuses_local.py" in tracked, "git lists no file of this repository here"
    assert git("ls-files", "-ci", "--exclude-per-directory=.gitignore") == []   # the repo's rules, not a user's
    prefixes = ("ansible/%s/" % _CFG_DEFAULTS["filter_plugins"],   # the paths the held cfg names (fold 9's read B, N3)
                "ansible/%s/ansible_collections/" % _CFG_DEFAULTS["collections_path"])
    assert sorted(f for f in tracked if f.startswith(prefixes)) == \
        sorted(prefixes[0] + f for f in _FILTER_FILES)   # a .gitignore negation commits no plain add


def test_the_refusal_is_an_assert_on_an_allowlist_then_a_freeze():
    """The refusal file is two tasks, each with a name and its module and nothing else (no `when:`, no `ignore_errors`).
    The first is an `assert` whose condition holds the host's connection to the importing play's `refusal_connections`
    (none given: every host refused) and refuses a list that names the local plugin under any spelling ansible-core
    loads it by; before both, it refuses every host of the play when the `hostvars` of any host of the play
    (`ansible_play_hosts_all`) hold a `refusal_connections` key (they hold the inventory's vars, those of a vars file
    beside the playbooks, set_facts, include_vars, registered vars and `-e`, never the import's own vars), a presence
    test, which renders nothing. The second is a `set_fact` of `ansible_connection` alone, to the
    allowlist's own spelling of the value the assert passed, or '' (which no connection plugin loads). Guards the
    refusal being weakened to a debug line, a skippable task, a denylist of spellings (the first cut refused two, and
    ansible-core ran a host's tasks on the controller under four more and under network_cli, httpapi and netconf: its
    read, B1), or an undefined connection read as allowed; the freeze dropped, or storing the host's own value, which a
    template that renders by context or by the moment turns into `local` by the time the role runs (fold 1's read A,
    S1); and the presence condition dropped: the import's list is task vars, which a list an earlier play's
    set_fact left on the host outranks, and with one widened to network_cli install-node-exporter.yml ran
    node_exporter on the controller on 2.18 and 2.19 with every scan test green (fold 9's read A, S3); or that
    condition a test of the list's value, which 2.19 renders in the host's own context, where a template that reads a
    play's magic var is undefined, while the connection's condition renders it widened: a list so templated by `-e`
    or an earlier play's include_vars passed it, and the role ran over network_cli (fold 11's read A, S1); or the
    presence read by the host's own name (`hostvars[inventory_hostname]`), which an earlier play's set_fact of the
    list beside `inventory_hostname: localhost` passed for every host, the role running over network_cli (fold 12's
    read A, S1); or the presence test after the connection's, which a null list set outside the import failed with no
    word of the outside list (its N1)."""
    with open(os.path.join(PLAYBOOKS, REFUSAL), encoding="utf-8") as fh:
        tasks = yaml.safe_load(fh)
    assert len(tasks) == 2
    check, freeze = tasks
    assert set(check) == {"name", "ansible.builtin.assert"}, sorted(check)
    that = check["ansible.builtin.assert"]["that"]
    assert " ".join(that[0].split()) == ("(ansible_play_hosts_all | map('extract', hostvars) | select('contains', "
                                         "'refusal_connections') | list | length == 0)")
    assert that[1] == "(ansible_connection | default('')) in (refusal_connections | default([]))"
    assert " ".join(that[2].split()) == ("(refusal_connections | default([])) | intersect([%s]) | length == 0"
                                         % ", ".join("'%s'" % n for n in _LOCAL_FAMILY))
    assert len(that) == 3
    assert set(freeze) == {"name", "ansible.builtin.set_fact"}, sorted(freeze)
    assert list(freeze["ansible.builtin.set_fact"]) == ["ansible_connection"]
    assert " ".join(freeze["ansible.builtin.set_fact"]["ansible_connection"].split()) == _FREEZE


def test_every_allowlist_names_only_connections_that_run_the_role_where_it_is_written_to_run():
    """Each group-dispatching play passes an allowlist. install-node-exporter.yml passes the ssh family as it names it
    (`ssh` and `paramiko_ssh`, bare and `ansible.builtin.`; the `paramiko` alias and the `ansible.legacy.` spellings are
    refused on purpose, which fails closed), within `_SSH_FAMILY`, the bound every play's list stays within.
    wire-logging.yml passes the dispatched method's `wiring_connections` (logging/<method>.yml), each method with a
    wiring role naming a non-empty list. No list names the local plugin. A network plugin (whose non-network modules run
    on the controller) is named only for a wiring role whose every task, through its includes, is network-safe
    (`_network_unsafe`); every other name is of the ssh family. A play whose list is not of the ssh family alone runs no
    task or handler of its own (through its includes, and the fixed roles it includes) that is not network-safe, beside
    the role it dispatches (`_play_network_unsafe`), unless the task's own `when` holds it to a literal list within the
    ssh family (`_ssh_only`: the wiring's probe and re-check, I-92). Guards an allowlist widened to a connection that
    runs the role on the control node (its read, B1), and a controller-side task added to a role that a network plugin
    reaches (fold 1's read B, S4: the old check only asked whether the role included the CLI backend) or to the
    dispatching play itself (fold 2's read B, S5: a `command` or a handler beside the include ran on the controller for
    a network_cli host). The walk fails closed on a network role with handlers or a meta dependency, and on a vendor
    network module (`cisco.ios.ios_config`) that `_NETWORK_SAFE` does not name: the next network method may need it
    widened, never this test weakened (its N4)."""
    allow = _allowlists()
    by_play = {k.split(":")[0]: v for k, v in allow.items()}
    assert by_play["install-node-exporter.yml"] == ["ssh", "ansible.builtin.ssh", "paramiko_ssh",
                                                    "ansible.builtin.paramiko_ssh"], by_play
    assert by_play["wire-logging.yml"] == _WIRE_ALLOWLIST, by_play
    assert all(isinstance(v, list) or v == _WIRE_ALLOWLIST for v in allow.values()), allow
    for name, conns in allow.items():
        if isinstance(conns, list):
            assert conns and set(conns) <= set(_SSH_FAMILY), (name, conns)
    methods = [doc for _name, doc in _methods() if isinstance(doc, dict) and doc.get("wiring_role")]
    assert methods, "expected the push methods to name a wiring role"
    for m in methods:
        conns = m.get("wiring_connections")
        assert isinstance(conns, list) and conns, (m["wiring_role"], conns)
        assert not set(conns) & set(_LOCAL_FAMILY), (m["wiring_role"], conns)
        if set(conns) & set(_NETWORK_FAMILY):
            assert _network_unsafe(m["wiring_role"]) == [], (m["wiring_role"], conns)
        assert set(conns) <= set(_SSH_FAMILY + _NETWORK_FAMILY), (m["wiring_role"], conns)
    for rel, directory, play in _plays(PLAYBOOKS):
        conns = _allowlists().get("%s: %s" % (rel, play.get("name")))
        if conns is not None and not (isinstance(conns, list) and set(conns) <= set(_SSH_FAMILY)):
            assert _play_network_unsafe(play, directory) == [], (rel, _play_network_unsafe(play, directory))


def test_the_network_safety_walk_flags_a_controller_side_task_and_passes_a_network_one(tmp_path):
    """On planted roles, `_network_unsafe` passes a role of network-safe modules (bare, under core's or netcommon's
    collection, and in `action:` form), a task delegated to localhost, an include_role and an import_role into such a
    role with its `tasks_from` (bare, and with `.yml`), a role whose `meta/` declares no dependencies, and two roles
    that include each other. It flags a task of any other module, one in a block's `rescue` or `always`, one in a role
    that an include_role reaches, a templated role, a missing task file, a role in `action:` form, a task-file include,
    a role with `meta/` dependencies (in `main.yml`, `main.yaml`, `main.json` or a bare `main`, and in a JSON
    `__ansible_unsafe` wrapper, which 2.18 reads as what it wraps), one whose `meta/` sets `collections:`, one with a
    task that sets `collections:` (fold 4's read B, S2: a bare name then runs a collection's role), one with handlers, a
    `tasks_from: conf` whose bare `tasks/conf` (which ansible-core reads before `conf.yml`) runs a controller-side task,
    a role whose `main.yaml` runs one beside a safe bare `main` (every spelling that exists is read), and a role that
    `<playbooks>/roles/` shadows (and one a role it includes reaches), an include_role that carries `args:` or is
    written as an `action:` mapping (ansible-core takes a `tasks_from` from the `args`, or from the mapping's `module:`
    words), a `cli_config` or `cli_command` of another collection (its code, not netcommon's), and a JSON task file
    whose `__ansible_unsafe` wrapper, beside a safe task, includes a role that runs one (2.18 runs the wrapped task
    alone). Guards a walk that misses a controller-side task a network plugin would run on the runner (fold 1's read B,
    S4; fold 2's read B, S6; fold 3's read B, S3 and S5; fold 6's read B, S1, and read A, N4; fold 7's read B, S1; fold
    8's reads A and B, S1); the walk's 2.18 reading of a task file was held by no row (fold 9's read B, Q2: the wrapped
    task file)."""
    debug = {"ansible.builtin.debug": {"msg": "x"}}
    roles = {
        "safe": [{"ansible.builtin.assert": {"that": ["true"]}}, {"ansible.builtin.set_fact": {"a": 1}}, debug,
                 {"ansible.netcommon.cli_config": {"lines": ["x"]}}, {"cli_command": {"command": "x"}},
                 {"action": "ansible.netcommon.cli_command command=x"},
                 {"ansible.builtin.include_role": {"name": "back", "tasks_from": "conf"}},
                 {"ansible.builtin.import_role": {"name": "back", "tasks_from": "conf.yml"}},
                 {"name": "write", "ansible.builtin.copy": {"dest": "/tmp/x"}, "delegate_to": "localhost"}],
        "back": None,
        "meta_ok": [debug],
        "loop_a": [{"ansible.builtin.include_role": {"name": "loop_b"}}],
        "loop_b": [{"ansible.builtin.import_role": {"name": "loop_a"}}, debug],
        "other_module": [{"name": "edit", "ansible.builtin.lineinfile": {"path": "/etc/x", "line": "y"}}],
        "in_rescue": [{"block": [debug], "rescue": [{"ansible.builtin.template": {"src": "a", "dest": "b"}}]}],
        "in_always": [{"block": [debug], "always": [{"ansible.builtin.command": "id"}]}],
        "reaches": [{"ansible.builtin.include_role": {"name": "other_module"}}],
        "templated": [{"ansible.builtin.include_role": {"name": "{{ r }}"}}],
        "missing_file": [{"ansible.builtin.include_role": {"name": "back", "tasks_from": "nope"}}],
        "action_form": [{"action": "ansible.builtin.include_role name=back"}],
        "task_file": [{"ansible.builtin.include_tasks": "other.yml"}],
        "with_deps": [debug],
        "with_handlers": [debug],
        "meta_yaml": [debug],
        "meta_bare": [debug],
        "bare_from": [{"ansible.builtin.include_role": {"name": "back2", "tasks_from": "conf"}}],
        "back2": None,
        "meta_json": [debug],
        "meta_wrapped": [debug],
        "yaml_first": None,
        "meta_coll": [debug],
        "task_coll": [dict(debug, collections=["ns.coll"])],
        "args_from": [{"ansible.builtin.include_role": {"name": "back"}, "args": {"tasks_from": "nope"}}],
        "other_coll": [{"x.y.cli_config": {"lines": ["x"]}}],
        "other_coll_action": [{"action": "x.y.cli_command command=x"}],
        "action_args_from": [{"action": {"module": "ansible.builtin.include_role", "name": "back",
                                         "args": {"tasks_from": "nope"}}}],
        "action_words_from": [{"action": {"module": "ansible.builtin.include_role tasks_from=nope", "name": "back"}}],
    }
    for role, tasks in roles.items():
        (tmp_path / role / "tasks").mkdir(parents=True)
        (tmp_path / role / "tasks" / "main.yml").write_text(yaml.safe_dump(tasks or [debug]))
    (tmp_path / "back" / "tasks" / "conf.yml").write_text(yaml.safe_dump(
        [{"ansible.netcommon.cli_command": {"command": "show run"}}, {"ansible.builtin.meta": "noop"}]))
    (tmp_path / "meta_ok" / "meta").mkdir()
    (tmp_path / "meta_ok" / "meta" / "main.yml").write_text(yaml.safe_dump({"galaxy_info": {}, "dependencies": []}))
    (tmp_path / "with_deps" / "meta").mkdir()
    (tmp_path / "with_deps" / "meta" / "main.yml").write_text(yaml.safe_dump({"dependencies": ["back"]}))
    (tmp_path / "with_handlers" / "handlers").mkdir()
    (tmp_path / "with_handlers" / "handlers" / "main.yml").write_text(yaml.safe_dump([debug]))
    for role, meta in (("meta_yaml", "main.yaml"), ("meta_bare", "main")):
        (tmp_path / role / "meta").mkdir()
        (tmp_path / role / "meta" / meta).write_text(yaml.safe_dump({"dependencies": ["back"]}))
    (tmp_path / "meta_json" / "meta").mkdir()
    (tmp_path / "meta_json" / "meta" / "main.json").write_text('{"dependencies": ["back"]}')
    (tmp_path / "meta_wrapped" / "meta").mkdir()   # 2.18's JSON reader takes the wrapper as what it wraps
    (tmp_path / "meta_wrapped" / "meta" / "main.json").write_text('{"__ansible_unsafe": {"dependencies": ["back"]}}')
    (tmp_path / "meta_coll" / "meta").mkdir()
    (tmp_path / "meta_coll" / "meta" / "main.yml").write_text(yaml.safe_dump({"collections": ["ns.coll"]}))
    (tmp_path / "yaml_first" / "tasks" / "main.yml").unlink()
    (tmp_path / "yaml_first" / "tasks" / "main.yaml").write_text(yaml.safe_dump([{"ansible.builtin.command": "id"}]))
    (tmp_path / "yaml_first" / "tasks" / "main").write_text(yaml.safe_dump([debug]))
    (tmp_path / "back2" / "tasks" / "conf.yml").write_text(yaml.safe_dump([debug]))
    (tmp_path / "back2" / "tasks" / "conf").write_text(yaml.safe_dump([{"ansible.builtin.command": "id"}]))
    (tmp_path / "wrapped_sibling" / "tasks").mkdir(parents=True)   # 2.18 runs the wrapped task, not the debug beside it
    (tmp_path / "wrapped_sibling" / "tasks" / "main.yml").write_text(json.dumps([dict(debug, __ansible_unsafe={
        "ansible.builtin.include_role": {"name": "other_module"}})]))
    unsafe = {role: _network_unsafe(role, roles=str(tmp_path)) for role in roles}
    passed = sorted(role for role, found in unsafe.items() if not found)
    assert passed == ["back", "back2", "loop_a", "loop_b", "meta_ok", "safe"], unsafe
    assert unsafe["reaches"] == ["other_module/main: edit"], unsafe
    assert unsafe["missing_file"] == ["back/nope: not read"], unsafe
    assert unsafe["args_from"] == ["args_from/main: an include whose `args:` is not read"], unsafe
    assert unsafe["action_args_from"] == ["action_args_from/main: an include whose `args:` is not read"], unsafe
    assert unsafe["action_words_from"] == ["action_words_from/main: an include whose `args:` is not read"], unsafe
    assert unsafe["other_coll"] == ["other_coll/main: ['x.y.cli_config']"], unsafe
    assert _network_unsafe("wrapped_sibling", roles=str(tmp_path)) == ["other_module/main: edit"]
    (tmp_path / "pb" / "roles" / "safe" / "tasks").mkdir(parents=True)
    assert _network_unsafe("safe", roles=str(tmp_path), playbooks=str(tmp_path / "pb"))[0].startswith(
        "safe/main: shadowed")
    (tmp_path / "pb2" / "roles" / "back" / "tasks").mkdir(parents=True)     # a role `safe` includes, shadowed
    assert any(u.startswith("back/conf: shadowed") for u in
               _network_unsafe("safe", roles=str(tmp_path), playbooks=str(tmp_path / "pb2"))), "nested shadow"


def test_the_play_walk_flags_a_controller_side_task_beside_the_dispatched_role(tmp_path):
    """`_play_network_unsafe` passes a dispatching play whose own tasks are network-safe (the shipped refusal, a
    `debug`, a task delegated to localhost, and the `{{ role }}` include it leaves to each method's check), and flags a
    controller-side task after the refusal, one inside a block, a handler, one in an included task file, a fixed role it
    includes that is not network-safe (by an include or by `roles:`), a task file it cannot read (a templated path), one
    named by a mapping's `_raw_params`, an include or include_role that carries `args:` or is written as an `action:`
    mapping (a path or a `tasks_from` in the `args`, or in the mapping's `module:` words, which the walk does not read),
    a module of another collection that shares a network-safe name, a play, a task or a block that sets `collections:`,
    and a fixed role that a subdirectory play's own `roles/` shadows (included, or named in `roles:`). Guards the play
    walk being dropped or narrowed: wire-logging.yml passes it today, so only these rows show it can fail (fold 2's read
    B, S5: a `command` beside the include ran on the controller for a network_cli host; fold 3's read B, S3 and S5; fold
    4's read B, S2 and N5: a task's `collections:`, and a sub play's shadow, each ran a role the walk had not read on
    the controller; fold 5's read B, S1 and N1; fold 6's read B, S1: an `args:` import and an `args:` `tasks_from` each
    ran a `command` on the controller for a network_cli host; fold 6's read A, N4: another collection's `cli_config`
    counted as network-safe; fold 7's read B, S1; fold 8's reads A and B, S1: an `action:` mapping's `args` and its
    `module:` words each ran a `tasks_from` the walk had not read)."""
    base, roles = tmp_path / "playbooks", tmp_path / "roles"
    (base / "tasks").mkdir(parents=True)
    shutil.copy(os.path.join(PLAYBOOKS, REFUSAL), str(base / REFUSAL))
    (roles / "fixed" / "tasks").mkdir(parents=True)
    (roles / "fixed" / "tasks" / "main.yml").write_text(
        yaml.safe_dump([{"name": "edit", "ansible.builtin.command": "id"}]))
    (base / "tasks" / "extra.yml").write_text(yaml.safe_dump([{"name": "extra", "ansible.builtin.command": "id"}]))
    refuse = {"ansible.builtin.import_tasks": REFUSAL, "vars": {"refusal_connections": ["network_cli"]}}
    role = {"ansible.builtin.include_role": {"name": "{{ role }}"}}
    debug = {"name": "note", "ansible.builtin.debug": {"msg": "x"}}
    (roles / "ok" / "tasks").mkdir(parents=True)
    (roles / "ok" / "tasks" / "main.yml").write_text(yaml.safe_dump([debug]))
    (roles / "ok" / "tasks" / "evil.yml").write_text(yaml.safe_dump([{"name": "evil", "ansible.builtin.command": "x"}]))
    args_include = {"ansible.builtin.import_tasks": None, "args": {"_raw_params": "tasks/extra.yml"}}
    args_from = {"ansible.builtin.include_role": {"name": "ok"}, "args": {"tasks_from": "evil"}}
    action_args = {"action": {"module": "ansible.builtin.include_role", "name": "ok", "args": {"tasks_from": "evil"}}}
    action_words = {"action": {"module": "ansible.builtin.include_role tasks_from=evil", "name": "ok"}}
    plays = {
        "ok": dict(tasks=[refuse, debug, {"name": "kept", "ansible.builtin.copy": {"dest": "/tmp/x"},
                                          "delegate_to": "localhost"}, role]),
        "inline": dict(tasks=[refuse, {"name": "probe", "ansible.builtin.command": "id"}, role]),
        "in_block": dict(tasks=[refuse, {"block": [{"name": "wait", "ansible.builtin.wait_for": {"port": 22}}]}, role]),
        "handler": dict(tasks=[refuse, role],
                        handlers=[{"name": "record", "ansible.builtin.lineinfile": {"path": "x"}}]),
        "task_file": dict(tasks=[refuse, {"ansible.builtin.include_tasks": "tasks/extra.yml"}, role]),
        "fixed_role": dict(tasks=[refuse, {"ansible.builtin.include_role": {"name": "fixed"}}, role]),
        "unread": dict(tasks=[refuse, {"ansible.builtin.include_tasks": "{{ extra }}"}, role]),
        "roles_entry": dict(tasks=[refuse, role], roles=["fixed"]),
        "collections": dict(tasks=[refuse, role], collections=["ns.coll"]),
        "task_collections": dict(tasks=[refuse, dict(role, collections=["ns.coll"])]),
        "block_collections": dict(tasks=[refuse, {"block": [role], "collections": ["ns.coll"]}]),
        "raw_params": dict(tasks=[refuse, {"ansible.builtin.include_tasks": {"_raw_params": "tasks/extra.yml"}},
                                  role]),
        "args_include": dict(tasks=[refuse, args_include, role]),
        "args_from": dict(tasks=[refuse, args_from, role]),
        "action_args": dict(tasks=[refuse, action_args, role]),
        "action_words": dict(tasks=[refuse, action_words, role]),
        "other_coll": dict(tasks=[refuse, {"name": "push", "x.y.cli_config": {"lines": ["x"]}}, role]),
    }
    found = {name: _play_network_unsafe(dict(play, hosts="g"), str(base), str(roles)) for name, play in plays.items()}
    assert found == {"ok": [], "inline": ["probe"], "in_block": ["wait"], "handler": ["record"],
                     "task_file": ["extra"], "fixed_role": ["fixed/main: edit"],
                     "unread": ["not read: {'_unread': '{{ extra }}'}"], "roles_entry": ["fixed/main: edit"],
                     "collections": ["sets `collections:`"], "task_collections": ["sets `collections:`"],
                     "block_collections": ["sets `collections:`"], "raw_params": ["extra"],
                     "args_include": ["not read: {'_unread': %r}" % ("{{ action form or args: None }}",)],
                     "args_from": ["not read: %s" % (args_from,)], "other_coll": ["push"],
                     "action_args": ["not read: %s" % (action_args,)],
                     "action_words": ["not read: %s" % (action_words,)]}, found
    (base / "sub" / "roles" / "safe" / "tasks").mkdir(parents=True)   # a subdirectory play has its own roles/
    (roles / "safe" / "tasks").mkdir(parents=True)
    (roles / "safe" / "tasks" / "main.yml").write_text(yaml.safe_dump([debug]))
    sub = dict(hosts="g", tasks=[{"ansible.builtin.include_role": {"name": "safe"}}, role])
    assert any(u.startswith("safe/main: shadowed") for u in
               _play_network_unsafe(sub, str(base / "sub"), str(roles))), "sub shadow"
    assert any(u.startswith("safe/main: shadowed") for u in _play_network_unsafe(
        dict(hosts="g", roles=["safe"], tasks=[role]), str(base / "sub"), str(roles))), "sub roles: shadow"


@pytest.mark.parametrize("when, flagged", [
    ("ansible_connection in ['ssh', 'ansible.builtin.ssh', 'paramiko_ssh', 'ansible.builtin.paramiko_ssh']", False),
    (["ansible_connection in ['ssh']", "_probe is not unreachable"], False),
    ("ansible_connection in ['ssh', 'network_cli']", True),
    ("ansible_connection in []", True),
    ("ansible_connection in ssh_family", True),
    ("ansible_connection in ['ssh'] or true", True),
    ("ansible_connection not in ['network_cli']", True),
    (None, True),
])
def test_the_play_walk_passes_only_a_task_its_own_when_holds_to_a_literal_ssh_family(tmp_path, when, flagged):
    """In a planted play whose allowlist is a network plugin's, a `ping` beside the dispatched role passes the play walk
    when its own `when` holds, as the string or one ANDed item of a list, `ansible_connection in [...]` with a literal
    list within the ssh family; it is flagged when the list names a network plugin, is empty or is a variable (a
    block's, a task's or a fact's value outranks a play's), when the condition is or-ed or negated, when there is no
    `when`, and when the gate sits on its block rather than on the task. Guards I-92's gate: wire-logging.yml's probe
    and re-check run a `ping`, which over network_cli runs on the controller and answers for a device that is down, so
    the walk lets one through only where the host's frozen connection is of the ssh family; a gate widened to
    network_cli, or moved into a var, fails here and in the shipped play's row."""
    base, roles = tmp_path / "playbooks", tmp_path / "roles"
    (base / "tasks").mkdir(parents=True)
    roles.mkdir()
    shutil.copy(os.path.join(PLAYBOOKS, REFUSAL), str(base / REFUSAL))
    refuse = {"ansible.builtin.import_tasks": REFUSAL, "vars": {"refusal_connections": ["ssh", "network_cli"]}}
    role = {"ansible.builtin.include_role": {"name": "{{ role }}"}}
    ping = {"name": "probe", "ansible.builtin.ping": None}
    task = dict(ping, when=when) if when is not None else ping
    play = dict(hosts="g", tasks=[refuse, {"block": [task, role]}])
    assert _play_network_unsafe(play, str(base), str(roles)) == (["probe"] if flagged else []), when
    if when is not None and not flagged:
        gated_block = dict(hosts="g", tasks=[refuse, {"block": [ping, role], "when": when}])
        assert _play_network_unsafe(gated_block, str(base), str(roles)) == ["probe"], when


def test_the_scan_finds_a_planted_play_and_passes_a_refusing_one(tmp_path):
    """On planted playbooks the scan flags a group play with no refusal, with the refusal second, with a `when:`,
    `ignore_errors`, `tags` or an `ansible_connection` var on its import, with no allowlist on it, with `roles:` and the
    refusal in `tasks`, with implicit fact gathering (set, or left to its default under `pre_tasks`), with a play-level
    `ignore_errors`, with `setup` (as a key or in `action:` form), and with the refusal resolved from a subdirectory to
    a file that is not the shipped one. It reads a role included inside a block, its `rescue` and its `always`, in
    `pre_tasks` and `post_tasks`, inside an imported task file (under an `ansible.legacy.` key too, or named by a
    mapping's `_raw_params`), by a handler, by an `ansible.legacy.` key, in `action:` form, by `roles:` in dict form,
    with `apply:`, by a variable, and a role's task file imported by path; a task file included from a templated path,
    in `action:` form, from a path that names no file, from a path in `args:` (a play with no refusal that dispatches
    through it), or nested beside the file that includes it (with a decoy where the scan once looked), and one three
    deep that ansible-core resolves from the playbook's directory joined with its includer's path as written (with a
    decoy beside the includer); two task files that include each other; a `connection: local` play; a play that sets
    `device_role` itself (in its vars, a vars_prompt as a list or one mapping, a vars file, a role entry's vars, a
    task's vars, a `set_fact` as a mapping, `k=v` words (an `action:` mapping's `module:` words too), an `action:`
    form's `args` or behind a templated key, or an include_vars); a play that sets `ansible_connection` after the
    refusal (an include's vars, a `set_fact` as `k=v` words or a mapping, a role entry's param) or tags a role import; a
    play in a subdirectory; and an `import_playbook` of a file outside the scanned tree, of a templated path, of a path
    holding `$` or opening with `~`, of a collection playbook, of a `.json` or extension-less file, or of a file that is
    not there. It passes a play that opens with the refusal and its allowlist, the same under `pre_tasks` with `roles:`,
    a subdirectory play that imports the shipped refusal by its relative path, a play including `{{ device_role }}`, and
    a play on localhost. After the refusal it also flags a `set_fact` through `args:` (with a mapping or none, or a
    template: ansible-core renders it into the arguments), behind a templated key, or with a value that only mentions
    `refusal_connections` (not the freeze, word for word), or as `k=v` words behind a templated key or in `args:`;
    `module_defaults` on the play, a block or a role entry; a role entry's `vars` or tags; a tag or a `set_fact` inside
    an imported file; a `set_fact` behind a key under `{%` or `{#` (a mapping, or `k=v` words), or as the `args` of an
    `action:` form (a mapping, or words) or the `k=v` words of an `action:` mapping's `module:`; an `action:` that names
    its module by a template or glued to one (`{{ 'set_fact' }} ansible_connection=local`, `set_fact{{ '' }} …`,
    `set<tab>{{- '_fact' }} …`); an `ansible_`, templated, `{%` or `{#` loop or index variable; `meta:
    clear_host_errors` (as a key or in `action:` form); a delegated block, static import (of a role, of a task file, in
    `action:` form), include `apply:` or role entry; a `local_action:` import of a role (words, or a mapping); a
    templated include, one through `$KXV` (beside a decoy named so), one through `~` (beside a decoy under a literal
    `~`), a quoted one (beside a decoy named with the quotes), one whose mapping's `_raw_params` names a file that sets
    `ansible_connection` (alone, and with a `file` naming an empty one: ansible-core takes `_raw_params`), one whose
    mapping names its path neither way, and one whose path is in the task's `args:`; a task but a `set_fact` that
    carries `args:` (an `apply: {delegate_to: …}` on the dispatch, `meta`'s `clear_host_errors`) or is written as an
    `action:` or `local_action:` mapping (its `args`, or its `module:` words, name the role that runs: `include_role
    name=x` beside `name: "{{ device_role }}"`); a play, a task after the refusal, or a task file it includes, in a JSON
    `__ansible_unsafe` wrapper, which 2.18 reads as what it wraps; and a refusal imported through `$KXV/../` (a
    directory named so leads to the shipped file); an include through `link/../` (beside a decoy at the path the link
    leads to); an include inside an included file (which it still reads: the role the three-deep include reaches is
    among the play's roles); and an `import_playbook` through a symlinked directory and `..`. It passes, after the
    refusal, a `set_fact` of another name (a mapping, `k=v` words, through `args:`, and in `action:` form), a task of
    its own delegated to localhost, a loop variable of its own and another `meta`. Guards a scan that sees only the
    shapes the two real plays happen to have (its read, S1: A1-A18; fold 1's read B, S1 and S2: B1-B10, F9, and the
    rules no planted row held; fold 2's read B, S2 to S4: C1, C6b, C6c, C7, C30 to C33; fold 3's read A, S1, S2 and N4,
    and read B, S1, S2, S4 and S5; fold 4's mutant V1; fold 4's read A, S1, and read B, S1, N1 and N3; fold 5's re-aimed
    mutant W27; fold 5's read B, S1, S2, N1 and N3; fold 6's read B, S1: each new row ran a role on the controller for
    an ssh host, or would let a decoy stand in for the file that runs; fold 7's read B, S1; fold 8's read A, S1 and N2,
    and read B, S1 and S2: an `action:` mapping's `module:` words, and on 2.18 a wrapped `set_fact` in an included file,
    ran a role on the controller with every check green; fold 12's read B, S1: so did an `action:` naming `set_fact` by
    a template, after the refusal, on both cores, as one whose word a tab split from a `{{-` tag did: fold 13's read B,
    S1). It flags, too, a `set_fact` of ansible-core's own `_variable_params` after the refusal, as a key or in `args:`
    (fold 15's read A, S1: a templated one set `ansible_connection: local` on both cores), and of its `_raw_params`,
    the other name `_INTERNAL_ARGS` holds opaque."""
    base = tmp_path / "playbooks"
    (base / "tasks").mkdir(parents=True)
    (base / "sub" / "tasks").mkdir(parents=True)
    (tmp_path / "roles" / "node_exporter" / "tasks").mkdir(parents=True)
    (tmp_path / "roles" / "node_exporter" / "tasks" / "Debian.yml").write_text("[]\n")
    (tmp_path / "elsewhere").mkdir()
    shutil.copy(os.path.join(PLAYBOOKS, REFUSAL), str(base / REFUSAL))
    shutil.copy(os.path.join(PLAYBOOKS, REFUSAL), str(base / "sub" / REFUSAL))   # a decoy beside a sub play
    inner = [{"ansible.builtin.include_role": {"name": "x"}}]
    (base / "tasks" / "inner.yml").write_text(yaml.safe_dump(inner))
    (base / "tasks" / "nest-a.yml").write_text(yaml.safe_dump([{"ansible.builtin.import_tasks": "nest-b.yml"}]))
    (base / "tasks" / "nest-b.yml").write_text(yaml.safe_dump(inner))
    (base / "nest-b.yml").write_text("[]\n")                         # a decoy where the scan once looked (B7)
    (base / "tasks" / "deep-a.yml").write_text(yaml.safe_dump([{"ansible.builtin.import_tasks": "sub/deep-b.yml"}]))
    (base / "tasks" / "sub").mkdir()
    (base / "tasks" / "sub" / "deep-b.yml").write_text(yaml.safe_dump([{"ansible.builtin.import_tasks": "deep-c.yml"}]))
    (base / "tasks" / "sub" / "deep-c.yml").write_text("[]\n")       # beside its includer: what fold 2 read
    (base / "sub" / "deep-c.yml").write_text(yaml.safe_dump(inner))   # <base>/<dir of sub/deep-b.yml>: ansible's
    (base / "tasks" / "tagged.yml").write_text(yaml.safe_dump([{"ansible.builtin.debug": None, "tags": ["t"]}]))
    (base / "tasks" / "set-conn.yml").write_text(yaml.safe_dump([{"ansible.builtin.set_fact":
                                                                   {"ansible_connection": "local"}}]))
    (base / "tasks" / "cwd-a.yml").write_text(yaml.safe_dump([{"ansible.builtin.import_tasks": "cwd-b.yml"}]))
    (base / "cwd-b.yml").write_text("[]\n")   # where the scan looks; ansible-core tries <cwd>/tasks/cwd-b.yml first
    os.symlink("sub/tasks", str(base / "link"))
    (base / "tasks" / "cycle-a.yml").write_text(yaml.safe_dump([{"ansible.builtin.import_tasks": "cycle-b.yml"}]))
    (base / "tasks" / "cycle-b.yml").write_text(yaml.safe_dump([{"ansible.builtin.import_tasks": "cycle-a.yml"}]
                                                               + inner))
    (base / "lex.yml").write_text(yaml.safe_dump([{"ansible.builtin.set_fact": {"ansible_connection": "local"}}]))
    (base / "sub" / "lex.yml").write_text("[]\n")   # link/../lex.yml through the link: a decoy
    (base / "tasks" / "$KXV.yml").write_text("[]\n")   # a decoy named with the literal $KXV
    (base / "$KXV").mkdir()                               # `$KXV/../` reaches the shipped refusal, read literally
    (base / "~").mkdir()
    (base / "~" / "kx.yml").write_text("[]\n")              # a decoy under a literal ~
    (base / "'tasks").mkdir()
    (base / "'tasks" / "set-conn.yml'").write_text("[]\n")  # the quoted path, read literally
    allow = {"refusal_connections": ["ssh"]}
    refuse = {"name": "r", "ansible.builtin.import_tasks": REFUSAL, "vars": allow}
    role = {"ansible.builtin.include_role": {"name": "node_exporter"}}
    own = {"ansible.builtin.include_role": {"name": "{{ device_role }}"}}
    debug = {"ansible.builtin.debug": None}
    off = {"gather_facts": False}
    flagged = {
        "none.yml": dict(off, hosts="g", tasks=[role]),
        "second.yml": dict(off, hosts="g", tasks=[debug, refuse, role]),
        "when.yml": dict(off, hosts="g", tasks=[dict(refuse, when="false"), role]),
        "ignore.yml": dict(off, hosts="g", tasks=[dict(refuse, ignore_errors=True), role]),               # A2
        "tags.yml": dict(off, hosts="g", tasks=[dict(refuse, tags=["never"]), role]),                     # A3
        "conn-var.yml": dict(off, hosts="g", tasks=[dict(refuse, vars=dict(allow, ansible_connection="ssh")),
                                                    role]),                                               # A4
        "no-list.yml": dict(off, hosts="g", tasks=[{"ansible.builtin.import_tasks": REFUSAL}, role]),
        "facts.yml": {"hosts": "g", "gather_facts": True, "tasks": [refuse, role]},                       # A1
        "facts-pre.yml": {"hosts": "g", "roles": ["node_exporter"], "pre_tasks": [refuse]},                # A16
        "play-ignore.yml": dict(off, hosts="g", ignore_errors=True, tasks=[refuse, role]),                 # A5
        "setup.yml": dict(off, hosts="g", tasks=[refuse, {"ansible.builtin.setup": None}, role]),
        "action-setup.yml": dict(off, hosts="g", tasks=[refuse, {"action": "ansible.builtin.setup"}, role]),  # B3
        "roles.yml": dict(off, hosts="g", roles=["node_exporter"], tasks=[refuse]),
        "block.yml": dict(off, hosts="g", tasks=[{"block": [{"import_role": {"name": "y"}}]}]),
        "rescue.yml": dict(off, hosts="g", tasks=[{"block": [debug], "rescue": [role]}]),
        "always.yml": dict(off, hosts="g", tasks=[{"block": [debug], "always": [role]}]),
        "pre.yml": dict(off, hosts="g", pre_tasks=[role]),
        "post.yml": dict(off, hosts="g", post_tasks=[role]),
        "imported.yml": dict(off, hosts="g", tasks=[{"ansible.builtin.import_tasks": "tasks/inner.yml"}]),
        "imported-legacy.yml": dict(off, hosts="g", tasks=[{"ansible.legacy.import_tasks": "tasks/inner.yml"}]),
        "handler.yml": dict(off, hosts="g", tasks=[{"ansible.builtin.debug": {"msg": "x"}}],
                            handlers=[{"name": "h", "ansible.builtin.include_role": {"name": "z"}}]),
        "legacy-role.yml": dict(off, hosts="g", tasks=[{"ansible.legacy.include_role": {"name": "x"}}]),   # A6
        "action-role.yml": dict(off, hosts="g", tasks=[{"action": "ansible.builtin.include_role name=x"}]),  # B1
        "action-role-dict.yml": dict(off, hosts="g", tasks=[{"action": {"module": "ansible.builtin.include_role",
                                                                        "name": "x"}}]),                   # B2
        "roles-dict.yml": dict(off, hosts="g", roles=[{"role": "node_exporter"}]),                         # A11
        "apply.yml": dict(off, hosts="g", tasks=[{"ansible.builtin.include_role": {"name": "node_exporter",
                                                                         "apply": {"tags": ["t"]}}}]),    # A12
        "by-var.yml": dict(off, hosts="g", vars={"my_role": "{{ device_role }}"},
                           tasks=[{"ansible.builtin.include_role": {"name": "{{ my_role }}"}}]),           # A13
        "role-file.yml": dict(off, hosts="g",
                              tasks=[{"ansible.builtin.import_tasks": "../roles/node_exporter/tasks/Debian.yml"}]),
        "action-tasks.yml": dict(off, hosts="g",
                                 tasks=[{"action": "ansible.builtin.import_tasks tasks/inner.yml"}]),     # B5
        "templated-tasks.yml": dict(off, hosts="g", vars={"t": "tasks/inner.yml"},
                                    tasks=[{"ansible.builtin.include_tasks": "{{ t }}"}]),                 # B6
        "missing-tasks.yml": dict(off, hosts="g", tasks=[{"ansible.builtin.include_tasks": "tasks/missing.yml"}]),
        "nested.yml": dict(off, hosts="g", tasks=[{"ansible.builtin.import_tasks": "tasks/nest-a.yml"}]),   # B7
        "deep.yml": dict(off, hosts="g", tasks=[{"ansible.builtin.import_tasks": "tasks/deep-a.yml"}]),     # C1
        "prompt-map.yml": dict(off, hosts="g", vars_prompt={"name": "device_role", "default": "x"},
                               tasks=[own]),                                                               # C7
        "inc-conn.yml": dict(off, hosts="g", tasks=[refuse, dict(role, vars={"ansible_connection": "local"})]),
        "set-conn.yml": dict(off, hosts="g", tasks=[refuse, {"ansible.builtin.set_fact": "ansible_connection=local"},
                                                    role]),
        "set-conn-map.yml": dict(off, hosts="g", tasks=[refuse, {"ansible.builtin.set_fact":
                                                                 {"ansible_connection": "local"}}, role]),
        "set-conn-near-freeze.yml": dict(off, hosts="g", tasks=[refuse, {"ansible.builtin.set_fact": {
            "ansible_connection": "{{ 'local' if refusal_connections is defined else '' }}"}}, role]),
        "loop-var-conn.yml": dict(off, hosts="g", tasks=[refuse, dict(role, loop=["local"],
                                                        loop_control={"loop_var": "ansible_connection"})]),
        "role-param-conn.yml": dict(off, hosts="g", pre_tasks=[refuse],
                                    roles=[{"role": "node_exporter", "ansible_connection": "local"}]),   # C31
        "tagged-role.yml": dict(off, hosts="g", tasks=[refuse, {"ansible.builtin.import_role":
                                                                {"name": "node_exporter"}, "tags": ["ne"]}]),  # C32
        "cycle.yml": dict(off, hosts="g", tasks=[{"ansible.builtin.import_tasks": "tasks/cycle-a.yml"}]),
        "role-vars-conn.yml": dict(off, hosts="g", pre_tasks=[refuse],
                                   roles=[{"role": "node_exporter", "vars": {"ansible_connection": "local"}}]),
        "tagged-role-entry.yml": dict(off, hosts="g", pre_tasks=[refuse],
                                      roles=[{"role": "node_exporter", "tags": ["t"]}]),
        "tagged-nested.yml": dict(off, hosts="g", tasks=[refuse, {"ansible.builtin.import_tasks": "tasks/tagged.yml"},
                                                       role]),
        "set-conn-nested.yml": dict(off, hosts="g", tasks=[refuse, {"ansible.builtin.import_tasks":
                                                                    "tasks/set-conn.yml"}, role]),
        "set-args.yml": dict(off, hosts="g", tasks=[refuse, {"ansible.builtin.set_fact": {},
                                                          "args": {"ansible_connection": "local"}}, role]),
        "set-key-template.yml": dict(off, hosts="g", tasks=[refuse, {"ansible.builtin.set_fact":
                                                              {"{{ 'ansible_' ~ 'connection' }}": "local"}}, role]),
        "module-defaults.yml": dict(off, hosts="g", tasks=[refuse, role],
                                    module_defaults={"ansible.builtin.set_fact": {"ansible_connection": "local"}}),
        "nested-cwd.yml": dict(off, hosts="g", tasks=[{"ansible.builtin.import_tasks": "tasks/cwd-a.yml"}]),
        "set-args-none.yml": dict(off, hosts="g", tasks=[refuse, {"ansible.builtin.set_fact": None,
                                                               "args": {"ansible_connection": "local"}}, role]),
        "set-args-words.yml": dict(off, hosts="g", tasks=[refuse, {"ansible.builtin.set_fact": None,
                                                                "args": "ansible_connection=local"}, role]),
        "set-kv-template.yml": dict(off, hosts="g", tasks=[refuse, {"ansible.builtin.set_fact":
                                                                 "{{ 'ansible_' ~ 'connection' }}=local"}, role]),
        "set-args-template.yml": dict(off, hosts="g", tasks=[refuse, {"ansible.builtin.set_fact": None,
                                                                   "args": "{{ conn_args }}"}, role]),
        "set-variable-params.yml": dict(off, hosts="g", tasks=[refuse, {"ansible.builtin.set_fact": {
            "_variable_params": "{{ conn }}"}}, role]),   # fold 15's read A, S1
        "set-args-variable-params.yml": dict(off, hosts="g", tasks=[refuse, {"ansible.builtin.set_fact": None,
                                                                          "args": {"_variable_params": "{{ c }}"}},
                                                                  role]),
        "set-raw-params.yml": dict(off, hosts="g", tasks=[refuse, {"ansible.builtin.set_fact": None,
                                                                "args": {"_raw_params": "ansible_connection=local"}},
                                                            role]),
        "block-module-defaults.yml": dict(off, hosts="g", tasks=[refuse, {"block": [role], "module_defaults": {
            "ansible.builtin.set_fact": {"ansible_connection": "local"}}}]),
        "role-module-defaults.yml": dict(off, hosts="g", pre_tasks=[refuse], roles=[{"role": "node_exporter",
                                                                                   "module_defaults": {}}]),
        "index-var.yml": dict(off, hosts="g", tasks=[refuse, dict(role, loop=["x"],
                                                                  loop_control={"index_var": "ansible_become"})]),
        "loop-var-template.yml": dict(off, hosts="g", tasks=[refuse, dict(role, loop=["x"],
                                                                          loop_control={"loop_var": "{{ v }}"})]),
        "clear-errors.yml": dict(off, hosts="g", tasks=[refuse, {"ansible.builtin.meta": "clear_host_errors"}, role]),
        "clear-errors-action.yml": dict(off, hosts="g", tasks=[refuse, {"action": "meta clear_host_errors"}, role]),
        "delegate-block.yml": dict(off, hosts="g", tasks=[refuse, {"block": [role], "delegate_to": "localhost"}]),
        "delegate-apply.yml": dict(off, hosts="g", tasks=[refuse, {"ansible.builtin.include_role": {
            "name": "node_exporter", "apply": {"delegate_to": "localhost"}}}]),
        "delegate-import.yml": dict(off, hosts="g", tasks=[refuse, {"ansible.builtin.import_role":
                                                                    {"name": "node_exporter"},
                                                                    "delegate_to": "localhost"}]),
        "delegate-role-entry.yml": dict(off, hosts="g", pre_tasks=[refuse],
                                        roles=[{"role": "node_exporter", "delegate_to": "localhost"}]),
        "delegate-import-tasks.yml": dict(off, hosts="g", tasks=[refuse, {
            "ansible.builtin.import_tasks": "tasks/inner.yml", "delegate_to": "localhost"}]),
        "delegate-action.yml": dict(off, hosts="g", tasks=[refuse, {
            "action": "ansible.builtin.import_role name=node_exporter", "delegate_to": "localhost"}]),
        "lexical-include.yml": dict(off, hosts="g", tasks=[refuse, {
            "ansible.builtin.import_tasks": "link/../lex.yml"}, role]),
        "envvar-include.yml": dict(off, hosts="g", tasks=[refuse, {
            "ansible.builtin.import_tasks": "tasks/$KXV.yml"}, role]),
        "refusal-envvar.yml": dict(off, hosts="g", tasks=[dict(refuse, **{"ansible.builtin.import_tasks":
                                                                          "$KXV/../" + REFUSAL}), role]),
        "local-action-import.yml": dict(off, hosts="g", tasks=[refuse, {
            "local_action": "ansible.builtin.import_role name=node_exporter"}]),
        "local-action-import-dict.yml": dict(off, hosts="g", tasks=[refuse, {"local_action": {
            "module": "ansible.builtin.import_role", "name": "node_exporter"}}]),
        "set-key-block.yml": dict(off, hosts="g", tasks=[refuse, {"ansible.builtin.set_fact": {
            "{% if true %}ansible_connection{% endif %}": "local"}}, role]),
        "set-key-comment.yml": dict(off, hosts="g", tasks=[refuse, {"ansible.builtin.set_fact": {
            "{# c #}ansible_connection": "local"}}, role]),
        "set-kv-block.yml": dict(off, hosts="g", tasks=[refuse, {"ansible.builtin.set_fact":
                                                                 "{%if 1%}ansible_connection{%endif%}=local"}, role]),
        "set-nested-args.yml": dict(off, hosts="g", tasks=[refuse, {"action": {
            "module": "ansible.builtin.set_fact", "args": {"ansible_connection": "local"}}}, role]),
        "set-nested-args-words.yml": dict(off, hosts="g", tasks=[refuse, {
            "action": "ansible.builtin.set_fact args=ansible_connection=local"}, role]),
        "loop-var-block.yml": dict(off, hosts="g", tasks=[refuse, dict(role, loop=["x"], loop_control={
            "loop_var": "{% if true %}ansible_connection{% endif %}"})]),
        "loop-var-comment.yml": dict(off, hosts="g", tasks=[refuse, dict(role, loop=["x"], loop_control={
            "loop_var": "{# c #}ansible_connection"})]),
        "set-fact-templated.yml": dict(off, hosts="g", tasks=[{"ansible.builtin.set_fact": {
            "{{ 'device' ~ '_role' }}": "x"}}, own]),
        "raw-params-include.yml": dict(off, hosts="g", tasks=[refuse, {"ansible.builtin.import_tasks": {
            "_raw_params": "tasks/set-conn.yml"}}, role]),
        "raw-params-and-file.yml": dict(off, hosts="g", tasks=[refuse, {"ansible.builtin.import_tasks": {
            "_raw_params": "tasks/set-conn.yml", "file": "tasks/sub/deep-c.yml"}}, role]),
        "raw-params-dispatch.yml": dict(off, hosts="g", tasks=[{"ansible.builtin.import_tasks": {
            "_raw_params": "tasks/inner.yml"}}]),
        "set-action-template.yml": dict(off, hosts="g", tasks=[refuse, {   # fold 12's read B, S1
            "action": "{{ 'set_fact' }} ansible_connection=local"}, role]),
        "set-action-tab-template.yml": dict(off, hosts="g", tasks=[refuse, {   # fold 13's read B, S1
            "action": "set\t{{- '_fact' }} ansible_connection=local"}, role]),
        "set-action-glued.yml": dict(off, hosts="g", tasks=[refuse, {
            "action": "set_fact{{ '' }} ansible_connection=local"}, role]),
        "set-action-module-kv.yml": dict(off, hosts="g", tasks=[refuse, {"action": {
            "module": "ansible.builtin.set_fact ansible_connection=local"}}, role]),
        "device-role-module-kv.yml": dict(off, hosts="g", tasks=[{"action": {
            "module": "ansible.builtin.set_fact device_role=x"}}, own]),
        "device-role-args.yml": dict(off, hosts="g", tasks=[{"action": {
            "module": "ansible.builtin.set_fact", "args": {"device_role": "x"}}}, own]),
        "tilde-include.yml": dict(off, hosts="g", tasks=[refuse, {"ansible.builtin.import_tasks": "~/kx.yml"},
                                                         role]),
        "quoted-include.yml": dict(off, hosts="g", tasks=[refuse, {
            "ansible.builtin.import_tasks": "'tasks/set-conn.yml'"}, role]),
        "no-path-include.yml": dict(off, hosts="g", tasks=[refuse, {"ansible.builtin.import_tasks": {
            "path": "tasks/set-conn.yml"}}, role]),
        "args-include.yml": dict(off, hosts="g", tasks=[refuse, {"ansible.builtin.import_tasks": None,
                                                              "args": {"_raw_params": "tasks/set-conn.yml"}}, role]),
        "args-dispatch.yml": dict(off, hosts="g", tasks=[{"ansible.builtin.import_tasks": None,
                                                          "args": {"_raw_params": "tasks/inner.yml"}}]),
        "args-apply-delegate.yml": dict(off, hosts="g", tasks=[refuse, {"ansible.builtin.include_role": {
            "name": "node_exporter"}, "args": {"apply": {"delegate_to": "localhost"}}}]),
        "args-clear-errors.yml": dict(off, hosts="g", tasks=[refuse, {"ansible.builtin.meta": None, "args": {
            "_raw_params": "clear_host_errors"}}, role]),
        "args-action-apply.yml": dict(off, hosts="g", tasks=[refuse, {"action": {   # fold 7's read B, S1
            "module": "ansible.builtin.include_role", "name": "node_exporter",
            "args": {"apply": {"delegate_to": "localhost"}}}}]),
        "args-action-name.yml": dict(off, hosts="g", tasks=[{"action": {   # the args' name is the one that runs
            "module": "ansible.builtin.include_role", "name": "{{ device_role }}",
            "args": {"name": "node_exporter"}}}]),
        "args-action-words.yml": dict(off, hosts="g", tasks=[{"action": {   # the module words' name runs
            "module": "ansible.builtin.include_role name=node_exporter", "name": "{{ device_role }}"}}]),
        "words-local-import.yml": dict(off, hosts="g", tasks=[{"local_action": {   # fold 8's read A, S1 and N2
            "module": "ansible.builtin.import_role name=node_exporter", "name": "{{ device_role }}"}}]),
        "words-action-from.yml": dict(off, hosts="g", tasks=[refuse, {"action": {   # the words' `tasks_from` runs
            "module": "ansible.builtin.include_role tasks_from=main", "name": "node_exporter"}}]),
        "mapping-bare.yml": dict(off, hosts="g", tasks=[refuse, {"action": {   # no mapping form is read, words or none
            "module": "ansible.builtin.include_role", "name": "node_exporter"}}]),
        "unread-after.yml": dict(off, hosts="g", vars={"t": "tasks/inner.yml"},
                                 tasks=[refuse, {"ansible.builtin.include_tasks": "{{ t }}"}, role]),
        "connection-local.yml": dict(off, hosts="g", connection="local", tasks=[role]),                    # A7
        "sets-device-role.yml": dict(off, hosts="g", vars={"device_role": "node_exporter"}, tasks=[own]),  # A14
        "prompt.yml": dict(off, hosts="g", vars_prompt=[{"name": "device_role", "default": "x"}],
                           tasks=[own]),                                                                   # B8
        "vars-files.yml": dict(off, hosts="g", vars_files=["v.yml"], tasks=[own]),
        "role-entry-vars.yml": dict(off, hosts="g", roles=[{"role": "{{ device_role }}",
                                                            "vars": {"device_role": "x"}}]),
        "task-vars.yml": dict(off, hosts="g", tasks=[dict(own, vars={"device_role": "x"})]),
        "set-fact.yml": dict(off, hosts="g", tasks=[{"ansible.builtin.set_fact": {"device_role": "x"}}, own]),
        "set-fact-kv.yml": dict(off, hosts="g", tasks=[{"ansible.builtin.set_fact": "device_role=x"}, own]),  # B9
        "include-vars.yml": dict(off, hosts="g", tasks=[{"ansible.builtin.include_vars": "v.yml"}, own]),
        "sub/plain.yml": dict(off, hosts="g", tasks=[role]),                                                # A9
        "sub/decoy.yml": dict(off, hosts="g", tasks=[refuse, role]),                                        # A18
    }
    passed = {
        "sub/ok-sub.yml": dict(off, hosts="g", tasks=[dict(refuse, **{"ansible.builtin.import_tasks":
                                                                      "../" + REFUSAL}), role]),
        "ok.yml": dict(off, hosts="g", tasks=[refuse, role]),
        "ok-pre.yml": dict(off, hosts="g", roles=["node_exporter"], pre_tasks=[refuse]),
        "ok-after.yml": dict(off, hosts="g", tasks=[
            refuse, {"ansible.builtin.set_fact": {"_seen": "{{ ansible_connection }}"}},
            {"action": "ansible.builtin.set_fact _seen2=x"}, {"ansible.builtin.set_fact": "_seen3=x"},
            {"ansible.builtin.set_fact": None, "args": {"_seen4": "x"}},   # the one task `args:` may sit on
            {"ansible.builtin.debug": {"msg": "x"}, "delegate_to": "localhost"},
            dict(role, loop=["x"], loop_control={"loop_var": "_item", "index_var": "_i"}),
            {"ansible.builtin.meta": "flush_handlers"}]),
        "own-role.yml": {"hosts": "all", "tasks": [own]},
        "control.yml": {"hosts": "localhost", "tasks": [role]},
    }
    for name, play in {**flagged, **passed}.items():
        (base / name).write_text(yaml.safe_dump([dict(play, name=name)]))
    wrapped = {   # 2.18 reads `{"__ansible_unsafe": x}` in a JSON file as `x` (fold 8's read B, S2)
        "json-unsafe-play.yml": [{"__ansible_unsafe": dict(off, name="json-unsafe-play.yml", hosts="g",
                                                           tasks=[role])}],
        "json-unsafe-after.yml": [dict(off, name="json-unsafe-after.yml", hosts="g", tasks=[
            refuse, {"__ansible_unsafe": {"ansible.builtin.set_fact": {"ansible_connection": "local"}}}, role])],
        "json-unsafe-include.yml": [dict(off, name="json-unsafe-include.yml", hosts="g", tasks=[   # every gate green
            refuse, {"ansible.builtin.include_tasks": "tasks/wrapped.json.yml"}, role])]}
    for name, doc in wrapped.items():
        (base / name).write_text(json.dumps(doc))
    (base / "tasks" / "wrapped.json.yml").write_text(json.dumps([{"name": "Named", "__ansible_unsafe": {
        "ansible.builtin.set_fact": {"ansible_connection": "local"}}}]))
    flagged.update(dict.fromkeys(wrapped))   # each is refused, and each dispatches
    (tmp_path / "elsewhere" / "outside.yml").write_text(yaml.safe_dump([dict(off, hosts="g", tasks=[role])]))
    imports = {"imports-outside.yml": "../elsewhere/outside.yml",                                          # A10
               "imports-templated.yml": "{{ pb }}", "imports-fqcn.yml": "ns.coll.pb",                       # B10
               "imports-json.yml": "plays.json", "imports-bare.yml": "plays",                               # C6b/c
               "imports-missing.yml": "absent.yml", "imports-through-link.yml": "link/../../ok.yml",
               "imports-envvar.yml": "$KXV.yml", "imports-tilde.yml": "~/kx.yml"}
    (base / "$KXV.yml").write_text("[]\n")   # a decoy named with the literal $KXV
    (base / "plays.json").write_text("[]\n")
    (base / "plays").write_text("[]\n")
    for name, ref in imports.items():
        (base / name).write_text(yaml.safe_dump([{"ansible.builtin.import_playbook": ref}]))
    missing, dispatching = group_dispatch_without_refusal(str(base))
    assert sorted({m.split(":")[0] for m in missing}) == sorted(set(flagged) | set(imports)), missing
    deep = yaml.safe_load((base / "deep.yml").read_text())[0]          # read as well as counted unread
    assert "x" in _roles(deep, str(base), _roles_dir(str(base))), _roles(deep, str(base), _roles_dir(str(base)))
    assert sorted(d.split(":")[0] for d in dispatching) == sorted(set(flagged) | {"ok.yml", "ok-pre.yml",
                                                                                   "ok-after.yml", "sub/ok-sub.yml"})


@pytest.mark.parametrize("name,data", [
    ("utf16.yml", "- hosts: g\n  tasks: []\n".encode("utf-16")),
    ("latin1.yml", b"- hosts: g\n  name: caf\xe9\n  tasks: []\n"),
    ("nul.yml", b"- hosts: g\n  tasks: []\n\x00\n"),
    ("two-docs.yml", b"- hosts: g\n  tasks: []\n---\n- hosts: h\n  tasks: []\n"),
    ("tagged.yml", b"- hosts: g\n  vars: {x: !unsafe '{{ y }}'}\n  tasks: []\n"),
    ("deep.yml", "".join("  " * i + "a:\n" for i in range(600)).encode() + b"  " * 600 + b"1\n"),
], ids=["utf-16", "not-utf-8", "nul", "several-documents", "a-tag", "nested-600-deep"])
def test_a_playbook_file_the_scan_cannot_read_fails_it_by_name(tmp_path, name, data):
    """A file under ansible/playbooks/ that `_read` cannot parse (UTF-16, a byte that is not UTF-8, a NUL, several
    documents, a tag the safe loader does not know, a block nested 600 deep) fails the dispatch scan and the
    allowlist reader with `_Unreadable`, which names the file, as a playbook and as a task file a play includes; a
    JSON play holding a byte that is not UTF-8 inside a string is read, as 2.18 reads it. Guards the two tests that
    read the tree through `_read` ending in a traceback on each (fold 12's read B, N1): the tagged one is a play
    ansible-core runs, so the scan must fail on it, never skip it."""
    base = tmp_path / "playbooks"
    (base / "tasks").mkdir(parents=True)
    (base / name).write_bytes(data)
    for scan in (group_dispatch_without_refusal, _allowlists):
        with pytest.raises(_Unreadable, match=re.escape(name) + r": a file the scan cannot read \("):
            scan(str(base))
    (base / name).rename(base / "tasks" / name)
    (base / "play.yml").write_text(yaml.safe_dump([{"hosts": "g", "tasks": [
        {"ansible.builtin.include_tasks": "tasks/" + name}, {"ansible.builtin.include_role": {"name": "x"}}]}]))
    for scan in (group_dispatch_without_refusal, _allowlists):
        with pytest.raises(_Unreadable, match=re.escape(name) + r": a file the scan cannot read \("):
            scan(str(base))
    (base / "tasks" / name).unlink()
    (base / "json.yml").write_bytes(b'[{"hosts": "g", "name": "caf\xe9", "tasks": []}]')
    assert [p.get("hosts") for _rel, _d, p in _plays(str(base)) if _rel == "json.yml"] == ["g"]
