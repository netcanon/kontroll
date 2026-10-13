"""Shared AST-based read-only-by-construction pins (#133).

Instead of a brittle substring grep over source text — which trips on comments/strings and can't scope to a single
function — these parse a module's AST and assert a named function calls NO write/actuation verb (nor opens a file
for writing). Used by the read-only service reads (`fleet.list_services`, and the forthcoming `pending.*` /
`backups.*`) to guard, IN CI, against any of them silently gaining an actuation path — the INVARIANT D* / C10
read-only contract. One robust seam, not three copy-pasted greps (M9 / review §2.3).

Leading-underscore module name ⇒ pytest does not collect it as a test; it is imported by the `test_*` files.

THE PIN IS TRANSITIVE (2026-10-12). `assert_read_only` used to read a function's DIRECT calls, so a read view whose
write sat one helper away passed — a refactor that split a view into helpers silently un-pinned it. It now walks
what the function can REACH by name (`write_paths`): helpers of the same module, called or handed on as a callback;
functions of other modules of this repository reached through an import; and, in every function on the way, the
write PRIMITIVES themselves (`_is_direct_mutator`), so a write the verb list does not name is seen too. A failure
names the path from the view to the write.

#134 / P0b — the COMPLETENESS closure. The NAME half of the per-function pins fails OPEN: it checks calls against
`WRITE_VERBS`, so a mutator the registry doesn't KNOW about is invisible to it — a read view could call an
unregistered writer by a path the closure cannot follow and the pin would pass. `assert_write_verbs_complete` closes
that hole: it AST-scans the pinned layer
(`scripts/kontroll/service/*.py` + `gitio.py`) for every DIRECT mutator (a write-mode `open`, a git-MUTATING argv,
a SOPS write) and asserts each is registered in `WRITE_VERBS` — so a new writer that isn't registered breaks CI,
making "add the verb in the same commit" an ENFORCED gate, not a convention. `assert_write_verbs_resolve` is the
no-rot complement (every entry is a real `def` or a bare git verb). The 2026-06-24 sweep that introduced this found
seven real gaps — incl. `sops_write_domain` (an UNPINNED secret-writer) — and registered them. (Tested in
`test_readonly_completeness.py`, incl. proof the gate fires on a planted unregistered writer.)
"""
import ast
import glob
import os

