"""The deploy's git in a content clone — the SHAPE of the tasks that decide who runs it (C10).

WHY: armed, the network service writes its content clone (`…/api/repo`, `…/onboard-gui/repo`): its config, its
hooks. A root git there runs what the service put there as root. ansible/playbooks/tasks/content-clone.yml is the
one place deploy-stack.yml makes and updates a content clone: root's git only in a clone that is root's, the
service uid's git (setpriv, no group, no new privileges, a session and an environment of its own) in the
service's, and a clone held by the wrong uid removed — never chowned — and made again by its owner.
tests/integration/test_content_clone_as_service.py runs those tasks as root with real uids; these rows hold the
structure on any host, so an edit that moves a git back into the play, chowns a clone, or drops part of the
prefix fails here first.
"""
import os

import yaml

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_PLAYBOOKS = os.path.join(_ROOT, "ansible", "playbooks")
_DEPLOY = os.path.join(_PLAYBOOKS, "deploy-stack.yml")
_TASK_FILE = os.path.join(_PLAYBOOKS, "tasks", "content-clone.yml")
_PREFIX = ["setsid", "--wait", "setpriv", "--reuid=1001", "--regid=1001", "--clear-groups", "--no-new-privs", "--",
           "env", "-i"]


def _walk(node, parents=()):
    """(task, the tasks it sits inside) for every task of a task list, blocks descended."""
    if isinstance(node, list):
        for item in node:
            yield from _walk(item, parents)
    elif isinstance(node, dict):
        yield node, parents
        for key in ("block", "rescue", "always", "tasks", "pre_tasks", "post_tasks"):
            if key in node:
                yield from _walk(node[key], parents + (node,))


def _deploy():
    with open(_DEPLOY, encoding="utf-8") as fh:
        return fh.read()


def _deploy_tasks():
    return list(_walk(yaml.safe_load(_deploy())))


def _clone_tasks():
    with open(_TASK_FILE, encoding="utf-8") as fh:
        return [t for t, _ in _walk(yaml.safe_load(fh))]


def _when(task):
    when = task.get("when")
    return [] if when is None else ([str(when)] if not isinstance(when, list) else [str(w) for w in when])


def _named(fragment):
    found = [t for t in _clone_tasks() if fragment in str(t.get("name", ""))]
    assert len(found) == 1, "%d tasks of the clone file are named like %r" % (len(found), fragment)
    return found[0]


def test_the_play_itself_runs_no_git_in_a_content_clone_and_trusts_none_for_root():
    """deploy-stack.yml holds no git of its own for a content clone any more: no git module, no `git remote
    add`, no `safe.directory` (the two tasks that told ROOT's git to trust a clone the service owns are gone —
    that trust is what let root run the service's hooks), and no recursive chown of a clone."""
    text = _deploy()
    for word in ("safe.directory", "ansible.builtin.git:", "remote add"):
        assert word not in text, "deploy-stack.yml still holds %r" % word
    for task, _ in _deploy_tasks():
        module = task.get("ansible.builtin.file") or {}
        if isinstance(module, dict) and module.get("recurse"):
            assert "/repo" not in str(module.get("path", "")), "a recursive file task on a content clone: %r" % task


def test_each_surface_makes_its_clone_with_the_one_task_file():
    """Both content clones are made by the same task file — one rule, one copy of it — each imported inside the
    block of the surface it serves, with that surface's clone (the directory its compose file mounts) and the
    canonical at the path the containers mount it."""
    imports = [(t, parents) for t, parents in _deploy_tasks() if "ansible.builtin.import_tasks" in t]
    clones = [(t, parents) for t, parents in imports if t["ansible.builtin.import_tasks"] == "tasks/content-clone.yml"]
    assert len(clones) == 2, "expected the API's and onboard-gui's import, got %d" % len(clones)
    seen = {}
    for task, parents in clones:
        v = task["vars"]
        assert v["_clone_canonical"] == "/srv/kontroll.git"
        service = v["_clone_service"]
        seen[service] = v["_clone_dir"]
        assert any("'%s' in stack_services" % service in " ".join(_when(p)) for p in parents), \
            "the %s clone is not inside the block that runs only when %s is deployed" % (service, service)
        leaf = v["_clone_dir"].split("}}")[-1]
        assert leaf == "/repo", v["_clone_dir"]
        with open(os.path.join(_ROOT, "docker", "services", service + ".yaml"), encoding="utf-8") as fh:
            assert "/%s/repo:" % ("api" if service == "api" else "onboard-gui") in fh.read(), \
                "%s's compose file does not mount the clone this import makes" % service
    assert seen == {"api": "{{ kontroll_api_dir }}/repo", "onboard-gui": "{{ kontroll_gui_dir }}/repo"}, seen


