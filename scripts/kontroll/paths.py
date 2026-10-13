"""Filesystem paths + shared constants for the kontroll service package.

`ROOT` is the repo-root READ seam: the derived directory constants (`MODULES_DIR`, …) are
bound once from the REAL tree at import, so loaders always read the real repo (read-only
input) even when a test has repointed ROOT for a mutator. `write_root()` is the WRITE seam:
where the file-mutating service functions write the instance-overlay propose tree + run git,
read DYNAMICALLY (so the tmp_repo test fixture's `paths.ROOT` repoint still diverts writes).

`write_root()` defaults to `ROOT` — so the CLI, the tests, and the legacy `/repo`-clone deploy
are byte-for-byte unchanged (WRITE_ROOT == ROOT is the identity). A Phase-B BAKED deploy sets
`KONTROLL_WRITE_ROOT=/propose` so the read-only image code tree at ROOT is never written: the
immutable code + data registries are read from the baked ROOT, while every write + git op
targets the thin propose clone (docs/reviews/2026-06-29-phase-b-baked-code/99-synthesis.md §0).
Lifted from the old galaxy.py module-level constants, now in one place every module imports.
"""
import ipaddress
import os
import re

# scripts/kontroll/paths.py -> scripts/kontroll -> scripts -> repo root (three dirnames).
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MODULES_DIR = os.path.join(ROOT, "modules")        # modules/<key>/module.yml — the SHIPPED device-class registry
VECTORS_DIR = os.path.join(ROOT, "vectors")
TELEMETRY_DIR = os.path.join(ROOT, "telemetry")   # telemetry/<method>.yml — the ingestion-protocol registry
DASHBOARD_LOCKS_DIR = os.path.join(ROOT, "dashboards", "derived")   # dashboards/derived/<method>.lock.yml — the Rung-4a derived-board pins (gen-dashboard-floor)
LOGGING_DIR = os.path.join(ROOT, "logging")       # logging/<method>.yml — the log-source/export registry (gen-logging)
DISCOVERY_DIR = os.path.join(ROOT, "discovery")   # discovery/<method>.yml — the passive lease-source registry (kontroll-discover)
OUI_DIR = os.path.join(ROOT, "oui")               # oui/oui-lookup.generated.json — the pinned IEEE OUI->vendor lookup (gen-oui; ROOT-bound read registry, display-only)
CAPABILITIES_DIR = os.path.join(ROOT, "capabilities")   # capabilities/<cap>.yml — the secondary-capability registry
SECRET_FORMS_DIR = os.path.join(ROOT, "secret-forms")   # secret-forms/<domain>.yml — secret-onboarding FORM schemas (NOT secrets)
KEY_ROLES_DIR = os.path.join(ROOT, "key-roles")         # key-roles/<role>.yml — keygen role schemas (NOT key material)
SETTINGS_DIR = os.path.join(ROOT, "settings")           # settings/<area>.yml — reconfigure knob METADATA (severity/blast_radius), NOT instance config
# NOTE: the SOPS-encrypted values (ansible/secrets/<domain>.sops.yml) and the recipiency rules (.sops.yaml) are
# INSTANCE config that moves into the `instance/` overlay — resolve them via resolve()/overlay_rel()/overlay_target
# below, NEVER a static ROOT-bound constant (a constant would silently read the legacy location post-move).
BACKENDS_DIR = os.path.join(ROOT, "ansible", "backends")
RECIPES_DIR = os.path.join(BACKENDS_DIR, "api", "recipes")
OVERRIDES_DIR = os.path.join(ROOT, "overrides")
GALAXY = "https://galaxy.ansible.com/api/v3/plugin/ansible"
PLUGIN_TYPES = ["cliconf", "httpapi", "netconf", "connection"]
TICK, CROSS, QMARK = "✓", "✗", "?"
MARKS = {"yes": TICK, "no": CROSS, "maybe": QMARK}