# tests/unit/_readonly_pins.py -> tests/unit -> tests -> repo root (three dirnames). Computed independently of
# kontroll.paths.ROOT so a test that repoints paths.ROOT at a tmp tree never diverts the SOURCE read.
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Call names (a bare `f(...)` → `f`, or an attribute `obj.method(...)` → `method`) that MUTATE the repo / actuate.
# A read-only view must call NONE of them. Explicit + conservative so the pin is meaningful, not a catch-all that
# trips on benign helpers. NB: subprocess `run` is deliberately ABSENT — a read-only service may shell out to a
# READ-only git (`for-each-ref`, `show`); pin those callers with `assert_no_git_write` instead.
WRITE_VERBS = frozenset({
    "commit", "commit_and_push", "push", "promote_ref", "_push_target",
    "write_inventory_host", "apply_onboard_plan", "apply_secret_plan", "build_secret_plan",
    "sops_set", "add_recipient", "seed_fresh_sops", "enable_in_fleet",
    # GUI-paradigm write surfaces (P0b/M2 — added in the same commit as the verb that introduced them):
    "apply_homepage_plan", "promote_board",
    # Phase 4b (#139): the shared single-object promote orchestrator (token → no_drop → apply) + the host-var
    # diff-producing merge. run_promote actuates (it calls apply_plan); owned_merge encodes the host-var mutation
    # discipline — a read view must call neither.
    "run_promote", "owned_merge",
    # Phase 5 (#140): the fleet-removal mutation verb (settings.apply_plan's open:w is caught directly). A read
    # surface must never disable a module.
    "disable_in_fleet",
    # Staging-isolation fix (F1, dogfood 2026-06-20): the content-clone roll-back to canonical main. A `reset
    # --hard` is a working-tree mutation — a read-back must never call it (it runs only inside commit_and_push).
    "_reset_content_clone_to_canonical",
    # Proposal-rebase fix (task_fd3b458d, dogfood 2026-06-29): rebases the staged proposal onto current canonical
    # main before the push (a `fetch` + `rebase` — both working-tree/ref mutations); runs only inside commit_and_push.
    "_rebase_proposal_onto_canonical",
    # #134 / P0b — the fail-CLOSED completeness sweep (assert_write_verbs_complete, below) found these DIRECT
    # mutators were NOT registered, so assert_read_only (which fails OPEN) could not see them. Registering closes the
    # hole: a read view that calls any of them now trips the pin. Each opens a file for writing or writes a SOPS
    # secret. Landed 2026-06-24 with the completeness check that keeps the list honest going forward.
    "sops_write_domain",          # gitio: re-encrypts a whole SOPS domain (the highest-stakes write) — was UNPINNED
    "_write_new",                 # gitio: the leaf "write a new file" helper
    "apply_plan",                 # the per-domain reconfigure apply step (backup/hostvars/identity/settings) writes module.yml/inventory/instance
    "apply_telemetry_plan", "apply_logging_plan",   # observe/logsvc: write the capability block into module.yml
    "add_capture_exception",      # capture_exception: writes config/capture-exceptions.yml
    "service_refresh",            # refresh: writes the catalog matrix cache (a read view must not refresh as a side effect)
    # R5 — the FIRST app-store WRITE verb: actuation.apply_actuation_plan writes the unit's committed configure-values
    # file (instance/actuation/<key>/vars.yml), which the route stages to proposed/<run_id>. Registered in the same
    # commit as the verb (P0b/#134) so a read view that calls it trips assert_read_only. `stage_plan` is the
    # orchestrator (recompute + verify_token + apply) — not a DIRECT mutator (so the completeness gate doesn't
    # require it), but registered too (R5 review GAP-3 / 30+34) so a read view that calls the stage orchestrator
    # — writing by delegation — also trips the pin (defense-in-depth for the delegation residual).
    "apply_actuation_plan", "stage_plan",
    # The app-store CREATE write verb: actuation.apply_create_unit writes the AUTHORED unit descriptor
    # (instance/actuation/<key>/unit.yml) the route stages to proposed/<run_id> — the seam that fills the empty
    # registry the R3/R4/R5 stages read. Registered in the same commit as the verb (P0b/#134). `stage_create_unit`
    # is the orchestrator (recompute + verify_token + apply) — registered too (defense-in-depth, like stage_plan).
    "apply_create_unit", "stage_create_unit",
    # FF-race Move 2 — the discard reaper: discard.discard_proposal deletes a STALE `proposed/<run_id>` ref from the
    # canonical (a `git update-ref -d` compare-and-delete). It is the ONE write in the pending surface, kept OUT of
    # the read-only pending.py in its own module; registered here in the SAME commit as the verb (P0b/#134) so a read
    # view that ever calls it trips assert_read_only, and so the fail-closed completeness gate (which detects the
    # `update-ref` argv) is satisfied. C10-safe: un-stage != promote (it removes Key-1's own output, never advances main).
    "discard_proposal",
    # canon.write_files / canon.export_commit write a commit's files into a directory the caller names (the promote
    # gate's temporary root; an export a play asks for). Not a write of the canonical — and still nothing a read view
    # may reach: a view that could export a tree could put a staged, unpromoted proposal's files on disk.
    "write_files", "export_commit",
    # A staged write that cannot land (2026-10-12). `write_module` is the ONE place the module reconfigures write
    # `modules/<key>/module.yml` (each used to carry its own open:w inside an apply_* verb); `_drop_refused`
    # removes what an apply wrote and `git add` would not take (a `git clean` of files at the request's own paths,
    # after the staging roll-back); `_unstage` takes a request's paths back out of the index of a caller that is
    # not the staging service (a path-scoped `git reset`). Each registered in the same commit as the verb (P0b/#134).
    "write_module", "_drop_refused", "_unstage",
})


def _looks_like_mode(s):
    """A file-open MODE token (`w`, `wb`, `a+`, `rb`, …) vs a filename: short + only mode chars. Lets _open_is_write
    find the mode in BOTH `open(file, 'w')` (builtin, arg 1) and `Path(p).open('w')` (method, arg 0) without
    mistaking a filename like `/var/w.txt` (has `/`, `.`) for a mode."""
    return isinstance(s, str) and 0 < len(s) <= 3 and set(s) <= set("rwxabt+U")


def _open_is_write(call):
    """True if an `open(...)` / `.open(...)` Call names a write/append/create/update mode. Position-aware: the
    BUILTIN `open(file, mode)` takes the mode at positional arg 1 (arg 0 is ALWAYS the filename, so a 1-arg
    `open('x')` read is NOT a write); the METHOD form `Path(p).open('w')` / `io.open(p, 'w')` takes the mode at arg
    0 OR 1, disambiguated from a filename by MODE-SHAPED filtering. The `mode=` kwarg counts in both forms."""
    modes = [kw.value.value for kw in call.keywords if kw.arg == "mode" and isinstance(kw.value, ast.Constant)]
    if isinstance(call.func, ast.Name):                          # builtin open(file, mode) — mode is positional 1
        if len(call.args) >= 2 and isinstance(call.args[1], ast.Constant):
            modes.append(call.args[1].value)
    else:                                                        # `.open(...)` — mode at arg 0 (Path) or 1 (io); a
        modes += [a.value for a in call.args                     # filename won't pass _looks_like_mode
                  if isinstance(a, ast.Constant) and _looks_like_mode(a.value)]
    return any(isinstance(m, str) and any(c in m for c in ("w", "a", "x", "+")) for m in modes)


