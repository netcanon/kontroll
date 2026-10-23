"""Every relative bind source and env_file of a stack service exists before `docker compose up` binds it.

WHY (the failure this guards). Docker creates a missing bind source as an empty, root-owned DIRECTORY, and
`docker compose config` validates the shape but never that a source exists, so only a live run shows it: Loki read
its config as "is a directory", Vector found no config, and snmp_exporter died with "not a directory: Are you
trying to mount a directory onto a file" on a fresh box (live-caught 2026-07-28) — its config was rendered only
when SNMP credentials existed, while the service was brought up regardless. The guard this replaces checked two
fragments by name; a third service was then found to ship the same fault.

The rule reads the tree; there is no allow-list. A source written relative to its fragment (a short-form `./` or
`../` source, a long-form `type: bind` source that is not absolute, and every `env_file`) is satisfied when:
- it is in the tree (tracked, where the tree is a git checkout);
- it is under `instance/` and `kontroll-init --fresh` scaffolds it: `instance.example/` ships the same path, not
  under `secrets/` and not the top-level README.md, which the scaffold skips (`leak-tokens.txt` is scaffolded from
  `leak-tokens.example.txt`); or
- a task of `ansible/playbooks/deploy-stack.yml` that comes before the stack's bring-up writes that exact path
  (`copy`/`template` `dest:`, or `file` `path:` with `state: touch` or `directory`), and that task — with every
  block around it — runs whenever the service does: no `when:`, or one that only tests `stack_services` membership
  and names the service and every service that starts it through `depends_on`. Any other condition (a
  secrets-present test, a length test) is the fault's own shape. The bring-up is the first `docker compose … up`
  in the playbook, in any play, run by `command` (`cmd`, free form or `argv`) or `shell`, or a
  `community.docker.docker_compose_v2` task that does not stop the stack; a writer after it, in any play, is too
  late. The tree has exactly one, and the test says so.

An env_file is held to the same rule even where it is `required: false`: compose then starts the service without
its settings, and nothing says so.

What the rule cannot see:
- a box whose overlay predates the scaffold's copy of a path: the scaffold never ran for it, so Docker creates the
  directory, root-owned, inside the overlay;
- `caddy`, which the play adds to the bring-up without its membership: its sources are absolute
  (`${KONTROLL_STORAGE_ROOT}`), outside this rule;
- a writer in a task file deploy-stack imports, or one whose path names a var the sweep cannot resolve: refused,
  as not a writer;
- where git will not list the files (no checkout, or one it refuses as of dubious ownership), the tree is read from
  the disk, where an ignored rendered file counts as in the tree. CI's fresh checkout still has git.
"""
import glob
import os
import re
import subprocess

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_WRITERS = {"ansible.builtin.copy": "dest", "copy": "dest", "ansible.builtin.template": "dest", "template": "dest",
            "ansible.builtin.file": "path", "file": "path"}
# A `when:` that only says "this service is in the stack": one or more `'<name>' in stack_services`, joined by `or`.
_MEMBER = re.compile(r"""^\s*['"]([\w.-]+)['"]\s+in\s+stack_services\s*$""")
_JINJA_VAR = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def _tracked(root):
    """The set of tracked files (relative, forward slashes) when `root` is a git checkout, else None."""
    try:
        top = subprocess.run(["git", "-C", root, "rev-parse", "--show-toplevel"], capture_output=True, text=True)
        run = subprocess.run(["git", "-C", root, "ls-files", "-z"], capture_output=True)
    except OSError:
        return None                                       # no git: read the disk
    if top.returncode != 0 or run.returncode != 0 or not run.stdout:
        return None
    if os.path.normcase(os.path.realpath(top.stdout.strip())) != os.path.normcase(os.path.realpath(root)):
        return None                                       # `root` sits inside another repository: read the disk
    return {f for f in run.stdout.decode("utf-8").split("\0") if f}


def _in_tree(rel, root, tracked):
    """Whether `rel` (a file or a directory) is in the tree: tracked, or on disk when the tree is not a checkout."""
    if tracked is None:
        return os.path.exists(os.path.join(root, rel))
    return rel in tracked or any(f.startswith(rel + "/") for f in tracked)


