"""install-prereqs.sh asks `docker compose` before installing a compose package, and says so when none can be (I-21).

WHY (the failure these guard): with Docker already installed (a GitHub-hosted runner; a host with the distribution's
docker.io), install-prereqs.sh ran `pkg_install docker-compose-plugin` under `|| true`. That package is in Docker's
own repository, not the distribution's, so apt printed "E: Package 'docker-compose-plugin' has no installation
candidate". Where compose was present, the error was harmless, but it read as a failure. Where compose was NOT
present (the distribution's docker.io alone), the `|| true` hid the failed install: the script said "Done" and
compose was missing until the installer failed later (the T34 smoke's fourth run, public PR #116).

scripts/lib/pkg.sh's `compose_ensure` asks compose first, as root and, given one, as the operator (the launcher runs
compose as the operator, whose own plugin root's docker cannot see), and installs nothing when it answers. Otherwise,
on apt, it installs the first compose v2 package the sources offer: Docker's docker-compose-plugin, Ubuntu's
docker-compose-v2, or Debian's docker-compose where its candidate is 2 or later. It reads apt's policy in the C
locale, never lets apt remove a package to make room, and asks compose again after each install. When compose still
does not answer, it returns 1, naming the packages; on dnf, zypper and pacman it installs nothing and says so.
install-prereqs.sh calls it after whichever Docker path ran, still installs the rest of the floor, and then exits 1
before "Done", saying why.

The compose_ensure rows source pkg.sh under `sh` with stand-ins for `docker`, `runuser`, `apt-cache`, `apt-get` and
the other package managers on the PATH. Each stand-in records its calls, so the rows can say which packages were
asked about and which were installed. The install-prereqs rows run a copy of the whole script under bash with
stand-ins for everything it would change as root (the package managers, docker, systemctl, groupadd, usermod,
getent, `id`); the copy's one change is where it writes apt's lock-timeout file, which the row asserts. Nothing is
installed on, or changed in, the machine running the tests. Two text rows read install-prereqs.sh as well.
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
LOCK_FILE = "/etc/apt/apt.conf.d/99kontroll-lock-timeout"
SH = shutil.which("sh")
BASH = shutil.which("bash")
needs_sh = pytest.mark.skipif(SH is None or os.name == "nt", reason="needs a POSIX sh (the stand-ins are sh scripts)")
needs_bash = pytest.mark.skipif(BASH is None or os.name == "nt",
                                reason="needs bash (install-prereqs.sh is a bash script; the stand-ins are sh)")

# The stand-ins. Each appends "<name> <args>" to $STATE/calls.
# - `docker compose version` answers once $STATE/compose exists, or, run by the runuser stand-in as user U, once
#   $STATE/compose-U exists (the operator's own plugin); a call made as U is recorded with " (as U)".
# - `runuser -u U -- CMD…` runs CMD with T_AS=U.
# - `apt-cache policy P` prints a candidate for a package listed in $STATE/candidates (a line "P" or "P VERSION"), a
#   `Candidate: (none)` for one in $STATE/none, and nothing for any other, as apt-cache does for a name it has never
#   heard of. Where $STATE/german exists, it prints apt's German words unless LC_ALL is C: gettext ignores LANG and
#   LANGUAGE under LC_ALL=C, and only then.
# - `apt-get install … P` installs P only if it is a candidate (else apt's own E: line and exit 100), and makes compose
#   answer only if P is listed in $STATE/provides. A P listed in $STATE/removes would remove docker-ce: apt refuses
#   under --no-remove (its own E: line), and otherwise records "REMOVED docker-ce" and installs.
DOCKER = """#!/bin/sh
echo "docker $*${T_AS:+ (as $T_AS)}" >> "$STATE/calls"
if [ "$1 $2" = "compose version" ]; then
  if [ -e "$STATE/compose" ] || { [ -n "${T_AS:-}" ] && [ -e "$STATE/compose-$T_AS" ]; }; then
    echo "Docker Compose version v2.29.1"; exit 0
  fi
  echo "docker: 'compose' is not a docker command." >&2; exit 1