def _called_names(func_node):
    """Every called name inside func_node: a bare `Call` → `func.id`; an attribute `Call` → `func.attr`; an
    `open(...)` in a write mode → the literal `open:w`."""
    names = set()
    for n in ast.walk(func_node):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        if isinstance(f, ast.Name):
            names.add("open:w" if (f.id == "open" and _open_is_write(n)) else f.id)
        elif isinstance(f, ast.Attribute):
            names.add(f.attr)
    return names


def find_function(module_rel, func_name):
    """The `ast.FunctionDef` for func_name in the module at ROOT-relative module_rel (asserts if absent)."""
    for node in ast.walk(_tree(os.path.join(ROOT, module_rel))):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            return node
    raise AssertionError("function %s not found in %s" % (func_name, module_rel))


# --- The transitive closure: what a function can REACH by name ------------------------------------------------- #
# Everything below is static and NAME-based, like the rest of this module. It follows: a call or a bare reference
# to a function of the same module (a reference, because a helper handed to map()/sorted()/a thread is reached
# without being called here); an attribute CALL whose name is a function of the same module (`self.helper()`); and a
# call or reference through an import that resolves to a module file of this repository (`alias.func`,
# `pkg.mod.func`, or a `from module import func` name), at any import depth. It does NOT follow what no static
# reading can: getattr() with a computed name, a function stored in a dict or passed in as an argument, a method
# reached through an object of another module's class. Those stay with the reviewer — and with the name check, which
# still fires on a registered verb however it is reached.
_PARSED = {}
_IMPORT_ROOTS = (os.path.join(ROOT, "scripts"), ROOT)    # where an ABSOLUTE import of repository code resolves


def _tree(path):
    """The parsed AST of the file at `path`, re-read when the file changes (the planted-module tests rewrite one)."""
    st = os.stat(path)
    key = (os.path.normpath(path), st.st_mtime_ns, st.st_size)
    if key not in _PARSED:
        with open(path, encoding="utf-8") as fh:
            _PARSED[key] = ast.parse(fh.read(), filename=path)
    return _PARSED[key]


def _module_functions(path):
    """{name: def} for the functions a call BY NAME can mean in the module file at `path`: its top-level functions
    and the methods of its top-level classes (the first definition of a name wins)."""
    out = {}
    for n in _tree(path).body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.setdefault(n.name, n)
        elif isinstance(n, ast.ClassDef):
            for m in n.body:
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out.setdefault(m.name, m)
    return out


def _module_file(base, parts):
    """The file `base/<parts…>` names as a module (`x.py`) or a package (`x/__init__.py`), or None."""
    cand = os.path.join(base, *parts)
    for c in (cand + ".py", os.path.join(cand, "__init__.py")):
        if os.path.isfile(c):
            return os.path.normpath(c)
    return None


def _resolve_import(path, level, dotted):
    """The repository file an import in the module at `path` names, or None when it is not one (the standard
    library, a third-party package). `level` is the count of leading dots of a relative import."""
    parts = dotted.split(".") if dotted else []
    if level:
        base = os.path.dirname(path)
        for _ in range(level - 1):
            base = os.path.dirname(base)
        return _module_file(base, parts)
    for root in _IMPORT_ROOTS:
        found = _module_file(root, parts) if parts else None
        if found:
            return found
    return None


def _imports(path):
    """({local name: module file}, {local name: (module file, function name)}) for every import statement in the
    module at `path` — at any depth, so an import inside a function body counts. A plain `import a.b` is keyed by
    its full dotted name, which is how its calls are spelled (`a.b.f()`)."""
    mods, funcs = {}, {}
    for n in ast.walk(_tree(path)):
        if isinstance(n, ast.Import):
            for a in n.names:
                target = _resolve_import(path, 0, a.name)
                if target:
                    mods[a.asname or a.name] = target
        elif isinstance(n, ast.ImportFrom):
            for a in n.names:
                sub = _resolve_import(path, n.level, ".".join(p for p in (n.module, a.name) if p))
                if sub:                                              # `from package import module`
                    mods[a.asname or a.name] = sub
                    continue
                target = _resolve_import(path, n.level, n.module or "")
                if target:                                           # `from module import function`
                    funcs[a.asname or a.name] = (target, a.name)
    return mods, funcs