def bind_sources(root):
    """[(fragment, service, kind, source, repo-relative path)] for every volume source written relative to its
    fragment (`./`, `../`) and every env_file, in every docker/services fragment."""
    out = []
    services_dir = os.path.join(root, "docker", "services")
    for frag in sorted(glob.glob(os.path.join(services_dir, "*.yaml"))):
        with open(frag, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh) or {}
        for name, svc in (doc.get("services") or {}).items():
            found = []
            for vol in (svc or {}).get("volumes") or []:
                if isinstance(vol, dict):                 # long form: a bind source is relative unless absolute
                    src = vol.get("source")
                    relative = isinstance(src, str) and (src.startswith(".") or vol.get("type") == "bind"
                                                         and not src.startswith(("/", "~", "$")))
                else:                                     # short form: a bare name is a named volume
                    src = str(vol).split(":", 1)[0]
                    relative = src.startswith(".")
                if relative:
                    found.append(("volume", src))
            env_file = (svc or {}).get("env_file")
            for entry in env_file if isinstance(env_file, list) else [env_file] if env_file else []:
                found.append(("env_file", entry.get("path") if isinstance(entry, dict) else entry))
            for kind, src in found:
                rel = os.path.relpath(os.path.normpath(os.path.join(services_dir, src)), root).replace(os.sep, "/")
                out.append((os.path.basename(frag), name, kind, src, rel))
    return out


def _resolve(text, play_vars):
    """`text` with `{{ playbook_dir }}` and the play's own string vars substituted, as a normalised repo-relative
    path; None when anything else is templated in it."""
    for _ in range(5):
        def sub(m):
            if m.group(1) == "playbook_dir":
                return "ansible/playbooks"
            value = play_vars.get(m.group(1))
            return value if isinstance(value, str) else m.group(0)
        text = _JINJA_VAR.sub(sub, text)
    if "{{" in text or "{%" in text:
        return None
    return os.path.normpath(text).replace(os.sep, "/")


def _members(when):
    """The services a `when:` names, if it only tests `stack_services` membership; None for any other condition."""
    if isinstance(when, list):
        if len(when) != 1:
            return None
        when = when[0]
    if not isinstance(when, str):
        return None
    names = []
    # A split, not a parse: it is safe only because EVERY term must match. A term that matches can be one disjunct
    # of a parenthesised group ANDed with a credentials test (`_has_creds and ('x' in stack_services or …)`); the
    # group's other terms then fail the match, and the whole `when:` is refused. Skipping a term would pass it.
    for term in re.split(r"\s+or\s+", when.strip()):
        m = _MEMBER.match(term)
        if not m:
            return None
        names.append(m.group(1))
    return names


def _walk(tasks, conditions=()):
    """Yield (task, the `when:` of the task and of every block around it) in document order."""
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        here = conditions + ((task["when"],) if "when" in task else ())
        if any(isinstance(task.get(k), list) for k in ("block", "rescue", "always")):
            for key in ("block", "rescue", "always"):
                yield from _walk(task.get(key), here)
        else:
            yield task, here


_UP = re.compile(r"\bdocker[\s-]+compose\b.*\bup\b", re.S)
_COMPOSE_MODULES = ("community.docker.docker_compose_v2", "docker_compose_v2")


def _brings_up(task):
    """Whether `task` brings the stack up: a `docker compose … up` run by `command` (`cmd`, free form or `argv`) or
    `shell`, or a `docker_compose_v2` task that does not stop it."""
    for module in ("ansible.builtin.command", "command", "ansible.builtin.shell", "shell"):
        args = task.get(module)
        if isinstance(args, dict):
            args = args.get("cmd") or " ".join(str(a) for a in args.get("argv") or [])
        if isinstance(args, str) and _UP.search(args):
            return True
    return any(isinstance(task.get(m), dict) and task[m].get("state", "present") not in ("absent", "stopped")
               for m in _COMPOSE_MODULES)


