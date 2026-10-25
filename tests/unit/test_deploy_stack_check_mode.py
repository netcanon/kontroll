"""deploy-stack.yml: read-only RESOLVER tasks must run under `--check` (`check_mode: false`).

WHY (the failure this guards — hit LIVE 2026-06-21 while arming the onboard-gui GUI propose path on prod):
the play resolves a few facts with `ansible.builtin.command` tasks that `register:` a var the `.env` render
+ early play vars then dereference — most critically `_secrets_dir_resolved.stdout` -> the `_secrets_dir`
play var -> every `community.sops.sops` lookup. A plain `command` task is SKIPPED under `--check`, so the
register is never set: `_secrets_dir` collapses to '' and the "Render docker/.env" task dies with
`could not locate file in lookup: /semaphore.sops.yml` (an absolute path = empty dir). That made the
CLAUDE.md-mandated `--check --diff` dry-run UNUSABLE for the stack's most important playbook.

The fix tags each read-only resolver `check_mode: false` so it executes even in check mode — they only
RESOLVE a path/flag/GID (no mutation), so running them under --check is safe. Which tasks are resolvers is found by
rule, not listed here: tests/unit/test_check_mode_resolvers.py holds every read-only command a control-node play
registers and reads again to the tag (a hand list of register names missed `_img_env`). This file pins the keystone
_secrets_dir resolver explicitly, and the resolver-vs-mutator boundary (the `up`/`gen-*` tasks must STAY
check-skipped so a dry-run never actuates).
"""
import os

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEPLOY = os.path.join(ROOT, "ansible", "playbooks", "deploy-stack.yml")

# A generator's read-only mode WITH the generator that implements it: `--env` means nothing to gen-observability.py,
# which would ignore the word and write. (`--check` is every generator's own dry mode.)
_READ_ONLY_MODES = {("scripts/gen-image-digests.py", "--env"), ("scripts/gen-requirements.py", "--fleet-check")}
_SHELL_SYNTAX = frozenset("&;|`$()<>")


def _gen_writes(cmd):
    """True when `cmd` runs a scripts/gen-* generator in a way this guard cannot call read-only. It is read-only
    only as ONE generator on ONE line, with no shell syntax, whose words after the script are exactly `--check` or
    exactly the one read-only mode listed for that script. Anything else — a second generator behind `&&`, a flag
    beside the mode (`gen-image-digests.py --env --refresh` refreshes: the network, and the lock rewritten) — is a
    writer until it is listed."""
    if "scripts/gen-" not in cmd:
        return False
    plain = cmd.replace("{{", "").replace("}}", "")                  # a Jinja expression is not shell syntax
    if cmd.count("scripts/gen-") != 1 or len(cmd.splitlines()) != 1 or _SHELL_SYNTAX & set(plain):
        return True
    words = cmd.split()
    at = next(n for n, word in enumerate(words) if "scripts/gen-" in word)
    rest = words[at + 1:]
    return not (rest == ["--check"] or any(script in words[at] and rest == [mode] for script, mode in _READ_ONLY_MODES))


def _walk(tasks):
    """Yield every leaf task dict, recursing into block/rescue/always containers."""
    for t in tasks or []:
        if not isinstance(t, dict):
            continue
        nested = False
        for key in ("block", "rescue", "always"):
            if isinstance(t.get(key), list):
                nested = True
                yield from _walk(t[key])
        if not nested:
            yield t


def _all_tasks():
    plays = yaml.safe_load(open(DEPLOY, encoding="utf-8"))
    out = []
    for play in plays if isinstance(plays, list) else []:
        if not isinstance(play, dict):
            continue
        for section in ("pre_tasks", "tasks", "post_tasks"):
            out += list(_walk(play.get(section)))
    return out


def _by_register():
    return {t["register"]: t for t in _all_tasks() if t.get("register")}