def _dotted(node):
    """`a.b.c` for a pure Name/Attribute chain, else None (a call result, a subscript — not a module)."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    return ".".join([node.id] + parts[::-1])


def _receiver_module(dotted, mods):
    """The module file a dotted receiver names — an imported name, or a submodule under an imported package
    (`pkg.mod` after `import pkg` / `from x import pkg`) — or None when the receiver is not an imported module."""
    if dotted in mods:
        return mods[dotted]
    parts = dotted.split(".")
    for i in range(len(parts) - 1, 0, -1):
        head = mods.get(".".join(parts[:i]))
        if head is not None:
            return _module_file(os.path.dirname(head), parts[i:]) if os.path.basename(head) == "__init__.py" else None
    return None


def _reaches(path, node):
    """The (module file, function name) pairs the function `node` of the module at `path` can hand control to."""
    functions = _module_functions(path)
    mods, funcs = _imports(path)
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
            if n.id in functions:
                out.add((path, n.id))                                # a same-module helper: called, or passed on
            elif n.id in funcs:
                out.add(funcs[n.id])                                 # a `from module import function` name
        elif isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Load):
            receiver = _dotted(n.value)
            target = _receiver_module(receiver, mods) if receiver else None
            if target is not None:
                out.add((target, n.attr))                            # alias.function — called, or passed on
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in functions:
            receiver = _dotted(n.func.value)
            if not (receiver and _receiver_module(receiver, mods)):
                out.add((path, n.func.attr))                         # obj.helper() where helper is defined here
    return out


def _writes_directly(fn):
    """True if fn — or a function or lambda nested in it — performs a write PRIMITIVE itself (a write-mode open in
    either spelling, `write_text`, `os.replace`, a mutating git argv, a SOPS write: see `_is_direct_mutator`)."""
    return any(_is_direct_mutator(scope) for scope in ast.walk(fn)
               if scope is fn or isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)))


def _closure(module_rel, func_name):
    """`(module file, name, def, trail)` for every function the closure visits from func_name — the pinned function
    first. The trail is the names from the pinned function to this one."""
    start = os.path.normpath(os.path.join(ROOT, module_rel))       # an absolute module_rel (a planted tmp module)
    seen = {(start, func_name)}                                    # passes through os.path.join unchanged
    order = [(start, func_name, find_function(module_rel, func_name), [func_name])]
    for path, _name, node, trail in order:                         # the list grows while it is walked
        for target, name in sorted(_reaches(path, node)):
            if (target, name) in seen:
                continue
            fn = _module_functions(target).get(name)
            if fn is None:                                         # an imported name that is not a function
                continue
            seen.add((target, name))
            label = name if target == start else "%s.%s" % (os.path.splitext(os.path.basename(target))[0], name)
            order.append((target, name, fn, trail + [label]))
    return order


def reachable(module_rel, func_name):
    """`(module file, name, trail)` for every function the pin's closure visits from func_name."""
    return [(path, name, trail) for path, name, _node, trail in _closure(module_rel, func_name)]


def write_paths(module_rel, func_name, forbidden=WRITE_VERBS):
    """Every way func_name reaches a write: a list of trails, each `[func_name, helper, …, <the write>]`, where the
    write is a `forbidden` verb called by name or `(a direct write)` for a write primitive. Empty for a function
    that is read-only as far as its names can be followed."""
    found = []
    for _path, _name, node, trail in _closure(module_rel, func_name):
        called = _called_names(node)
        found += [trail + [verb] for verb in sorted(called & set(forbidden))]
        if "open:w" in called or _writes_directly(node):
            found.append(trail + ["(a direct write)"])
    return found


def assert_read_only(module_rel, func_name, forbidden=WRITE_VERBS):
    """Assert func_name in module_rel reaches none of the `forbidden` write/actuation verbs and performs no write
    primitive — in its own body, or in anything it can reach by name (`write_paths`). The read-only-by-construction
    guard: a read view must never gain an actuation path, and splitting it into helpers, or moving the write into
    another module, must not be a way to hide one (the pin used to read the function's DIRECT calls only). A hit is
    fixed by not writing from a read view — never by trimming the closure."""
    found = write_paths(module_rel, func_name, forbidden)
    assert not found, "%s.%s must be read-only but reaches a write: %s" % (
        module_rel, func_name, "; ".join(" -> ".join(p) for p in found))


