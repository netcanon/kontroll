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
  block around it — runs whenever the service does: not under `rescue:`, not tagged `never`, and no `when:`, or one
  that only tests `stack_services` membership and names the service and every service that starts it through
  `depends_on`. Any other condition (a secrets-present test, a length test) is the fault's own shape, and so is a
  writer that never writes on the box: one that runs with `check_mode: true`, or is delegated to anything but
  `localhost` / `127.0.0.1` (on itself or on a block around it). The bring-up is the first `docker compose` (or
  `docker-compose`) call in the playbook whose subcommand is `up`, `run` or `create` (which create the sources
  too), in any play, run by `command` (`cmd`, free form, `argv` or `args: {cmd: …}`), `shell` or `raw`, or a compose
  module task (`docker_compose_v2`, or v1's `docker_compose`) that does not stop the stack; a writer after it, in
  any play, is too late. The subcommand is the first word after `compose` that is not a global option or the value
  of one (`-f`, `-p`, `--project-directory`, `--profile`, …); a `{{ … }}` is one word, and a call ends at `;`, `&`,
  `|` or `)`. The tree has exactly one bring-up, and the test says so.

An env_file is held to the same rule even where it is `required: false`: compose then starts the service without
its settings, and nothing says so.

What the rule cannot see:
- a box whose overlay predates the scaffold's copy of a path: the scaffold never ran for it, so Docker creates the
  directory, root-owned, inside the overlay;
- `caddy`, which the play adds to the bring-up without its membership: its sources are absolute
  (`${KONTROLL_STORAGE_ROOT}`), outside this rule. A service caddy started through `depends_on` would run with no
  membership the rule reads, so the tree test asserts caddy starts none;
- a bring-up in a shape other than those above (a script that runs compose, a command built from a var, a
  `docker` global option before `compose`): the window then ends at a later bring-up, or never. A value-taking
  global option the sweep does not list has its value read as the subcommand;
- a play that guarantees a dependency some other way (an assert, a `--no-deps` bring-up): the rule still asks the
  writer to name the starters, and refuses a safe one;
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
    # A split, not a parse: it is safe only because EVERY term must match. A term that matches can be an interior
    # disjunct of a parenthesised group ANDed with a credentials test (`_has_creds and ('y' in stack_services or
    # 'x' in stack_services or 'z' in stack_services)`); the group's edge terms then fail the match, and the whole
    # `when:` is refused. Skipping a term would pass it. A term that both opens and closes a paren pair is read
    # inside it (the house style, `('x' in stack_services) or …`); a group's edge terms carry one paren each.
    for term in re.split(r"\s+or\s+", when.strip()):
        term = term.strip()
        if term.startswith("(") and term.endswith(")"):
            term = term[1:-1]
        m = _MEMBER.match(term)
        if not m:
            return None
        names.append(m.group(1))
    return names


def _walk(tasks, conditions=()):
    """Yield (task, the `when:` of the task and of every block around it) in document order. A task under `rescue:`
    runs only when its block fails, one tagged `never` only when asked for, one with `check_mode: true` never
    changes the box, and one delegated off the control node writes elsewhere: each carries a condition no
    membership test reads, so it never counts as a writer that runs whenever the service does."""
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        here = conditions + ((task["when"],) if "when" in task else ())
        tags = task.get("tags")
        if "never" in ([tags] if isinstance(tags, str) else tags if isinstance(tags, list) else []):
            here += ("<tagged never>",)
        if _is_true(task.get("check_mode")):
            here += ("<check_mode: true>",)
        if task.get("delegate_to") is not None and task["delegate_to"] not in ("localhost", "127.0.0.1"):
            here += ("<delegated>",)
        if any(isinstance(task.get(k), list) for k in ("block", "rescue", "always")):
            for key in ("block", "rescue", "always"):
                yield from _walk(task.get(key), here + (("<rescue>",) if key == "rescue" else ()))
        else:
            yield task, here


_COMPOSE = re.compile(r"\bdocker(?:\s+|-)compose\b")
# docker compose's global options that take a value: the word after one is that value, not the subcommand.
_COMPOSE_VALUE_OPTIONS = ("-f", "--file", "-p", "--project-name", "--project-directory", "--profile", "--env-file",
                          "--ansi", "--progress", "--parallel")
# The subcommands that create a service's bind sources.
_CREATES = ("up", "run", "create")
_COMPOSE_MODULES = ("community.docker.docker_compose_v2", "docker_compose_v2", "community.docker.docker_compose",
                    "docker_compose")


def _subcommands(text):
    """The subcommand of each `docker compose` / `docker-compose` call in `text`: the first word after `compose`
    that is neither a global option nor the value of one. A `{{ … }}` is one word; a quote or a backslash ends a
    word, and `;`, `&`, `|` or `)` the call."""
    out = []
    text = re.sub(r"\{\{.*?\}\}", "X", text, flags=re.S)
    for m in _COMPOSE.finditer(text):
        skip = False
        for word in re.split(r"[\s'\"\\]+", re.split(r"[;&|)]", text[m.end():], maxsplit=1)[0]):
            if not word or skip:
                skip = skip and not word
            elif word.startswith("-"):
                skip = word in _COMPOSE_VALUE_OPTIONS
            else:
                out.append(word)
                break
    return out


def _brings_up(task):
    """Whether `task` brings the stack up: a `docker compose` call whose subcommand is `up`, `run` or `create`, run
    by `command` (`cmd`, free form, `argv`, or `args: {cmd: …}`), `shell` or `raw`, or a compose module task (v2 or
    v1) that does not stop it."""
    for module in ("ansible.builtin.command", "command", "ansible.builtin.shell", "shell", "ansible.builtin.raw",
                   "raw"):
        if module not in task:
            continue
        args = task.get(module)
        if not args and isinstance(task.get("args"), dict):
            args = task["args"]
        if isinstance(args, dict):
            args = args.get("cmd") or " ".join(str(a) for a in args.get("argv") or [])
        if isinstance(args, str) and any(word in _CREATES for word in _subcommands(args)):
            return True
    return any(isinstance(task.get(m), dict) and task[m].get("state", "present") not in ("absent", "stopped")
               and not _is_true(task[m].get("stopped")) for m in _COMPOSE_MODULES)


def _is_true(value):
    """Whether a module option reads as true (v1 `docker_compose`'s `stopped: true` stops the stack)."""
    return str(value).strip().lower() in ("true", "yes", "1")


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
    when credentials existed. It also asserts what the rule rests on: the sweep finds exactly one bring-up (a
    reshaped one it could not read would leave the window open to the end), and caddy, brought up without its
    membership, starts no other service (whose writers the rule could then not judge)."""
    assert unprovided(ROOT) == []
    assert len(bring_ups(ROOT)) == 1, "the sweep finds the one bring-up: %r" % bring_ups(ROOT)
    assert {s for s, by in starters(ROOT).items() if "caddy" in by} == {"caddy"}, \
        "caddy starts a service through depends_on, which the play brings up without its membership"
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
    (_RENDER + "      when: \"('y' in stack_services) or ('x' in stack_services)\"\n", False),
    ("    - name: try\n      block:\n        - name: fail\n          ansible.builtin.fail:\n            msg: no\n"
     "      rescue:\n        - name: render\n          ansible.builtin.template:\n            src: x.j2\n"
     "            dest: \"{{ playbook_dir }}/../../conf/x.yml\"\n", True),
    (_RENDER + "      tags: [never, render]\n", True),
    (_RENDER + "      tags: never\n", True),
    ("    - name: try\n      block:\n        - name: hi\n          ansible.builtin.debug:\n            msg: hi\n"
     "      always:\n        - name: render\n          ansible.builtin.template:\n            src: x.j2\n"
     "            dest: \"{{ playbook_dir }}/../../conf/x.yml\"\n", False),
    (_RENDER + "      check_mode: true\n", True),
    ("    - check_mode: true\n      block:\n"
     + _RENDER.replace("\n    ", "\n        ").replace("    - ", "        - ", 1), True),
    (_RENDER + "      delegate_to: sw1\n", True),
    (_RENDER + "      delegate_to: localhost\n", False),
], ids=["unconditional", "member", "member-of-two", "secrets-present", "another-service", "and-paren-group",
        "paren-group-and", "and-list",
        "block-condition", "another-path", "no-writer", "touch-via-stack-dir", "file-absent", "member-parens",
        "rescue-only", "tagged-never", "tagged-never-string", "always", "check-mode-true", "block-check-mode-true",
        "delegated-away", "delegated-local"])
def test_the_sweep_catches_a_conditionally_rendered_source(tmp_path, writer, flagged):
    """A planted fragment binding `../../conf/x.yml`, against a deploy-stack whose writer is unconditional, gated
    on the service's own membership (in paren terms too, the house style), or gated on something else: a
    secrets-present clause (the snmp_exporter fault's shape), another service, a credentials test ANDed with a
    parenthesised membership group (either way round — the house style's shape), a conjunction, a condition on the
    enclosing block, a writer of another path, none, a `file` task that removes rather than creates, a writer only
    under `rescue:` or tagged `never` (as a list or a string). A writer under `always:` runs whatever its block
    does, and passes. A writer that runs with `check_mode: true` (on itself or its block) never writes, and one
    delegated to another host writes there: both are flagged; one delegated to localhost passes. Guards a sweep
    that cannot fail, one that reads only the task's own `when:` and not its block's, and a membership split that
    skips the terms it cannot read (T24's read, F1); a membership test refused for its parens, or a writer that runs
    only on a failure or on request counted as one that always runs (its fold-2 read, N1 and N6); and the
    `never` string and `always:` rules no row held, and a writer that never writes on the box counted as one (its
    fold-3 read, N7 and N9)."""
    root = _plant(tmp_path, _FRAG, _PLAY % writer.rstrip("\n"))
    assert bool(unprovided(root)) is flagged, unprovided(root)


_UPS = {
    "cmd-folded": "      ansible.builtin.command:\n        cmd: docker compose up -d x\n",
    "cmd-literal": ("      ansible.builtin.command:\n        cmd: |\n          docker compose\n"
                    "          --project-directory /srv/stack\n          up -d x\n"),
    "argv": "      ansible.builtin.command:\n        argv: [docker, compose, up, -d, x]\n",
    "free-form": "      ansible.builtin.command: docker compose up -d x\n",
    "shell": "      ansible.builtin.shell: docker compose up -d x | tee /tmp/up.log\n",
    "compose-module": "      community.docker.docker_compose_v2:\n        project_src: /srv/stack\n",
    "args-cmd": "      ansible.builtin.command:\n      args:\n        cmd: docker compose up -d x\n",
    "raw": "      ansible.builtin.raw: docker compose up -d x\n",
    "compose-v1": "      community.docker.docker_compose:\n        project_src: /srv/stack\n",
    "run": "      ansible.builtin.command: docker compose run --rm x true\n",
    "create": "      ansible.builtin.command: docker compose create x\n",
    "sh-c-quoted": "      ansible.builtin.command: sh -c 'docker compose up'\n",
    "then-semicolon": "      ansible.builtin.shell: docker compose up; echo done\n",
    "then-and": "      ansible.builtin.shell: docker compose up&&echo done\n",
    "options-first": "      ansible.builtin.command: docker compose -f /srv/x.yml --profile web --dry-run up -d x\n",
    "legacy-binary": "      ansible.builtin.command: docker-compose up -d x\n",
}


@pytest.mark.parametrize("up", sorted(_UPS))
@pytest.mark.parametrize("later_play", [False, True], ids=["same-play", "later-play"])
def test_a_writer_after_the_up_is_too_late(tmp_path, up, later_play):
    """The render placed after the stack's bring-up does not provision the source, whichever way the bring-up is
    written — a folded or literal `cmd`, `argv`, free form, `args: {cmd: …}`, `shell`, `raw`, the compose module (v2
    or v1), a `run` or `create`, which create the sources too, an `up` inside a quoted `sh -c` or followed by `;` or
    `&&`, one after global options and their values, or the `docker-compose` binary — and whether the render is in
    the same play or a later one. Guards a writer moved below the bring-up, where Docker has already created the
    directory, and a detector that stops looking for the bring-up at a reformat or a play boundary (T24's read, F2),
    or at a shape it does not read, when an earlier bring-up in that shape ends the window (its fold-2 read, N7;
    its fold-3 read, N2: an `up` before punctuation)."""
    play = "- hosts: localhost\n  tasks:\n    - name: up\n%s%s%s" % (
        _UPS[up], "- hosts: localhost\n  tasks:\n" if later_play else "", _RENDER)
    root = _plant(tmp_path, _FRAG, play)
    assert bring_ups(root) == ["up"]
    assert unprovided(root)


def test_a_stopping_compose_task_is_not_the_bring_up(tmp_path):
    """A `docker_compose_v2` task that stops the stack, a v1 `docker_compose` with `stopped: true`, a `docker
    compose ps`, a `pull` from a directory whose path holds the word `up`, an `exec` whose command says `create`, a
    `config` under a profile named `create`, and a script that runs `config` and later says "run" come before the
    render without ending the window it may write in. Guards a detector that reads every compose call as the
    bring-up, or `up` inside a path (T24's fold-2 read, N7), or `up` / `run` / `create` anywhere after `compose`
    (its fold-3 read, N2), which would refuse a render that does run first."""
    play = _PLAY % ("    - community.docker.docker_compose_v2:\n        project_src: /srv/stack\n"
                    "        state: stopped\n    - community.docker.docker_compose:\n        project_src: /srv/stack\n"
                    "        stopped: true\n    - ansible.builtin.command: docker compose ps\n"
                    "    - ansible.builtin.command: docker compose --project-directory /srv/up-stack pull\n"
                    "    - ansible.builtin.command: docker compose exec -T api kontroll create token\n"
                    "    - ansible.builtin.command: docker compose --profile create config\n"
                    "    - ansible.builtin.shell: |\n        docker compose config -q\n"
                    "        echo \"checked; run it next\"\n"
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
    the scaffold over a copy of the shipped `instance.example/` (plus a nested file), each file marked with its own
    path: every `instance/` path the scaffold writes maps back to the file it copied, and every path it skips maps
    to none. Guards the mirror drifting from the scaffold it stands for: a new scaffold exception (skipping
    `trust/`, the T24 fold-2 read's K1) passed every suite while the sweep still passed a bind the scaffold no
    longer fills (its N2)."""
    import importlib.util
    import shutil
    monkeypatch.syspath_prepend(os.path.join(ROOT, "scripts"))
    spec = importlib.util.spec_from_file_location("kontroll_init_mirror",
                                                  os.path.join(ROOT, "scripts", "kontroll-init.py"))
    init = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(init)
    example = tmp_path / "instance.example"
    shutil.copytree(os.path.join(ROOT, "instance.example"), example)
    (example / "dash" / "a").mkdir(parents=True)
    (example / "dash" / "a" / "b.yml").write_text("", encoding="utf-8")
    shipped = sorted(p.relative_to(example).as_posix() for p in example.rglob("*") if p.is_file())
    for rel in shipped:
        (example / rel).write_text(rel, encoding="utf-8")
    monkeypatch.setattr(init.paths, "ROOT", str(tmp_path))
    created, _ = init.scaffold_overlay()
    copied = {c: (tmp_path / "instance" / c).read_text(encoding="utf-8") for c in created}
    assert "trust/keys/.gitkeep" in shipped and "secrets/README.md" in shipped, shipped
    for name in sorted(set(shipped) | set(created)):
        want = "instance.example/" + copied[name] if name in copied else None
        assert _scaffolded(name) == want, (name, _scaffolded(name), want)


@pytest.mark.usefixtures("isolated_git")
def test_the_tree_is_read_from_git_where_it_is_a_checkout(tmp_path):
    """In a git checkout, a source on disk that git does not track (a rendered file, here one not ignored) is not
    in the tree: a writer gated on credentials is still flagged. Guards the git branch of the tree read, which the
    other planted tests (no checkout) never take: read from the disk there, the dev box's rendered file would hide
    the fault (T24's fold-2 read, N5: its mutant G1). It takes `isolated_git`: run from a git hook, its `git init`
    and `add` would otherwise act on the caller's repository (its fold-3 read, N4)."""
    import shutil
    if not shutil.which("git"):
        pytest.skip("no git")
    play = _PLAY % (_RENDER + "      when: \"'x' in stack_services and (_snmp | length > 0)\"\n").rstrip("\n")
    root = _plant(tmp_path, _FRAG, play)
    subprocess.run(["git", "init", "-q", root], check=True)
    subprocess.run(["git", "-C", root, "add", "docker", "ansible"], check=True)
    (tmp_path / "conf").mkdir()
    (tmp_path / "conf" / "x.yml").write_text("rendered\n", encoding="utf-8")
    assert _tracked(root) == {"docker/services/x.yaml", "ansible/playbooks/deploy-stack.yml"}
    assert [line.split(" — ")[0] for line in unprovided(root)] == ["x.yaml: x: volume ../../conf/x.yml"]


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