def _deploy_tasks(root):
    """[(play vars, task, its `when:` chain)] of every deploy-stack.yml play, in document order."""
    with open(os.path.join(root, "ansible", "playbooks", "deploy-stack.yml"), encoding="utf-8") as fh:
        plays = yaml.safe_load(fh) or []
    out = []
    for play in plays:
        if not isinstance(play, dict):
            continue
        for section in ("pre_tasks", "tasks", "post_tasks"):
            out += [(play.get("vars") or {}, task, chain) for task, chain in _walk(play.get(section))]
    return out


def bring_ups(root):
    """The name of every deploy-stack.yml task that brings the stack up."""
    return [task.get("name") for _, task, _ in _deploy_tasks(root) if _brings_up(task)]


def provisioners(root):
    """{repo-relative path: [the `when:` chain of each deploy-stack.yml task that writes it before the stack's
    bring-up]}."""
    written = {}
    for play_vars, task, chain in _deploy_tasks(root):
        if _brings_up(task):
            break                                         # the bring-up: a writer after it, in any play, is too late
        for module, key in _WRITERS.items():
                args = task.get(module)
                if not isinstance(args, dict) or not isinstance(args.get(key), str):
                    continue
                if key == "path" and args.get("state") not in ("touch", "directory"):
                    continue
                path = _resolve(args[key], play_vars)
                if path:
                    written.setdefault(path, []).append(chain)
    return written


def starters(root):
    """{service: the services whose `up` starts it} — itself, and every service that reaches it through
    `depends_on` (list or map form), across the docker/services fragments."""
    deps = {}
    for frag in sorted(glob.glob(os.path.join(root, "docker", "services", "*.yaml"))):
        with open(frag, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh) or {}
        for name, svc in (doc.get("services") or {}).items():
            deps.setdefault(name, set()).update((svc or {}).get("depends_on") or ())
    out = {}
    for name in deps:
        reach, todo = set(), [name]
        while todo:
            here = todo.pop()
            if here not in reach:
                reach.add(here)
                todo += deps.get(here, ())
        for started in reach:
            out.setdefault(started, set()).add(name)
    return out


def _scaffolded(rel):
    """The `instance.example/` path `kontroll-init --fresh` copies to `instance/<rel>` (scaffold_overlay's own
    exceptions), or None where it copies nothing there."""
    if rel == "secrets" or rel.startswith("secrets/") or rel in ("README.md", "leak-tokens.example.txt"):
        return None
    return "instance.example/" + ("leak-tokens.example.txt" if rel == "leak-tokens.txt" else rel)


def unprovided(root):
    """["fragment: service: kind source — why"] for every source the rule above does not satisfy."""
    tracked = _tracked(root)
    written = provisioners(root)
    started_by = starters(root)
    out = []
    for frag, service, kind, src, rel in bind_sources(root):
        if _in_tree(rel, root, tracked):
            continue
        twin = _scaffolded(rel[len("instance/"):]) if rel.startswith("instance/") else None
        if twin and _in_tree(twin, root, tracked):
            continue
        chains = written.get(rel)
        if not chains:
            out.append("%s: %s: %s %s — not in the tree, nor scaffolded, and no task of deploy-stack.yml the sweep "
                       "can read writes %s before the stack's bring-up" % (frag, service, kind, src, rel))
            continue
        need = started_by.get(service, {service})
        if not any(all(need <= set(_members(w) or ()) for w in chain) for chain in chains):
            out.append("%s: %s: %s %s — %s is written only under a condition the service can run without (it runs "
                       "when any of %s is in the stack): %r" % (frag, service, kind, src, rel, sorted(need), chains))
    return out


def test_every_relative_bind_source_exists_or_is_provisioned_unconditionally():
    """No docker/services fragment binds a relative source, or reads an env_file, that is neither in the tree, nor
    an `instance/` path the scaffold ships, nor written by deploy-stack before the stack's `up` whenever the
    service runs. Guards Docker turning a missing file-bind source into a root-owned directory — the recurrent
    fault of the Loki and Vector configs (`../../` against `../`) and of the snmp_exporter config rendered only
    when credentials existed."""
    assert unprovided(ROOT) == []
    assert bring_ups(ROOT) == ["Bring up the selected stack service(s)"], "the sweep finds the one bring-up"
    sources = bind_sources(ROOT)
    assert any(kind == "env_file" for _, _, kind, _, _ in sources), "the walk reads env_file entries too"
    assert ("snmp-exporter.generated.yaml", "snmp-exporter") in {(f, s) for f, s, _, _, _ in sources}, \
        "the walk reaches the generated fragments"