# git subcommands that MUTATE the repo / working tree (the conservative blacklist a `subprocess.run(["git", …])`
# literal must avoid) and the read-only allow-list a chokepoint helper's call sites must pick their verb from. A
# read-only service that shells git rides its verb on a subprocess ARG LIST — invisible to assert_read_only's
# call-NAME check — so it is pinned here on the verb itself (review §2.3 / the _readonly_pins module docstring).
GIT_WRITE_VERBS = frozenset({
    "commit", "commit-tree", "add", "rm", "mv", "push", "fetch", "pull", "clone", "init", "update-ref",
    "symbolic-ref", "update-index", "write-tree", "reset", "checkout", "switch", "restore", "stash", "branch",
    "tag", "merge", "rebase", "cherry-pick", "apply", "am", "gc", "prune", "reflog", "filter-branch", "replace",
    "fast-import", "repack", "worktree", "notes", "config", "remote", "clean",
})
GIT_READONLY_VERBS = frozenset({
    "log", "show", "diff", "ls-files", "ls-tree", "rev-parse", "rev-list", "cat-file", "for-each-ref",
    "show-ref", "describe", "status", "shortlog", "blame", "name-rev",
})


def _list_str_elems(node):
    """The string-constant elements of an ast.List literal (None if `node` isn't a static list); a Starred / Name
    element yields None for that slot so a dynamic verb can't masquerade as a constant."""
    if not isinstance(node, ast.List):
        return None
    out = []
    for el in node.elts:
        out.append(el.value if isinstance(el, ast.Constant) and isinstance(el.value, str) else None)
    return out


def assert_no_git_write(module_rel, chokepoint="_git", readonly=GIT_READONLY_VERBS, write=GIT_WRITE_VERBS):
    """Pin a read-only service that shells git: (A) NO `subprocess.run(["git", …])` literal anywhere in the module
    names a mutating subcommand, and (B) EVERY call to the module's git chokepoint (`_git(cap, [<verb>, …])`)
    picks its verb from a STATIC list whose first element is a read-only verb. (B) is the strong guard — it refuses
    a dynamically-built verb (a non-constant first element fails) so a future edit can't smuggle `git rm` through
    the chokepoint. Complements assert_read_only (which can't see a verb riding a subprocess arg list)."""
    path = os.path.join(ROOT, module_rel)
    tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
    choke_sites = 0
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        fn = n.func
        # (A) a direct `subprocess.run(["git", …])` / `run(["git", …])` literal must contain no write verb.
        is_run = (isinstance(fn, ast.Attribute) and fn.attr == "run") or (isinstance(fn, ast.Name) and fn.id == "run")
        if is_run and n.args:
            elems = _list_str_elems(n.args[0])
            if elems and elems[0] == "git":
                bad = sorted(set(e for e in elems if e in write))
                assert not bad, "%s: subprocess git call uses write verb(s): %s" % (module_rel, ", ".join(bad))
        # (B) every chokepoint call must pass a static list whose first element is an allow-listed read-only verb.
        if isinstance(fn, ast.Name) and fn.id == chokepoint:
            choke_sites += 1
            listarg = next((a for a in n.args if isinstance(a, ast.List)), None)
            elems = _list_str_elems(listarg)
            assert elems and elems[0] in readonly, (
                "%s: %s(...) call site #%d must pass a static [<read-only verb>, …]; got %r"
                % (module_rel, chokepoint, choke_sites, None if elems is None else elems[:1]))
    assert choke_sites, "%s: no %s(...) call sites found — wrong chokepoint name?" % (module_rel, chokepoint)


# --- the seam: every git of a canonical-touching module runs in canon.git_kwargs ---------------------------------- #
# The canonical is a repository the network service writes into, so a git that reads or writes it must not take its
# orders from what is in it — nor from the caller's environment. `kontroll.canon.git_kwargs(<repo>)` is the one
# environment such a git runs in. That is a convention until something holds every call to it; this is that pin.
_SUBPROCESS_CALLS = frozenset({"run", "Popen", "call", "check_call", "check_output", "getoutput", "getstatusoutput"})
_OS_PROCESS_CALLS = frozenset({"system", "popen", "startfile"})


