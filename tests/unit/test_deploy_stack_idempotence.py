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

# compose's own lines (v5.3.1, captured byte for byte on 2026-10-20: one leading space, single spaces, one trailing
# space), all on stderr. A second `up -d` prints `Running` for every container it left alone, `Waiting`/`Healthy`
# for a dependency with a health check, and a warning for a volume it did not make; a change prints the rest.
_NOOP = [" Container semaphore-db Running ", " Container grafana Running ", " Container kontroll-api Running ",
         " Container vector Running ", " Container homepage Running "]
_NOOP_WAITING = [" Container semaphore-db Running ", " Container semaphore Running ",
                 " Container semaphore-db Waiting ", " Container semaphore-db Healthy ",
                 'time="2026-10-20T08:01:02Z" level=warning msg="volume kontroll-collections already exists but was '
                 'not created by Docker Compose. Use `external: true` to use an existing volume"']
_CHANGES = {
    "recreated": [" Container semaphore Recreate ", " Container semaphore Recreated ",
                  " Container semaphore Starting ", " Container semaphore Started "],
    "started": [" Container loki Running ", " Container vector Starting ", " Container vector Started "],
    "network": [" Network kontroll-socket Creating ", " Network kontroll-socket Created ",
                " Container vector Running "],
    "removed": [" Container kontroll-autopromoter Stopping ", " Container kontroll-autopromoter Removed "],
}


class _AnsibleUndefined(jinja2.StrictUndefined):
    """Ansible's undefined (ansible-core 2.18, the installer's): an attribute of it is undefined again, and any
    other use of it — iterating, comparing, printing — is an error that fails the task."""

    def __getattr__(self, name):
        if name[:2] == "__":
            raise AttributeError(name)
        return self


def _tasks(node):
    if isinstance(node, list):
        for item in node:
            yield from _tasks(item)
    elif isinstance(node, dict):
        yield node
        for key in ("block", "rescue", "always", "tasks", "pre_tasks", "post_tasks"):
            if key in node:
                yield from _tasks(node[key])


def _all_tasks():
    with open(_DEPLOY, encoding="utf-8") as fh:
        return list(_tasks(yaml.safe_load(fh)))


def _task(prefix):
    found = [t for t in _all_tasks() if str(t.get("name", "")).startswith(prefix)]
    assert len(found) == 1, "%d tasks named %r" % (len(found), prefix)
    return found[0]


def _evaluate(expression, **names):
    env = jinja2.Environment(undefined=_AnsibleUndefined)
    env.tests["search"] = lambda value, pattern: re.search(pattern, str(value)) is not None
    return env.from_string("{{ (%s) | string }}" % expression).render(**names) == "True"


def test_up_reports_a_change_only_when_compose_made_one():
    """The stack's `up` reports a change when compose created, recreated, started, stopped or removed something,
    and none when every container was left `Running` — with or without a dependency's `Waiting`/`Healthy` lines and
    the volume warning — and it pins compose's plain output, the lines it reads.
    Why: it was `changed_when: true`, so every deploy said the stack changed (the docstring above says what that
    hides); and under COMPOSE_PROGRESS=quiet or json compose prints none of those lines, so a recreate would read
    as no change."""
    task = _task("Bring up the selected stack service(s)")
    assert task.get("register") == "_stack_up"
    assert "docker compose --progress plain " in task["ansible.builtin.command"]["cmd"], \
        "`up` must pin the output format its change report reads"
    expression = str(task["changed_when"])
    for label, lines in (("every container Running", _NOOP), ("a dependency waited on", _NOOP_WAITING)):
        assert not _evaluate(expression, _stack_up={"stdout_lines": [], "stderr_lines": lines}), \
            "a second `up` must report no change: %s" % label
    for label, lines in _CHANGES.items():
        assert _evaluate(expression, _stack_up={"stdout_lines": [], "stderr_lines": lines}), label
        assert _evaluate(expression, _stack_up={"stdout_lines": lines, "stderr_lines": []}), label + " (stdout)"


def test_an_image_build_reports_a_change_only_when_the_id_moved():
    """Each image build (the runner, and the custom Vector image) registers its result, is preceded — as the task
    right before it, under the same `when` — by a look at the image it builds, and reports a change when the id
    moved or no image was there, and none when the layer cache gave back the image it had. A preview, where the
    module returns the image that was there and builds nothing, still reports that it would build; a run started
    past the look (`--start-at-task`) reports the build instead of failing on the missing look.
    Why: `rebuild: always` makes the module report a change on every run, so a deploy that rebuilt nothing said
    it rebuilt the runner — the second apply on the floating box (2026-10-20), where only one runner image
    existed, built by the first apply. A look at another tag, or after the build, would bring that back or hide a
    real build; and the installer's ansible-core 2.18 fails a task whose report reads a look that never ran."""
    tasks = _all_tasks()
    for build, look, result, before in (
            ("Build the Semaphore runner image", "Look at the Semaphore runner image before the build",
             "_runner_build", "_runner_image_before"),
            ("Build the custom Vector image", "Look at the custom Vector image before the build",
             "_vector_build", "_vector_image_before")):
        task, seen = _task(build), _task(look)
        assert task.get("register") == result, build
        assert seen.get("register") == before, look
        assert seen.get("when") == task["when"], "%s: the look runs exactly when the build does" % build
        assert tasks.index(seen) == tasks.index(task) - 1, "%s: the look is the task right before it" % build
        built = task["community.docker.docker_image_build"]
        name = built["name"] if ":" in built["name"] else "%s:%s" % (built["name"], built.get("tag", "latest"))
        assert seen["community.docker.docker_image_info"]["name"] == name, "%s: the look names its image" % build
        expression = str(task["changed_when"])

        def changed(after_id, before_ids, check=False):
            names = {"ansible_check_mode": check, result: {"image": {"Id": after_id} if after_id else {}}}
            if before_ids is not None:
                names[before] = {"images": [{"Id": i} for i in before_ids]}
            return _evaluate(expression, **names)

        assert not changed("sha256:aaa", ["sha256:aaa"]), "%s: the cache gave back the same image" % build
        assert changed("sha256:bbb", ["sha256:aaa"]), "%s: a new image" % build
        assert changed("sha256:aaa", []), "%s: the first build" % build
        assert changed("sha256:aaa", ["sha256:aaa"], check=True), "%s: a preview says it would build" % build
        assert changed("sha256:aaa", None), "%s: a run started past the look" % build


def test_an_image_build_keeps_its_id_when_the_cache_answers_it():
    """Each image build turns off BuildKit's default attestation.
    Why: both lab boxes run Docker's containerd image store (2026-10-20), which keeps the provenance attestation
    BuildKit adds to each build in a new manifest list — so a build the layer cache answered whole still got a new
    image id (measured on Docker 29.6.2: three cached builds, three ids; with the attestation off, one). The build
    then reported a change on every deploy and compose recreated what runs the image; the row above, which reads
    the id, cannot see that."""
    for build in ("Build the Semaphore runner image", "Build the custom Vector image"):
        assert str(_task(build).get("environment", {}).get("BUILDX_NO_DEFAULT_ATTESTATIONS")) == "1", build
