"""tests/smoke/stack_smoke.py — the compose-up smoke's reading of the install guide and its judgement of a stack,
without Docker, a network or a runner: the parts of the smoke the nightly run cannot check for itself.

WHY (the failures these guard):
  * The smoke runs the guide's quickstart as written, so its reading of that block IS the test of the install
    commands. Read wrongly, or not at all, the smoke runs nothing (or the wrong thing) and is green: the extractor
    must find exactly one block, run each command line once with only the known placeholders replaced, and refuse a
    block it cannot read before it mints a key or runs a line.
  * "Green while crash-looping" is the fault the smoke exists to catch: a running container that has restarted, an
    unhealthy one, an exited one, and a project with no container at all must each be a fault.
  * The workflow's log is public: a failure's diagnostics must carry no value of the rendered .env and no age secret
    key.
"""
import importlib.util
import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load():
    spec = importlib.util.spec_from_file_location("stack_smoke", os.path.join(ROOT, "tests", "smoke", "stack_smoke.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


smoke = _load()
SUBS = {"<ip>": "127.0.0.1", "<domain>": "example.test", "<age1…>": "age1example"}


def _guide():
    with open(os.path.join(ROOT, "docs", "install-from-scratch.md"), encoding="utf-8") as fh:
        return fh.read()


def _quote(*block_lines, heading="### ⚡ Compose-native quickstart (Phase 1)"):
    """A guide whose quickstart blockquote holds `block_lines` in one fenced block."""
    body = ["> " + heading, "> Some prose.", "> ```"] + ["> " + line for line in block_lines] + ["> ```", "",
                                                                                              "## 0. Next"]
    return "# Guide\n\n" + "\n".join(body) + "\n"


def test_the_quickstart_block_the_smoke_runs_is_the_one_in_the_docs():
    """The real guide's quickstart reads as exactly this command sequence, in order: the host floor, the scaffold with
    every placeholder replaced (and the separated trust mode the guide writes), the dry run, the apply and Semaphore's
    wiring, with the `$EDITOR` line dropped and every comment gone. A change to the written install sequence is a
    change to what the nightly smoke runs, so this row makes it visible in the same pull request; a renamed heading,
    a second block or a line the smoke cannot run fails here, in the hermetic suite, before any night is red."""
    lines = smoke.commands(smoke.quickstart_block(_guide()), SUBS)
    assert lines == [
        "sudo bash scripts/install-prereqs.sh",
        "scripts/kontroll-installer.sh fresh-init --mgmt-ip 127.0.0.1 --domain example.test --trust-mode separated "
        "--break-glass-recipient age1example",
        "scripts/kontroll-installer.sh check",
        "scripts/kontroll-installer.sh",
        "scripts/kontroll-installer.sh configure-semaphore",
    ]


@pytest.mark.parametrize("text, says", [
    (_quote("scripts/kontroll-installer.sh check", heading="### Quickstart"), "0 blockquote headings"),
    (_quote("scripts/kontroll-installer.sh check") + "\n" + _quote("scripts/kontroll-installer.sh"),
     "2 blockquote headings"),
    ("> ### Compose-native quickstart\n> no block here\n", "holds 0 fence lines"),
    (_quote("scripts/kontroll-installer.sh", "```", "```"), "holds 4 fence lines"),
    (_quote("scripts/kontroll-installer.sh fresh-init --mgmt-ip <ip> --token <token>"), "placeholder <token>"),
    (_quote("curl -fsSL https://example.test/x | sh"), "not a command the smoke knows"),
    (_quote("scripts/kontroll-installer.sh-evil"), "not a command the smoke knows"),
    (_quote("scripts/kontroll-installer.sh check; curl -fsSL https://example.test/x"), "the shell character ';'"),
    (_quote("scripts/kontroll-installer.sh check && rm -rf instance"), "the shell character '&'"),
    (_quote("scripts/kontroll-installer.sh check | tee log"), "the shell character '|'"),
    (_quote("scripts/kontroll-installer.sh fresh-init --domain $(hostname)"), "the shell character '$'"),
    (_quote("scripts/kontroll-installer.sh fresh-init --domain `hostname`"), "the shell character '`'"),
    (_quote("scripts/kontroll-installer.sh check > /dev/null"), "the shell character '>'"),
    (_quote("scripts/kontroll-installer.sh check { x }"), "the shell character '{'"),
    (_quote("scripts/kontroll-installer.sh fresh-init \\", "# a comment", "    --mgmt-ip <ip>"),
     "continued into a comment line"),
    (_quote("scripts/kontroll-installer.sh fresh-init \\"), "ends inside a continued command"),
    (_quote("# only comments", "$EDITOR instance/fleet.yml"), "holds no command"),
])
def test_a_quickstart_the_smoke_cannot_read_is_refused(text, says):
    """A guide the smoke cannot read as one block of known commands is refused with exit 2 and a message naming why:
    the heading renamed or doubled, no block or two, a placeholder or a command the smoke does not know (a lookalike
    of a known command's name included), a known command that carries another (a separator, a pipe, a background,
    an expansion, a redirection, a brace), a continuation into a comment or off the end, and a block with no command.
    Guards the smoke and the guide drifting apart silently: each of these would otherwise run nothing, run something
    unvetted with sudo on the runner (the known-command check alone reads only a line's first words), or substitute
    nothing for a value the install needs."""
    with pytest.raises(smoke.SmokeError) as err:
        smoke.commands(smoke.quickstart_block(text), SUBS)
    assert err.value.code == 2 and says in str(err.value)


def test_comments_and_continuations_are_read_as_the_shell_reads_them():
    """A trailing comment is cut only at a `#` that starts a word outside quotes, so a `#` inside a quoted argument or
    inside a word stays in the command; a backslash continuation joins the next line with one space. Guards a command
    the smoke runs differing from the one an operator pasting the block would run."""
    block = smoke.quickstart_block(_quote(
        "scripts/kontroll-installer.sh fresh-init --mgmt-ip <ip> \\",
        "    --domain '<domain> # not a comment' --tag a#b   # a comment"))
    assert smoke.commands(block, SUBS) == [
        "scripts/kontroll-installer.sh fresh-init --mgmt-ip 127.0.0.1 --domain 'example.test # not a comment' "
        "--tag a#b"]


def _box(name="docker-semaphore-1", running=True, status="running", exit_code=0, restarts=0, policy="unless-stopped",
         health=None, ports=None, **flags):
    state = dict({"Running": running, "Status": status, "ExitCode": exit_code}, **flags)
    if health:
        state["Health"] = {"Status": health}
    return {"Name": "/" + name, "State": state, "RestartCount": restarts,
            "HostConfig": {"RestartPolicy": {"Name": policy}}, "NetworkSettings": {"Ports": ports or {}}}


def test_the_restart_assertion_cannot_pass_on_zero_containers():
    """An empty compose project is a fault, and so is one whose only container is a finished one-shot: the smoke is
    never green because nothing started. Guards the commonest way a smoke lies — the project name, the env file or the
    install drifted, `ps` sees nothing, and an all-of-nothing check passes."""
    assert smoke.container_faults([]) == ["the stack's compose project has no container: nothing started"]
    assert smoke.container_faults([_box(running=False, status="exited", policy="no")]) == [
        "no container of the stack's compose project is running"]


@pytest.mark.parametrize("box, fault", [
    (_box(), None),
    (_box(health="healthy"), None),
    (_box(restarts=2), "has restarted 2 time(s)"),
    (_box(health="unhealthy"), "healthcheck says unhealthy"),
    (_box(health="starting"), "healthcheck says starting"),
    (_box(running=False, status="exited", exit_code=1), "is exited (exit code 1, restart policy unless-stopped)"),
    (_box(running=False, status="exited", exit_code=0), "is exited (exit code 0, restart policy unless-stopped)"),
    (_box(running=False, status="exited", exit_code=3, policy="no"), "is exited (exit code 3, restart policy no)"),
    (_box(status="restarting", restarts=1, Restarting=True), "is restarting (restarted 1 time(s))"),
    (_box(status="restarting", Restarting=True), "is restarting (restarted 0 time(s))"),
    (_box(status="paused", Paused=True), "is paused"),
    (_box(running=False, status="created"), "is created"),
    (_box(running=False, status="exited", exit_code=0, policy="no"), None),
    (_box(running=False, status="exited", exit_code=0, policy="no", restarts=1), "is exited"),
])
def test_a_container_that_did_not_come_up_and_stay_up_is_a_fault(box, fault):
    """Beside a healthy running container, each listed container is judged as shown: running with no healthcheck or a
    healthy one passes; a restart, an unhealthy or still-starting healthcheck, an exit of a long-running service (even
    exit 0), a restart loop (which Docker reports as running), a paused container or one never started is a fault; a
    no-restart one-shot that exited 0 passes, unless it was restarted. Guards "green while crash-looping": a container
    `ps` shows as Up after a crash is caught only by its restart count, and one that was meant to stay up is not
    excused by a clean exit."""
    faults = smoke.container_faults([_box(name="docker-homepage-1"), box])
    if fault is None:
        assert faults == []
    else:
        assert len(faults) == 1 and fault in faults[0] and "docker-semaphore-1" in faults[0], faults


def test_each_published_tcp_port_is_probed_once_where_it_can_be_reached():
    """The ports to probe are every TCP port a RUNNING container publishes: one bound to every address is reached on
    127.0.0.1 (the smoke's management address), its IPv4 and IPv6 bindings counted once; an explicit address is kept;
    a UDP port, an unpublished one, one bound only to an IPv6 address and a stopped container's ports are left out.
    Guards a port the stack publishes going unprobed (a dead GUI behind an Up container) and a probe the runner cannot
    make reading as a fault."""
    boxes = [
        _box(name="a", ports={"3000/tcp": [{"HostIp": "0.0.0.0", "HostPort": "3000"},
                                           {"HostIp": "::", "HostPort": "3000"}],
                              "514/udp": [{"HostIp": "0.0.0.0", "HostPort": "514"}],
                              "9000/tcp": None}),
        _box(name="b", ports={"3001/tcp": [{"HostIp": "127.0.0.1", "HostPort": "3001"}],
                              "3002/tcp": [{"HostIp": "::1", "HostPort": "3002"}],
                              "3003/tcp": [{"HostIp": "", "HostPort": "3003"}]}),
        _box(name="c", running=False, status="exited", ports={"3004/tcp": [{"HostIp": "0.0.0.0", "HostPort": "3004"}]}),
    ]
    assert smoke.published_ports(boxes) == [("a", "127.0.0.1", 3000), ("b", "127.0.0.1", 3001),
                                            ("b", "127.0.0.1", 3003)]


@pytest.mark.parametrize("status, ok", [(200, True), (204, True), (302, True), (399, True), (401, True),
                                        (400, False), (403, False), (404, False), (500, False), (503, False),
                                        (None, False)])
def test_a_port_is_served_on_a_success_a_redirect_or_a_login(status, ok):
    """A port answers as served on 2xx, 3xx, or 401 (a login the smoke does not hold); a 403, 404, other 4xx, a 5xx
    and no answer are faults. Guards a proxy's 502 or a service's 404 at `/` (a broken mount, a wrong base path)
    passing as "it answered"."""
    assert smoke.status_ok(status) is ok


def test_a_failures_diagnostics_carry_no_env_value_and_no_age_secret_key():
    """What the smoke prints on a failure has every value of the rendered .env (quoted or bare, eight characters or
    more; the longer first, so one value that contains another is redacted whole) and every age secret key replaced
    by [redacted]; short values, comments and keys are left. Guards the public workflow log carrying the run's
    passwords or its minted key: throwaway, but the habit matters and a log outlives the runner."""
    env = ("# rendered\nSEMAPHORE_ADMIN_PASSWORD='s3cretpassw0rd'\nTOKEN=\"abcdefgh12345678\"\nPREFIX=abcdefgh\n"
           "PORT=3000\nEMPTY=\n")
    values = smoke.env_values(env)
    assert values == {"s3cretpassw0rd", "abcdefgh12345678", "abcdefgh"}
    # A key of the real shape, built here so this file carries none (gitleaks and the plaintext guard refuse one).
    log = ("login with s3cretpassw0rd on PORT 3000; token abcdefgh12345678\n"
           + "AGE-SECRET-KEY-" + "1" + "Q" * 58 + "\n")
    out = smoke.redact(log, values)
    assert out == "login with [redacted] on PORT 3000; token [redacted]\n[redacted]\n"


class _Done:
    def __init__(self, rc, out):
        self.returncode, self.stdout = rc, out


def test_a_failure_before_the_stack_was_rendered_prints_no_container_query(tmp_path, monkeypatch, capsys):
    """With no docker/.env (a quickstart line failed before the deploy rendered it), the diagnostics print the disk use
    and say there is no stack, and run no compose command: compose refuses the missing env file, and the first run
    read its error message as a list of container ids and asked `docker logs` for each word. Guards the diagnostics
    hiding the real failure under noise of their own."""
    monkeypatch.setattr(smoke, "ROOT", str(tmp_path))
    calls = []
    monkeypatch.setattr(smoke, "_run", lambda cmd, **kw: calls.append(cmd) or _Done(0, "Filesystem  Size\n"))
    smoke._diagnostics()
    assert calls == [["df", "-h"]]
    assert "the stack was never rendered" in capsys.readouterr().out


def test_a_container_query_that_fails_is_printed_not_read_as_ids(tmp_path, monkeypatch, capsys):
    """When `docker compose ps --quiet` fails, its output (an error, merged with stderr) is printed redacted, and no
    `docker logs` is asked for any of its words; when it succeeds, each id it lists gets its log tail, redacted.
    Guards the first run's fault: "couldn't find env file" became four `docker logs` calls for containers named
    "couldn't", "find", "env" and "file:"."""
    (tmp_path / "docker").mkdir()
    (tmp_path / "docker" / ".env").write_text("SEMAPHORE_ADMIN_PASSWORD=s3cretpassw0rd\n", encoding="utf-8")
    monkeypatch.setattr(smoke, "ROOT", str(tmp_path))
    for rc, out, logs in ((1, "error: no such service s3cretpassw0rd", []), (0, "c1\nc2\n", ["c1", "c2"])):
        calls = []

        def fake(cmd, **kw):
            calls.append(cmd)
            if cmd[-1] == "--quiet":
                return _Done(rc, out)
            return _Done(0, "log line with s3cretpassw0rd" if cmd[:2] == ["docker", "logs"] else "ps table")
        monkeypatch.setattr(smoke, "_run", fake)
        smoke._diagnostics()
        printed = capsys.readouterr().out
        assert [c[-1] for c in calls if c[:2] == ["docker", "logs"]] == logs
        assert "s3cretpassw0rd" not in printed and "[redacted]" in printed


def test_the_smoke_refuses_without_the_throwaway_flag_and_runs_nothing(monkeypatch, capsys):
    """Without `--throwaway` the smoke exits 2 and starts no process: it installs Docker with sudo and a stack on the
    host it runs on, so a human running it by mistake on a control node they keep must be stopped by the default.
    Guards the flag being made a no-op or the check moved after the first command."""
    def no_process(*a, **kw):
        raise AssertionError("a process was started: %r" % (a,))
    monkeypatch.setattr(subprocess, "run", no_process)
    assert smoke.main([]) == 2
    assert "pass --throwaway only on a host nobody keeps" in capsys.readouterr().err


def _tree(tmp_path, guide):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "install-from-scratch.md").write_text(guide, encoding="utf-8")
    return tmp_path


def test_a_tree_that_already_has_an_instance_is_refused_before_anything_runs(tmp_path, monkeypatch):
    """A tree with an `instance/` is refused with exit 2 before a key is minted or a line runs: the smoke installs from
    a fresh clone, and on a configured tree its scaffold and its teardown (`down --volumes`) would act on the
    operator's own stack. Guards the smoke being pointed at a real control node's checkout."""
    root = _tree(tmp_path, _guide())
    (root / "instance").mkdir()
    monkeypatch.setattr(smoke, "ROOT", str(root))
    monkeypatch.setattr(smoke, "_mint_recipient", lambda: pytest.fail("a key was minted"))
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("a process was started"))
    with pytest.raises(smoke.SmokeError) as err:
        smoke.run(0, keep=False)
    assert err.value.code == 2 and "instance/ already exists" in str(err.value)