def _plant(tmp_path, frag, playbook, files=()):
    """A minimal tree: one compose fragment, a deploy-stack.yml, and the files named."""
    (tmp_path / "docker" / "services").mkdir(parents=True)
    (tmp_path / "docker" / "services" / "x.yaml").write_text(frag, encoding="utf-8")
    (tmp_path / "ansible" / "playbooks").mkdir(parents=True)
    (tmp_path / "ansible" / "playbooks" / "deploy-stack.yml").write_text(playbook, encoding="utf-8")
    for rel in files:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n", encoding="utf-8")
    return str(tmp_path)


_FRAG = """services:
  x:
    image: x
    volumes:
      - ../../conf/x.yml:/etc/x.yml:ro
"""
_PLAY = """- hosts: localhost
  vars:
    stack_dir: "{{ playbook_dir }}/../../docker"
  tasks:
%s
    - name: up
      ansible.builtin.command:
        cmd: docker compose --project-directory {{ stack_dir }} up -d x
"""
_RENDER = """    - name: render
      ansible.builtin.template:
        src: x.j2
        dest: "{{ playbook_dir }}/../../conf/x.yml"
"""


@pytest.mark.parametrize("writer,flagged", [
    (_RENDER, False),
    (_RENDER + "      when: \"'x' in stack_services\"\n", False),
    (_RENDER + "      when: \"'y' in stack_services or 'x' in stack_services\"\n", False),
    (_RENDER + "      when: \"'x' in stack_services and (_snmp | length > 0)\"\n", True),
    (_RENDER + "      when: \"'y' in stack_services\"\n", True),
    (_RENDER + "      when: \"_has_creds | bool and ('y' in stack_services or 'x' in stack_services or 'z' in "
               "stack_services)\"\n", True),
    (_RENDER + "      when: \"('y' in stack_services or 'x' in stack_services) and not in_container\"\n", True),
    (_RENDER + "      when:\n        - \"'x' in stack_services\"\n        - _has_creds | bool\n", True),
    ("    - name: wrapped\n      when: _has_creds | bool\n      block:\n        - name: render\n"
     "          ansible.builtin.template:\n            src: x.j2\n"
     "            dest: \"{{ playbook_dir }}/../../conf/x.yml\"\n          when: \"'x' in stack_services\"\n", True),
    ("    - name: elsewhere\n      ansible.builtin.template:\n        src: x.j2\n"
     "        dest: \"{{ playbook_dir }}/../../conf/y.yml\"\n", True),
    ("    - name: no writer\n      ansible.builtin.debug:\n        msg: hi\n", True),
    ("    - name: touch\n      ansible.builtin.file:\n        path: \"{{ stack_dir }}/../conf/x.yml\"\n"
     "        state: touch\n", False),
    ("    - name: absent\n      ansible.builtin.file:\n        path: \"{{ stack_dir }}/../conf/x.yml\"\n"
     "        state: absent\n", True),
], ids=["unconditional", "member", "member-of-two", "secrets-present", "another-service", "and-paren-group",
        "paren-group-and", "and-list",
        "block-condition", "another-path", "no-writer", "touch-via-stack-dir", "file-absent"])
def test_the_sweep_catches_a_conditionally_rendered_source(tmp_path, writer, flagged):
    """A planted fragment binding `../../conf/x.yml`, against a deploy-stack whose writer is unconditional, gated
    on the service's own membership, or gated on something else: a secrets-present clause (the snmp_exporter
    fault's shape), another service, a credentials test ANDed with a parenthesised membership group (either way
    round — the house style's shape), a conjunction, a condition on the enclosing block, a writer of another path,
    none, a `file` task that removes rather than creates. Guards a sweep that cannot fail, one that reads only the
    task's own `when:` and not its block's, and a membership split that skips the terms it cannot read (T24's
    read, F1)."""
    root = _plant(tmp_path, _FRAG, _PLAY % writer.rstrip("\n"))
    assert bool(unprovided(root)) is flagged, unprovided(root)


