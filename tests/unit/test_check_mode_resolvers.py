"""A read the control node's plays make for a later task runs under `--check` too, or says why it need not.

WHY (the failure this guards). A `command` / `shell` task is SKIPPED under `--check`, and a skipped task's register
is not undefined: it holds an empty `stdout` and a zero `rc`. So a resolver whose result a later task reads does not
fail where it is skipped — the mandated `--check --diff` dry-run goes wrong further on, at the task that reads it.
`_secrets_dir` went empty and the `.env` render died (live-caught 2026-06-21); the published-image digests
(`_img_env`) read as none, and `kontroll check` stopped at the fail-closed "no recorded digest" assert. Both were
fixed by `check_mode: false`. The guard before this one was a hand list of register names, which claimed to catch
"a new SOPS-feeding resolver added without it" and did not: `_img_env` was added without it, and without a row.

The rule reads the plays instead. Its scope is these tasks the control node runs:
- every task of a play under ansible/playbooks/ whose `hosts:` is the control node, or that sets the
  `ansible_connection: local` play var (a play var outranks a host's own), less a task delegated to anything but
  `localhost` or `127.0.0.1`. The control node is `localhost` or `127.0.0.1`: alone, as a list, or a string of them
  joined by `,` or `:`; a templated `hosts:` is read by its `default('…')` (or `d('…')`) string literals, and one
  with none is not the control node. And each task of any other play delegated to `localhost` or `127.0.0.1` (on
  itself or on a block around it);
- every task file such a play imports or includes by a literal path, and the files those import in turn;
- the tasks and handlers of every role such a play applies by a literal name (`include_role`, `import_role`,
  `roles:`).
There, a `command` / `shell` / `raw` task that declares itself read-only (`changed_when: false`) and whose
`register:` is named anywhere else must carry `check_mode: false` (on itself or on a block around it), or have a
row in `_EXEMPT` that says why an empty value in a dry-run is right. "Anywhere else" is:
- a YAML file under ansible/ (a play, a task file, a role), read through its parsed keys and values, so a comment is
  gone and a block scalar is whole (a `#` line in one is output Jinja still renders). A `register:` value is not a
  read, nor is a `name:` value (a task's, or a module parameter's) that is a string holding no `{{` or `{%` (a
  list or a mapping under `name:` is read). A file the parser refuses is read by lines, as a template is;
- a Jinja template in the tree (`.j2`, `.jinja2`), read by lines: every line counts but a `#` line, a `register:`
  line or a `name:` line that holds no `{{` or `{%`.
Every other mention counts. For the flag, that errs on the safe side. For the stale check it does not: an `_EXEMPT`
row stays alive while its register is named anywhere the corpus reads (a var of the same name, a template's text),
so a stale row fails only once that name is gone from everywhere under ansible/ and every template.

What the scope does not reach:
- `local_action:`; a role pulled in by another role's `meta/main.yml` `dependencies:` or named by its FQCN;
- a role applied both by a control-node play and by a device play (in scope, so the ban below spares it); an
  api-backend role, which its backend runs with `ansible_connection: local` (out of scope, so the ban holds it as a
  device role; none has a command task today);
- the `connection: local` keyword, on a play, block or task: a host's own `ansible_connection` (its backend's and
  group_vars') overrides it, as `_render-ssh-keys.yml` records, so such a task runs where its hosts are. Nor a task's
  or block's own `vars: {ansible_connection: local}`;
- a templated `delegate_to`, which is taken as a device: were it flagged, its advice (`check_mode: false`) would
  run a device read for real in the dry-run. So a resolver delegated to `"{{ inventory_hostname }}"` from a local
  play, or to `"{{ 'localhost' }}"` from a device play, is missed;
- a templated `hosts:` read by its default alone. One that defaults to `localhost` is taken as the control node,
  though an `-e` may point it at devices, and a forced read in its roles then escapes the ban below. A list default
  (`default(['localhost'])`) is read as a device play, and a literal joined to the template
  (`"{{ a | default('localhost') }},switches"`) is not read. None of these is on the tree;
- the `ansible_connection` play var set by `vars_files`, by a role's own `vars/`, or as `ansible.builtin.local`.

Every other role is out of scope by rule: it runs against a device, where forcing a read to run under `--check`
would execute the device command for real, so none of its tasks may carry `check_mode: false`.
"""
import os
import re

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_COMMANDS = ("ansible.builtin.command", "command", "ansible.builtin.shell", "shell", "ansible.builtin.raw", "raw")
_IMPORTS = ("ansible.builtin.import_tasks", "import_tasks", "ansible.builtin.include_tasks", "include_tasks")
_ROLES = ("ansible.builtin.include_role", "include_role", "ansible.builtin.import_role", "import_role")
_LOCAL = ("localhost", "127.0.0.1")
# A block's keyword that its tasks inherit, as far as the rule reads them.
_INHERITED = ("check_mode", "delegate_to")