def test_root_runs_git_only_unarmed_and_the_service_uid_runs_it_armed():
    """In the task file: the one git module task runs only when the deploy is NOT armed (the clone is then
    root's). Every command task is either the read-only look (`find`, unarmed) or the armed one, and the armed
    one starts with the exact prefix that makes it the service uid's: a session of its own (no terminal),
    uid and gid 1001, no supplementary group, no new privileges, an environment it builds — from `/`. No
    shell module, no become: nothing else starts a program in the clone."""
    tasks = _clone_tasks()
    gits = [t for t in tasks if "ansible.builtin.git" in t]
    assert len(gits) == 1 and "not (api_privileged | bool)" in _when(gits[0]), gits
    commands = [t for t in tasks if "ansible.builtin.command" in t]
    assert len(commands) == 2, [t.get("name") for t in commands]
    for task in commands:
        argv = task["ansible.builtin.command"]["argv"]
        if argv[0] == "find":
            assert "not (api_privileged | bool)" in _when(task) and "git" not in argv
            continue
        assert argv[:len(_PREFIX)] == _PREFIX, argv[:len(_PREFIX)]
        assert "api_privileged | bool" in _when(task)
        assert task["ansible.builtin.command"]["chdir"] == "/"
        assert [w.split("=")[0] for w in argv[len(_PREFIX):] if "=" in w][:6] == [
            "PATH", "HOME", "LC_ALL", "GIT_TERMINAL_PROMPT", "GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL"], argv
        assert "GIT_CEILING_DIRECTORIES={{ _clone_dir | dirname }}" in argv, "git may look above the clone"
    for task in tasks:
        for word in ("ansible.builtin.shell", "shell", "become", "become_user"):
            assert word not in task, "%s in %r" % (word, task.get("name"))


def test_root_changes_no_owner_in_a_clone_it_removes_one():
    """Nothing in the task file changes an owner, a group or a mode by path inside the clone, nor walks it
    recursively: a clone held by the wrong uid is REMOVED (the file module's `absent` — Python's rmtree, from
    open directories) and its owner makes it again; the one directory root makes for the armed clone is a new,
    empty one, made only where there is none. A recursive chown by path in a tree the service can write is
    a race the service can win."""
    for task in _clone_tasks():
        for key in ("ansible.builtin.file", "ansible.builtin.copy", "ansible.builtin.template"):
            module = task.get(key)
            if not isinstance(module, dict):
                continue
            target = str(module.get("path", module.get("dest", "")))
            if "_clone_dir" in target and module.get("state") == "directory":
                # root makes the armed clone's directory — new and empty — only when there is none
                assert module == {"path": "{{ _clone_dir }}", "state": "directory", "owner": "1001",
                                  "group": "1001", "mode": "0755"}, module
                assert "not _clone_st.stat.exists or (_clone_removed is changed)" in _when(task)
            elif "_clone_dir" in target:
                assert module == {"path": "{{ _clone_dir }}", "state": "absent"}, module
            assert not module.get("recurse"), task.get("name")
        argv = (task.get("ansible.builtin.command") or {}).get("argv", [])
        assert not any(word in ("chown", "chmod", "chgrp") for word in argv), task.get("name")


def test_the_clone_is_looked_at_as_itself_and_removed_before_any_git():
    """The look does not follow a link at the clone's path (`follow: false`: a link is not a clone, whoever
    owns what it points at); the refusal of a parent or storage root that is not root's alone comes first; the
    whole-tree look and the decision come before the removal, and the removal before either git — so no git,
    root's or the service's, ever starts in a clone that is to be removed."""
    names = [str(t.get("name", "")) for t in _clone_tasks()]
    st = _named("Look at the content clone")["ansible.builtin.stat"]
    assert st["follow"] is False and st["path"] == "{{ _clone_dir }}"
    order = [next(i for i, n in enumerate(names) if n.startswith(prefix)) for prefix in (
        "Look at the clone's parent", "Refuse unless the clone's parent", "Look at the content clone",
        "Unarmed — look for anything", "Decide whether the clone", "Remove the clone", "Unarmed — clone/update",
        "Armed — make the clone's directory", "Armed — clone/update")]
    assert order == sorted(order), list(zip(order, names))


