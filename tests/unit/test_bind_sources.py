"""Every relative bind source and env_file of a stack service exists before `docker compose up` binds it.

WHY (the failure this guards). Docker creates a missing bind source as an empty, root-owned DIRECTORY, and
`docker compose config` validates the shape but never that a source exists, so only a live run shows it: Loki read
its config as "is a directory", Vector found no config, and snmp_exporter died with "not a directory: Are you
trying to mount a directory onto a file" on a fresh box (live-caught 2026-07-28) — its config was rendered only
when SNMP credentials existed, while the service was brought up regardless. The guard this replaces checked two
fragments by name; a third service then shipped the same fault.

The rule reads the tree; there is no allow-list. A source written relative to its fragment (and every `env_file`)
is satisfied when:
- it is in the tree (tracked, where the tree is a git checkout);
- it is under `instance/` and `instance.example/` ships the same path, which `kontroll-init --fresh` scaffolds; or
- a task of `ansible/playbooks/deploy-stack.yml` that comes before the stack's `up` writes that exact path
  (`copy`/`template` `dest:`, or `file` `path:` with `state: touch` or `directory`), and that task — with every
  block around it — runs whenever the service does: no `when:`, or one that only tests `stack_services` membership
  and names the service. Any other condition (a secrets-present test, a length test) is the fault's own shape.

An env_file is held to the same rule even where it is `required: false`: compose then starts the service without
its settings, and nothing says so.
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
                src = vol.get("source") if isinstance(vol, dict) else str(vol).split(":", 1)[0]
                if isinstance(src, str) and src.startswith("."):
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


def provisioners(root):
    """{repo-relative path: [the `when:` chain of each deploy-stack.yml task that writes it before the stack's
    `up`]}."""
    with open(os.path.join(root, "ansible", "playbooks", "deploy-stack.yml"), encoding="utf-8") as fh:
        plays = yaml.safe_load(fh) or []
    written = {}
    for play in plays:
        if not isinstance(play, dict):
            continue
        play_vars = play.get("vars") or {}
        tasks = []
        for section in ("pre_tasks", "tasks", "post_tasks"):
            tasks += list(_walk(play.get(section)))
        for task, chain in tasks:
            cmd = task.get("ansible.builtin.command") or task.get("command") or {}
            cmd = cmd.get("cmd", "") if isinstance(cmd, dict) else str(cmd)
            if re.search(r"\bdocker compose\b.*\bup\b", cmd):
                break                                     # the stack's `up`: a writer after it is too late
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


def unprovided(root):
    """["fragment: service: kind source — why"] for every source the rule above does not satisfy."""
    tracked = _tracked(root)
    written = provisioners(root)
    out = []
    for frag, service, kind, src, rel in bind_sources(root):
        if _in_tree(rel, root, tracked):
            continue
        if rel.startswith("instance/") and _in_tree("instance.example/" + rel[len("instance/"):], root, tracked):
            continue
        chains = written.get(rel)
        if not chains:
            out.append("%s: %s: %s %s — not in the tree, and nothing writes %s before the stack's up"
                       % (frag, service, kind, src, rel))
            continue
        if not any(all(service in (_members(w) or ()) for w in chain) for chain in chains):
            out.append("%s: %s: %s %s — %s is written only under a condition the service can run without: %r"
                       % (frag, service, kind, src, rel, chains))
    return out


def test_every_relative_bind_source_exists_or_is_provisioned_unconditionally():
    """No docker/services fragment binds a relative source, or reads an env_file, that is neither in the tree, nor
    an `instance/` path the scaffold ships, nor written by deploy-stack before the stack's `up` whenever the
    service runs. Guards Docker turning a missing file-bind source into a root-owned directory — the recurrent
    fault of the Loki and Vector configs (`../../` against `../`) and of the snmp_exporter config rendered only
    when credentials existed."""
    assert unprovided(ROOT) == []
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
], ids=["unconditional", "member", "member-of-two", "secrets-present", "another-service", "and-list",
        "block-condition", "another-path", "no-writer", "touch-via-stack-dir", "file-absent"])
def test_the_sweep_catches_a_conditionally_rendered_source(tmp_path, writer, flagged):
    """A planted fragment binding `../../conf/x.yml`, against a deploy-stack whose writer is unconditional, gated
    on the service's own membership, or gated on something else: a secrets-present clause (the snmp_exporter
    fault's shape), another service, a conjunction, a condition on the enclosing block, a writer of another path,
    none, a `file` task that removes rather than creates. Guards a sweep that cannot fail, and one that reads only
    the task's own `when:` and not its block's."""
    root = _plant(tmp_path, _FRAG, _PLAY % writer.rstrip("\n"))
    assert bool(unprovided(root)) is flagged, unprovided(root)


def test_a_writer_after_the_up_is_too_late(tmp_path):
    """The render placed after the stack's `up` does not provision the source. Guards a writer moved below the
    bring-up, where Docker has already created the directory."""
    play = """- hosts: localhost
  tasks:
    - name: up
      ansible.builtin.command:
        cmd: docker compose up -d x
%s""" % _RENDER
    assert unprovided(_plant(tmp_path, _FRAG, play))


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
    assert unprovided(root) == ["x.yaml: x: env_file ../.env.x — not in the tree, and nothing writes docker/.env.x "
                                "before the stack's up"]
    root = _plant(tmp_path / "b", frag, play, files=("conf/x.yml", "docker/.env.x"))
    assert [line.split(" — ")[0] for line in unprovided(root)] == ["x.yaml: x: volume ../../instance/dash/x"]