def test_an_account_with_the_services_uid_is_refused_before_anything_runs(tmp_path, monkeypatch):
    """Run as uid 1001, the network service's uid, the smoke refuses with exit 2 before a key is minted or a line
    runs: the install refuses such an operator (the canonical cannot tell the two apart; the quickstart's first line,
    install-prereqs.sh, says so since I-18, where the dry run said it after the scaffold and the mint), so every night
    would be red for a reason that is not the tree's. A GitHub-hosted runner's
    own account has that uid, which is how the first run found it. Guards the workflow's operator account being
    dropped, which would make the smoke fail late on every run."""
    root = _tree(tmp_path, _guide())
    monkeypatch.setattr(smoke, "ROOT", str(root))
    monkeypatch.setattr(smoke, "_uid", lambda: 1001)
    monkeypatch.setattr(smoke, "_mint_recipient", lambda: pytest.fail("a key was minted"))
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("a process was started"))
    with pytest.raises(smoke.SmokeError) as err:
        smoke.run(0, keep=False)
    assert err.value.code == 2 and "uid 1001, the network service's" in str(err.value)


def test_a_guide_the_smoke_cannot_read_is_refused_before_a_key_is_minted(tmp_path, monkeypatch):
    """The guide's block is read, and refused if it must be, before the break-glass key is minted and before the first
    line runs: a drifted guide costs nothing and leaves nothing behind. Guards the validation being reordered after
    the mint, which would run the known lines of a block whose later lines are unknown."""
    root = _tree(tmp_path, _quote("sudo bash scripts/install-prereqs.sh", "rm -rf /"))
    monkeypatch.setattr(smoke, "ROOT", str(root))
    monkeypatch.setattr(smoke, "_uid", lambda: 1000)
    monkeypatch.setattr(smoke, "_mint_recipient", lambda: pytest.fail("a key was minted"))
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("a process was started"))
    with pytest.raises(smoke.SmokeError) as err:
        smoke.run(0, keep=False)
    assert err.value.code == 2 and "'rm -rf /' is not a command the smoke knows" in str(err.value)