def write_root():
    """The WRITE root — where mutators write the instance-overlay propose tree + run git, DISTINCT from ROOT (the
    read root for the immutable code + data registries). Read dynamically (like the legacy `paths.ROOT` reads) so a
    test that repoints `paths.ROOT` (the tmp_repo fixture + ~25 unit-test sites) still diverts writes to the
    throwaway tree. Defaults to ROOT, so with `KONTROLL_WRITE_ROOT` UNSET — the CLI, the tests, and the legacy
    `/repo`-clone deploy — every write is byte-for-byte identical (the zero-behaviour-change identity). A Phase-B
    BAKED deploy sets `KONTROLL_WRITE_ROOT=/propose`: ROOT is the read-only baked image tree (`/opt/kontroll`) the
    mutators must NEVER write, and write_root() is the thin propose clone that carries the operator-mutable
    `instance/` overlay + the C10 git remote. Design-of-record: docs/reviews/2026-06-29-phase-b-baked-code/."""
    return os.environ.get("KONTROLL_WRITE_ROOT") or ROOT


def matrix_cache():
    """Where `refresh` writes the derived capability-matrix cache: under write_root(), never the code root (which
    a baked deploy mounts read-only — a constant bound to ROOT made `POST /refresh` fail there). The file is
    never part of a proposal (the stage is path-scoped, and the root `.gitignore` ignores the file), and the
    staging clone's roll-back (`git reset --hard`) leaves an ignored file where it is. With write_root()==ROOT
    this is the path it always was."""
    return os.path.join(write_root(), "capability-matrix.generated.json")


# --- Instance-overlay resolution (genericization arc, Decision A: a dedicated `instance/` dir) ----------------
# The TOOL ships generic stubs at the legacy in-tree paths; a given instance's REAL config (its inventory,
# secrets, .sops.yaml recipients, fleet selection, instance.yml) lives in a private, gitignored `instance/`
# overlay. Readers go through resolve()/overlay_rel(): the overlay file shadows the shipped stub when present,
# else the legacy path is used. PER-FILE fallback — a half-populated overlay still reads each missing file from
# its legacy location. With NO overlay file present (today), these are the IDENTITY on every mapped path: the
# zero-behavior-change invariant the Phase-2 step-(i) repoint relies on. Full plan: local/genericization-plan.md;
# refactor surface + the two ⚠ couplings: local/genericization-manifest.md §4.
INSTANCE_DIR_NAME = "instance"
EXAMPLE_DIR_NAME = "instance.example"   # the SHIPPED placeholder overlay (TEST-NET addrs, one fake recipient)

# Legacy ROOT-relative path -> overlay equivalent. Matched exactly OR as a directory prefix
# (ansible/secrets/network.sops.yml -> instance/secrets/network.sops.yml). Inventory + secrets keep their
# RELATIVE arrangement inside the overlay so ansible's `inventory_dir/../secrets` lookup and the group_vars
# SOPS vars-plugin still resolve (manifest §4, coupling 2).
_OVERLAY_MAP = (
    ("config/fleet.yml", "instance/fleet.yml"),
    ("config/instance.yml", "instance/instance.yml"),
    (".sops.yaml", "instance/.sops.yaml"),
    ("ansible/inventory", "instance/inventory"),
    ("ansible/secrets", "instance/secrets"),
    # actuation/<key>/unit.yml — the app-store unit registry (R2). A unit is an INSTANCE-specific operator choice
    # (a chosen, version-pinned install) staged by the GUI + promoted; it lives in the private overlay (stripped
    # from the public tree by make-bundle.sh's instance/ strip). The shipped tree's `actuation/` carries only the
    # schema README, so resolve("actuation") is the empty legacy dir until the first unit is promoted into instance/.
    ("actuation", "instance/actuation"),
)


