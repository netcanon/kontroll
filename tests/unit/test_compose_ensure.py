"""install-prereqs.sh asks `docker compose` before installing a compose package, and says so when none can be (I-21).

WHY (the failure these guard): with Docker already installed (a GitHub-hosted runner; a host with the distribution's
docker.io), install-prereqs.sh ran `pkg_install docker-compose-plugin` under `|| true`. That package is in Docker's
own repository, not the distribution's, so apt printed "E: Package 'docker-compose-plugin' has no installation
candidate". Where compose was present, the error was harmless, but it read as a failure. Where compose was NOT
present (the distribution's docker.io alone), the `|| true` hid the failed install: the script said "Done" and
compose was missing until the installer failed later (the T34 smoke's fourth run, public PR #116).

scripts/lib/pkg.sh's `compose_ensure` asks compose first and installs nothing when it answers. Otherwise, on apt, it
installs whichever compose v2 package the sources offer (Docker's docker-compose-plugin, else the distribution's
docker-compose-v2) and asks compose again after each. When compose still does not answer, it returns 1, naming both
packages. install-prereqs.sh calls it after whichever Docker path ran, still installs the rest of the floor, and
then exits 1 before "Done", saying why.

The runtime rows source pkg.sh under `sh` with stand-ins for `docker`, `apt-cache` and `apt-get` on the PATH. Each
stand-in records its calls, so the rows can say which packages were asked about and which were installed. Nothing
is installed on the machine running the tests. The wiring rows read install-prereqs.sh itself, which needs root and
a real package manager to run whole.
"""
import os
import re
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PKG = os.path.join(ROOT, "scripts", "lib", "pkg.sh")
PREREQS = os.path.join(ROOT, "scripts", "install-prereqs.sh")
SH = shutil.which("sh")
needs_sh = pytest.mark.skipif(SH is None or os.name == "nt", reason="needs a POSIX sh (the stand-ins are sh scripts)")

# The stand-ins. Each appends "<name> <args>" to $STATE/calls. `docker compose version` answers only once
# $STATE/compose exists. `apt-cache policy P` prints a candidate for a package listed in $STATE/candidates, a
# `Candidate: (none)` for one in $STATE/none, and nothing for any other, as apt-cache does for a name it has never
# heard of. `apt-get install … P` installs P only if it is a candidate (else apt's own E: line and exit 100), and
# makes compose answer only if P is listed in $STATE/provides.
DOCKER = """#!/bin/sh
echo "docker $*" >> "$STATE/calls"
if [ "$1 $2" = "compose version" ]; then
  [ -e "$STATE/compose" ] && { echo "Docker Compose version v2.29.1"; exit 0; }
  echo "docker: 'compose' is not a docker command." >&2; exit 1
fi
[ "$1" = "--version" ] && { echo "Docker version 26.1.5"; exit 0; }
exit 0
"""
APT_CACHE = """#!/bin/sh
echo "apt-cache $*" >> "$STATE/calls"
[ "$1" = policy ] || exit 0
if grep -qx "$2" "$STATE/candidates" 2>/dev/null; then
  printf '%s:\\n  Installed: (none)\\n  Candidate: 2.29.1-1\\n  Version table:\\n' "$2"
elif grep -qx "$2" "$STATE/none" 2>/dev/null; then
  printf '%s:\\n  Installed: (none)\\n  Candidate: (none)\\n  Version table:\\n' "$2"
fi
exit 0
"""
APT_GET = """#!/bin/sh
echo "apt-get $*" >> "$STATE/calls"
[ "$1" = install ] || exit 0
for p in "$@"; do case "$p" in -*|install) continue ;; esac; pkg="$p"; done
if ! grep -qx "$pkg" "$STATE/candidates" 2>/dev/null; then
  echo "E: Package '$pkg' has no installation candidate" >&2; exit 100
fi
grep -qx "$pkg" "$STATE/provides" 2>/dev/null && : > "$STATE/compose"
exit 0
"""


