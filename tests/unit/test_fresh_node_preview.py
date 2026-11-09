"""A `--check --diff` dry-run works on a brand-new node — without hiding anything that is genuinely wrong.

`--check` skips the tasks that CREATE things. On a node where nothing has ever been applied, the tasks that
then clone from the canonical, or take ownership of a TLS cert, have nothing to act on and fail:

    fatal: '/srv/kontroll.git' does not appear to be a git repository
    fatal: file (/var/lib/kontroll/api/tls/api.key) is absent, cannot continue

So the mandated dry-run could not pass on a fresh node — and `install-prereqs` prints `check` as step 3, before
the first apply (live-caught 2026-07-28 standing up the floating-prod box). That is worse than an
inconvenience: CLAUDE.md mandates `--check --diff` before any state-changing apply, and an operator who learns
the dry-run always fails the first time is one who will skip it when it counts.

The fix is one narrow fact, `_fresh_preview` = check mode AND no canonical yet, gating the tasks whose inputs
provably cannot exist: the content clones. The TLS owner tasks gate on narrower facts of their own, check mode AND
no cert yet, each read by a `stat` before its cert is made: "no canonical" never meant "no cert", and a node with a
canonical that first enabled a self_signed service failed its dry-run on the cert the apply would make (I-27,
2026-11-08). Two properties make each gate safe, and both are pinned here:

  * It is NARROW. On an already-provisioned box it is false, so the dry-run keeps FULL fidelity exactly where
    fidelity is worth something — previewing a change against a system that already holds state.
  * It does not silence REAL preconditions. The G9 age-key gate still fails a fresh-node preview, because the
    operator genuinely must provision that key, and telling them before they apply is the dry-run doing its
    job. "Make check exit 0" was never the goal; "make check tell the truth usefully" was.
"""
import os
import re

import yaml

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DEPLOY = os.path.join(_ROOT, "ansible", "playbooks", "deploy-stack.yml")


def _text():
    with open(_DEPLOY, encoding="utf-8") as fh:
        return fh.read()


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
    """deploy-stack.yml's tasks and those of each task file it imports (the content clones are made in one)."""
    tasks = list(_tasks(yaml.safe_load(_text())))
    for task in list(tasks):
        rel = task.get("ansible.builtin.import_tasks")
        if isinstance(rel, str):
            with open(os.path.join(os.path.dirname(_DEPLOY), rel), encoding="utf-8") as fh:
                tasks.extend(_tasks(yaml.safe_load(fh)))
    return tasks


def _named(fragment):
    for task in _all_tasks():
        if fragment in str(task.get("name", "")):
            return task
    raise AssertionError("no task whose name contains %r" % fragment)


def _when_of(task):
    when = task.get("when")
    if when is None:
        return []
    return [str(when)] if not isinstance(when, list) else [str(w) for w in when]


def test_the_flag_requires_BOTH_check_mode_and_a_missing_canonical():
    """Narrowness is the whole safety argument. `ansible_check_mode` alone would degrade every dry-run on every
    box; a missing-canonical test alone would skip real work during a real apply. Only the conjunction is safe."""
    expr = str(_named("Decide whether this dry-run can see past")["ansible.builtin.set_fact"]["_fresh_preview"])
    assert "ansible_check_mode" in expr, "must only ever engage in check mode — never during an apply"
    assert "_canonical_head.stat.exists" in expr, "must only engage when the canonical genuinely does not exist"
    assert " and " in expr, "the two conditions must be ANDed, not ORed"


def test_the_canonical_probe_reads_truthfully_in_check_mode():
    """A `stat` that is itself check-skipped returns nothing useful, so the flag would be computed from an
    undefined result. `check_mode: false` on a read-only task is the established pattern here (PR #57 fixed the
    same class for the path resolvers). It probes HEAD rather than the directory because a bind mount can leave
    an empty `/srv/kontroll.git` that is no repository at all."""
    task = _named("Detect a BRAND-NEW node being previewed")
    assert task.get("check_mode") is False, "the probe must run for real in check mode"
    assert task["ansible.builtin.stat"]["path"].endswith("/HEAD"), \
        "probe HEAD, not the directory — an empty dir exists but is not a repo"


