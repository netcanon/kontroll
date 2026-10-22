"""The ONE knob renderer (Phase 3, #137), exercised directly in a real browser via `page.evaluate` over the
top-level pure render functions (the `renderCapPromote`/`renderHomepageResult` precedent — render functions stay
global so a synthetic descriptor can drive them with NO write). WHY (the failures these guard): goal-2 widens the
GUI's widget vocabulary from `<select>`-only to bool/int/text/secret; the renderer is the data-driven seam that
makes "expose a knob" a descriptor edit, never UI code. These prove (a) each knob `type` produces the right
widget carrying `data-param`, (b) the render-table dispatches by `source_kind` (closing the specced-but-unbuilt
Stage-3 gap), (c) `knobSelection` harvests by type (checkbox→bool, number→int, blank omitted), and — the
load-bearing security check — (d) a SECRET knob is write-only and NEVER pre-filled with a value (C11). Selectors
are data-testid/structure of the returned node only. opt-in via the `e2e` marker.
"""
import pytest

pytestmark = pytest.mark.e2e


def test_render_knob_widget_per_type(page):
    """renderKnob emits the right widget per `type`: enum→<select>, bool→checkbox, int→number(min/max), text→
    text(pattern) — each carrying data-param=<key>. Guards the type→widget table (the widened vocabulary)."""
    page.goto("/")
    out = page.evaluate("""() => ({
        enumW: renderKnob({key:'transport', type:'enum', allowed:['tcp','udp'], default:'udp'}).querySelector('[data-param]').outerHTML,
        boolW: renderKnob({key:'on', type:'bool', default:true}).querySelector('[data-param]').outerHTML,
        intW:  renderKnob({key:'port', type:'int', range:{min:1,max:65535}, default:443}).querySelector('[data-param]').outerHTML,
        textW: renderKnob({key:'name', type:'text', pattern:'[a-z]+'}).querySelector('[data-param]').outerHTML,
    })""")
    assert out["enumW"].startswith("<select") and 'data-param="transport"' in out["enumW"]
    assert "<option" in out["enumW"]
    assert 'type="checkbox"' in out["boolW"] and 'data-param="on"' in out["boolW"]
    assert 'type="number"' in out["intW"] and 'max="65535"' in out["intW"] and 'min="1"' in out["intW"]
    assert 'type="text"' in out["textW"] and 'pattern="[a-z]+"' in out["textW"]


def test_secret_knob_is_write_only_and_never_prefilled(page):
    """A `secret` knob (renderKnob secret branch) renders a write-only <input type=password> with NO value, only
    a NAMES-only 'set — leave blank to keep' placeholder when already_set. THE C11 check: a credential is never
    pre-filled/echoed in the GUI. Guards a regression that would leak a secret value into the rendered widget."""
    page.goto("/")
    html = page.evaluate("() => renderKnob({key:'pw', type:'secret', already_set:true}).querySelector('[data-param]').outerHTML")
    assert 'type="password"' in html and 'data-param="pw"' in html
    assert 'value="' not in html                                  # never pre-filled with a value (C11)
    assert "set — leave blank to keep" in html                    # NAMES-only set hint, not the value


def test_render_secret_field_knob_preserves_the_secret_widget(page):
    """renderSecretKnob (the `fields` source — the secrets dialog) reproduces the existing secret widget: a
    data-field input with the secret-field testid, a textarea for a multiline (PEM) secret, and NO pre-filled
    value for a secret. Guards the secrets-dialog convergence keeping its write-only/data-field contract."""
    page.goto("/")
    out = page.evaluate("""() => {
        const txtNode = renderSecretKnob({key:'user', label:'User', secret:false, default:'admin'});
        return {
            pw:  renderSecretKnob({key:'token', label:'Token', secret:true, already_set:true, generate:'hex32'}).outerHTML,
            pem: renderSecretKnob({key:'ssh_private_key', label:'Key', secret:true, multiline:true}).outerHTML,
            txtHtml: txtNode.outerHTML,
            txtValue: txtNode.querySelector('[data-field]').value,   // the pre-filled default (a DOM property, not an attr)
        };
    }""")
    assert 'data-field="token"' in out["pw"] and 'data-testid="secret-field"' in out["pw"]
    assert 'type="password"' in out["pw"] and 'data-testid="secret-generate"' in out["pw"]
    assert 'value="' not in out["pw"]                            # a SECRET is never pre-filled, even with a default (C11)
    assert "<textarea" in out["pem"] and 'data-field="ssh_private_key"' in out["pem"]
    assert 'type="text"' in out["txtHtml"] and out["txtValue"] == "admin"   # a NON-secret field keeps its default


