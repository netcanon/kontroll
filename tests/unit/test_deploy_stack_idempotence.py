"""A second deploy reports 0 changed — deploy-stack.yml's change reports, evaluated.

WHY: CLAUDE.md holds every play to "a second apply must report 0 changed", and deploy-stack.yml's header said a
second run reports no change. It did not: on the floating box's deploy (2026-10-20) a second apply, which changed
nothing — every container kept the start time of the first — reported the runner image's build (`rebuild: always`
reports a change on every run) and the stack's `up` (`changed_when: true`). A report that is always "changed"
cannot tell anyone the run that did change something. These rows read the play's own expressions and evaluate
them with Jinja against what compose and the image-build module return, so the reports are tested, not trusted.
"""
import os
import re

import jinja2
import yaml

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DEPLOY = os.path.join(_ROOT, "ansible", "playbooks", "deploy-stack.yml")

# compose's own lines from the floating box, 2026-10-20: a second `up -d` (every container left alone), and the
# shapes a change prints (a rebuilt image recreating a container, a stopped one started, a new network).
_NOOP = [" Container semaphore-db  Running", " Container grafana  Running", " Container kontroll-api  Running",
         " Container vector  Running", " Container homepage  Running"]
_CHANGES = {
    "recreated": [" Container semaphore  Recreate", " Container semaphore  Recreated", " Container semaphore  Starting",
                  " Container semaphore  Started"],
    "started": [" Container loki  Running", " Container vector  Starting", " Container vector  Started"],
    "network": [" Network kontroll-socket  Creating", " Network kontroll-socket  Created",
                " Container vector  Running"],
    "removed": [" Container kontroll-autopromoter  Stopping", " Container kontroll-autopromoter  Removed"],
}


def _tasks(node):
    if isinstance(node, list):
        for item in node:
            yield from _tasks(item)
    elif isinstance(node, dict):
        yield node
        for key in ("block", "rescue", "always", "tasks", "pre_tasks", "post_tasks"):
            if key in node:
                yield from _tasks(node[key])


def _task(prefix):
    with open(_DEPLOY, encoding="utf-8") as fh:
        found = [t for t in _tasks(yaml.safe_load(fh)) if str(t.get("name", "")).startswith(prefix)]
    assert len(found) == 1, "%d tasks named %r" % (len(found), prefix)
    return found[0]


def _evaluate(expression, **names):
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined)
    env.tests["search"] = lambda value, pattern: re.search(pattern, str(value)) is not None
    return env.from_string("{{ (%s) | string }}" % expression).render(**names) == "True"


def test_up_reports_a_change_only_when_compose_made_one():
    """The stack's `up` reports a change when compose created, recreated, started, stopped or removed something,
    and none when every container was left `Running` — the second apply's output, measured on the floating box.
    Why: it was `changed_when: true`, so every deploy said the stack changed; the docstring above says what that
    hides."""
    task = _task("Bring up the selected stack service(s)")
    assert task.get("register") == "_stack_up"
    expression = str(task["changed_when"])
    assert not _evaluate(expression, _stack_up={"stdout_lines": [], "stderr_lines": _NOOP}), \
        "a second `up` that left every container Running must report no change"
    for label, lines in _CHANGES.items():
        assert _evaluate(expression, _stack_up={"stdout_lines": [], "stderr_lines": lines}), label
        assert _evaluate(expression, _stack_up={"stdout_lines": lines, "stderr_lines": []}), label + " (stdout)"


def test_an_image_build_reports_a_change_only_when_the_id_moved():
    """Each image build (the runner, and the custom Vector image) registers its result, is preceded by a look at
    the image it replaces, and reports a change when the id moved or no image was there — and none when the layer
    cache gave back the image it had. A preview, which builds nothing, still reports that it would build.
    Why: `rebuild: always` makes the module report a change on every run, so a deploy that rebuilt nothing said
    it rebuilt the runner — the second apply on the floating box (2026-10-20), where only one runner image
    existed, built by the first apply."""
    for build, look, result, before in (
            ("Build the Semaphore runner image", "Look at the Semaphore runner image before the build",
             "_runner_build", "_runner_image_before"),
            ("Build the custom Vector image", "Look at the custom Vector image before the build",
             "_vector_build", "_vector_image_before")):
        task = _task(build)
        assert task.get("register") == result, build
        assert _task(look).get("register") == before, look
        assert _task(look).get("when") == task["when"], "%s: the look runs exactly when the build does" % build
        expression = str(task["changed_when"])

        def changed(after_id, before_ids, check=False):
            names = {"ansible_check_mode": check, result: {"image": {"Id": after_id} if after_id else {}},
                     before: {"images": [{"Id": i} for i in before_ids]}}
            return _evaluate(expression, **names)

        assert not changed("sha256:aaa", ["sha256:aaa"]), "%s: the cache gave back the same image" % build
        assert changed("sha256:bbb", ["sha256:aaa"]), "%s: a new image" % build
        assert changed("sha256:aaa", []), "%s: the first build" % build
        assert changed("", ["sha256:aaa"], check=True), "%s: a preview says it would build" % build