fi
[ "$1" = "--version" ] && { echo "Docker version 26.1.5"; exit 0; }
exit 0
"""
RUNUSER = """#!/bin/sh
echo "runuser $*" >> "$STATE/calls"
[ "$1" = -u ] && [ "$3" = -- ] || exit 2
T_AS="$2"; export T_AS
shift 3
exec "$@"
"""
APT_CACHE = """#!/bin/sh
echo "apt-cache $*" >> "$STATE/calls"
[ "$1" = policy ] || exit 0
v="$(awk -v p="$2" '$1 == p { print ($2 == "" ? "2.29.1-1" : $2); exit }' "$STATE/candidates" 2>/dev/null)"
[ -n "$v" ] || { grep -qx "$2" "$STATE/none" 2>/dev/null && v="(none)"; }
[ -n "$v" ] || exit 0
echo "$2:"
if [ -e "$STATE/german" ] && [ "${LC_ALL:-}" != C ]; then
  [ "$v" = "(none)" ] && v="(keine)"
  echo "  Installiert: (keine)"; echo "  Installationskandidat: $v"; echo "  Versionstabelle:"
else
  echo "  Installed: (none)"; echo "  Candidate: $v"; echo "  Version table:"
fi
exit 0
"""
APT_GET = """#!/bin/sh
echo "apt-get $*" >> "$STATE/calls"
[ "$1" = install ] || exit 0
for p in "$@"; do case "$p" in -*|install) continue ;; esac; pkg="$p"; done
if ! awk -v p="$pkg" '$1 == p { f = 1 } END { exit !f }' "$STATE/candidates" 2>/dev/null; then
  echo "E: Package '$pkg' has no installation candidate" >&2; exit 100
fi
if grep -qx "$pkg" "$STATE/removes" 2>/dev/null; then
  case " $* " in *" --no-remove "*) echo "E: Packages need to be removed but remove is disabled." >&2; exit 100 ;; esac
  echo "REMOVED docker-ce" >> "$STATE/calls"
