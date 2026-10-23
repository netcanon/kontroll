"""Canned shapes: the dictionaries a browser test or a route test is served in place of a real service call (S33).

Each lives here once, and `tests/unit/test_mock_conformance.py` holds a row for each (bar `E2E_UNIT`, which the
actuation registry's own validator holds) that builds the smallest real fixture, calls the real function and
compares the two SHAPES — the key set and each value's type, never the values.
A canned dict written from belief keeps a UI test green while the server emits something else (L16's second most
recurrent fault), and a second hand copy drifts from the first. Add a dict here and its row there; a seam the e2e
server replaces with no row, in a form the conformance file's parse reads or refuses, fails
`test_every_replaced_seam_has_a_conformance_row`. Nothing scans a `page.route` body
or a route test's fake, so one written by hand in its test is caught only in review: tests/README.md lists the ones
that still are.

Values are RFC-5737 / RFC-7042 examples and shaped fakes only — never a real key, address or host. Not collected by
pytest (the leading underscore), like `_hermetic.py`.
"""
import copy

# A REGISTERED actuation unit, so the Index Automations section and the configure dialog render in a real browser
# (`catalog.load_actuation_units`). `_actuation_schema.validate_unit` must accept it.
E2E_UNIT = {
    "schema": 1, "key": "community-docker-swarm",
    "unit": {"kind": "role", "collection": "community.docker", "name": "swarm"},
    "install": {"collections": [{"name": "community.docker", "version": "==3.10.4"}],
                "provenance": {"source": "galaxy", "signature": "adaptive"}},
    "target": {"device_class": "docker_host", "inventory_group": "docker_hosts", "blast_radius": "LAN"},
    "knobs": [{"key": "listen_port", "label": "Listen port", "type": "int",
               "range": {"min": 1, "max": 65535}, "default": 2377}],
}

# `provenance.fleet_provenance`: one unsigned-pinned collection, so the amber Supply-chain badge renders.
FLEET_PROVENANCE = {
    "available": True, "default_policy": "adaptive", "keyring_present": False,
    "collections": [{"name": "community.docker", "pin": "==4.0.0", "pin_kind": "exact", "policy": "adaptive",
                     "class": "unsigned-pinned", "digest_recorded": False,
                     "note": "verified by pin + checksum — no signature served by this source"}],
    "summary": {"total": 1, "unsigned_pinned": 1, "signed": 0, "digests_recorded": 0}}

# `provenance.fleet_provenance` before the trust sidecar is generated: the GUI degrades, never errors.
FLEET_PROVENANCE_UNAVAILABLE = {
    "available": False, "default_policy": None, "keyring_present": False, "collections": [],
    "summary": {"total": 0, "unsigned_pinned": 0, "signed": 0, "digests_recorded": 0}}

# `provenance.image_provenance`: one digest-pinned image, so the amber image badge renders.
IMAGE_PROVENANCE = {
    "available": True, "signing_configured": False,
    "images": [{"name": "kontroll-control", "ref": "ghcr.io/example/kontroll-control", "tag": "v1",
                "digest": "sha256:" + "a" * 64, "digest_short": "a" * 12, "class": "digest-pinned",
                "note": "Docker verifies this @sha256: on every pull — not a signature (a digest-pin is not signed)"}],
    "summary": {"total": 1, "digest_pinned": 1, "local_build": 0, "signed": 0}}

# `discovery.read_inbox`: two un-onboarded candidates — a normal host, and one whose hostname is a `<script>`
# payload (the F6 safe-render e2e: it must show as literal text). The second has no OUI match, so no vendor line.
DISCOVERY_INBOX = {
    "generated_at": "2026-07-03T00:00:00+00:00",
    "sources": [{"key": "opnsense", "method": "dhcp_leases_opnsense", "host": "192.0.2.1",
                 "status": "ok", "count": 2}],
    "candidates": [
        {"ip": "192.0.2.42", "mac": "00:00:5e:00:53:01", "hostname": "sw-lab-3",
         "vendor": "IEEE Example Vendor Inc",
         "source_key": "opnsense", "source_host": "192.0.2.1"},
        {"ip": "192.0.2.43", "mac": "00:00:5e:00:53:02", "hostname": "<script>window.__xss=1</script>",
         "vendor": None,
         "source_key": "opnsense", "source_host": "192.0.2.1"}]}


def _pending(paths, changes):
    """`/api/pending` (`pending.list_pending`) with one STALE proposal: `main` advanced past it (the FF-race)."""
    return {
        "proposals": [{
            "run_id": "abc123def456", "ref": "refs/heads/proposed/abc123def456", "sha": "de" * 20,
            "staged_at": "2026-07-05T00:00:00+00:00", "author": "kontroll",
            "subject": "feat(onboard): stage cisco_ios",
            "paths": paths, "changes": changes, "changes_read": True,
            "promotable": False, "repropose_hint": {"kind": "onboard", "key": "cisco_ios"}}],
        "unrecognised": [],
        "canonical": "/srv/kontroll.git", "promote_task": "promote-proposal",
        "note": None, "note_kind": None, "main_note": None, "skipped_note": None}


