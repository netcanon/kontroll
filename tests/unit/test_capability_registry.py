"""The secondary-capability registry (capabilities/<cap>.yml) — catalog.load_capabilities() +
registered_capabilities().

These pin the foundation of the generalized secondary-capability dialog seam (Capability-track Phase 7,
docs/observability/secondary-capability-dialog.md §2). They guard that the registry is a true DROP-IN (a
dropped-in descriptor alone appears, with no loader edit — the modularity the whole seam rests on), that the
shipped telemetry descriptor carries every field the shell dispatches over (so the parameterized shell never
hits a missing key), and — critically — that the descriptor's wiring is REAL, not vapor: the suggester it
names must be an importable callable and the vector/generator it points at must exist on disk. The last pin
is what stops a descriptor from silently referencing a function or file that was renamed or never written.
"""
import importlib
import os
import re

import pytest
import yaml

from kontroll import catalog, paths

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
pytestmark = pytest.mark.unit

# The fields the shared dialog/route/promote shell reads off EVERY descriptor (the parameterization
# contract). A descriptor missing one of these would make the generic shell raise at dispatch time.
_REQUIRED_TOP = {"name", "label", "order", "vector", "suggester", "service", "block_key", "block_shape",
                 "method_source", "render_method_stage", "resource_stage", "generator", "enact_kind"}
_REQUIRED_SUGGESTER = {"module", "suggest", "declared"}


def test_registry_loads_telemetry_sorted_by_order():
    """catalog.load_capabilities() discovers the capabilities/<cap>.yml drop-ins and returns them sorted by
    `order` — the registry the generalized seam consumes. Guards that telemetry (instance #1) is present and
    that the sort is honored, so the route loop / pins iterate a stable, ordered registry."""
    caps = catalog.load_capabilities()
    by_name = {c["name"]: c for c in caps}
    assert "telemetry" in by_name
    assert [c["name"] for c in caps] == sorted(by_name, key=lambda n: by_name[n]["order"])
    assert catalog.registered_capabilities() == [c["name"] for c in caps]


def test_telemetry_descriptor_has_every_field_the_shell_dispatches_over():
    """The shipped telemetry descriptor carries the full parameterization contract (the _REQUIRED_TOP /
    _REQUIRED_SUGGESTER keys). Guards the shared shell against a KeyError at dispatch: a descriptor that
    omitted, say, `enact_kind` or `block_shape` would crash the generic route, not just degrade telemetry."""
    tel = next(c for c in catalog.load_capabilities() if c["name"] == "telemetry")
    assert _REQUIRED_TOP <= set(tel), "missing: %s" % (_REQUIRED_TOP - set(tel))
    assert _REQUIRED_SUGGESTER <= set(tel["suggester"])
    assert tel["block_key"] == "metrics" and tel["block_shape"] == "list"
    assert tel["enact_kind"] == "prometheus_reload"


def test_telemetry_descriptor_wiring_is_real_not_vapor():
    """Every reference in the telemetry descriptor resolves to a real artifact: the suggester module imports
    and its suggest/declared names are callables, the named vector file exists, and the generator script
    exists. Guards the seam's worst failure mode — a descriptor pointing at a renamed function or an unwritten
    generator would pass a schema check but explode only when an operator opens the dialog."""
    tel = next(c for c in catalog.load_capabilities() if c["name"] == "telemetry")
    mod = importlib.import_module("kontroll.service." + tel["suggester"]["module"])
    assert callable(getattr(mod, tel["suggester"]["suggest"]))
    assert callable(getattr(mod, tel["suggester"]["declared"]))
    svc = importlib.import_module("kontroll.service." + tel["service"]["module"])
    assert callable(getattr(svc, "build_plan")) and callable(getattr(svc, "apply_plan"))
    assert os.path.exists(os.path.join(ROOT, "vectors", tel["vector"] + ".yml"))
    assert os.path.exists(os.path.join(ROOT, tel["generator"]["script"]))


