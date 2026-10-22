"""Every task that makes the config captures dir gives it the mode the deploy gives it (C8).

WHY: the captures hold device configs, secrets included, and SECURITY.md closes their dir by its own mode, `0700`,
under a storage root every local account may pass through. deploy-stack.yml makes it `0700`, but the nightly
backup (backup-configs.yml, and each class's `backup` entrypoint, which makes the dir itself) made the same dir
`0750`: each run undid the other, and the dir stood open to its group from the first backup to the next deploy.
Found on a box's `--check --diff` (2026-10-22). These rows hold every such task to the deploy's mode, so a class
added with `0750` copied from an older one fails here, not on a box.
"""
import glob
import os

import yaml

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_ANSIBLE = os.path.join(_ROOT, "ansible")
_DEPLOY = os.path.join(_ANSIBLE, "playbooks", "deploy-stack.yml")
_BACKUP_PLAY = os.path.join(_ANSIBLE, "playbooks", "backup-configs.yml")


def _walk(node):
    """Every task of a task list or play list, blocks descended."""
    if isinstance(node, list):
        for item in node:
            yield from _walk(item)
    elif isinstance(node, dict):
        yield node
        for key in ("block", "rescue", "always", "tasks", "pre_tasks", "post_tasks"):
            if key in node:
                yield from _walk(node[key])


def _dir_tasks(path, var):
    """The `file` tasks in `path` that make the directory `{{ var }}`."""
    with open(path, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    for task in _walk(doc):
        args = task.get("ansible.builtin.file") or task.get("file")
        if isinstance(args, dict) and args.get("state") == "directory" \
                and str(args.get("path", "")).replace(" ", "") == "{{%s}}" % var:
            yield args


def _capture_dir_tasks():
    """(file, args) for each task that makes `config_backup_dir`: the playbooks', and every role's task files'."""
    files = sorted(glob.glob(os.path.join(_ANSIBLE, "playbooks", "*.yml"))
                   + glob.glob(os.path.join(_ANSIBLE, "roles", "*", "tasks", "*.yml")))
    return [(os.path.relpath(f, _ROOT), args) for f in files for args in _dir_tasks(f, "config_backup_dir")]


def _deploy_mode():
    tasks = list(_dir_tasks(_DEPLOY, "kontroll_backups_dir"))
    assert len(tasks) == 1, tasks
    return str(tasks[0]["mode"])


def test_the_deploy_closes_the_captures_dir():
    """deploy-stack.yml makes the captures dir `0700`, as SECURITY.md states: the mode the rows below hold every
    other task to. If the deploy's own mode moved, a row comparing against it would pass on any value."""
    assert _deploy_mode() == "0700"


def test_every_task_that_makes_the_captures_dir_gives_the_deploys_mode():
    """Each task that makes `config_backup_dir` — the backup play's and each class's `backup` entrypoint's —
    sets the deploy's mode. One that sets another (`0750` was every one of them) changes the dir's mode at each
    backup, and the deploy changes it back: a dry-run that reports a change no one made, and a captures dir open
    to its group between the two."""
    want = _deploy_mode()
    found = _capture_dir_tasks()
    assert any(f == os.path.relpath(_BACKUP_PLAY, _ROOT) for f, _ in found), found
    assert any("/roles/" in f.replace(os.sep, "/") for f, _ in found), found
    wrong = [(f, args.get("mode")) for f, args in found if str(args.get("mode")) != want]
    assert not wrong, wrong