def test_every_fresh_preview_gate_on_a_working_task_is_negative():
    """`_fresh_preview` means "cannot be previewed", so any task that DOES something must gate on NOT it. A
    positive gate would invert the meaning — the task would run ONLY during the fresh-node preview, which is
    exactly backwards and reads as correct at a glance.

    `debug` tasks are exempt and must be: the partial-preview banner exists precisely to fire when the flag is
    true. The distinction is doing work versus reporting, which is why the exemption is by module rather than
    by name — a second announcement should not have to be added to an allow-list."""
    offenders = []
    for task in _all_tasks():
        if "ansible.builtin.debug" in task or "debug" in task:
            continue
        for cond in _when_of(task):
            if "_fresh_preview" not in cond:
                continue
            if not re.search(r"not\s*\(?\s*_fresh_preview", cond):
                offenders.append("%s: %s" % (task.get("name", "<unnamed>"), cond.strip()))
    assert not offenders, "gates on _fresh_preview must be negative:\n  " + "\n  ".join(offenders)


def test_the_partial_preview_is_announced():
    """A dry-run that silently covers less than it appears to is worse than one that fails: the operator reads a
    wall of green and concludes the deploy was fully previewed. The banner must fire on exactly the flag, and say
    what a later `check` still skips: owning the cert of a service enabled later, said where it is. Guards the old
    words coming back ("from then on `check` previews the whole play"), false since I-27 skips that owning on any
    node."""
    task = _named("Say plainly that this is a partial preview")
    assert _when_of(task) == ["_fresh_preview | bool"], "the banner must fire on exactly the fresh-preview flag"
    msg = str(task["ansible.builtin.debug"]["msg"])
    assert "PARTIAL PREVIEW" in msg
    assert "SKIPPED" in msg, "it must say that steps were skipped, not merely that the node is new"
    assert "init" in msg, "and name the apply that makes the next check complete"
    assert "previews the whole play" not in msg, "a later check still skips owning a cert not made yet (I-27)"
    assert "a service enabled later" in msg and "said where it is skipped" in msg, \
        "it must say which owning a later check skips, and that it is said where"


def test_the_real_precondition_gate_is_NOT_silenced():
    """THE ONE THAT MATTERS. The remaining fresh-node failure is the G9 age key, and it is CORRECT: the operator
    must provision that key, and a dry-run reporting it before they apply is the dry-run earning its keep.

    The tempting "fix" for a still-red check is to add `_fresh_preview` to this gate too. That would convert a
    fail-closed security boundary into a silent pass on precisely the node where it has never been satisfied —
    trading a real warning for a green tick. Chasing exit 0 is how safety checks die."""
    task = _named("Refuse with the actual remedy")
    conds = " ".join(_when_of(task))
    assert "_fresh_preview" not in conds, \
        "the G9 age-key refusal must NOT be gated on the fresh-node preview — it is a real precondition, and " \
        "a fresh node is exactly where it has never been met"
    assert "onboard-gui" in conds, "still scoped to a deploy that actually brings the GUI up"