# The Discard e2e: a stale onboard proposal that adds its drop-in.
PENDING_STALE = _pending(
    ["instance/inventory/onboarded-cisco_ios.yml"],
    [{"status": "A", "path": "instance/inventory/onboarded-cisco_ios.yml", "exact": True}])

# The Re-propose e2e: a stale onboard proposal that adds its drop-in and changes its module file.
PENDING_STALE_ONBOARD = _pending(
    ["instance/inventory/onboarded-cisco_ios.yml", "modules/cisco_ios/module.yml"],
    [{"status": "A", "path": "instance/inventory/onboarded-cisco_ios.yml", "exact": True},
     {"status": "M", "path": "modules/cisco_ios/module.yml", "exact": True}])

# `POST /api/pending/<run_id>/discard`'s answer when the ref was deleted.
DISCARD_ANSWER = {"discarded": True, "run_id": "abc123def456", "ref": "refs/heads/proposed/abc123def456",
                  "sha": "de" * 20, "run_id_audit": "0123456789ab"}

# Its answer when the ref moved or vanished since it was listed (the compare-and-delete refused): the row stays.
DISCARD_REFUSED = {"error": "the proposal moved or vanished since it was listed — refresh and retry",
                   "run_id": "abc123def456"}

# `homepage.read_board`: one hand-made section holding one plain tile.
HOMEPAGE_BOARD = {
    "title": "t", "theme": None,
    "sections": [{"name": "A", "generated": False, "style": None, "columns": None,
                  "items": [{"name": "Grafana", "href": "https://grafana.example.test", "description": None,
                             "has_widget": False, "widget_type": None, "present": True}]}]}

# `homepage.build_homepage_plan` for a board that reorders a section: a diff, nothing removed.
HOMEPAGE_PLAN = {
    "error": None, "paths": ["instance/dashboards/homepage/services.yaml",
                             "instance/dashboards/homepage/settings.yaml"],
    "diff": [{"file": "services.yaml", "unified": "@@ reorder @@"}],
    "summary": {"removed_tiles": [], "removed_widgets": [], "changed": True},
    "text_after": {"services": "---\n", "settings": "---\n"},
    "token_parts": ["---\n", "---\n"], "plan_token": "tok"}

# `keygen.apply_keygen_plan`: a FAKE keypair (the `1FAKE` sentinel the gitleaks allowlist exempts; never a real
# key), placed offline.
KEYGEN_RESULT = {"error": None, "changed": True, "role": "break-glass",
                 "public_key": "age1pubdummyxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxq",
                 "private_key": "AGE-SECRET-KEY-1FAKETESTPRIVATEKEYZZZ",
                 "placement": "offline", "private_key_path": None,
                 "recipient_domains": ["network", "proxmox", "dashboards"], "high_blast": True,
                 "sops_updatekeys": "sops --config instance/.sops.yaml updatekeys instance/secrets/*.sops.yml",
                 "next_steps": ["Store the shown private key OFFLINE on trusted media — it is not saved here.",
                                "So the new recipient lands on main, promote proposed/<run_id>: …"],
                 "paths": [".sops.yaml"]}


def keygen_answer(changed, staged):
    """`POST /api/keygen/<role>`'s answer over `KEYGEN_RESULT`: STAGED as `proposed/<run_id>`, committed to `main` (a
    deploy that does not stage), or nothing changed (the key was already a recipient, so nothing was committed and
    the route names no ref)."""
    run_id = "0123456789ab"
    out = copy.deepcopy({k: KEYGEN_RESULT[k] for k in ("role", "private_key", "public_key", "placement",
                                                       "private_key_path", "recipient_domains", "high_blast",
                                                       "sops_updatekeys")})
    out.update({"changed": changed, "committed": changed, "staged": staged, "run_id": run_id,
                "target_ref": "proposed/" + run_id if staged else ("main" if changed else None),
                "next_steps": KEYGEN_RESULT["next_steps"][:2 if staged else 1],
                "note": None if changed else "public key was already a recipient; nothing staged"})
    return out


def service_units(collection):
    """`units.service_units` for an installed collection that ships one role."""
    return {"collection": collection,
            "units": [{"kind": "role", "name": "swarm", "id": collection + "/role/swarm"}]}


# `catalog.local_installed`: the one collection the e2e server reports installed.
LOCAL_INSTALLED = {"cisco.ios": "5.0.0"}