def assert_git_runs_in_the_seam(module_rel, verbs):
    """Pin a module whose git runs on the CANONICAL: every process it starts is `subprocess.<call>(["git",
    "<verb>", …], …, **canon.git_kwargs(<repo>))` — nothing else.

      - the argv is ONE list literal (not a name, a concatenation or a shell string), so the verb can be read;
      - its second element is the verb, a constant from `verbs`: no global option stands before it (`-C` would
        name a repository and `-c` set a key beside the seam, which names the one and sets the others);
      - the call passes `**canon.git_kwargs(...)` (inside `canon.py` itself, `**git_kwargs(...)`) and no `env`,
        `cwd`, `shell` or `executable` of its own;
      - the call carries a `timeout` (a `Popen` has none of its own: whoever writes one owns its deadline), one
        positional argument — the argv — and no `**` but the seam's;
      - the module reaches `subprocess` by that name only (no `from subprocess import run`, no alias) and starts
        nothing through `os.system` / `os.popen` / `os.exec*` / `os.spawn*` / `os.posix_spawn` — a call this pin
        could not see.

    `verbs` is the set THIS module runs today, not the read-only list: the seam is proven for the verbs its
    readers use, and a module that needs another one says so where it is pinned.

    It complements the verb pins above: those say WHICH git a read view may run, this says IN WHAT."""
    path = os.path.join(ROOT, module_rel)
    tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
    in_canon = module_rel.replace("\\", "/").rsplit("/", 1)[-1] == "canon.py"
    seam_names = {"git_kwargs"} if in_canon else {"canon.git_kwargs"}
    imports_canon = in_canon
    sites = 0
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            assert n.module != "subprocess", "%s:%d: import subprocess by its own name — a bare run() is not seen" % (
                module_rel, n.lineno)
            if n.module == "kontroll" and any(a.name == "canon" and a.asname is None for a in n.names):
                imports_canon = True
        if isinstance(n, ast.Import):
            assert not any(a.name == "subprocess" and a.asname for a in n.names), (
                "%s:%d: subprocess is imported under another name — its calls are not seen" % (module_rel, n.lineno))
        if not isinstance(n, ast.Call) or not isinstance(n.func, ast.Attribute):
            continue
        owner = n.func.value.id if isinstance(n.func.value, ast.Name) else None
        where = "%s:%d" % (module_rel, n.lineno)
        assert not (owner == "os" and (n.func.attr in _OS_PROCESS_CALLS
                                       or n.func.attr.startswith(("exec", "spawn", "posix_spawn")))), (
            "%s: os.%s starts a process outside the seam" % (where, n.func.attr))
        if owner != "subprocess" or n.func.attr not in _SUBPROCESS_CALLS:
            continue
        sites += 1
        argv = n.args[0] if n.args else None
        assert isinstance(argv, ast.List), (
            "%s: the argv of a process started here must be one list literal — not a name, a concatenation or a "
            "shell string" % where)
        elems = _list_str_elems(argv)
        assert len(elems) > 1 and elems[0] == "git" and elems[1] is not None, (
            "%s: the argv must open with a static ['git', '<verb>', …]" % where)
        verb = elems[1]
        assert not verb.startswith("-"), (
            "%s: `%s` stands before the verb — the seam names the repository and sets every key" % (where, verb))
        assert verb in verbs, "%s: `git %s` is not a verb this module may run on the canonical" % (where, verb)
        seam = [k for k in n.keywords if k.arg is None and isinstance(k.value, ast.Call)
                and _dotted(k.value.func) in seam_names]
        assert len(seam) == 1, "%s: a git of the canonical must pass **canon.git_kwargs(<repo>)" % where
        own = sorted(k.arg for k in n.keywords if k.arg in ("env", "cwd", "shell", "executable"))
        assert not own, "%s: %s beside **canon.git_kwargs — the seam decides those" % (where, ", ".join(own))
        assert len(n.args) == 1 and all(k.arg is not None or k in seam for k in n.keywords), (
            "%s: one argv and the seam — no other positional argument and no second ** (either can carry an "
            "executable, a cwd or a shell this pin would not read)" % where)
        assert n.func.attr == "Popen" or any(k.arg == "timeout" for k in n.keywords), (
            "%s: a git of the canonical carries a timeout — one that never answers would hold the worker" % where)
    assert imports_canon, "%s: `from kontroll import canon` not found — whose git_kwargs is this?" % module_rel
    assert sites, "%s: no subprocess call found — is this the module that runs git?" % module_rel


# --- #134 / P0b: the fail-CLOSED completeness sweep -------------------------------------------------------------- #
# assert_no_git_write and the NAME half of assert_read_only fail OPEN: a verb NOT in WRITE_VERBS is invisible to
# them, so a read view that reaches an UNREGISTERED mutator by a path the closure cannot follow silently passes.
# assert_write_verbs_complete closes that hole — it is the
# per-commit discipline (#134/P0b): if a new function in the pinned layer performs a direct write and is not
# registered, CI fails here, forcing the same-commit WRITE_VERBS update that makes assert_read_only exhaustive.