def _overlay_of(rel):
    """The overlay equivalent of a legacy ROOT-relative path, or None if the path is not instance config."""
    rel = rel.replace(os.sep, "/")
    for legacy, overlay in _OVERLAY_MAP:
        if rel == legacy:
            return overlay
        if rel.startswith(legacy + "/"):
            return overlay + rel[len(legacy):]
    return None


def _overlay_file_present(overlay):
    """True when the instance-overlay FILE `overlay` (a write_root()-relative `instance/…` path) exists.

    The joined path is normalised and must still lie under the write root BEFORE the filesystem is asked: a mapped
    path that climbs out with `..` is "absent", so overlay_rel() never names a location outside the tree. Request
    names are validated at the seam (SECURITY.md C22); this is the same rule at the join itself, in the form a
    static analyser can follow. `abspath`, not `realpath`, on purpose: an operator's `instance/` may be a symlink
    to a directory kept elsewhere, and that overlay must keep being found."""
    root = os.path.abspath(write_root())
    full = os.path.abspath(os.path.join(root, overlay))
    if not full.startswith(root.rstrip(os.sep) + os.sep):
        return False
    return os.path.exists(full)


def _overlay_dir_present():
    """True when this tree has an `instance/` overlay directory at all (a configured node), under write_root().

    These two probes are the ONLY place the overlay's presence is decided — overlay_rel(), resolve() and
    overlay_target() all ask them. The test suite makes itself hermetic through them (tests/conftest.py): at the
    real checkout it answers "absent", so every test reads the shipped example whatever a private overlay holds.
    There is no production switch for that, on purpose: a setting that makes a live node read the example is a
    hazard."""
    return os.path.isdir(os.path.join(write_root(), INSTANCE_DIR_NAME))


def overlay_rel(rel):
    """Resolve a legacy ROOT-relative instance path to the overlay location when that file is present, else the
    legacy path — returned ROOT-relative (posix). The instance overlay lives in the WRITE root (the propose clone),
    so the existence check + every overlay-mapped resolution is against `write_root()` (read DYNAMICALLY — the
    tmp_repo fixture's repoint diverts it; a BAKED deploy points it at the clone, never the read-only image). Only
    overlay-mapped instance paths route through here; the data registries are read via the ROOT-bound `*_DIR`
    constants (V1 §2.3 — the two seams are non-overlapping). Used for strings that must stay overlay-relative (e.g.
    the sops --filename-override that must match a .sops.yaml path_regex)."""
    overlay = _overlay_of(rel)
    if overlay is not None and _overlay_file_present(overlay):
        return overlay
    return rel.replace(os.sep, "/")


def _example_rel(rel):
    """The shipped-example equivalent of a legacy instance path (config/fleet.yml -> instance.example/fleet.yml),
    or None for a non-instance path."""
    overlay = _overlay_of(rel)
    if overlay is None:
        return None
    return EXAMPLE_DIR_NAME + overlay[len(INSTANCE_DIR_NAME):]


def resolve(rel, write=False):
    """overlay_rel() as an ABSOLUTE path under the WRITE root — the form file readers of INSTANCE-overlay config
    want (the live `instance/` lives in the propose clone, not the baked image). Safe to hand to a `_load(rel)` that
    prepends a root (os.path.join drops the redundant root when the second arg is absolute). For registry paths use
    the `*_DIR` constants (ROOT), never resolve() — resolve() is the instance-overlay seam (write_root).

    THIRD TIER — the shipped example (public-checkout floor, genericization Phase 5): on a tree that has NO
    `instance/` overlay dir at all (a public clone, a CI checkout, a node before `kontroll-init --fresh`) AND no
    legacy in-tree file either, a READ of a mapped instance path falls through to the shipped
    `instance.example/` placeholder under ROOT — so the generators' `--check` gates, the service readers and the
    test suite run against TEST-NET example data instead of dying on a missing file. The fallback is deliberately
    NARROW: it never applies once an `instance/` dir exists (a configured node that lost a file still fails loud,
    exactly as before), it never rewrites `overlay_rel()` (the STAGED/git-add path string, which must never name
    the example), and `write=True` disables it entirely — every writer (keygen's .sops.yaml, onboard's
    enable_in_fleet, kontroll-init's personalize) passes it, so the shipped example can never become a write target
    (tests/unit/test_paths_overlay.py pins all three properties)."""
    r = overlay_rel(rel)
    if not write and r == rel.replace(os.sep, "/"):               # no overlay file shadows it
        example = _example_rel(rel)
        if (example is not None
                and not _overlay_dir_present()                                         # an UNCONFIGURED tree only
                and not os.path.exists(os.path.join(write_root(), r))                 # and no legacy file either
                and os.path.exists(os.path.join(ROOT, example))):                      # and the example ships it
            return os.path.join(ROOT, example)
    return os.path.join(write_root(), r)


