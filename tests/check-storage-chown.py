#!/usr/bin/env python3
"""Assert every storage-root-derived compose bind SOURCE has a matching deploy-stack provisioner (the M-2 / C8
at-rest contract). A relocatable bind whose host source is NOT created uid-scoped BEFORE `up` is the G9 hole:
`docker compose up` auto-creates the missing source as a root:root 0755 DIRECTORY — world-readable, and for a
secret-bearing store (logs/backups/audit) a C12/C8 at-rest leak. This is the loud CI guard that closes G9 and
keeps a future relocated store from silently re-opening it.

Design (docs/reviews/2026-06-16-storage-paths/): in scope = every compose bind source whose RESOLVED-DEFAULT
path is under /var/lib/kontroll (NOT a hand-maintained var-name list — a new ${KONTROLL_*:-/var/lib/kontroll/x}
is caught with zero maintenance). A source is COVERED iff its OWN resolved path is provisioned — EXACT match, no
permissive grandparent walk (a chowned ancestor does not prove a leaf file/dir exists uid-scoped). The three
provisioners, parsed from deploy-stack.yml via a yaml.safe_load task-graph walk:
  * a file/copy/template task that sets owner+group  -> the dir/file it creates (loop items expanded);
  * a git task                                       -> its cloned repo tree (its CONTENTS are app-managed, so a
                                                        bind at-or-under the repo, e.g. repo/local, is covered);
                                                        a task file the play imports (`import_tasks`, with its
                                                        `vars:` put in) is read as the play's own tasks — the
                                                        content clones are made in tasks/content-clone.yml;
  * a fail-closed file-absence guard                 -> the age.key case: the file is host-provisioned, never
                                                        minted, so the deploy refuses to `up` without it rather
                                                        than let Docker auto-create it. EITHER shape counts — a
                                                        stat task carrying its own `failed_when`, or a stat that
                                                        `register`s and a later `assert` requiring
                                                        `<register>.stat.exists`. The split shape exists because
                                                        `failed_when` cannot carry a message, and a refusal that
                                                        does not say what to provision is pure spin-up friction
                                                        (2026-07-28); recognising both keeps the choice between
                                                        them a readability decision, not a CI-passing one.
Excluded: relative repo-tree binds (../), host-OS absolutes (/var/run,/var/log,/run/,/etc/), the canonical git
(/srv/kontroll.git — provisioned by local-canonical.yml), the operator run-logs (${KONTROLL_OPERATOR_HOME}), named
volumes (Docker-managed; no '/' in the source), and the compose-native installer's bare storage-ROOT bind (it is
the PROVISIONER — it binds /var/lib/kontroll to CREATE the uid-scoped tree deploy-stack populates; the root itself
is a non-secret parent, and its secret children are each separately provisioned + guarded — a subdir bind in
installer.yaml is NOT exempt). Functions take the service-file list + deploy text as params so a unit test can
inject a synthetic un-provisioned bind and PROVE the guard flags it.
"""
import glob
import os
import re
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The deploy-stack "Derive the per-store dirs" set_fact default map: a {{ fact }} resolves to its today-exact
# path — the same literal the matching compose ${VAR:-DEFAULT} normalises to. Keep in sync with deploy-stack.yml.
FACT_DEFAULT = {
    "kontroll_storage_root": "/var/lib/kontroll",
    "kontroll_logs_dir": "/var/lib/kontroll/loki",
    "kontroll_backups_dir": "/var/lib/kontroll/backups",
    "kontroll_api_audit_dir": "/var/lib/kontroll/api/audit",
    "kontroll_gui_audit_dir": "/var/lib/kontroll/onboard-gui/audit",   # M11: onboard-gui audit off the code/propose tree
    "kontroll_ansible_log_dir": "/var/lib/kontroll/ansible-log",
    "kontroll_api_dir": "/var/lib/kontroll/api",
    "kontroll_gui_dir": "/var/lib/kontroll/onboard-gui",
    "kontroll_caddy_dir": "/var/lib/kontroll/caddy",
    "kontroll_vector_dir": "/var/lib/kontroll/vector",   # holds the file_tail_ssh read key (S11)
}
EXCLUDE_PREFIX = ("../", "/var/run", "/var/log", "/run/", "/etc/", "/srv/kontroll.git",
                  "${KONTROLL_CANONICAL_GIT", "${KONTROLL_OPERATOR_HOME")