# git subcommands that ALWAYS mutate. A STRICTER subset of GIT_WRITE_VERBS that DELIBERATELY excludes the dual-use
# verbs (`remote get-url`, `config --get`, `branch`/`tag` list forms, `symbolic-ref HEAD`, `stash/notes/reflog/
# worktree list`) which also have read forms — those would false-positive a read helper (e.g. gitio.offsite_remote_
# exists shells `git remote get-url`). assert_no_git_write keeps the broader blacklist; completeness uses this.
GIT_MUTATING_VERBS = frozenset({
    "commit", "commit-tree", "add", "rm", "mv", "push", "fetch", "pull", "clone", "init", "update-ref",
    "update-index", "write-tree", "reset", "checkout", "switch", "restore", "merge", "rebase", "cherry-pick",
    "apply", "am", "gc", "filter-branch", "replace", "fast-import", "repack", "clean",
})
_SOPS_WRITE_FLAGS = frozenset({"--set", "--encrypt", "-e", "--rotate", "-r"})   # `--decrypt` is a READ; these mutate
_SOPS_WRITE_CALLS = frozenset({"sops_set", "sops_write_domain"})                # the named sops-write chokepoints
# Filesystem-write METHODS whose names DON'T collide with a common builtin, so detecting them by bare `.attr` is
# safe (a `pathlib.Path(p).write_text(...)`, `shutil.rmtree(...)`).
_FS_WRITE_METHODS = frozenset({"write_text", "write_bytes", "rmtree"})
# The collision-PRONE fs-write methods (`os.rename`/`os.replace`/`shutil.move` — the atomic-write idiom) detected
# ONLY when receiver-qualified (`os.<m>` / `shutil.<m>`), so `str.replace`/`dict.copy`/`list.remove`/`os.path.join`
# never false-positive. This shrinks the atomic-write residual the review flagged (a writer using `os.replace` is no
# longer invisible). `mkdir`/`makedirs` are deliberately EXCLUDED (dir setup often precedes a read-cache).
_OS_WRITE_METHODS = frozenset({"rename", "replace", "remove", "unlink", "truncate"})
_SHUTIL_WRITE_METHODS = frozenset({"move", "copy", "copy2", "copyfile", "copytree"})

# Functions allow-listed as NON-verbs despite touching a write/subprocess primitive — each a documented, intentional
# exception (NOT a forgotten registration). Keep this SHORT and justified.
WRITE_COMPLETENESS_ALLOWLIST = frozenset({
    "_run",   # gitio's NEUTRAL subprocess chokepoint: it runs whatever git argv it is GIVEN (read OR write), so it
              # is not itself a verb — a read view legitimately calls it for `git log`/`for-each-ref`. Its callers'
              # argv is pinned per-verb by assert_no_git_write instead.
})


def _pinned_layer():
    """The layer where mutators + read-views coexist and the read-only pins apply: the service modules, the gitio
    I/O chokepoint, and `canon` (the readers of the canonical — it holds no write verb, and the sweep is what keeps
    it so). Routes (api/, gui/app.py) and CLIs orchestrate these and are NOT read-pinned ⇒ out of scope."""
    svc = sorted(os.path.relpath(p, ROOT).replace("\\", "/")
                 for p in glob.glob(os.path.join(ROOT, "scripts", "kontroll", "service", "*.py")))
    return ["scripts/kontroll/gitio.py", "scripts/kontroll/canon.py"] + svc


def _static_list_prefix(node):
    """The leading string constants of a list literal, tolerating a `["git","commit"] + extra` BinOp prefix (gitio's
    idiom). Returns the static prefix (with None for non-constant slots) or None if `node` isn't list-ish."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _list_str_elems(node.left)   # the static prefix carries the verb; the dynamic `+ paths` tail is moot
    return _list_str_elems(node)


def _git_verb(elems):
    """The git subcommand in a `["git", <opts>, <verb>, …]` argv: skip a leading `git`, then skip option tokens
    (`-x`/`--x`, and the value after `-C`/`-c`), returning the first bare token = the verb (None if undeterminable)."""
    if not elems or elems[0] != "git":
        return None
    i = 1
    while i < len(elems):
        tok = elems[i]
        if tok is None:
            return None                      # a dynamic token in the verb position — can't determine
        if tok in ("-C", "-c"):
            i += 2; continue                 # these take a following value
        if tok.startswith("-"):
            i += 1; continue
        return tok
    return None


def _own_nodes(fn):
    """Every node inside fn's body that belongs to fn — NOT descending into a nested def/lambda (those are their own
    scopes, visited as their own FunctionDefs by the caller's walk)."""
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        n = stack.pop()
        yield n
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        stack.extend(ast.iter_child_nodes(n))