def overlay_target(rel):
    """The WRITE location for a file that may NOT exist yet — overlay_rel is file-existence based and would send a
    brand-new file to the legacy path; this is DIR-active based: prefer the overlay when the `instance/` overlay is
    present (the instance/ dir exists in the WRITE root), else legacy. Returns ROOT-relative (posix). Use it for
    new-file writes (a drop-in inventory host, a new/rewritten secret domain) AND for the matching git-add path, so
    the write target and the staged path always AGREE (the C10 staging invariant). The instance/ check is against
    `write_root()` (the propose clone holds the overlay). With NO instance/ dir (today / WRITE_ROOT==ROOT) it is
    identical to legacy on every mapped path — the same zero-behavior-change guarantee as resolve()."""
    overlay = _overlay_of(rel)
    if overlay is not None and _overlay_dir_present():
        return overlay
    return rel.replace(os.sep, "/")


# --- Request-boundary validators (2026-10-08 review, finding 1; CodeQL py/path-injection) --------------------------
# Every request field that becomes a PATH COMPONENT (a device-class key, an actuation unit key, a secret domain, an
# inventory group) or an INVENTORY KEY (a host name, an address) passes one of these at the service seam and again at
# the join that uses it, and every DROP-IN write target passes confined() (the writers that do not are named in
# SECURITY.md C22; confined() raises RepositoryDataError, because its refusal is never a field's). A closed
# charset, not a denylist: a value that passes cannot carry a separator, a `..`, a NUL, a newline or a YAML/shell
# metacharacter — and CodeQL's "this path depends on a user-provided value" is answered once, here. Each raises
# RequestBoundaryError naming the FIELD and the rule, never the value; the API and the GUI both answer 422,
# before any write.
# The patterns are applied with fullmatch(). `$` also matches BEFORE a final newline, so `^…$` with match() says yes
# to `cisco_ios\n` — a character the contract above excludes, through a pattern that looks closed. Each pattern
# also ends in `\Z` (end of string, no newline exception), so a later `.match()` on one of them stays exact
# instead of becoming a prefix test.
# (A pattern elsewhere whose repetition is UNBOUNDED — a unit key, a remote name — keeps a leading `^` as well:
# end-anchored and start-unanchored it would be quadratic under search(). fullmatch() never searches; the anchor
# makes the pattern safe under any method, which is the same reason for the \Z.)
_COMPONENT_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_FQCN_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\.[a-z][a-z0-9_]{0,63}\Z")
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_HOSTNAME_RE = re.compile(r"%s(?:\.%s)*\.?\Z" % (_LABEL, _LABEL))
# The characters of an IPv4/IPv6 literal. The standard library's parser is NOT the charset: ipaddress.ip_address()
# accepts an IPv6 zone id (`fe80::1%eth0`) and takes any text after the `%` — a newline, braces, quotes.
_IP_LITERAL_RE = re.compile(r"[0-9A-Fa-f:.]{2,45}\Z")