# (file, register) → why the read stays skipped in a dry-run. Every row is checked to still name such a read.
_EXEMPT = {
    ("bootstrap.yml", "age_pub"): "the control key is minted by the task before it, which a dry-run skips on a fresh "
                                  "node: forced to run, the read fails on the absent file. Only the summary line "
                                  "prints it",
    ("bootstrap.yml", "ansible_pub"): "the same, for the ansible SSH key the task before it mints",
    ("deploy-stack.yml", "compose_ps"): "the `ps` after the bring-up, for display only; a dry-run brings nothing up",
}


def _is_false(value):
    """Whether a `changed_when` / `check_mode` value reads as false: YAML's `false`, or the string `"false"`."""
    return str(value).strip().lower() == "false"


def _walk(tasks, inherited=None):
    """Every leaf task, recursing into block/rescue/always; each carries the `check_mode` and `delegate_to` of the
    blocks around it unless it sets its own."""
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        here = dict(inherited or {}, **{k: task[k] for k in _INHERITED if k in task})
        if any(isinstance(task.get(k), list) for k in ("block", "rescue", "always")):
            for key in ("block", "rescue", "always"):
                yield from _walk(task.get(key), here)
        else:
            yield dict(here, **task)


def _load(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _tasks_of(path):
    doc = yaml.safe_load(_load(path))
    return list(_walk(doc)) if isinstance(doc, list) else []


def _is_control_node(play):
    """Whether every task of `play` runs on the control node: it sets the `ansible_connection: local` play var, or its
    `hosts:` names only `localhost` / `127.0.0.1`. A templated `hosts:` is read by its `default('…')` (or `d('…')`)
    literals: one that only mentions `localhost` (`hostvars['localhost']`, a `difference(['localhost'])`) is a device
    play."""
    hosts = play.get("hosts")
    play_vars = play.get("vars") if isinstance(play.get("vars"), dict) else {}
    if play_vars.get("ansible_connection") == "local":
        return True
    if isinstance(hosts, str) and ("{{" in hosts or "{%" in hosts):
        hosts = ",".join(re.findall(r"\b(?:default|d)\(\s*['\"]([^'\"]*)['\"]", hosts))
        if not hosts:
            return False
    if isinstance(hosts, str):
        hosts = [h.strip() for h in re.split(r"[,:]", hosts)]
    return all(h in _LOCAL for h in (hosts if isinstance(hosts, list) else [hosts]))


def _on_control_node(task, whole):
    """Whether `task` (with its blocks' keywords) runs on the control node, in a play that does (`whole`) or not. A
    `delegate_to` decides it: `localhost` or `127.0.0.1` in, anything else (a templated one too) out."""
    if task.get("delegate_to") is not None:
        return task["delegate_to"] in _LOCAL
    return whole


def _role_files(roles_dir, name):
    out = []
    for sub in ("tasks", "handlers"):
        folder = os.path.join(roles_dir, name, sub)
        if os.path.isdir(folder):
            out += [os.path.join(folder, f) for f in sorted(os.listdir(folder)) if f.endswith((".yml", ".yaml"))]
    return out


def control_node_task_files(root):
    """{path: [the leaf tasks in it the control node runs]} over ansible/playbooks/ and what its control-node plays
    import, include or apply (the scope the module docstring names)."""
    playbooks_dir = os.path.join(root, "ansible", "playbooks")
    roles_dir = os.path.join(root, "ansible", "roles")
    scoped = {}

    def add(path, tasks):
        scoped.setdefault(path, []).extend(tasks)
        for task in tasks:
            for key in _IMPORTS:
                target = task.get(key)
                target = target.get("file") if isinstance(target, dict) else target
                if isinstance(target, str) and "{{" not in target:
                    imported = os.path.normpath(os.path.join(os.path.dirname(path), target))
                    if imported not in scoped:
                        add(imported, _tasks_of(imported))
            for key in _ROLES:
                spec = task.get(key)
                name = spec.get("name") if isinstance(spec, dict) else None
                if isinstance(name, str) and "{{" not in name:
                    for role_file in _role_files(roles_dir, name):
                        if role_file not in scoped:
                            add(role_file, _tasks_of(role_file))

    for name in sorted(os.listdir(playbooks_dir)):
        if not name.endswith(".yml"):
            continue
        path = os.path.join(playbooks_dir, name)
        doc = yaml.safe_load(_load(path))
        tasks = []
        for play in doc if isinstance(doc, list) else []:
            if not isinstance(play, dict):
                continue
            whole = _is_control_node(play)
            for section in ("pre_tasks", "tasks", "post_tasks", "handlers"):
                tasks += [t for t in _walk(play.get(section)) if _on_control_node(t, whole)]
            for role in (play.get("roles") or []) if whole else []:
                role_name = role if isinstance(role, str) else (role.get("role") or role.get("name"))
                for role_file in _role_files(roles_dir, role_name):
                    if role_file not in scoped:
                        add(role_file, _tasks_of(role_file))
        if tasks:
            add(path, tasks)
    return scoped


# Template lines that name a register without reading it, unless they hold Jinja: a `register:` or `name:` line.
_NOT_A_READ = re.compile(r"^\s*(?:-\s+)?(?:register|name):")


def _templates(value):
    return isinstance(value, str) and ("{{" in value or "{%" in value)


def _is_read_line(line):
    """Whether a template's `line` can read a register: one that holds a `{{` or a `{%` (a `#` line too: in a
    template it is output Jinja still renders), or one that is neither a `#` line nor a line of `_NOT_A_READ`."""
    if _templates(line):
        return True
    return not (line.lstrip().startswith("#") or _NOT_A_READ.match(line))


def _scalars(node):
    """Every key and value a parsed YAML document holds, a block scalar whole, less the values that name a register
    without reading it: a `register:` value, and a `name:` value that is a string holding no Jinja (a list or a
    mapping under `name:`, a package list, is walked)."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield str(key)
            if key == "register" or key == "name" and isinstance(value, str) and not _templates(value):
                continue
            yield from _scalars(value)
    elif isinstance(node, list):
        for item in node:
            yield from _scalars(item)
    elif node is not None:
        yield str(node)


def _corpus(root):
    """The text of every file a register can be read from: each YAML file under ansible/, as `_scalars` reads it
    (by lines, as a template, where the parser refuses it), and each Jinja template in the tree, the lines
    `_is_read_line` refuses dropped. Installed collections (`ansible_collections/`, which a bootstrapped box holds
    under ansible/) are not the repository's own reads."""
    texts = []
    for top, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", "node_modules", "ansible_collections")]
        rel = os.path.relpath(top, root).replace(os.sep, "/")
        for f in files:
            text = None
            if f.endswith((".yml", ".yaml")) and (rel + "/").startswith("ansible/"):
                text = _load(os.path.join(top, f))
                try:
                    texts.append("\n".join(s for doc in yaml.safe_load_all(text) for s in _scalars(doc)))
                    continue
                except yaml.YAMLError:
                    pass
            elif f.endswith((".j2", ".jinja2")):
                text = _load(os.path.join(top, f))
            if text is not None:
                texts.append("\n".join(line for line in text.splitlines() if _is_read_line(line)))
    return texts


def unforced_reads(root):
    """{(file name, register)} of every control-node read-only command whose register is read elsewhere and which
    does not carry `check_mode: false`."""
    corpus = _corpus(root)
    found = set()
    for path, tasks in control_node_task_files(root).items():
        for task in tasks:
            register = task.get("register")
            if not register or not any(k in task for k in _COMMANDS) or not _is_false(task.get("changed_when")):
                continue
            word = re.compile(r"\b%s\b" % re.escape(register))
            reads = sum(len(word.findall(text)) for text in corpus)        # the `register:` lines are not in it
            if reads > 0 and not _is_false(task.get("check_mode")):
                found.add((os.path.basename(path), register))
    return found


def forced_device_reads(root):
    """["<path>: <task name>"] for every `command` / `shell` / `raw` task that carries `check_mode: false` (on itself
    or a block around it) in a role no control-node play applies — `.role-template` included."""
    roles_dir = os.path.join(root, "ansible", "roles")
    control = {os.path.normpath(p) for p in control_node_task_files(root)}
    offenders = []
    for top, dirs, files in os.walk(roles_dir):
        dirs.sort()
        for name in sorted(f for f in files if f.endswith((".yml", ".yaml"))):
            path = os.path.normpath(os.path.join(top, name))
            if path in control:
                continue
            try:
                tasks = _tasks_of(path)
            except yaml.YAMLError:
                continue
            for task in tasks:
                if any(k in task for k in _COMMANDS) and _is_false(task.get("check_mode")):
                    offenders.append("%s: %s" % (os.path.relpath(path, root).replace(os.sep, "/"), task.get("name")))
    return offenders


def test_every_control_node_read_runs_in_check_mode_or_is_exempted():
    """Every read-only command the control node runs, registers and reads again carries `check_mode: false`, or has
    an `_EXEMPT` row; and every row still names such a read. Guards the next `_img_env` — a resolver whose empty
    value silently changes the mandated dry-run — and an exemption left behind after its read was forced, or
    removed, where it would excuse the next read of that name."""
    found = unforced_reads(ROOT)
    assert found - set(_EXEMPT) == set(), "add `check_mode: false`, or an _EXEMPT row that says why: %r" % sorted(
        found - set(_EXEMPT))
    assert set(_EXEMPT) - found == set(), "stale _EXEMPT rows: %r" % sorted(set(_EXEMPT) - found)


def test_the_resolvers_that_feed_the_render_are_found():
    """The detector's scope reaches the resolvers the `.env` render and the canonical play depend on, and the role
    deploy-stack runs on the control node: removing `check_mode: false` from any of them would be reported. Guards
    a scope that silently stopped reading deploy-stack.yml, local-canonical.yml, the task file deploy-stack imports
    or the roles it applies — the sweep would then pass on nothing."""
    scoped = control_node_task_files(ROOT)
    names = {os.path.relpath(p, ROOT).replace(os.sep, "/") for p in scoped}
    assert {"ansible/playbooks/deploy-stack.yml", "ansible/playbooks/local-canonical.yml",
            "ansible/playbooks/bootstrap.yml", "ansible/playbooks/tasks/content-clone.yml",
            "ansible/roles/logging_journald_receiver/tasks/main.yml"} <= names, sorted(names)
    forced = {(os.path.basename(p), t.get("register")) for p, tasks in scoped.items() for t in tasks
              if _is_false(t.get("check_mode")) and _is_false(t.get("changed_when"))}
    for read in (("deploy-stack.yml", "_secrets_dir_resolved"), ("deploy-stack.yml", "_img_env"),
                 ("content-clone.yml", "_clone_foreign")):
        assert read in forced, read


def test_no_device_role_read_is_forced_to_run_in_check_mode():
    """No `command` / `shell` / `raw` task of a role no control-node play applies carries `check_mode: false`. Such
    a role runs against a device, so forcing a read to run under `--check` executes the device command for real
    during the dry-run meant to change nothing. Guards the resolver fix being copied onto a device read."""
    assert forced_device_reads(ROOT) == []


def _read(name, register, extra=""):
    return ("    - name: %s\n      ansible.builtin.command: echo r\n      register: %s\n      changed_when: false%s\n"
            % (name, register, extra))


def _use(register):
    return "    - ansible.builtin.debug:\n        msg: \"{{ %s.stdout }}\"\n" % register


_PLANTED = ("""- hosts: localhost
  vars:
    _val: "{{ _probe.stdout }}"
  tasks:
    - name: a resolver
      ansible.builtin.command: cat /etc/hostname
      register: _probe
      changed_when: false%s
    - block:
        - name: a nested resolver
          ansible.builtin.shell: echo x
          register: _nested
          changed_when: false
        - ansible.builtin.debug:
            msg: "{{ _nested.stdout }}"
    - check_mode: false
      block:
        - name: a resolver its block forces
          ansible.builtin.command: echo y
          register: _block_forced
          changed_when: false
        - ansible.builtin.debug:
            msg: "{{ _block_forced.stdout }}"
""" + _read("read once, never used", "_unused") + _read("named only in a comment", "_commented") + """\
    # _commented is never read: this line names it
    - name: a writer
      ansible.builtin.command: touch /tmp/x
      register: _writer
    - ansible.builtin.debug:
        msg: "{{ _writer.rc }}"
""" + _read("read only in an imported file", "_via_import") + _read("read only in a template", "_in_template")
    + _read("read-only as a string", "_string_false").replace("changed_when: false", "changed_when: \"false\"")
    + """    - ansible.builtin.import_tasks: tasks/more.yml
""" + _use("_imported") + _use("_deeper") + _use("_string_false") + """    - ansible.builtin.include_role:
        name: ctl
- hosts: [localhost, 127.0.0.1]
  roles: [ctlr]
  tasks:
""" + _read("a resolver in a listed-host play", "_listed") + _use("_listed") + """- hosts: all
  vars:
    ansible_connection: local
  tasks:
""" + _read("a resolver in a local-connection play", "_conn_local") + _use("_conn_local") + """- hosts: all
  connection: local
  roles: [dev]
  tasks:
""" + _read("a read in a play with only the keyword", "_kw_local") + _use("_kw_local") + """- hosts: all
  tasks:
    - name: a device read
      ansible.builtin.command: show version
      register: _device
      changed_when: false
""" + _use("_device") + _read("a delegated resolver", "_delegated", "\n      delegate_to: localhost")
    + _use("_delegated"))
_PLANTED_MORE = """- name: an imported resolver
  ansible.builtin.command: echo y
  register: _imported
  changed_when: false
- ansible.builtin.debug:
    msg: "{{ _via_import.stdout }}"
- ansible.builtin.import_tasks: deeper.yml
"""
_PLANTED_DEEPER = """- name: a resolver two imports down
  ansible.builtin.command: echo z
  register: _deeper
  changed_when: false
"""
_PLANTED_ROLE = """- name: a role's resolver
  ansible.builtin.command: echo r
  register: _ctl
  changed_when: false%s
- ansible.builtin.debug:
    msg: "{{ _ctl.stdout }}"
"""


def _plant(tmp_path, forced):
    """A tree under tmp_path: a playbook with every case the sweep must tell apart, the task files it imports, a
    role it applies by `include_role` (`ctl`) and one by `roles:` (`ctlr`), a role nothing on the control node
    applies (`dev`), a hidden template role, and a template that reads a register."""
    books = tmp_path / "ansible" / "playbooks"
    (books / "tasks").mkdir(parents=True)
    mark = "\n  check_mode: false" if forced else ""
    (books / "p.yml").write_text(_PLANTED % mark.replace("\n", "\n    "), encoding="utf-8")
    (books / "tasks" / "more.yml").write_text(_PLANTED_MORE, encoding="utf-8")
    (books / "tasks" / "deeper.yml").write_text(_PLANTED_DEEPER, encoding="utf-8")
    for role, body in (("ctl", _PLANTED_ROLE % mark), ("ctlr", _PLANTED_ROLE.replace("_ctl", "_ctlr") % mark),
                       ("dev", _PLANTED_ROLE.replace("_ctl", "_dev") % mark),
                       (".role-template", _PLANTED_ROLE.replace("_ctl", "_tpl") % mark)):
        (tmp_path / "ansible" / "roles" / role / "tasks").mkdir(parents=True)
        (tmp_path / "ansible" / "roles" / role / "tasks" / "main.yml").write_text(body, encoding="utf-8")
    (tmp_path / "prom").mkdir()
    (tmp_path / "prom" / "x.yml.j2").write_text("value: {{ _in_template.stdout }}\n", encoding="utf-8")
    return str(tmp_path)


_UNFORCED = {("p.yml", r) for r in ("_probe", "_nested", "_via_import", "_in_template", "_string_false", "_listed",
                                    "_conn_local", "_delegated")} | {("more.yml", "_imported"),
                                                                     ("deeper.yml", "_deeper"), ("main.yml", "_ctl"),
                                                                     ("main.yml", "_ctlr")}


@pytest.mark.parametrize("forced,expect", [
    (False, _UNFORCED),
    (True, _UNFORCED - {("p.yml", "_probe"), ("main.yml", "_ctl"), ("main.yml", "_ctlr")}),
], ids=["unforced", "forced"])
def test_the_detector_flags_a_planted_unlisted_resolver(tmp_path, forced, expect):
    """A planted control-node tree. Each of these is flagged until it carries `check_mode: false`: a resolver read
    by a play var; one nested in a block; one read only by a task file the play imports; one read only by a
    template; one marked read-only by the string "false"; one in an imported file, and one two imports down; one
    in a role the play applies, by `include_role` or `roles:`; one in a play whose `hosts:` is a list of
    `localhost` and `127.0.0.1`; one in a play that sets the `ansible_connection: local` play var; one delegated to
    localhost from a play for `all`. These are not flagged: a read nothing uses, one named only in a comment, a
    writer, a device read, a read whose block forces it, and a read in a play for `all` that sets only the
    `connection: local` keyword, which a host's own `ansible_connection` overrides.

    Guards a detector that cannot fail, and the scope gaps its first read found (T24's read, F3, F11, F12): a read
    counted only in its own file, a nested import, a control-node role, a play written as a list of local names,
    run over a local connection or delegated, a mark on the block, and the string form of `changed_when`; and a
    keyword-only play taken as the control node (its fold-3 read, N5)."""
    assert unforced_reads(_plant(tmp_path, forced)) == expect


_LOCAL_PLAY = "- hosts: localhost\n  tasks:\n"
_READ_R = _LOCAL_PLAY + _read("r", "_r")
_IN_ALL = "- hosts: all\n  tasks:\n"
_EDGES = {
    # (playbook, {other file: its text}, flagged)
    "hash-line-in-template": (_READ_R, {"prom/x.conf.j2": "# digest {{ _r.stdout }}\n"}, True),
    "hash-line-statement-in-template": (_READ_R, {"prom/x.conf.j2": "# {% if _r.rc == 0 %}up{% endif %}\n"}, True),
    "read-only-in-a-jinja2-template": (_READ_R, {"prom/x.conf.jinja2": "value: {{ _r.stdout }}\n"}, True),
    "named-only-in-a-template-comment": (_READ_R, {"prom/x.conf.j2": "# _r feeds this file\n"}, False),
    "named-only-on-a-template-name-line": (_READ_R, {"prom/x.conf.j2": "name: _r\n"}, False),
    "hash-line-in-block-scalar": (_READ_R + "    - ansible.builtin.copy:\n"
                                  "        dest: /tmp/x\n        content: |\n          # {{ _r.stdout }}\n",
                                  {}, True),
    "read-only-in-a-commented-out-task": (_READ_R + "    # - ansible.builtin.debug:\n"
                                          "    #     msg: \"{{ _r.stdout }}\"\n", {}, False),
    "read-in-a-file-with-a-vault-tag": (_READ_R, {"ansible/group_vars/all.yml": "secret: !vault |\n"
                                                  "  $ANSIBLE_VAULT;1.1;AES256\n  3132\nseen: \"{{ _r.stdout }}\"\n"},
                                        True),
    "read-only-in-a-collection": (_READ_R, {"ansible/ansible_collections/ns/c/roles/r/tasks/main.yml":
                                            "- ansible.builtin.debug:\n    msg: \"{{ _r.stdout }}\"\n"}, False),
    "named-only-in-task-names": (_LOCAL_PLAY + _read("collect (_r)", "_r")
                                 + "    - name: show _r\n      ansible.builtin.debug:\n        msg: hi\n", {}, False),
    "read-in-a-name-list": (_READ_R + "    - ansible.builtin.package:\n        name: [\"{{ _r.stdout }}\", curl]\n",
                            {}, True),
    "read-in-a-templated-name": (_READ_R + "    - name: \"show {{ _r.stdout }}\"\n      ansible.builtin.debug:\n"
                                 "        msg: hi\n", {}, True),
    "register-value-on-its-own-line": (_LOCAL_PLAY + "    - name: r\n      ansible.builtin.command: echo r\n"
                                       "      register:\n        _r\n      changed_when: false\n", {}, False),
    "same-register-elsewhere": (_READ_R + _IN_ALL
                                + "    - ansible.builtin.command: show version\n      register: _r\n", {}, False),
    "templated-hosts-local": ("- hosts: \"{{ target | default('localhost') }}\"\n  tasks:\n" + _read("r", "_r")
                              + _use("_r"), {}, True),
    "templated-hosts-device": ("- hosts: \"{{ target | default('switches') }}\"\n  tasks:\n" + _read("r", "_r")
                               + _use("_r"), {}, False),
    "templated-hosts-mentions-localhost": ("- hosts: \"{{ hostvars['localhost']._wire_group | "
                                           "default('__no_target__') }}\"\n  tasks:\n" + _read("r", "_r")
                                           + _use("_r"), {}, False),
    "templated-hosts-no-default": ("- hosts: \"{{ groups['all'] | difference(['localhost']) }}\"\n  tasks:\n"
                                   + _read("r", "_r") + _use("_r"), {}, False),
    "hosts-comma-string": ("- hosts: localhost,127.0.0.1\n  tasks:\n" + _read("r", "_r") + _use("_r"), {}, True),
    "hosts-comma-space": ("- hosts: localhost, 127.0.0.1\n  tasks:\n" + _read("r", "_r") + _use("_r"), {}, True),
    "hosts-colon-string": ("- hosts: localhost:127.0.0.1\n  tasks:\n" + _read("r", "_r") + _use("_r"), {}, True),
    "hosts-mixed": ("- hosts: localhost:switches\n  tasks:\n" + _read("r", "_r") + _use("_r"), {}, False),
    "templated-hosts-d-alias": ("- hosts: \"{{ target | d('localhost') }}\"\n  tasks:\n" + _read("r", "_r")
                                + _use("_r"), {}, True),
    "play-var-connection-local": ("- hosts: all\n  vars:\n    ansible_connection: local\n  tasks:\n"
                                  + _read("r", "_r") + _use("_r"), {}, True),
    "play-connection-keyword": ("- hosts: all\n  connection: local\n  tasks:\n" + _read("r", "_r") + _use("_r"),
                                {}, False),
    "task-connection-keyword": (_IN_ALL + _read("r", "_r", "\n      connection: local") + _use("_r"), {}, False),
    "block-connection-keyword": (_IN_ALL + "    - connection: local\n      block:\n        - name: r\n"
                                 "          ansible.builtin.command: echo r\n          register: _r\n"
                                 "          changed_when: false\n        - ansible.builtin.debug:\n"
                                 "            msg: \"{{ _r.stdout }}\"\n", {}, False),
    "delegated-to-a-device": (_LOCAL_PLAY + _read("r", "_r", "\n      delegate_to: \"{{ item }}\"") + _use("_r"),
                              {}, False),
}


@pytest.mark.parametrize("case", sorted(_EDGES))
def test_the_corpus_and_the_scope_at_their_edges(tmp_path, case):
    """One read-only resolver `_r`, flagged exactly where a later task can read it on the control node.

    Flagged: read only on a `#` line of a template (by `{{` or by `{%`) or of a block scalar (output Jinja renders),
    in a `.jinja2` template, in a task name that templates it, in a `name:` list (a package's), or in a YAML file
    the parser refuses (a `!vault` tag), which is read by lines; in a play whose `hosts:` is templated to default
    (by `default` or `d`) to `localhost`, or is `localhost` and `127.0.0.1` joined by `,` (with or without a space)
    or `:`; in a play run with the `ansible_connection` play var.

    Not flagged: read only in a commented-out task or under `ansible_collections/`; named only in task names, on a
    template's `#` or `name:` line that holds no Jinja, by its own `register:` value on a line of its own, or by
    another play's `register:` of its own; in a play templated
    to default to a device group, to name a device group by `hostvars['localhost']`, or with no default; in a play
    for `localhost:switches`, which names a device group too; in a play
    or task or block for `all` that sets only the `connection: local` keyword (a host's `ansible_connection`
    overrides it); or delegated to a device.

    Guards T24's fold-2 read: a `#` line dropped whole hid a real read (N3, a regression from fold 1, which read the
    raw text); a task name or a same-named register elsewhere kept a stale `_EXEMPT` row alive and flagged an unread
    resolver (N4); and the scope missed control-node plays written these ways, or swept in a task that runs its
    command on a device (N8). And its fold-3 read: a templated `hosts:` that only mentions `localhost` (the
    wire-logging device play) taken as the control node, so a forced device read in its roles escaped the ban (N1);
    a commented-out read keeping a stale row alive (N3); the keyword taken as the control node (N5); `:` hosts
    (N6); and rules no row held (N7: `{%`, a templated name, the collections skip, the comma's space; and, once
    YAML no longer reached them, a template's `#` and `name:` lines). And its fold-4 read: a `name:` list dropped
    whole, Jinja and all (N2); a mixed `hosts:` taken as local, which no row refused (N3); `d(…)` (N4)."""
    playbook, others, flagged = _EDGES[case]
    books = tmp_path / "ansible" / "playbooks"
    books.mkdir(parents=True)
    (books / "p.yml").write_text(playbook, encoding="utf-8")
    for rel, text in others.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text, encoding="utf-8")
    assert unforced_reads(str(tmp_path)) == ({("p.yml", "_r")} if flagged else set())


def test_a_forced_read_in_a_device_role_is_refused(tmp_path):
    """In the planted tree with every role read forced, the role only a play for `all` with the `connection: local`
    keyword applies (`dev`) and the hidden template role are offenders; the role the control-node play applies
    (`ctl`) is not. Guards a ban that skips `.role-template` or forbids the fix in a role the control node runs
    (T24's read, F10), and one that spares a role whose play only sets the keyword, which a host's own
    `ansible_connection` overrides (its fold-3 read, N5)."""
    assert forced_device_reads(_plant(tmp_path, True)) == [
        "ansible/roles/.role-template/tasks/main.yml: a role's resolver",
        "ansible/roles/dev/tasks/main.yml: a role's resolver"]