def test_render_knob_group_dispatches_by_source_kind(page):
    """renderKnobGroup fills the group's testid container by source_kind: `params`→renderKnob rows;
    `curated_list` with no knobs→an honest empty state (no speculative chips); `none`→nothing. Guards the
    render-table that closes the Stage-3 gap (testid_reference §Stage-3)."""
    page.goto("/")
    out = page.evaluate("""() => {
        const mk = () => { const d = document.createElement('div');
            d.innerHTML = '<div data-testid="cap-params"></div><div data-testid="cap-telemetry-dashboards"></div>'; return d; };
        const p = mk(); renderKnobGroup(p, {testid:'cap-params', source_kind:'params',
            knobs:[{key:'schedule', type:'enum', allowed:['a','b']}]});
        const c = mk(); renderKnobGroup(c, {testid:'cap-telemetry-dashboards', source_kind:'curated_list', knobs:[]});
        const n = mk(); renderKnobGroup(n, {testid:'cap-telemetry-dashboards', source_kind:'none', knobs:[]});
        return {
            params: p.querySelector('[data-testid=cap-params]').innerHTML,
            curated: c.querySelector('[data-testid=cap-telemetry-dashboards]').innerHTML,
            none: n.querySelector('[data-testid=cap-telemetry-dashboards]').innerHTML };
    }""")
    assert 'data-testid="knob-schedule"' in out["params"] and "<select" in out["params"]
    assert 'data-testid="knob-empty"' in out["curated"]           # curated_list w/o data → honest empty state
    assert out["none"].strip() == ""                              # none → renders nothing


def test_render_knob_group_renders_actuation_unit_curated_knobs(page):
    """renderKnobGroup with source_kind `unit` (an app-store unit's CURATED configure knobs, R3) fills the
    `unit-config-fields` container with renderKnob rows — proving the worked-extraction configure surface
    (operator-declared knobs, not argspec) rides the EXISTING renderer with zero new widget code (design 24 §6.1)."""
    page.goto("/")
    html = page.evaluate("""() => {
        const d = document.createElement('div');
        d.innerHTML = '<div data-testid="unit-config-fields"></div>';
        renderKnobGroup(d, {testid:'unit-config-fields', source_kind:'unit', knobs:[
            {key:'mode', type:'enum', allowed:['fast','safe'], default:'safe'},
            {key:'retries', type:'int', range:{min:0,max:5}, default:3}]});
        return d.querySelector('[data-testid=unit-config-fields]').innerHTML;
    }""")
    assert 'data-testid="knob-mode"' in html and "<select" in html      # the curated enum renders a select
    assert 'data-testid="knob-retries"' in html and 'type="number"' in html and 'max="5"' in html


def test_knob_selection_harvests_by_type(page):
    """knobSelection returns {method, params} harvesting by widget type: checkbox→bool, number→Number,
    select/text→string, and a BLANK input is OMITTED (leave-blank-to-keep). Guards the propose/promote payload
    is the same {method, params} the old capSelection produced (backward compatible)."""
    page.goto("/")
    sel = page.evaluate("""() => {
        const d = document.createElement('div');
        d.innerHTML = '<select data-testid="cap-method"><option>snmp</option></select><div data-testid="cap-params"></div>';
        const wrap = d.querySelector('[data-testid=cap-params]');
        wrap.appendChild(renderKnob({key:'module', type:'enum', allowed:['if_mib'], default:'if_mib'}));
        wrap.appendChild(renderKnob({key:'on', type:'bool', default:true}));
        wrap.appendChild(renderKnob({key:'port', type:'int', default:443}));
        wrap.appendChild(renderKnob({key:'blank', type:'text'}));   // left empty -> omitted
        return knobSelection(d);
    }""")
    assert sel["method"] == "snmp"
    assert sel["params"] == {"module": "if_mib", "on": True, "port": 443}   # blank omitted; number is a real int


def test_a_group_testid_the_selector_could_not_parse_does_not_throw(page):
    """Verifies: renderKnobGroup given a testid that is not a valid bare selector token (a space, a bracket, a
    quote) returns without throwing and finds the container that carries exactly that testid. Guards against
    (T22 / F9): the unquoted selector — a throw there leaves the dialog's buttons unwired (the server holds the
    charset; this is the page's own belt)."""
    page.goto("/")
    out = page.evaluate("""() => {
        const res = {};
        for (const t of ['a b', 'x]', 'q"]']) {
            const d = document.createElement('div'); const c = document.createElement('div');
            c.dataset.testid = t; d.appendChild(c);
            try { renderKnobGroup(d, {testid: t, source_kind: 'params', knobs: [{key: 'k', type: 'text'}]});
                  res[t] = c.querySelector('[data-param]') ? 'rendered' : 'empty'; }
            catch (e) { res[t] = 'threw'; }
        }
        return res; }""")
    assert out == {"a b": "rendered", "x]": "rendered", 'q"]': "rendered"}