_UPS = {
    "cmd-folded": "      ansible.builtin.command:\n        cmd: docker compose up -d x\n",
    "cmd-literal": "      ansible.builtin.command:\n        cmd: |\n          docker compose\n          up -d x\n",
    "argv": "      ansible.builtin.command:\n        argv: [docker, compose, up, -d, x]\n",
    "free-form": "      ansible.builtin.command: docker compose up -d x\n",
    "shell": "      ansible.builtin.shell: docker compose up -d x | tee /tmp/up.log\n",
    "compose-module": "      community.docker.docker_compose_v2:\n        project_src: /srv/stack\n",
}


@pytest.mark.parametrize("up", sorted(_UPS))
@pytest.mark.parametrize("later_play", [False, True], ids=["same-play", "later-play"])
def test_a_writer_after_the_up_is_too_late(tmp_path, up, later_play):
    """The render placed after the stack's bring-up does not provision the source, whichever way the bring-up is
    written — a folded or literal `cmd`, `argv`, free form, `shell`, the compose module — and whether the render is
    in the same play or a later one. Guards a writer moved below the bring-up, where Docker has already created the
    directory, and a detector that stops looking for the bring-up at a reformat or a play boundary (T24's read,
    F2)."""
    play = "- hosts: localhost\n  tasks:\n    - name: up\n%s%s%s" % (
        _UPS[up], "- hosts: localhost\n  tasks:\n" if later_play else "", _RENDER)
    root = _plant(tmp_path, _FRAG, play)
    assert bring_ups(root) == ["up"]
    assert unprovided(root)


def test_a_stopping_compose_task_is_not_the_bring_up(tmp_path):
    """A `docker_compose_v2` task that stops the stack, and a `docker compose ps`, come before the render without
    ending the window it may write in. Guards a detector that reads every compose call as the bring-up, which would
    refuse a render that does run first."""
    play = _PLAY % ("    - community.docker.docker_compose_v2:\n        project_src: /srv/stack\n"
                    "        state: stopped\n    - ansible.builtin.command: docker compose ps\n"
                    + _RENDER.rstrip("\n"))
    root = _plant(tmp_path, _FRAG, play)
    assert bring_ups(root) == ["up"]
    assert unprovided(root) == []


def test_a_dependency_start_needs_its_starters_membership(tmp_path):
    """A source of a service another service starts through `depends_on` is provisioned only by a writer whose
    membership test names the starter too: grafana starts prometheus, so a prometheus source written under
    `'prometheus' in stack_services` alone is missing when only grafana is deployed. Guards membership read as
    "runs" (T24's read, F5)."""
    frag = _FRAG + ("  y:\n    image: y\n    depends_on: [x]\n"
                    "  z:\n    image: z\n    depends_on:\n      y: {condition: service_started}\n")
    gated = _RENDER + "      when: \"%s\"\n"
    root = _plant(tmp_path / "a", frag, _PLAY % (gated % "'x' in stack_services").rstrip("\n"))
    assert "it runs when any of ['x', 'y', 'z'] is in the stack" in unprovided(root)[0]
    root = _plant(tmp_path / "b", frag, _PLAY % (gated % "'x' in stack_services or 'y' in stack_services or "
                                                          "'z' in stack_services").rstrip("\n"))
    assert unprovided(root) == []


def test_the_tree_and_the_instance_example_twin_satisfy_a_source(tmp_path):
    """A source in the tree passes; an `instance/` source passes when `instance.example/` ships the same path and is
    flagged when it does not; an env_file is held to the same rule. Guards the scaffold rule passing a path the
    scaffold never writes, and env_file entries escaping the sweep."""
    frag = """services:
  x:
    image: x
    volumes:
      - type: bind
        source: ../../conf/x.yml
        target: /etc/x.yml
      - ../../instance/dash/x:/app/x
    env_file:
      - path: ../.env.x
        required: false
"""
    play = _PLAY % "    - name: nothing\n      ansible.builtin.debug:\n        msg: hi"
    root = _plant(tmp_path / "a", frag, play, files=("conf/x.yml", "instance.example/dash/x/a.yml"))
    assert unprovided(root) == ["x.yaml: x: env_file ../.env.x — not in the tree, nor scaffolded, and no task of "
                                "deploy-stack.yml the sweep can read writes docker/.env.x before the stack's bring-up"]
    root = _plant(tmp_path / "b", frag, play, files=("conf/x.yml", "docker/.env.x"))
    assert [line.split(" — ")[0] for line in unprovided(root)] == ["x.yaml: x: volume ../../instance/dash/x"]