class RequestBoundaryError(ValueError):
    """A request field failed its closed-charset check. A ValueError (so a handler written for the planner's other
    guards still maps it to a 4xx before any write) that also names the FIELD, so the response, the audit line and
    any log can say which field was refused WITHOUT the value: a refused `key` of `../../etc` — or a credential
    pasted into the wrong box — must not travel on in a message. The message states the field and the rule only."""

    def __init__(self, field, message):
        super().__init__(message)
        self.field = field

    @property
    def code(self):
        """A stable machine code for the refused FIELD: `collection` → `invalid_collection`, `device-class key` →
        `invalid_device_class_key`. What a client keys its next-step copy on — derived from the field name, never
        from the value. The API and the GUI both put it beside the message in their 422 body."""
        return "invalid_%s" % re.sub(r"[^a-z0-9]+", "_", str(self.field).lower()).strip("_")


class RepositoryDataError(Exception):
    """The repository's OWN data or layout stopped a request: a module file whose adopted name is not a name, a
    credential field that a registry or a module declares with a name that is not one, a backend that names no
    role and whose name none can be built from (or whose registry entry holds a role that is not a name), a write
    target that does not resolve under the write root, a block of a module file the block writer cannot edit where
    it stands. The fault is not in the request, and the remedy is the file or the layout (a request can sometimes
    step around it — by naming a backend itself — and the file stays wrong for the next caller): an operator fixes it.
    Deliberately NOT a ValueError — a handler written for the planner's guards must not answer it as a refused
    field (422, "your request is malformed", a `code` naming a field nobody sent). Each app maps it once, to 409
    `repository_data`. `what` names the KIND of thing, for the audit line; the message says where to look and
    which rule; neither ever carries the value."""
    status = 409
    code = "repository_data"

    def __init__(self, what, message):
        super().__init__(message)
        self.what = what


def component(value, what="name"):
    """ONE path component from a request field — a device-class key, a unit key, a secret domain, an inventory
    group: lowercase letters, digits, `_` and `-`; 1–64 chars; starting with a letter or digit. Returns the value."""
    if not isinstance(value, str) or not _COMPONENT_RE.fullmatch(value):
        raise RequestBoundaryError(what, "%s is not a valid name: 1-64 chars of [a-z0-9_-], starting with a letter "
                                   "or digit" % what)
    return value


def is_component(value):
    """True when `value` would pass component(). For ENUMERATORS over data the tool reads back — a directory
    listing, a fleet file's `enabled_modules` — which must SKIP a malformed entry: it is not a request field, and
    raising a request-boundary refusal for it would answer a request with a 422 that names a field nobody sent."""
    return isinstance(value, str) and _COMPONENT_RE.fullmatch(value) is not None


# Names Ansible gives a meaning of its own: its two built-in groups and its implicit host. A device class attached
# to `all` addresses every host wherever its group is a play's pattern — the logging wiring play, the backup
# schedule's limit, an app-store unit's play — (`ungrouped`: every host that has no group); a group NAMED
# `localhost` takes the name the control-node plays address, so those plays run on its hosts instead. A HOST of
# one of these names does it from the other side: an inventory host named `localhost` replaces the implicit
# control node, and with a host named `all` the pattern `all` addresses that one host (each measured).
RESERVED_NAMES = frozenset({"all", "ungrouped", "localhost"})
# A HOST has more names that are the control node to Ansible: its own list is ('127.0.0.1', 'localhost', '::1') —
# an inventory host carrying any of them becomes the host every `hosts: localhost` play and every
# `delegate_to: localhost` task is given (ansible.constants.LOCALHOST; measured). A group cannot carry them:
# component() admits no dot and no colon. The comparison upstream is an exact string, so it is one here.
RESERVED_HOST_NAMES = RESERVED_NAMES | {"127.0.0.1", "::1"}