IN_SCOPE_ROOT = "/var/lib/kontroll"
_UNWRAP_RE = re.compile(r'^\$\{[A-Z_]+:-([^}]*)\}(.*)$')   # ${VAR:-DEFAULT}<suffix> -> DEFAULT<suffix>


def _norm(p):
    return os.path.normpath(str(p).strip().strip('"').strip("'")).replace("\\", "/")


def _resolve_facts(p):
    for fact, default in FACT_DEFAULT.items():
        p = p.replace("{{ %s }}" % fact, default).replace("{{%s}}" % fact, default)
    return p


def _split_source(body):
    """The bind SOURCE = the substring before the first ':' that is NOT inside a ${...}. None if there is none
    (a named volume reference without a path, or a non-volume list item)."""
    depth = 0
    for i, ch in enumerate(body):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(0, depth - 1)
        elif ch == ":" and depth == 0:
            return body[:i].strip()
    return None


def _unwrap_compose(src):
    """A compose bind SOURCE -> its today-exact literal path. ${VAR:-DEFAULT}<suffix> -> DEFAULT<suffix>; a bare
    ${VAR} / ${VAR:?...} (no fail-closed default) -> None (the fail-closed rule is pinned by a separate test)."""
    m = _UNWRAP_RE.match(src)
    if m:
        return m.group(1) + m.group(2)
    if src.startswith("${"):
        return None
    return src


def compose_storage_sources(service_files):
    """[(relpath, lineno, normpath, raw_source)] for every IN-SCOPE storage bind source (resolves under
    /var/lib/kontroll, not excluded). Named volumes / host-OS / relative / canonical-git / operator-home drop out."""
    out = []
    for f in service_files:
        with open(f, encoding="utf-8") as fh:
            for n, line in enumerate(fh, 1):
                s = line.strip()
                if not s.startswith("- "):
                    continue
                src = _split_source(s[2:].strip().strip('"'))
                if not src or src.startswith(EXCLUDE_PREFIX):
                    continue
                lit = _unwrap_compose(src)
                if lit is None:
                    continue
                np = _norm(_resolve_facts(lit))
                # The compose-native installer (the PROVISIONER) binds the bare storage ROOT to CREATE the tree
                # deploy-stack then populates uid-scoped — the root itself is a non-secret parent, so it is not a
                # consumer G9 hole. NARROW: only installer.yaml, only the exact root (a subdir bind there still flags).
                if os.path.basename(f) == "installer.yaml" and np == IN_SCOPE_ROOT:
                    continue
                if np.startswith(IN_SCOPE_ROOT):
                    out.append((os.path.relpath(f, ROOT).replace(os.sep, "/"), n, np, src))
    return out


def _walk_tasks(node, acc):
    """Recursively collect task dicts from a play's task list (descend block/rescue/always)."""
    if isinstance(node, list):
        for item in node:
            _walk_tasks(item, acc)
    elif isinstance(node, dict):
        acc.append(node)
        for key in ("block", "rescue", "always"):
            if key in node:
                _walk_tasks(node[key], acc)


def _put_in(node, values):
    """`node` with every `{{ name }}` of `values` replaced by its value, in every string it holds."""
    if isinstance(node, str):
        for name, value in values.items():
            node = node.replace("{{ %s }}" % name, value).replace("{{%s}}" % name, value)
        return node
    if isinstance(node, list):
        return [_put_in(item, values) for item in node]
    if isinstance(node, dict):
        return {key: _put_in(item, values) for key, item in node.items()}
    return node


def _imported(tasks, playbook_dir):
    """The tasks of each task file a task of `tasks` imports (`import_tasks: <file>`, relative to the playbook's
    directory), with the import's `vars:` put in — so a provisioner moved into a task file is still seen."""
    out = []
    for t in tasks:
        if not isinstance(t, dict):
            continue
        rel = t.get("ansible.builtin.import_tasks") or t.get("import_tasks")
        if not isinstance(rel, str):
            continue
        with open(os.path.join(playbook_dir, rel), encoding="utf-8") as fh:
            sub = []
            _walk_tasks(yaml.safe_load(fh) or [], sub)
        values = {str(k): str(v) for k, v in (t.get("vars") or {}).items()}
        out.extend(_put_in(s, values) for s in sub)
    return out