@pytest.mark.parametrize("keep, rendered, down", [(False, True, True), (True, True, False), (False, False, False)])
def test_the_first_failed_line_stops_the_run_and_the_stack_is_taken_down(tmp_path, monkeypatch, keep, rendered,
                                                                          down):
    """The quickstart's lines run in order through `bash -c` from the tree's root; the first that fails stops the run
    with exit 1, naming it, after the diagnostics are printed; then the stack is taken down with its volumes, unless
    `--keep` is given or nothing was rendered yet (no docker/.env, so no stack to take down). Guards a failed line
    being ignored (the next one runs on a broken install and the smoke judges whatever is left) and a scratch VM left
    with a stack up after a failure."""
    root = _tree(tmp_path, _quote("sudo bash scripts/install-prereqs.sh", "scripts/kontroll-installer.sh check",
                                  "scripts/kontroll-installer.sh"))
    if rendered:
        (root / "docker").mkdir()
        (root / "docker" / ".env").write_text("X=1\n", encoding="utf-8")
    calls = []
    monkeypatch.setattr(smoke, "ROOT", str(root))
    monkeypatch.setattr(smoke, "_uid", lambda: 1000)
    monkeypatch.setattr(smoke, "_mint_recipient", lambda: "age1example")
    monkeypatch.setattr(smoke, "_diagnostics", lambda: calls.append("diagnostics"))
    monkeypatch.setattr(smoke, "_run", lambda cmd, **kw: calls.append(cmd))

    def fake_run(cmd, cwd=None, **kw):
        calls.append((cmd, cwd))
        return subprocess.CompletedProcess(cmd, 0 if len(calls) == 1 else 7)
    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(smoke.SmokeError) as err:
        smoke.run(0, keep=keep)
    assert err.value.code == 1 and "quickstart line 2 failed (exit 7): scripts/kontroll-installer.sh check" in str(
        err.value)
    expected = [(["bash", "-c", "sudo bash scripts/install-prereqs.sh"], str(root)),
                (["bash", "-c", "scripts/kontroll-installer.sh check"], str(root)), "diagnostics"]
    if down:
        expected.append(smoke.STACK + ["down", "--volumes", "--remove-orphans"])
    assert calls == expected


@pytest.mark.skipif(shutil.which("age-keygen") is None, reason="age-keygen is not installed here")
def test_the_minted_recipient_is_a_public_key_and_its_private_half_is_gone(tmp_path, monkeypatch):
    """The break-glass recipient the smoke mints is an age public key (`age1…`), and the private half is deleted
    before the mint returns: nothing the run leaves on the host can open what the scaffold encrypts to it. Guards the
    private key file outliving the mint, and the recipient being the control key (the scaffold refuses that)."""
    def mkdtemp(prefix=""):
        (tmp_path / "bg").mkdir()
        return str(tmp_path / "bg")
    monkeypatch.setattr(smoke.tempfile, "mkdtemp", mkdtemp)
    recipient = smoke._mint_recipient()
    assert recipient.startswith("age1") and len(recipient) == 62
    assert not (tmp_path / "bg").exists()