def test_registry_is_drop_in(tmp_path, monkeypatch):
    """A capabilities/<cap>.yml dropped into the registry dir alone appears in registered_capabilities(),
    with no loader edit — the registry's own "add an X" proof. Guards the modularity invariant the seam
    rests on: registering a capability is a file drop, never a code change. (A bare README is ignored.)"""
    (tmp_path / "zztest.yml").write_text(
        yaml.safe_dump({"name": "zztest", "order": 99}), encoding="utf-8")
    (tmp_path / "README.md").write_text("# ignored", encoding="utf-8")
    monkeypatch.setattr(paths, "CAPABILITIES_DIR", str(tmp_path))
    assert catalog.registered_capabilities() == ["zztest"]


def test_missing_registry_dir_is_empty_not_an_error(tmp_path, monkeypatch):
    """An absent capabilities/ dir yields an empty registry (not an exception) — so the seam degrades to
    "no secondary capabilities", and the INVARIANT D* PRIMARY pin (which must survive an empty registry)
    has a real empty-registry case to lean on."""
    monkeypatch.setattr(paths, "CAPABILITIES_DIR", str(tmp_path / "does-not-exist"))
    assert catalog.load_capabilities() == []
    assert catalog.registered_capabilities() == []


# The containers the page itself makes for a knob group (index.html: the capability dialog's `cap-params`, the
# secrets dialog's `secret-fields`, the unit-configure pane's `unit-config-fields`) — a literal group the service
# writes may name these; a descriptor's Stage-3 value may not (it is its capability's own, `cap-<cap>-…`).
_GROUP_CONTAINERS = {"cap-params", "secret-fields", "unit-config-fields"}


def _page_text():
    with open(os.path.join(ROOT, "gui", "templates", "index.html"), encoding="utf-8") as f:
        return f.read()


# a hyphenated token opened by a quote, a backtick or `=` and closed by a quote, a backtick, `]`, `>`, whitespace, `/`
# or a backslash: a JS string or template literal, an attribute value quoted, unquoted or in escaped quotes
_TOKEN = re.compile(r"""['"`=]([a-z][a-z0-9-]*-[a-z0-9-]+)(?=['"`\]>\s/\\])""")
# after a quote, a backtick or `=` (an unquoted attribute): a literal that is `cap-` or `cap-<word>-` up to its closing
# quote (a prefix a testid is built from), or one that opens `cap-…${` — how a `cap-` testid built at run time is
# spelled. Not read, e.g.: `['cap', x].join('-')`, `'cap' + '-' + x`, a placeholder filled in later.
_BUILT = re.compile(r"""['"`=]cap-(?:[a-z0-9-]*-)?['"`]|['"`=]cap-[a-z0-9-]*\$\{""")


def _page_tokens(text=None):
    """Every `_TOKEN` in `text` (default: the page) — each testid the page creates as a literal among them, and more
    (class names and the like), which only make the checks below stricter. A hyphenless testid is not seen; none can
    collide with a `cap-<cap>-` value. A testid the page builds at run time is not a token: `_built_cap_testids`
    reads its spellings."""
    return set(_TOKEN.findall(_page_text() if text is None else text))


def _built_cap_testids(text=None):
    """Every `_BUILT` spelling in `text` (default: the page): a `cap-` prefix literal or a `cap-…${` template, how a
    testid built at run time is spelled — which would occupy every capability's namespace at once and which no token
    shows."""
    return _BUILT.findall(_page_text() if text is None else text)


def _page_namespaces(page):
    """The capability names whose `cap-<cap>-` namespace already holds a testid the page creates — a capability
    registered under one could name that testid and still pass the server's rule."""
    owners = set()
    for token in page:
        if token.startswith("cap-"):
            parts = token[len("cap-"):].split("-")
            owners |= {"-".join(parts[:i]) for i in range(1, len(parts))}
    return owners