def deploy_provisioners(deploy_text, playbook_dir=None):
    """(exact_owned, git_dests, stat_guarded): resolved today-exact paths deploy-stack provisions uid-scoped.
    exact_owned = file/copy/template tasks that set owner+group (loop items expanded); git_dests = git clone
    dests; stat_guarded = stat tasks with a failed_when (fail-closed file-absence guards). The tasks of a file
    the play imports count as its own (`playbook_dir`: where to find it; deploy-stack.yml's by default)."""
    tasks = []
    for doc in yaml.safe_load_all(deploy_text):
        for play in (doc if isinstance(doc, list) else []):
            if isinstance(play, dict):
                for key in ("pre_tasks", "tasks", "post_tasks", "handlers"):
                    if key in play:
                        _walk_tasks(play[key], tasks)
    tasks += _imported(tasks, playbook_dir or os.path.join(ROOT, "ansible", "playbooks"))
    exact_owned, git_dests, stat_guarded = set(), set(), set()
    _stat_registers = {}
    for t in tasks:
        if not isinstance(t, dict):
            continue
        for mod_key, path_key in (("file", "path"), ("copy", "dest"), ("template", "dest")):
            mod = t.get("ansible.builtin." + mod_key) or t.get(mod_key)
            if isinstance(mod, dict) and mod.get("owner") and mod.get("group"):
                p = mod.get(path_key)
                if not isinstance(p, str):
                    continue
                if "{{ item." in p and isinstance(t.get("loop"), list):
                    for it in t["loop"]:
                        if isinstance(it, dict) and "path" in it:
                            exact_owned.add(_norm(_resolve_facts(str(it["path"]))))
                else:
                    exact_owned.add(_norm(_resolve_facts(p)))
        git = t.get("ansible.builtin.git") or t.get("git")
        if isinstance(git, dict) and git.get("dest"):
            git_dests.add(_norm(_resolve_facts(str(git["dest"]))))
        stat = t.get("ansible.builtin.stat") or t.get("stat")
        if isinstance(stat, dict) and stat.get("path"):
            resolved = _norm(_resolve_facts(str(stat["path"])))
            if t.get("failed_when"):
                stat_guarded.add(resolved)               # self-contained guard
            elif t.get("register"):
                _stat_registers[str(t["register"])] = resolved   # …or half of a stat+assert pair

    # Second pass: a stat that only REGISTERS is fail-closed iff some assert requires its `.stat.exists`. Two
    # passes because the assert follows the stat (and nothing guarantees it must).
    for t in tasks:
        if not isinstance(t, dict):
            continue
        block = t.get("ansible.builtin.assert") or t.get("assert")
        if not isinstance(block, dict):
            continue
        that = block.get("that")
        conds = [that] if isinstance(that, str) else [str(c) for c in (that or [])]
        for register, resolved in _stat_registers.items():
            # Directional on purpose: `not X.stat.exists` asserts the file is ABSENT — a refuse-if-present check,
            # the opposite of a provisioner. Counting it would mark the bind covered by a guard that requires the
            # source not to exist.
            needle = re.compile(r"(?<!not )\b%s\.stat\.exists\b" % re.escape(register))
            if any(needle.search(c) for c in conds):
                stat_guarded.add(resolved)
    return exact_owned, git_dests, stat_guarded


def offending(service_files=None, deploy_text=None):
    """In-scope compose bind sources with NO uid-scoped deploy-stack provisioner. Empty == the C8 contract holds.
    Params are injectable so a unit test can prove the guard flags a synthetic un-provisioned store."""
    if service_files is None:
        service_files = sorted(glob.glob(os.path.join(ROOT, "docker", "services", "*.yaml")))
    if deploy_text is None:
        with open(os.path.join(ROOT, "ansible", "playbooks", "deploy-stack.yml"), encoding="utf-8") as fh:
            deploy_text = fh.read()
    exact_owned, git_dests, stat_guarded = deploy_provisioners(deploy_text)
    bad = []
    for rel, n, np, raw in compose_storage_sources(service_files):
        if np in exact_owned or np in stat_guarded:
            continue
        if any(np == d or np.startswith(d + "/") for d in git_dests):     # at-or-under a cloned repo tree
            continue
        bad.append((rel, n, np, raw))
    return bad


def main():
    bad = offending()
    if bad:
        print("storage bind source(s) with NO uid-scoped deploy-stack provisioner (G9 / C8 at-rest hole):")
        for rel, n, np, raw in bad:
            print("  %s:%d  %s  (-> %s)" % (rel, n, raw, np))
        return 1
    print("every storage-root-derived compose bind source has a uid-scoped deploy-stack provisioner (C8 at-rest)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