def inventory_group(value, what="inventory group"):
    """An inventory group a device class may be ATTACHED to — the onboard field, the identity knob, the scaffold's
    argument, a group adopted from a module file: one path component (component()) that is not a name in
    RESERVED_NAMES. A closed deny-list, applied where a group is CHOSEN; a group merely read back by a generator
    or a play is never refused. Returns the value."""
    component(value, what)
    if value in RESERVED_NAMES:
        raise RequestBoundaryError(what, "%s is a name Ansible reserves (all, ungrouped, localhost) — choose the "
                                   "functional group this device belongs to" % what)
    return value


def enabled_keys(fleet):
    """The device-class keys a PARSED fleet file enables, without the entries that are not a key at all: a
    `- null`, a number, a mapping, a name outside the charset. Every reader that enumerates the fleet and resolves
    each class through module_file() takes its keys from here, so one bad line in a fleet file cannot turn every
    route that enumerates classes into a refusal."""
    mods = fleet.get("enabled_modules") if isinstance(fleet, dict) else None
    return [key for key in mods if is_component(key)] if isinstance(mods, list) else []


def fleet_problems(fleet):
    """What is wrong with a PARSED fleet file's `enabled_modules`, as messages that name the entry's PLACE — its
    number among the list's entries, counted from one; comment and blank lines of the file are not entries — and
    the rule, and never the entry: [] when it is a list of class keys, and when there is no list (the key absent or
    null — a fleet with no class). The complement of enabled_keys(): every entry that function drops has one
    message here, and a file that is not a mapping, or a value that is there and is not a list (an empty mapping
    or an empty string included), has one too. For the readers that have no request to
    answer: a generator that RENDERS prints these and skips the entry; the one that decides what is INSTALLED
    stops (gen-requirements.py), and the install and deploy plays run its check — and stop on it — before anything
    walks the list."""
    if not isinstance(fleet, dict):
        return ["the file is not a mapping"]
    mods = fleet.get("enabled_modules")
    if mods is None:
        return []
    if not isinstance(mods, list):
        return ["enabled_modules is not a list"]
    return ["enabled_modules entry %d is not a device-class key (%s)" % (i + 1, _NOT_A_KEY[isinstance(key, str)])
            for i, key in enumerate(mods) if not is_component(key)]


# Why an entry is not a key, by what YAML made of the line — never by its content. Most such lines are not text at
# all: an unquoted `no`, `on`, `~`, a number or a date is another type to YAML, and reads like a key in the file.
_NOT_A_KEY = {
    True: "1-64 chars of [a-z0-9_-], starting with a letter or digit",
    False: "YAML does not read it as text: an unquoted no, on, off, ~, a number or a date is another type, and a "
           "`name: value` or a nested list is not a key",
}


def fleet_list_unreadable(fleet):
    """True when a PARSED fleet file holds no LIST of classes at all: the file is not a mapping, or its
    `enabled_modules` is there and is not a list. (Absent or empty is a fleet with no class: readable.) A renderer
    must not take such a file for an empty fleet — it would write nothing and PRUNE every file it ever generated;
    it renders nothing and prunes nothing instead, and says so."""
    if not isinstance(fleet, dict):
        return True
    mods = fleet.get("enabled_modules")
    return mods is not None and not isinstance(mods, list)


def collection_fqcn(value, what="collection"):
    """An Ansible collection name, exactly `namespace.name` in Galaxy's charset (lowercase, digits, `_`) — the form
    every `ansible_collections/<ns>/<name>` join and every `ansible-doc` / `ansible-galaxy` argv word expects."""
    if not isinstance(value, str) or not _FQCN_RE.fullmatch(value):
        raise RequestBoundaryError(what, "%s is not a collection name (expected namespace.name, lowercase)" % what)
    return value


def is_collection_fqcn(value):
    """True when `value` would pass collection_fqcn(). For a VALIDATOR of data the tool reads back — a unit
    descriptor's `unit.collection` — which answers with a message of its own: a descriptor is not a request field,
    and a request-boundary refusal for it would name a field nobody sent."""
    return isinstance(value, str) and _FQCN_RE.fullmatch(value) is not None