def _stage_testid_faults(descriptors, page):
    """([(capability, fault)], how many name a testid) over `descriptors`: a `resource_stage.testid` the server
    would drop, or one the page creates — a control or a container. (The server's rule already keeps a value out of
    `knob-<key>`: it must start `cap-<cap>-`.)"""
    from kontroll.service.configurable import group_testid
    faults, named = [], 0
    for d in descriptors:
        testid = (d.get("resource_stage") or {}).get("testid")
        if testid is None:
            continue
        named += 1
        if group_testid(testid, d.get("name", "cap")) != testid:
            faults.append((d.get("name"), "dropped by the server"))
        elif testid in page:
            faults.append((d.get("name"), "a testid the page creates"))
    return faults, named


def _reserved_taken():
    """The registered capability names whose `cap-<cap>-` namespace the page occupies — reserved until T23, so none
    may be registered."""
    return _page_namespaces(_page_tokens()) & set(catalog.registered_capabilities())


def _literal_group_testids():
    """The group testids the configurable service writes as literals (not descriptor data)."""
    import re
    with open(os.path.join(ROOT, "scripts", "kontroll", "service", "configurable.py"), encoding="utf-8") as f:
        return set(re.findall(r'"testid": "([^"]+)"', f.read()))


def test_a_group_testid_is_never_a_control_of_its_dialog():
    """Verifies: every shipped capability's `resource_stage.testid`, as its descriptor writes it, passes the server's
    rule (so none is dropped, and none is a `knob-<key>` row) and is no testid the page creates — a control, a
    container; and every group testid the service writes as a literal is none the page creates for anything but the
    group containers it makes itself. Guards against (T22 / F9): the page creates a group's container first and finds
    the dialog's elements by testid, first match first — a descriptor naming `cap-promote` leaves the real Promote
    disabled with its handler on the container; one naming `cap-params` or `knob-<key>` has Stage 3 wipe the method's
    knobs, so Propose harvests the wrong ones — and against a descriptor whose testid the server drops, losing its whole
    Stage-3 group with nothing but a log line."""
    page = _page_tokens()
    assert {"cap-promote", "cap-propose", "cap-params"} <= page                 # the scan finds the dialog's own
    faults, named = _stage_testid_faults(catalog.load_capabilities(), page)
    assert faults == []
    assert named, "no shipped capability names a Stage-3 testid — the check above ran over nothing"
    literals = _literal_group_testids()
    assert _GROUP_CONTAINERS <= literals                                        # the allow-list names real groups
    assert literals & (page - _GROUP_CONTAINERS) == set(), sorted(literals & (page - _GROUP_CONTAINERS))


def test_the_registry_check_refuses_a_descriptor_in_the_occupied_namespace(tmp_path, monkeypatch):
    """Verifies: the check above refuses a descriptor of a capability named `cred` that names `cap-cred-prereq` — a
    value the server's rule accepts (its own namespace) that is a control the page creates — and one whose value
    the server drops; and that the reservation finds the registered `cred`. Guards against: the one collision the
    namespace leaves passing unseen. The shipped registry never reaches the page clause, so a change that dropped it
    would stay green over shipped data alone."""
    for name, testid in (("cred", "cap-cred-prereq"), ("zz", "cap-other-x")):
        (tmp_path / (name + ".yml")).write_text(
            yaml.safe_dump({"name": name, "order": 99, "resource_stage": {"testid": testid}}), encoding="utf-8")
    monkeypatch.setattr(paths, "CAPABILITIES_DIR", str(tmp_path))
    faults, named = _stage_testid_faults(catalog.load_capabilities(), _page_tokens())
    assert named == 2 and sorted(faults) == [("cred", "a testid the page creates"), ("zz", "dropped by the server")]
    assert _reserved_taken() == {"cred"}                                          # and the reservation refuses it