def _is_direct_mutator(fn):
    """True if fn DIRECTLY performs a write primitive: opens a file for writing (bare or attribute-form, mode-gated),
    calls a fs-write method (`write_text`/`write_bytes`/`rmtree`, or a receiver-qualified `os.rename`/`os.replace`/
    `shutil.move`/…), shells a git-MUTATING subcommand on a statically-recoverable argv, or writes a SOPS secret
    (`sops --set/--encrypt/--rotate`, or a named sops-write chokepoint). 'Direct' = in fn's OWN body, not via a
    nested scope. This is the signal that fn must be a WRITE_VERBS member. RESIDUAL (narrow): a write whose primitive
    is reached via an ALIASED import (`from os import replace`) or a non-`os`/`shutil` receiver isn't auto-detected —
    such a writer is a style deviation from the codebase's `open('w')` idiom that a human reviewer catches."""
    for n in _own_nodes(fn):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        # a write-mode open() — bare `open(...,'w')` OR attribute-form `io.open(...,'w')` / `Path(p).open('w')`
        # (mode-gated, so a read `Path(p).open()` is NOT flagged).
        if (isinstance(f, ast.Name) and f.id == "open") or (isinstance(f, ast.Attribute) and f.attr == "open"):
            if _open_is_write(n):
                return True
        name = f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else None)
        if name in _SOPS_WRITE_CALLS:
            return True
        if isinstance(f, ast.Attribute):
            if f.attr in _FS_WRITE_METHODS:                                # path.write_text/write_bytes, .rmtree
                return True
            if isinstance(f.value, ast.Name) and (                        # receiver-qualified os.* / shutil.* writes
                    (f.value.id == "os" and f.attr in _OS_WRITE_METHODS) or
                    (f.value.id == "shutil" and f.attr in _SHUTIL_WRITE_METHODS)):
                return True
        if name in ("run", "_run") and n.args:
            elems = _static_list_prefix(n.args[0])
            if elems:
                if elems[0] == "git" and _git_verb(elems) in GIT_MUTATING_VERBS:
                    return True
                if elems[0] == "sops" and any(e in _SOPS_WRITE_FLAGS for e in elems):
                    return True
    return False


def assert_write_verbs_complete():
    """Fail CLOSED: every DIRECT mutator in the pinned layer (service/*.py, gitio.py, canon.py) MUST be in WRITE_VERBS
    (or the documented WRITE_COMPLETENESS_ALLOWLIST). assert_read_only/assert_no_git_write fail OPEN — a verb they
    don't know is invisible — so this is the same-commit discipline (#134/P0b) that keeps the read-only/C10
    guarantee from silently eroding: a new writer that isn't registered breaks CI here, naming the exact file:func.
    SCOPE (documented residual): it pins DIRECT write primitives + the sops/git-write chokepoint callers; a pure
    DELEGATING orchestrator (one that only calls another registered verb, never writes itself) is NOT required —
    those (capability.promote, the routes) are not read-pinned surfaces, and requiring them would cascade to routes.
    The one known unpinned write PATH is the `observe/logsvc/backup.regenerate_*` → `gen-*.py` generator delegation
    (the generators write the repo but live OUTSIDE the pinned layer); those callers are pure delegators reachable
    only from the registered `apply_*` verbs, so no read view reaches them — accepted as a documented residual."""
    missing = []
    for rel in _pinned_layer():
        tree = ast.parse(open(os.path.join(ROOT, rel), encoding="utf-8").read(), filename=rel)
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            if node.name in WRITE_VERBS or node.name in WRITE_COMPLETENESS_ALLOWLIST:
                continue
            if _is_direct_mutator(node):
                missing.append("%s:%s" % (rel, node.name))
    assert not missing, (
        "these DIRECT mutators (write-open / git-write / sops-write) are NOT in WRITE_VERBS — register each in the "
        "SAME commit (P0b/#134) so assert_read_only can catch a read view that calls it:\n  " + "\n  ".join(sorted(missing)))


def assert_write_verbs_resolve():
    """No-rot: every WRITE_VERBS entry is EITHER a real `def` somewhere under scripts/ OR a bare git-mutating
    subcommand (`commit`/`push`/… guard a hypothetical git-LIBRARY `.commit()` call, not a python def). Catches a
    typo or a rename that silently drops a verb from the pin (a dead entry = a verb that no longer guards anything)."""
    defined = set()
    for p in glob.glob(os.path.join(ROOT, "scripts", "**", "*.py"), recursive=True):
        for node in ast.walk(ast.parse(open(p, encoding="utf-8").read(), filename=p)):
            if isinstance(node, ast.FunctionDef):
                defined.add(node.name)
    orphan = sorted(v for v in WRITE_VERBS if v not in defined and v not in GIT_MUTATING_VERBS)
    assert not orphan, ("WRITE_VERBS entries resolve to no `def` under scripts/ and are not bare git verbs "
                        "(typo / dead after a rename?): %s" % ", ".join(orphan))