def hostname(value, what="host"):
    """A device address or inventory host name: an IPv4/IPv6 literal WITHOUT a zone id, or an RFC-1123 hostname
    (labels of letters, digits and hyphens joined by single dots, at most 253 chars). Content rather than a path, but
    it becomes an inventory KEY, an inventory variable Ansible templates, and part of a lookup expression, so the
    closed charset keeps `..`, separators, braces and every other metacharacter out."""
    if isinstance(value, str) and value:
        if _IP_LITERAL_RE.fullmatch(value):
            try:
                ipaddress.ip_address(value)
                return value
            except ValueError:
                pass
        if len(value) <= 253 and _HOSTNAME_RE.fullmatch(value):
            return value
    raise RequestBoundaryError(what, "%s is not an IP address (without a zone id) or an RFC-1123 hostname" % what)


def inventory_host(value, what="host_name"):
    """The NAME a host is given in the inventory, where a request chooses it: a hostname() that is not a name in
    RESERVED_HOST_NAMES. The address (`ansible_host`) is not a name of the inventory and takes hostname() alone.
    Returns the value."""
    hostname(value, what)
    if value in RESERVED_HOST_NAMES:
        raise RequestBoundaryError(what, "%s is a name Ansible reserves (all, ungrouped, localhost, 127.0.0.1, "
                                   "::1) — give the host a name of its own" % what)
    return value


_NOT_CONFINED = ("a write target does not resolve inside the repository (a symlink on the way, or a wrong write "
                 "root) — a fault of the server's layout, not of the request")


def confined(rel_path):
    """The ABSOLUTE write target for a repo-relative path, or RepositoryDataError if it would land outside
    write_root(): an absolute path, a `..` that climbs out, a NUL, or a symlink that points out. The last check
    before every drop-in write (gitio._write_new / write_inventory_host, the actuation descriptor + values
    writers, module_write_path). Every caller passes a path the SERVER built from parts that already passed a
    validator, so a refusal here is never about a request field: it is the layout (under an `instance/` that is a
    symlink out of the write root no DROP-IN is written; the fixed-path writers — the fleet file, a secret domain
    — do not pass here and still write through the link) or a bug — and it is answered as such."""
    if not isinstance(rel_path, str) or not rel_path or os.path.isabs(rel_path) or "\x00" in rel_path:
        raise RepositoryDataError("path", _NOT_CONFINED)
    root = os.path.realpath(write_root())
    full = os.path.realpath(os.path.join(root, rel_path))
    if full != root and not full.startswith(root + os.sep):
        raise RepositoryDataError("path", _NOT_CONFINED)
    return full


def confined_file(rel_path):
    """True when repo-relative `rel_path` names a regular FILE inside write_root(). The rule is confined()'s — an
    absolute path, a `..` that climbs out, a NUL or a symlink that points out is "no", never an exception — with
    the path normalised and checked HERE, where the filesystem is asked: a static analyser does not credit a check
    made inside another function (_overlay_file_present is the same idea for the overlay probe)."""
    try:
        confined(rel_path)
    except RepositoryDataError:
        return False
    root = os.path.realpath(write_root())
    full = os.path.realpath(os.path.join(root, rel_path))
    if not full.startswith(root.rstrip(os.sep) + os.sep):
        return False
    return os.path.isfile(full)