@pytest.mark.parametrize("snippet, owners, built", [
    ("`cap-links-hint`", {"links"}, False),
    ("'<i data-testid=\"cap-links-hint\">?</i>'", {"links"}, False),
    ("'<i data-testid=cap-links-hint>?</i>'", {"links"}, False),
    ("'<i data-testid=cap-links-hint class=\"m\">?</i>'", {"links"}, False),
    ("'<i data-testid=cap-links-hint/>'", {"links"}, False),
    ("'<i data-testid=\\'cap-links-hint\\'>?</i>'", {"links"}, False),
    ("'cap-promote'", set(), False),
    ("'cap-cred-open'", {"cred"}, False),
    ("'cap-links-' + x", {"links"}, True),
    ("E('span', null, null, 'cap-' + dlg.dataset.cap + '-status')", set(), True),
    ("E('span', null, null, `cap-${dlg.dataset.cap}-status`)", set(), True),
    ("E('span', null, null, `cap-` + dlg.dataset.cap + `-status`)", set(), True),
    ("const CAPP = 'cap-'; E('span', null, null, CAPP + x)", set(), True),
    ("'cap-'.concat(dlg.dataset.cap, '-status')", set(), True),
    ("'<i data-testid=\"cap-' + x + '-s\">'", set(), True),
    ("`<i data-testid=\"cap-${x}-s\">`", set(), True),
    ("'<i data-testid=cap-' + x + '-s>'", set(), True),
    ("`<i data-testid=cap-${x}-s>`", set(), True),
    ("`cap-links-${x}`", set(), True),
])
def test_the_page_readers_see_each_spelling(snippet, owners, built):
    """Verifies: the token scan opens a capability's namespace for each way the page can spell a testid literal — a
    template literal, an attribute value double-quoted (the page's own style), unquoted (before `>`, a space or `/>`)
    or in escaped quotes — and only past a second hyphen (`cap-promote` opens none); and `_built_cap_testids` finds
    each spelling of a `cap-` testid built at run time — `'cap-' +`, a `${…}` template (mid-word too), a backtick
    prefix, a prefix held in a constant, `.concat`, and each in markup, the attribute double-quoted or unquoted — and
    none in a literal testid. Guards against: the checks over the page going blind with every test green — today's page
    uses few of these spellings, so a narrowed pattern would still find nothing there and pass."""
    assert _page_namespaces(_page_tokens(snippet)) == owners
    assert bool(_built_cap_testids(snippet)) == built


def test_cred_is_the_one_capability_name_the_page_occupies():
    """Verifies: `cred` is the only capability name whose `cap-<cap>-` namespace already holds a testid the page
    creates (its `cap-cred-*` controls) — each hyphen after the first opening one more name, so `cap-a-b-c` opens
    `a` and `a-b` — and the page holds no `cap-` prefix literal or `cap-…${` template (`_BUILT`: a testid built at
    run time, which the token scan cannot see and which would occupy every capability's namespace at once); and the
    name `cred` is reserved: no registered capability has it. Guards against: the page adding a `cap-<word>-…`
    testid, which opens a second name a descriptor could clash through, while configurable.py and testid_reference.md
    still say `cred` is the one — the registry check would refuse a literal clash itself, but not a built one, and
    the claim around it would rot silently. T23 (wiring the dialog by reference) removes the class and the
    reservation."""
    assert _page_namespaces({"cap-a-b-c", "cap-x"}) == {"a", "a-b"}
    owners = _page_namespaces(_page_tokens())
    assert owners == {"cred"}, sorted(owners)
    built = _built_cap_testids()
    assert built == [], ("the page holds a cap- prefix literal or a cap-…${ template (a testid built at run time): %s"
                         % built)
    taken = _reserved_taken()
    assert not taken, ("capability name reserved until T23: its cap-<name>-* namespace holds the page's own "
                       "controls — %s" % sorted(taken))