def test_a_preview_never_starts_git_in_a_clone_it_would_remove():
    """In check mode the removal does not happen, so the clone a preview would remove is still there; the git
    module runs git in it even to preview (as root, in a clone that is not root's), and the service uid's git
    would refuse a clone of root's. Both git tasks therefore skip, in check mode, a clone that is not kept."""
    gate = "not ansible_check_mode or not _clone_st.stat.exists or (_clone_keep | bool)"
    for prefix in ("Unarmed — clone/update", "Armed — clone/update"):
        assert gate in _when(_named(prefix)), prefix


def test_the_service_uids_git_names_main_in_full():
    """The armed script fetches and resets by FULL ref names — `refs/heads/main` from the canonical into
    `refs/remotes/local/main`, and `main` checked out at that — because a short name resolves to a tag of the same name
    first, and the canonical's refs are written by the service too. `changed` is reported by the script's own
    last line, so a second deploy is `0 changed`."""
    task = _named("Armed — clone/update")
    script = task["vars"]["_clone_as_service"]
    assert "+refs/heads/main:refs/remotes/local/main" in script
    assert "checkout -q --force -B main refs/remotes/local/main" in script
    assert "ls-remote \"$url\" refs/heads/main" in script
    assert "exec </dev/null" in script
    assert task["changed_when"] == "'state=changed' in _clone_service_git.stdout_lines"


def test_every_task_of_the_clone_file_waits_out_a_fresh_node_preview():
    """On a brand-new node previewed with `--check` there is no canonical to clone (`_fresh_preview`): every task
    of the file that looks or acts stands down then (the one that only remembers a removal follows the removal,
    which stood down)."""
    for task in _clone_tasks():
        if str(task.get("name", "")).startswith("Remember a clone made again"):
            assert _when(task) == ["_clone_removed is changed"]
            continue
        assert "not (_fresh_preview | bool)" in " ".join(_when(task)), task.get("name")


def test_a_service_whose_clone_was_made_again_is_restarted_after_up():
    """A clone removed and made again is a NEW directory; a running container keeps the one it mounted, so the
    play restarts the services whose clone it replaced — after `up`, and only those `up` brought up."""
    names = [str(t.get("name", "")) for t, _ in _deploy_tasks()]
    up = names.index("Bring up the selected stack service(s)")
    restart = [i for i, n in enumerate(names) if n.startswith("Restart a service whose content clone was made again")]
    assert len(restart) == 1 and restart[0] > up
    task = _deploy_tasks()[restart[0]][0]
    assert "restart" in task["ansible.builtin.command"]["cmd"] and "_clones_replaced" in task["ansible.builtin.command"]["cmd"]
    assert "intersect(_up_services)" in " ".join(_when(task))


def test_each_clone_is_made_after_the_task_that_makes_its_parent_roots():
    """The task file refuses to go on unless the clone's parent (`{{ kontroll_api_dir }}`, `{{ kontroll_gui_dir }}`)
    is root's alone, so the parent must already be made root's when the import runs: inside each surface's block,
    the host-dirs task that makes it so comes BEFORE the import. Imported ahead of it, a fresh node's deploy stops
    on a missing parent, and a node the previous build left (its parent 1001's) stops on the refusal."""
    seen = 0
    for task, parents in _deploy_tasks():
        if task.get("ansible.builtin.import_tasks") != "tasks/content-clone.yml":
            continue
        block = parents[-1]["block"]
        parent_dir = task["vars"]["_clone_dir"].rsplit("/repo", 1)[0]
        here = next(i for i, t in enumerate(block) if t is task)
        makers = [i for i, t in enumerate(block)
                  if isinstance(t.get("ansible.builtin.file"), dict)
                  and t["ansible.builtin.file"].get("owner") == "{{ item.owner }}"
                  and t["ansible.builtin.file"].get("state") == "directory"
                  and any(isinstance(item, dict) and item.get("path") == parent_dir and item.get("owner") == "0"
                          for item in t.get("loop", []))]
        assert makers and makers[0] < here, "%s: its clone is imported before its parent is made root's" % (
            task["vars"]["_clone_service"])
        seen += 1
    assert seen == 2