def test_the_gates_cover_the_tasks_that_actually_failed():
    """The four live-caught sites, by name, so a rename or a re-ordering that drops a gate is caught here rather
    than on the next fresh box. Each one failed a real fresh-node dry-run before this fix. The two clone sites
    are one task file now (tasks/content-clone.yml, imported for the API and for onboard-gui): its two git tasks,
    root's and the service uid's. The two TLS owner sites gate on their own cert's probe in a dry-run and not on
    `_fresh_preview`, which was false on a node with a canonical and no cert yet (I-27)."""
    for fragment in ("Unarmed — clone/update the local canonical into the content tree",
                     "Armed — clone/update the local canonical into the content tree"):
        conds = " ".join(_when_of(_named(fragment)))
        assert "_fresh_preview" in conds, "%r lost its fresh-node gate — a fresh dry-run will fail here" % fragment
    for fragment, probe in (("Own the TLS material as the runtime uid", "_api_crt"),
                            ("Own the onboard-gui TLS material", "_gui_crt")):
        conds = " ".join(_when_of(_named(fragment)))
        assert "not (ansible_check_mode and not %s.stat.exists)" % probe in conds, \
            "%r lost its no-cert-yet gate — a dry-run before its cert is made will fail here" % fragment
        assert "_fresh_preview" not in conds, \
            "%r is gated on the node being new again, not on its cert (I-27)" % fragment


def _block_tasks(fragment):
    """The tasks of the deploy block whose name contains `fragment`, in order."""
    for task in _all_tasks():
        if fragment in str(task.get("name", "")) and "block" in task:
            return task["block"]
    raise AssertionError("no block whose name contains %r" % fragment)


def test_each_cert_probe_reads_the_generators_own_path_before_it_runs_and_the_skip_is_said():
    """For the API and for onboard-gui: a `stat` of exactly the path the cert generator's `creates` names, marked
    `check_mode: false` as the canonical's probe is (a read-only probe says outright that it reads in a dry-run),
    registered as the owner task's probe, and placed BEFORE the generator, so in an apply it reads "absent" as well
    and the gate's check-mode term is all that keeps an apply owning what it made; then the owner task, gated on
    exactly the generator's gate AND not (a dry-run that found no cert); then a note, fired on exactly the case the
    owner task skips, says the owning was not previewed. Guards a probe of another path (the gate would judge a file
    the generator does not look at), a probe moved after the generator (an apply's gate would then pass without its
    check-mode term and nothing would notice it gone), an owner gate that loses `_needs_cert` (in byo_proxy/acme the
    probe is skipped, so every dry-run fails evaluating it and every apply fails on a cert kontroll does not make, or
    re-owns the operator's own), and a skip nothing reports (a dry-run that silently covers less than it appears
    to)."""
    for block, probe in (("Provision the API host artifacts", "_api_crt"),
                         ("Provision the onboard-gui host artifacts", "_gui_crt")):
        tasks = _block_tasks(block)
        names = [str(t.get("name", "")) for t in tasks]
        gen = next(i for i, t in enumerate(tasks) if "Generate a self-signed TLS cert" in names[i])
        look = next(i for i, t in enumerate(tasks) if t.get("register") == probe)
        own = next(i for i, t in enumerate(tasks) if "TLS material as the runtime uid" in names[i])
        say = next(i for i, t in enumerate(tasks) if "cannot preview owning" in names[i])
        assert look < gen < own < say, (block, names)
        stat_task, gen_task = tasks[look], tasks[gen]
        assert stat_task.get("check_mode") is False, "%s must read in a dry-run" % probe
        assert stat_task["ansible.builtin.stat"]["path"] == gen_task["ansible.builtin.command"]["creates"], \
            "%s must look at the cert the generator makes" % probe
        assert _when_of(stat_task) == ['_needs_cert.stdout == "yes"'], "probe on exactly the generator's gate"
        assert _when_of(gen_task) == ['_needs_cert.stdout == "yes"'], "the generator on exactly the resolver's answer"
        own_when = '_needs_cert.stdout == "yes" and not (ansible_check_mode and not %s.stat.exists)' % probe
        assert _when_of(tasks[own]) == [own_when], "the owner task on the generator's gate and its own probe, exactly"
        assert _when_of(tasks[say]) == ['_needs_cert.stdout == "yes" and ansible_check_mode and not %s.stat.exists'
                                       % probe], "the note must fire on exactly the skipped case"
        assert "NOT PREVIEWED" in str(tasks[say]["ansible.builtin.debug"]["msg"])