def _cmd_str(t):
    for k in ("ansible.builtin.command", "command", "ansible.builtin.shell", "shell"):
        v = t.get(k)
        if isinstance(v, dict):
            return str(v.get("cmd", ""))
        if v is not None:
            return str(v)
    return ""


def test_secrets_dir_resolver_keystone():
    """The `_secrets_dir_resolved` task — the exact one whose check-skip emptied `_secrets_dir` — exists, is
    the read-only `paths.py resolve` command, and carries check_mode:false + changed_when:false. Pinned
    explicitly so a refactor that drops/renames the keystone of the regression is caught loudly."""
    t = _by_register().get("_secrets_dir_resolved")
    assert t is not None, "the _secrets_dir_resolved resolver is gone — _secrets_dir would break under --check"
    assert t.get("check_mode") is False, "the keystone resolver must be check_mode:false"
    assert t.get("changed_when") is False, "the resolver is read-only — keep changed_when:false"
    assert "paths.py resolve" in _cmd_str(t), "the resolver should run scripts/kontroll/paths.py resolve"


def test_mutating_tasks_are_not_check_mode_false():
    """The compose `up` and `restart`, the `gen-*` regenerators and the stale-config record (`--write`) MUTATE
    (start or restart containers / write generated files / write the stamp); they must STAY check-skipped (never
    check_mode:false) so a `--check` dry-run never actuates. Pins the resolver-vs-mutator boundary the fix draws,
    so a future careless `check_mode: false` on a writer is caught — a stamp written in a dry-run would record a
    config no service loaded, and the next apply would leave that service on its old one. A generator's
    read-only modes are named one by one: a new mode is a writer here until it is listed."""
    offenders = []
    for t in _all_tasks():
        cmd = _cmd_str(t)
        # A gen-* call MUTATES only when it WRITES (the regenerators); its read-only modes (`--env`, `--check`,
        # `--fleet-check`) are resolvers, not writers — e.g. gen-image-digests.py --env reads the lock to feed the
        # .env pin (C0), and gen-requirements.py --fleet-check reads the fleet list and resolves nothing.
        # stale-config.py is the same: its find reads (check_mode:false, so a dry-run names the stale services);
        # its `--write` records.
        mutates = (("compose" in cmd and ("up" in cmd or " restart " in cmd)) or _gen_writes(cmd)
                   or ("stale-config.py" in cmd and "--write" in cmd))
        if mutates and t.get("check_mode") is False:
            offenders.append(t.get("name"))
    assert not offenders, "mutating task(s) must NOT be check_mode:false (would actuate during --check): %r" % offenders


@pytest.mark.parametrize("cmd,writes", [
    ("python3 scripts/gen-observability.py --fleet-check", True),
    ("python3 scripts/gen-requirements.py --fleet-check && python3 scripts/gen-backup.py", True),
    ("python3 scripts/gen-requirements.py --fleet-checkpoint", True),
    ("python3 scripts/gen-image-digests.py --env --refresh v1.2.3", True),
    ("python3 scripts/gen-observability.py; echo --check", True),
    ("python3 scripts/gen-requirements.py", True),
    ("python3 {{ playbook_dir }}/../../scripts/gen-observability.py", True),
    ("python3 scripts/gen-backup.py --check", False),
    ("python3 scripts/gen-image-digests.py --env", False),
    ("python3 -B scripts/gen-requirements.py --fleet-check", False),
])
def test_a_generator_command_is_read_only_only_in_a_listed_mode(cmd, writes):
    """The rule the check-mode guard applies to a `scripts/gen-*` command: read-only as ONE generator, without shell
    syntax, followed by exactly `--check` or exactly the mode listed for that script; a writer otherwise.

    Guards the guard: a rule that looked for a mode's word anywhere in the command called three writers read-only
    — a second generator chained behind the fleet check, `gen-image-digests.py --env --refresh` (which refreshes
    over the network and rewrites the lock), and a bare generator followed by `; echo --check` — so any of them
    could have been marked `check_mode: false` and run during the mandated dry-run."""
    assert _gen_writes(cmd) is writes