def test_the_armed_script_is_not_expanded_by_the_command_module():
    """The armed task hands `sh -c` a script whose `$dest`, `$url`, `$mode`, `$changed` are the SCRIPT's. ansible's
    command module expands `$NAME` in its arguments from the deploy's environment unless told not to
    (`expand_argument_vars`, default true since ansible-core 2.16): the task must say `false`."""
    task = _named("Armed — clone/update")
    assert task["ansible.builtin.command"].get("expand_argument_vars") is False


def test_the_refusal_covers_the_clones_parent_and_the_storage_root_before_anything_else():
    """Before anything looks at the clone or runs git, the task file stats the clone's parent (the entry itself,
    not through a link) and the storage root, and refuses unless each is a directory of root's that neither its
    group nor anybody else may write — the parent because a writer of it could exchange the clone for a link
    between the look and root's git, the storage root because it holds the configuration file both gits read —
    and that anybody may pass through (`xoth`): armed, the service's git (uid and gid 1001, no supplementary
    group) reaches its clone through both and that file through the storage root, by the bit for others, and a
    root-owned 0700 storage root stops it with a git error instead."""
    tasks = _clone_tasks()
    look, refuse = tasks[0], tasks[1]
    paths = [(i["path"], i["follow"]) for i in look["loop"]]
    assert paths == [("{{ _clone_dir | dirname }}", False), ("{{ kontroll_storage_root }}", True)], paths
    that = " ".join(refuse["ansible.builtin.assert"]["that"])
    for needle in ("isdir", "uid | default(-1)) == 0", "wgrp", "woth", "xoth"):
        assert needle in that, needle
    assert refuse["loop"] == "{{ _clone_dirs_st.results }}"


def test_a_missing_storage_root_is_made_roots_and_passable_before_any_store_is_made_in_it():
    """deploy-stack.yml makes a MISSING storage root root's, 0711, and does so before any task writes a path under
    it. Ansible's `file` gives every parent it creates the owner and mode of the path it was asked for, so without
    this the first store would make the root its own (the Vector dir 10002 0700, the backups dir 1001 0700) and the
    clone file's refusal would stop the first apply of a fresh host-direct node. Only a missing root: one that
    exists is left to that refusal, which prints the fix, so the play never changes an owner it did not make. And
    only after the play has checked the root's path (absolute, never '/'): a relative root would otherwise be made
    before that check refused it. The look does not follow a link: a link there, even one whose target is missing,
    is a root that exists, left as it is."""
    tasks = [t for t, _ in _deploy_tasks()]
    guards = [i for i, t in enumerate(tasks) if isinstance(t.get("ansible.builtin.assert"), dict)
              and "kontroll_storage_root is match" in str(t["ansible.builtin.assert"].get("that"))]
    looks = [i for i, t in enumerate(tasks) if isinstance(t.get("ansible.builtin.stat"), dict)
             and t["ansible.builtin.stat"].get("path") == "{{ kontroll_storage_root }}"]
    makes = [i for i, t in enumerate(tasks) if isinstance(t.get("ansible.builtin.file"), dict)
             and t["ansible.builtin.file"].get("path") == "{{ kontroll_storage_root }}"]
    assert len(guards) == 1 and len(looks) == 1 and len(makes) == 1, (guards, looks, makes)
    look, make = tasks[looks[0]], tasks[makes[0]]
    assert guards[0] < looks[0] < makes[0], "the root is looked at or made before its path is checked"
    assert look["ansible.builtin.stat"].get("follow", False) is False, look
    register = look["register"]
    assert make["ansible.builtin.file"] == {"path": "{{ kontroll_storage_root }}", "state": "directory",
                                            "owner": "0", "group": "0", "mode": "0711"}
    assert _when(make) == ["not %s.stat.exists" % register], _when(make)
    derived = {name for t in tasks if isinstance(t.get("ansible.builtin.set_fact"), dict)
               for name, value in t["ansible.builtin.set_fact"].items() if "kontroll_storage_root" in str(value)}
    derived.discard("kontroll_storage_root")
    assert {"kontroll_vector_dir", "kontroll_backups_dir", "kontroll_api_dir"} <= derived, derived
    writers = ("ansible.builtin.file", "ansible.builtin.copy", "ansible.builtin.template", "ansible.builtin.command",
               "ansible.builtin.import_tasks")
    under = [i for i, t in enumerate(tasks) if i != makes[0] and any(k in t for k in writers)
             and any(name in str(t) for name in derived | {"kontroll_storage_root"})]
    assert len(under) >= 10, under
    early = [tasks[i].get("name") for i in under if i < makes[0]]
    assert not early, "these write under the storage root before it is made: %s" % early