fi
grep -qx "$pkg" "$STATE/provides" 2>/dev/null && : > "$STATE/compose"
exit 0
"""
V1 = """#!/bin/sh
echo "docker-compose $*" >> "$STATE/calls"
echo "docker-compose version 1.29.2, build unknown"
exit 0
"""
# install-prereqs.sh's root-only commands. `id -u` says 0 (so the root check passes), and nothing else of `id` is
# answered. groupadd makes `getent group 1001` answer. python3 and sops stand in for the legacy toolchain's.
ID = """#!/bin/sh
[ "$1" = -u ] && echo 0
exit 0
"""
GROUPADD = """#!/bin/sh
echo "groupadd $*" >> "$STATE/calls"
: > "$STATE/group1001"
exit 0
"""
GETENT = """#!/bin/sh
[ "$1 $2" = "group 1001" ] && [ -e "$STATE/group1001" ] && { echo "kontroll:x:1001:"; exit 0; }
exit 2
"""
CURL = """#!/bin/sh
echo "curl $*" >> "$STATE/calls"
exit 1
"""


def _record(name):
    return '#!/bin/sh\necho "%s $*" >> "$STATE/calls"\nexit 0\n' % name


def _world(tmp_path, candidates=(), none=(), provides=(), removes=(), compose=False, operator_compose=(),
           german=False, v1=False, floor=False):
    """Write the stand-ins and their state under tmp_path; return (bindir, state). With `floor`, also the stand-ins
    for the root-only commands install-prereqs.sh runs."""
    bindir, state = tmp_path / "bin", tmp_path / "state"
    bindir.mkdir()
    state.mkdir()
    tools = {"docker": DOCKER, "runuser": RUNUSER, "apt-cache": APT_CACHE, "apt-get": APT_GET,
             "dnf": _record("dnf"), "zypper": _record("zypper"), "pacman": _record("pacman")}
    if v1:
        tools["docker-compose"] = V1
    if floor:
        tools.update({"id": ID, "groupadd": GROUPADD, "getent": GETENT, "curl": CURL,
                      "systemctl": _record("systemctl"), "rc-update": _record("rc-update"),
                      "usermod": _record("usermod"), "python3": _record("python3"), "sops": "#!/bin/sh\nexit 0\n"})
        # pkg_detect prefers apt-get, which is a stand-in: the other managers would never be asked
        for name in ("dnf", "zypper", "pacman"):
            del tools[name]
    for name, body in tools.items():
        f = bindir / name
        f.write_text(body, encoding="utf-8", newline="\n")
        f.chmod(0o755)
    for name, items in (("candidates", candidates), ("none", none), ("provides", provides), ("removes", removes)):
        (state / name).write_text("".join(i + "\n" for i in items), encoding="utf-8", newline="\n")
    if compose:
        (state / "compose").write_text("", encoding="utf-8")
    for user in operator_compose:
        (state / ("compose-" + user)).write_text("", encoding="utf-8")
    if german:
        (state / "german").write_text("", encoding="utf-8")
    return bindir, state


def _calls(state):
    calls_file = state / "calls"
    return calls_file.read_text(encoding="utf-8").splitlines() if calls_file.exists() else []


def _ensure(tmp_path, pkg_mgr="apt", as_user="", german=False, **world):
    """Source pkg.sh under sh with the stand-ins first on the PATH and run `compose_ensure <as_user>` with PKG_MGR set;
    return its exit code, its stdout and stderr, and the stand-ins' calls in order. With `german`, the caller's
    locale is a German one (LANG and LANGUAGE), as sudo and ssh pass it on."""
    bindir, state = _world(tmp_path, german=german, **world)
    env = dict(os.environ, PATH="%s%s%s" % (bindir, os.pathsep, os.environ.get("PATH", "")), STATE=str(state),
               PKG_MGR=pkg_mgr)
    env.pop("LC_ALL", None)
    if german:
        env.update(LANG="de_DE.UTF-8", LANGUAGE="de:en")
    r = subprocess.run([SH, "-c", '. "$1" && compose_ensure "$2"', "sh", PKG, as_user], env=env,
                       capture_output=True, text=True, timeout=60)
    return r.returncode, r.stdout, r.stderr, _calls(state)


def _prereqs_run(tmp_path, args=(), sudo_user=None, **world):
    """Run a copy of install-prereqs.sh (and the scripts/lib/ beside it) whole under bash, with the stand-ins and those
    for its root-only commands first on the PATH and Docker present; return its exit code, its stdout and stderr
    merged in order, the stand-ins' calls, the path it was run by, and the lock-timeout file it wrote. The copy's
    one change is that file's path (asserted to occur once), since the real one is under /etc. The floor's own
    packages are candidates (the stand-in apt-get reads the last package of an install)."""
    world["candidates"] = ["curl", "python3-venv"] + list(world.get("candidates", ()))
    bindir, state = _world(tmp_path, floor=True, **world)
    kit = tmp_path / "kit" / "scripts"
    (kit / "lib").mkdir(parents=True)
    for rel in ("lib/pkg.sh", "lib/canonical-group.sh"):
        shutil.copyfile(os.path.join(ROOT, "scripts", rel), kit / rel)
    lock = tmp_path / "apt.conf.d" / "99kontroll-lock-timeout"
    lock.parent.mkdir()
    assert " " not in lock.as_posix(), "the script writes the lock file through an unquoted redirect"
    text = _prereqs()
    assert text.count(LOCK_FILE) == 1, "the lock-timeout file is written once, where the copy redirects it"
    script = kit / "install-prereqs.sh"
    script.write_text(text.replace(LOCK_FILE, lock.as_posix()), encoding="utf-8", newline="\n")
    env = dict(os.environ, PATH="%s%s%s" % (bindir, os.pathsep, os.environ.get("PATH", "")), STATE=str(state))
    for var in ("SUDO_USER", "PKG_MGR", "LC_ALL", "DEBIAN_FRONTEND"):
        env.pop(var, None)
    if sudo_user:
        env["SUDO_USER"] = sudo_user
    r = subprocess.run([BASH, str(script)] + list(args), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       text=True, timeout=120)
    return r.returncode, r.stdout, _calls(state), str(script), (lock.read_text(encoding="utf-8")
                                                                 if lock.exists() else None)


def _installed(calls):
    """The packages the stand-in apt-get was asked to install, in order."""
    return [c.split()[-1] for c in calls if c.startswith("apt-get install")]


def _compose_installed(calls):
    """The compose packages among them (a whole run also installs the floor's git, tar and curl)."""
    return [p for p in _installed(calls) if p.startswith("docker-compose")]


def _says_what_to_install(stderr):
    """The refusal names the compose v2 packages, and Docker's repository, so the operator knows what to install."""
    assert "'docker compose' does not answer" in stderr, stderr
    assert "docker-compose-plugin" in stderr and "docker-compose-v2 on" in stderr, stderr
    assert "docker-compose on Debian 13" in stderr, stderr
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
    """apt knows docker-compose-plugin but has no version of it to install (`Candidate: (none)`, as when a pin of -1
    holds it, or another package names it and no source carries it). compose_ensure does not ask apt-get for it, so
    apt prints no E: line, and goes on to docker-compose-v2."""
    rc, out, err, calls = _ensure(tmp_path, candidates=["docker-compose-v2"], none=["docker-compose-plugin"],
                                  provides=["docker-compose-v2"])
    assert rc == 0, (out, err)
    assert _installed(calls) == ["docker-compose-v2"], calls
    assert "E:" not in out + err, (out, err)


@needs_sh
def test_a_host_whose_apt_speaks_another_language_still_gets_its_compose_package(tmp_path):
    """apt translates "Candidate:" (German: "Installationskandidat:"), and sudo and ssh pass the operator's LANG and
    LANGUAGE on. compose_ensure reads the policy under LC_ALL=C, so on such a host it still finds docker-compose-v2
    and installs it. Read in the caller's locale, the English word never matched, every package was skipped, and the
    script refused, advising the very package it had skipped (both reads of #137, S-1)."""
    rc, out, err, calls = _ensure(tmp_path, german=True, candidates=["docker-compose-v2"],
                                  provides=["docker-compose-v2"])
    assert rc == 0, (out, err)
    assert _installed(calls) == ["docker-compose-v2"], calls


@needs_sh
@pytest.mark.parametrize("version, installed", [("2.26.1-4", True), ("1.29.2-3", False), ("1:1.29.2-6", False)])
def test_debians_docker_compose_is_installed_only_where_it_is_compose_v2(tmp_path, version, installed):
    """Debian names compose v2 `docker-compose` (2.26.1 in Debian 13), where Ubuntu's is docker-compose-v2. Before
    Debian 13 that name is the v1 tool, which gives `docker-compose`, not `docker compose`. compose_ensure installs
    Debian's docker-compose when its candidate is 2 or later, and skips a v1 candidate, with or without an epoch.
    Before the fold, a Debian 13 host with docker.io was refused although its sources offered compose v2 (reader A's
    S-4)."""
    rc, out, err, calls = _ensure(tmp_path, candidates=["docker-compose " + version], provides=["docker-compose"])
    if installed:
        assert rc == 0, (out, err)
        assert _installed(calls) == ["docker-compose"], calls
    else:
        assert rc == 1, (out, err)
        assert _installed(calls) == [], calls
        _says_what_to_install(err)


@needs_sh
def test_the_legacy_v1_tool_on_the_path_is_not_taken_for_compose(tmp_path):
    """A host with the v1 `docker-compose` alone (Debian 12's docker.io + docker-compose 1.29) has no `docker compose`,
    which is what the stack and the installer run. compose_ensure does not ask the v1 tool, and refuses (reader A's
    N-2)."""
    rc, out, err, calls = _ensure(tmp_path, v1=True)
    assert rc == 1, (out, err)
    assert not [c for c in calls if c.startswith("docker-compose ")], calls
    _says_what_to_install(err)


@needs_sh
def test_a_compose_package_that_would_remove_the_engine_is_not_installed(tmp_path):
    """A compose package whose install would remove the engine already there (it Depends on a docker.io that
    Conflicts with docker-ce): with `-y` alone, apt-get removes docker-ce without asking. compose_ensure installs
    with --no-remove, so apt aborts, nothing is removed, and the refusal follows. Before the fold it returned 0 with
    the engine swapped, under a comment that said no engine is installed over Docker (reader A's N-4)."""
    rc, out, err, calls = _ensure(tmp_path, candidates=["docker-compose-v2"], provides=["docker-compose-v2"],
                                  removes=["docker-compose-v2"])
    assert rc == 1, (out, err)
    assert "REMOVED docker-ce" not in calls, calls
    assert [c for c in calls if c.startswith("apt-get install")] == \
        ["apt-get install -y -qq --no-remove docker-compose-v2"], calls
    _says_what_to_install(err)


@needs_sh
def test_the_operators_own_compose_counts_when_roots_does_not_answer(tmp_path):
    """The launcher runs compose as the operator, and a plugin in the operator's own ~/.docker/cli-plugins answers for
    them but not for root. Given the operator, compose_ensure asks as the operator (runuser) when root's compose does
    not answer, and installs nothing when theirs does. Before the fold such a host, which the launcher could use, was
    refused (reader A's S-2)."""
    rc, out, err, calls = _ensure(tmp_path, as_user="op", operator_compose=["op"])
    assert rc == 0, (out, err)
    assert calls == ["docker compose version", "runuser -u op -- docker compose version",
                     "docker compose version (as op)"], calls


@needs_sh
@pytest.mark.parametrize("as_user", ["", "root"])
def test_with_no_operator_only_roots_compose_counts(tmp_path, as_user):
    """With no operator, or root as the operator, compose_ensure asks root's compose only: another user's plugin does
    not make root's answer (the legacy host deploy runs compose as root). runuser is never called."""
    rc, out, err, calls = _ensure(tmp_path, as_user=as_user, operator_compose=["op"])
    assert rc == 1, (out, err)
    assert not [c for c in calls if c.startswith("runuser")], calls


@needs_sh
def test_no_compose_package_in_the_sources_is_refused_by_name(tmp_path):
    """No package is in the sources: compose_ensure installs nothing, returns 1, and says which packages would do,
    and where Docker's is. This is the case the old `|| true` passed, leaving the installer to fail later."""
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
def test_another_package_manager_installs_no_compose_and_says_so(tmp_path, pkg_mgr):
    """On dnf, zypper and pacman this script installs no compose package (on dnf, where Docker was absent,
    get.docker.com installed the plugin with the engine; it refuses openSUSE and Arch, reader A's N-6). compose_ensure
    there asks compose, asks no package manager anything (each
    has a recording stand-in), and when compose does not answer returns 1, saying it installs compose only on apt
    and naming Docker's plugin and that manager. It used to say a package "could not be installed from this host's
    package sources", which nothing had tried (reader A's S-3, reader B's N-3)."""
    rc, out, err, calls = _ensure(tmp_path, pkg_mgr=pkg_mgr)
    assert rc == 1, (out, err)
    assert calls == ["docker compose version"], calls
    assert "installs a compose" in err and "package only on apt" in err, err
    assert "docker-compose-plugin" in err and "with %s," % pkg_mgr in err, err
    assert "https://docs.docker.com/engine/install/" in err, err


@needs_bash
def test_install_prereqs_without_compose_installs_the_rest_of_the_floor_and_exits_1_before_done(tmp_path):
    """install-prereqs.sh, run whole, on a host whose Docker is there and whose compose is not, with no compose
    package on offer: it starts the daemon, provisions the canonical group, prints the summary with compose MISSING,
    then exits 1 with "NOT done" and never "Done". The re-run advice is the path the script was run by. Before the
    fix the script said "Done" here; the text rows alone let four mutants bring that back (both reads of #137, N-1
    and N-2)."""
    rc, out, calls, script, lock = _prereqs_run(tmp_path)
    assert rc == 1, out
    assert "==> Done" not in out, out
    assert out.index("==> The C10 canonical group") < out.index("==> Host floor installed:") \
        < out.index("compose : MISSING") < out.index("==> NOT done"), out
    assert "ERROR: Docker is installed but 'docker compose' does not answer" in out, out
    assert "re-run: sudo bash %s" % script in out, out
    assert "systemctl enable --now docker" in calls and "groupadd -g 1001 kontroll" in calls, calls
    assert lock == 'DPkg::Lock::Timeout "600";\n', lock


@needs_bash
def test_install_prereqs_with_a_compose_package_on_offer_installs_it_and_is_done(tmp_path):
    """The I-21 host: the distribution's Docker, and docker-compose-v2 in its sources. install-prereqs.sh, run whole,
    installs docker-compose-v2 (only), prints compose's version in the summary and says Done, rc 0."""
    rc, out, calls, script, lock = _prereqs_run(tmp_path, candidates=["docker-compose-v2"],
                                                provides=["docker-compose-v2"])
    assert rc == 0, out
    assert "==> Done" in out and "NOT done" not in out, out
    assert _compose_installed(calls) == ["docker-compose-v2"], calls
    assert "    compose : Docker Compose version v2.29.1\n" in out, out


@needs_bash
def test_install_prereqs_takes_the_operators_own_compose_and_installs_nothing(tmp_path):
    """Run by sudo (SUDO_USER=op) on a host where only the operator's own plugin answers: install-prereqs.sh asks as
    the operator, installs no compose package, says in the summary whose compose answered, and is Done. The launcher
    runs compose as the operator, so this host works (reader A's S-2)."""
    rc, out, calls, script, lock = _prereqs_run(tmp_path, sudo_user="op", operator_compose=["op"])
    assert rc == 0, out
    assert "==> Done" in out, out
    assert "    compose : Docker Compose version v2.29.1 (as op; root's does not answer)\n" in out, out
    assert _compose_installed(calls) == [], calls
    assert not [c for c in calls if c.startswith("apt-cache")], calls
    assert "usermod -aG docker op" in calls, calls


@needs_bash
def test_install_prereqs_with_host_ansible_needs_roots_compose(tmp_path):
    """With --with-host-ansible the legacy host deploy runs compose through ansible's become, as root, so the
    operator's own plugin is not enough: install-prereqs.sh asks root's compose only, and exits 1, NOT done, telling
    the operator to re-run with the flag they gave."""
    rc, out, calls, script, lock = _prereqs_run(tmp_path, args=["--with-host-ansible"], sudo_user="op",
                                                operator_compose=["op"])
    assert rc == 1, out
    assert "==> Done" not in out and "==> NOT done" in out, out
    assert not [c for c in calls if c.startswith("runuser")], calls
    assert "re-run: sudo bash %s --with-host-ansible" % script in out, out


def _prereqs():
    with open(PREREQS, encoding="utf-8") as fh:
        return fh.read()


def test_install_prereqs_asks_compose_after_either_docker_path_and_no_longer_hides_a_failed_plugin():
    """install-prereqs.sh's text calls compose_ensure once, after the Docker section's last branch (get.docker.com)
    and before the daemon is started, so it runs whichever way Docker came: already there, apt, or get.docker.com.
    It no longer installs docker-compose-plugin under `|| true` where Docker is present, the I-21 line. The apt
    path's install of docker-ce and the plugin from Docker's repository stays as it was. The run rows above hold
    what a run does; this row holds the text a run cannot reach (the get.docker.com branch)."""
    s = _prereqs()
    assert len(re.findall(r'^compose_ensure "\$COMPOSE_AS" \|\| COMPOSE_OK=0$', s, re.M)) == 1, "one call, kept"
    assert s.index("curl -fsSL https://get.docker.com | sh") < s.index('compose_ensure "$COMPOSE_AS" ||') \
        < s.index("svc_enable_now docker"), "the call follows every Docker path, before the daemon is started"
    assert "pkg_install docker-compose-plugin; } || true" not in s
    assert "pkg_install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin" in s


def test_install_prereqs_exits_1_before_done_when_compose_does_not_answer():
    """install-prereqs.sh's text puts the gate on COMPOSE_OK after the summary and the rest of the floor (the
    canonical group and the optional toolchain come after the call), and its exit 1 before "Done", pointing at the
    ERROR compose_ensure printed and the command as it was run. Before the fix it printed "Done" with compose
    missing, and the installer failed later."""
    s = _prereqs()
    tail = s[s.index("==> Host floor installed:"):]
    gate = re.search(r'if \[ "\$COMPOSE_OK" -eq 0 \]; then\n(.*?)\nfi\n', tail, re.S)
    assert gate, "the gate on COMPOSE_OK sits after the summary"
    assert gate.group(1).rstrip().endswith("exit 1"), gate.group(1)
    assert "the ERROR above names" in gate.group(1) and "sudo bash $0 $*" in gate.group(1), gate.group(1)
    assert tail.index("exit 1") < tail.index("==> Done"), "it exits before saying Done"
    assert s.index('compose_ensure "$COMPOSE_AS" ||') < s.index("canonical_group_ensure"), "the rest still runs"
    assert re.search(r"^COMPOSE_OK=1$", s, re.M) and s.index("COMPOSE_OK=1") < s.index("compose_ensure \"$COMPOSE_AS")