def _ensure(tmp_path, pkg_mgr="apt", compose=False, candidates=(), none=(), provides=()):
    """Source pkg.sh under sh with the stand-ins first on the PATH and run `compose_ensure` with PKG_MGR set; return
    its exit code, its stdout and stderr, and the stand-ins' calls in order."""
    bindir, state = tmp_path / "bin", tmp_path / "state"
    bindir.mkdir()
    state.mkdir()
    for name, body in (("docker", DOCKER), ("apt-cache", APT_CACHE), ("apt-get", APT_GET)):
        f = bindir / name
        f.write_text(body, encoding="utf-8", newline="\n")
        f.chmod(0o755)
    for name, items in (("candidates", candidates), ("none", none), ("provides", provides)):
        (state / name).write_text("".join(i + "\n" for i in items), encoding="utf-8", newline="\n")
    if compose:
        (state / "compose").write_text("", encoding="utf-8")
    env = dict(os.environ, PATH="%s%s%s" % (bindir, os.pathsep, os.environ.get("PATH", "")), STATE=str(state),
               PKG_MGR=pkg_mgr)
    r = subprocess.run([SH, "-c", '. "$1" && compose_ensure', "sh", PKG], env=env, capture_output=True, text=True,
                       timeout=60)
    calls_file = state / "calls"
    calls = calls_file.read_text(encoding="utf-8").splitlines() if calls_file.exists() else []
    return r.returncode, r.stdout, r.stderr, calls


def _installed(calls):
    """The packages the stand-in apt-get was asked to install, in order."""
    return [c.split()[-1] for c in calls if c.startswith("apt-get install")]


def _says_what_to_install(stderr):
    """The refusal names both compose v2 packages and Docker's repository, so the operator knows what to install."""
    assert "'docker compose' does not answer" in stderr, stderr
    assert "docker-compose-v2" in stderr and "docker-compose-plugin" in stderr, stderr
    assert "https://docs.docker.com/engine/install/" in stderr, stderr


@needs_sh
def test_a_host_whose_compose_answers_installs_nothing_and_prints_no_apt_error(tmp_path):
    """The I-21 case as the runner saw it: Docker with compose already there. compose_ensure returns 0 after asking
    compose once, and asks apt nothing, so no "has no installation candidate" line is printed. Before the fix, the
    script asked apt for docker-compose-plugin unconditionally."""
    rc, out, err, calls = _ensure(tmp_path, compose=True)
    assert rc == 0, (out, err)
    assert calls == ["docker compose version"], calls
    assert "E:" not in out + err, (out, err)


@needs_sh
def test_the_distributions_docker_gets_the_distributions_compose_package(tmp_path):
    """A host with the distribution's docker.io and no compose: docker-compose-plugin is not in its sources (apt has
    no entry), and docker-compose-v2 is. compose_ensure installs docker-compose-v2, never asks apt-get for the
    plugin, and returns 0 once compose answers. Before the fix, the plugin's failed install was hidden by `|| true`
    and compose stayed missing."""
    rc, out, err, calls = _ensure(tmp_path, candidates=["docker-compose-v2"], provides=["docker-compose-v2"])
    assert rc == 0, (out, err)
    assert _installed(calls) == ["docker-compose-v2"], calls
    assert "installing docker-compose-v2" in out, out
    assert "E:" not in out + err, (out, err)
    assert calls[-1] == "docker compose version", calls


@needs_sh
def test_dockers_own_repository_gets_dockers_plugin_first(tmp_path):
    """Where Docker's own repository is in the sources, both packages may be candidates. compose_ensure installs
    Docker's docker-compose-plugin, the one the apt deb822 path and the installer image install, and stops there."""
    both = ["docker-compose-plugin", "docker-compose-v2"]
    rc, out, err, calls = _ensure(tmp_path, candidates=both, provides=both)
    assert rc == 0, (out, err)
    assert _installed(calls) == ["docker-compose-plugin"], calls


@needs_sh
def test_a_package_with_no_candidate_is_not_asked_for(tmp_path):
    """apt knows docker-compose-plugin but has no version of it to install (`Candidate: (none)`, as when Docker's
    repository was removed). compose_ensure does not ask apt-get for it, so apt prints no E: line, and goes on to
    docker-compose-v2."""
    rc, out, err, calls = _ensure(tmp_path, candidates=["docker-compose-v2"], none=["docker-compose-plugin"],
                                  provides=["docker-compose-v2"])
    assert rc == 0, (out, err)
    assert _installed(calls) == ["docker-compose-v2"], calls
    assert "E:" not in out + err, (out, err)