# --- The modules registry overlay (Phase-B Fork B, synthesis §2) -------------------------------------------------
# `modules/` is the ONE registry that grows with operator action (onboarding a new device-class) and is EDITED in
# place (reconfigure of a shipped class's identity/telemetry/logging/backup blocks). So unlike the other read-only
# `*_DIR` registries (vectors/telemetry/logging/…, which stay ROOT-bound), the runtime service readers resolve a
# module file through this overlay: the WRITE-mutable clone (write_root) SHADOWS the immutable baked ROOT. A SHIPPED
# class is baked read-only into the image at ROOT; an operator-ONBOARDED class (and any reconfigured shipped one)
# lives in the propose clone, so the clone copy wins when present. The DECISION (D1, ratified by the catalog-reader
# trace): operator classes land in the canonical's `modules/`, NOT instance/modules — so the deploy-time generators
# (gen-requirements et al.) keep reading `modules/` off the canonical clone with ZERO change, and this overlay is
# confined to the baked runtime readers. With write_root()==ROOT (CLI/tests/legacy `/repo` deploy) both helpers are
# byte-for-byte the plain ROOT join — the zero-behaviour-change identity.

def module_file(key):
    """Absolute path to `modules/<key>/module.yml` (the device-class descriptor) — the clone (write_root) shadows
    the baked ROOT, so an operator-onboarded/reconfigured class in the propose clone is read in preference to a
    pristine SHIPPED class baked at ROOT. The runtime-reader analogue of resolve() for the modules registry. With
    write_root()==ROOT this == os.path.join(ROOT, "modules", key, "module.yml") (identity)."""
    component(key, "device-class key")                       # the request boundary: no separator ever reaches the join
    clone = os.path.join(write_root(), "modules", key, "module.yml")
    if os.path.exists(clone):
        return clone
    return os.path.join(ROOT, "modules", key, "module.yml")


def module_write_path(key):
    """The absolute WRITE target for `modules/<key>/module.yml`: always under write_root(), never the code root.
    The write-side twin of module_file(). A READER has two places to look — the clone's copy when there is one,
    the baked one otherwise; a WRITER has one. So a reconfigure of a SHIPPED class the clone does not hold yet is
    read from ROOT and written into the clone, which shadows the baked copy from then on. With write_root()==ROOT
    this names the same file module_file() does.

    Why it has a name: each reconfigure used to spell the join itself, three of them against ROOT — so on a baked
    install the plan was computed from the image's bytes, the write went to the read-only image tree, and the
    scoped commit staged a path in the clone where nothing had changed."""
    component(key, "device-class key")
    return confined("modules/%s/module.yml" % key)


def module_keys():
    """Sorted device-class keys across BOTH module roots — pristine SHIPPED classes baked at ROOT plus operator-
    onboarded/reconfigured ones in the write_root clone (deduped; the clone shadows ROOT on a shared key). The scan
    analogue of module_file() for the one runtime reader that ENUMERATES classes (catalog.module_for_collection).
    With write_root()==ROOT this is just sorted(os.listdir(ROOT/modules))."""
    keys = set()
    for base in (os.path.join(ROOT, "modules"), os.path.join(write_root(), "modules")):
        if os.path.isdir(base):
            for name in os.listdir(base):
                # a class is a DIRECTORY whose name is a valid key — README.md, _core.yml and a stray file are not
                # classes, and module_file() now refuses a non-key, so the enumerator must not hand it one (C22)
                if os.path.isdir(os.path.join(base, name)) and is_component(name):
                    keys.add(name)
    return sorted(keys)


def _cli(argv):
    """Tiny CLI so a NON-Python caller (ansible/playbooks/deploy-stack.yml) can resolve an instance-overlay path
    through the SAME seam the service layer uses: `python3 scripts/kontroll/paths.py resolve ansible/secrets` prints
    the absolute overlay (or legacy) dir. This keeps deploy-stack's secret READ path identical to the GUI
    onboarding WRITE path (secrets.py overlay_target) — no hardcoded `instance/secrets` that could silently drift
    from the overlay map under a relocation. resolve() is existence-based, matching secrets.py's read (L31)."""
    if len(argv) == 2 and argv[0] == "resolve":
        return resolve(argv[1])
    raise SystemExit("usage: paths.py resolve <root-relative-path>")


if __name__ == "__main__":
    import sys
    print(_cli(sys.argv[1:]))