@pytest.mark.parametrize("source,ships,flagged", [
    ("../../instance/dash", "instance.example/dash/a.yml", False),
    ("../../instance/secrets", "instance.example/secrets/README.md", True),
    ("../../instance/README.md", "instance.example/README.md", True),
    ("../../instance/leak-tokens.txt", "instance.example/leak-tokens.example.txt", False),
    ("../../instance/leak-tokens.example.txt", "instance.example/leak-tokens.example.txt", True),
], ids=["scaffolded", "secrets-skipped", "readme-skipped", "renamed", "renamed-away"])
def test_the_twin_rule_is_what_the_scaffold_copies(tmp_path, source, ships, flagged):
    """An `instance/` source passes on its `instance.example/` twin only where `kontroll-init --fresh` copies it:
    never `secrets/` or the top-level README.md, and `leak-tokens.txt` from its `.example` name. Guards a twin rule
    wider than the scaffold, which passes a bind the scaffold never fills (T24's read, F6)."""
    frag = "services:\n  x:\n    image: x\n    volumes:\n      - %s:/app/x\n" % source
    play = _PLAY % "    - name: nothing\n      ansible.builtin.debug:\n        msg: hi"
    assert bool(unprovided(_plant(tmp_path, frag, play, files=(ships,)))) is flagged


def test_the_scaffold_mirror_matches_scaffold_overlay(tmp_path, monkeypatch):
    """`_scaffolded` maps a path the way scripts/kontroll-init.py's `scaffold_overlay` copies it, checked by running
    the scaffold over a planted `instance.example/`. Guards the mirror drifting from the scaffold it stands for."""
    import importlib.util
    monkeypatch.syspath_prepend(os.path.join(ROOT, "scripts"))
    spec = importlib.util.spec_from_file_location("kontroll_init_mirror",
                                                  os.path.join(ROOT, "scripts", "kontroll-init.py"))
    init = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(init)
    shipped = ("README.md", "leak-tokens.example.txt", "fleet.yml", "secrets/README.md", "dash/a/b.yml")
    for rel in shipped:
        (tmp_path / "instance.example" / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / "instance.example" / rel).write_text("x\n", encoding="utf-8")
    monkeypatch.setattr(init.paths, "ROOT", str(tmp_path))
    created, _ = init.scaffold_overlay()
    assert {_scaffolded(c) for c in created} <= {"instance.example/" + rel for rel in shipped}
    assert sorted(c for c in ("README.md", "leak-tokens.txt", "leak-tokens.example.txt", "fleet.yml", "secrets",
                              "secrets/README.md", "dash/a/b.yml") if _scaffolded(c)) == sorted(created)


def test_a_bare_relative_long_form_bind_counts(tmp_path):
    """A long-form `type: bind` source written without `./` is resolved by compose against the fragment's
    directory, so the sweep holds it to the rule; a short-form bare name is a named volume and is not. Guards a
    relative bind escaping the sweep for want of a leading dot (T24's read, F7)."""
    frag = ("services:\n  x:\n    image: x\n    volumes:\n      - type: bind\n        source: conf/x.yml\n"
            "        target: /etc/x.yml\n      - type: bind\n        source: /srv/abs.yml\n        target: /a\n"
            "      - data:/var/lib/x\n")
    play = _PLAY % "    - name: nothing\n      ansible.builtin.debug:\n        msg: hi"
    assert [line.split(" — ")[0] for line in unprovided(_plant(tmp_path, frag, play))] == [
        "x.yaml: x: volume conf/x.yml"]