@needs_sh
def test_no_compose_package_in_the_sources_is_refused_by_name(tmp_path):
    """Neither package is in the sources: compose_ensure installs nothing, returns 1, and says which packages would
    do, and where Docker's is. This is the case the old `|| true` passed, leaving the installer to fail later."""
    rc, out, err, calls = _ensure(tmp_path, none=["docker-compose-plugin"])
    assert rc == 1, (out, err)
    assert _installed(calls) == [], calls
    _says_what_to_install(err)


@needs_sh
def test_an_install_that_leaves_compose_silent_is_not_taken_for_compose(tmp_path):
    """A package installs but compose still does not answer (here docker-compose-plugin installs and provides
    nothing). compose_ensure asks compose again after each install, so it tries docker-compose-v2 next, and returns
    1 when that does not answer either. An install's exit code alone is not proof that compose works."""
    both = ["docker-compose-plugin", "docker-compose-v2"]
    rc, out, err, calls = _ensure(tmp_path, candidates=both)
    assert rc == 1, (out, err)
    assert _installed(calls) == both, calls
    _says_what_to_install(err)


@needs_sh
@pytest.mark.parametrize("pkg_mgr", ["dnf", "zypper", "pacman"])
def test_another_package_manager_with_no_compose_is_refused_by_name(tmp_path, pkg_mgr):
    """On dnf, zypper and pacman, get.docker.com is what installs compose. compose_ensure there asks compose,
    installs nothing (it asks apt nothing), and when compose does not answer returns 1 with the same words."""
    rc, out, err, calls = _ensure(tmp_path, pkg_mgr=pkg_mgr)
    assert rc == 1, (out, err)
    assert calls == ["docker compose version"], calls
    _says_what_to_install(err)


def _prereqs():
    with open(PREREQS, encoding="utf-8") as fh:
        return fh.read()


def test_install_prereqs_asks_compose_after_either_docker_path_and_no_longer_hides_a_failed_plugin():
    """install-prereqs.sh calls compose_ensure once, after the Docker section's last branch (get.docker.com) and
    before the daemon is started, so it runs whichever way Docker came: already there, apt, or get.docker.com. It no
    longer installs docker-compose-plugin under `|| true` where Docker is present, the I-21 line. The apt path's
    install of docker-ce and the plugin from Docker's repository stays as it was."""
    s = _prereqs()
    assert len(re.findall(r"^compose_ensure \|\| COMPOSE_OK=0$", s, re.M)) == 1, "one call, its result kept"
    assert s.index("curl -fsSL https://get.docker.com | sh") < s.index("compose_ensure ||") \
        < s.index("svc_enable_now docker"), "the call follows every Docker path, before the daemon is started"
    assert "pkg_install docker-compose-plugin; } || true" not in s
    assert "pkg_install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin" in s


def test_install_prereqs_exits_1_before_done_when_compose_does_not_answer():
    """When compose_ensure failed, install-prereqs.sh still installs the rest of the floor (the canonical group and
    the optional toolchain come after the call) and then exits 1 before it prints "Done", naming the packages. Before
    the fix it printed "Done" with compose missing, and the installer failed later."""
    s = _prereqs()
    tail = s[s.index("==> Host floor installed:"):]
    gate = re.search(r'if \[ "\$COMPOSE_OK" -eq 0 \]; then\n(.*?)\nfi\n', tail, re.S)
    assert gate, "the gate on COMPOSE_OK sits after the summary"
    assert gate.group(1).rstrip().endswith("exit 1"), gate.group(1)
    assert "docker-compose-v2" in gate.group(1) and "docker-compose-plugin" in gate.group(1), gate.group(1)
    assert tail.index("exit 1") < tail.index("==> Done"), "it exits before saying Done"
    assert s.index("compose_ensure ||") < s.index("canonical_group_ensure"), "the rest of the floor still runs"
    assert re.search(r"^COMPOSE_OK=1$", s, re.M) and s.index("COMPOSE_OK=1") < s.index("compose_ensure ||")
