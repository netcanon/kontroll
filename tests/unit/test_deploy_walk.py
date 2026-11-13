"""tests/unit/_deploy_walk.py, the deploy's walk that the alert URL's no_log pin (test_alerting_metrics.py) and the
platform-core drift pin (test_gen_secret_env.py) read (T32): on a tree of its own that holds one of each form
Ansible loads, it reaches each; where two places hold a name it reads the file in each place it lists from Ansible's
search orders, and where two spellings do, the one Ansible's role loader takes; and it fails on a file it cannot find,
or cannot name."""
import configparser
import glob
import json
import os
import re

import pytest
import yaml
from _deploy_walk import CWD, INVENTORY, ROLES_PATH, Unfollowed, Walk, _delimited, args, kv, load, strings

pytestmark = pytest.mark.unit

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _task(mark):
    return {"name": mark, "ansible.builtin.debug": {"msg": mark}}


def _tree(root, files):
    for rel, doc in files.items():
        path = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(doc if isinstance(doc, str) else json.dumps(doc, indent="\t") if rel.endswith(".json")
                     else yaml.safe_dump(doc))
    return str(root)


def _rel(root, paths):
    return {os.path.relpath(f, str(root)).replace(os.sep, "/") for f in paths}


_PLAY = {
    "hosts": "localhost",
    "vars_files": ["vars/vf.yml", "vfboth.yml", ["vars/nothere.yml", "vars/ff.yml", "vars/ff2.yml"]],
    "vars": {"pv": "{{ lookup('template', 'pvt.j2') }}"},
    "roles": ["r", {"role": "dep_user"}, "pr", "pdir", "nest/in"],
    "pre_tasks": [_task("pre")],
    "tasks": [
        _task("plain"),
        {"block": [_task("in-block")], "rescue": [_task("in-rescue")], "always": [_task("in-always")]},
        {"name": "imp", "ansible.builtin.import_tasks": "tasks/imported.yml"},
        {"name": "inc", "ansible.legacy.include_tasks": {"file": "tasks/included.yml", "apply": {"tags": "x"}}},
        {"include_vars": "vars/iv.yml"},
        {"ansible.builtin.include_vars": {"file": "vars/ivf.yml"}},
        {"ansible.builtin.include_vars": {"dir": "vars/ivdir"}},
        {"ansible.builtin.include_vars": "both.yml"},
        {"name": "kv", "ansible.builtin.include_role": "name=r tasks_from=other handlers_from=fb4"},
        {"name": "named", "ansible.builtin.import_role": {"name": "r", "vars_from": "fb4", "defaults_from": "dfb4"}},
        {"name": "tpl", "ansible.builtin.template": {"src": "t.j2", "dest": "/x"}},
        {"name": "pdtpl", "ansible.builtin.template": {"src": "{{ playbook_dir }}/files/pd.j2", "dest": "/z"}},
        {"name": "lk", "ansible.builtin.set_fact": {"x": "{{ lookup('ansible.builtin.template', 'lt.j2') }}"
                                                         "{{ query ('template', 'lq.j2') }}"
                                                         "{{ q(\"template\", \"lq2.j2\") }}"}},
        {"name": "lk2", "ansible.builtin.set_fact": {
            "y": "{{ lookup('ansible.legacy.template', 'l1.j2' , 'own/lo.j2', convert_data=False) }}"}},
        {"name": "kvinc", "ansible.builtin.include_tasks": "file=tasks/kv.yml"},
        {"include_vars": "file=vars/kvv.yml"},
        {"ansible.builtin.include_vars": {"dir": "vars/extdir", "extensions": ["yml", "conf"]}},
        {"name": "argstpl", "ansible.builtin.template": None, "args": {"src": "at.j2", "dest": "/a"}},
        {"name": "acttpl", "action": "ansible.builtin.template src=act.j2 dest=/b"},
        {"name": "latpl", "local_action": {"module": "template", "src": "la.j2", "dest": "/c"}},
        {"name": "wt", "ansible.builtin.debug": {"var": "item"}, "with_template": ["wt.j2"]},
        {"name": "wt1", "ansible.builtin.debug": {"var": "item"}, "with_template": "wt1.j2"},
        {"name": "wtb", "ansible.builtin.debug": {"var": "item"}, "with_ansible.legacy.template": ["wtb.j2"]},
        {"name": "cqa", "ansible.builtin.include_tasks": "cq/a.yml"},
        {"name": "caa", "ansible.builtin.include_tasks": "ca/a.yml"},
    ],
    "post_tasks": [_task("post")], "handlers": [_task("play-handler")]}
_FORMS = {
    "ansible/playbooks/deploy-stack.yml": [{"name": "first", "ansible.builtin.import_playbook": "first.yml"}, _PLAY,
                                           {"ansible.builtin.import_playbook": "sub/p.yml"}],
    "ansible/playbooks/sub/p.yml": [{"hosts": "localhost", "roles": ["subr"], "tasks": [
        {"name": "toprole", "ansible.builtin.include_role": {"name": "toprole"}},
        {"name": "subpd", "ansible.builtin.template": {"src": "{{ playbook_dir }}/subpd.j2", "dest": "/s"}},
        {"name": "sbb", "ansible.builtin.include_tasks": "{{ playbook_dir }}/sb/b.yml"}]},
        {"ansible.builtin.import_playbook": "sib.yml"}],                     # beside its importer
    "ansible/playbooks/sub/sib.yml": [{"hosts": "localhost", "tasks": [_task("sib-beside-its-importer")]}],
    "ansible/playbooks/sub/roles/subr/tasks/main.yml": [_task("subr-beside-the-imported-playbook"),
                                                        {"include_vars": "subpb.yml"}, {"include_vars": "topv.yml"}],
    "ansible/playbooks/sub/subpb.yml": {"a": 1},                   # in its own playbook's directory
    "ansible/playbooks/sub/subpd.j2": "the nested playbook's own playbook_dir\n",
    "ansible/playbooks/sub/sb/b.yml": [{"name": "sbc", "ansible.builtin.include_tasks": "r1/c.yml"}],
    "ansible/playbooks/sub/r1/c.yml": [{"name": "sbd", "ansible.builtin.include_tasks": "d2.yml"}],  # its playbook's
    "ansible/playbooks/sub/sb/r1/d2.yml": [_task("under-an-absolute-parent-in-a-nested-playbook")],
    "ansible/playbooks/topv.yml": {"a": 1},                        # in the top playbook's: an over-read
    "ansible/playbooks/roles/toprole/tasks/main.yml": [_task("toprole-in-the-top-playbooks-roles")],  # over-read
    "ansible/roles/nest/in/meta/main.yml": {"dependencies": ["nsib"]},
    "ansible/roles/nest/in/tasks/main.yml": [_task("nested-role")],
    "ansible/roles/nest/nsib/tasks/main.yml": [_task("beside-its-dependent")],   # beside the role named by a path
    "ansible/playbooks/first.yml": [{"hosts": "localhost", "tasks": [_task("imported-play")]}],
    "ansible/playbooks/tasks/imported.yml": [_task("from-import")],
    "ansible/playbooks/tasks/included.yml": [_task("from-include"), {"ansible.builtin.include_vars": "near.yml"},
                                             {"name": "nest", "ansible.builtin.include_tasks": "tasks/nested.yml"},
                                             {"name": "beside", "ansible.builtin.include_tasks": "beside.yml"},
                                             {"name": "pmid", "ansible.builtin.include_tasks": "pin/m.yml"},
                                             {"name": "ttpl", "ansible.builtin.template": {"src": "tt.j2",
                                                                                          "dest": "/t"}}],
    "ansible/playbooks/tasks/nested.yml": [_task("from-nested")],  # named from the playbook's directory
    "ansible/playbooks/tasks/beside.yml": [_task("from-beside")],  # beside its includer, and
    "ansible/playbooks/beside.yml": [_task("beside-in-the-playbook-dir")],          # in the playbook's directory
    "ansible/playbooks/tasks/kv.yml": [_task("from-a-k-v-include")],
    "ansible/playbooks/tasks/pin/m.yml": [{"name": "pleaf", "ansible.builtin.include_tasks": "pl.yml"}],
    "ansible/playbooks/pin/pl.yml": [_task("under-its-parents-directory")],
    "ansible/pin/pl.yml": [_task("under-its-parents-directory-in-the-working-directory")],
    "ansible/playbooks/cq/a.yml": [{"name": "cqb", "ansible.builtin.include_tasks": "cr/b.yml"}],
    "ansible/playbooks/cr/b.yml": [{"name": "cqc", "ansible.builtin.include_tasks": "c.yml"}],   # not under cq/
    "ansible/playbooks/cq/cr/c.yml": [_task("under-its-parents-joined-directories")],
    "ansible/playbooks/ca/a.yml": [{"name": "cab", "ansible.builtin.include_tasks": "{{ playbook_dir }}/cb/b.yml"}],
    "ansible/playbooks/cb/b.yml": [{"name": "cac", "ansible.builtin.include_tasks": "cc/c.yml"}],
    "ansible/playbooks/cb/cc/c.yml": [{"name": "cad", "ansible.builtin.include_tasks": "d.yml"}],
    "ansible/playbooks/ca/d.yml": [_task("under-a-parent-above-an-absolute-one")],
    "ansible/playbooks/tasks/templates/tt.j2": "{% include 'tinc.j2' %}\n",
    "ansible/playbooks/tasks/tinc.j2": "beside the task file\n",
    "ansible/playbooks/tasks/vars/near.yml": {"a": 1},
    "ansible/playbooks/tasks/near.yml": {"a": 1},                  # Ansible takes the vars/ copy first; both read
    "ansible/playbooks/pb.yml": {"a": 1},
    "ansible/playbooks/vars/pv.yml": {"a": 1},
    "ansible/playbooks/both.yml": {"a": 1},
    "ansible/playbooks/vars/both.yml": {"a": 1},
    "ansible/playbooks/vfboth.yml": {"a": 1},
    "ansible/playbooks/vars/vfboth.yml": {"a": 1},
    "ansible/playbooks/vars/ff.yml": {"a": 1},
    "ansible/playbooks/vars/ff2.yml": {"a": 1},                   # a first-found list: every entry that is there
    "ansible/playbooks/vars/kvv.yml": {"a": 1},
    "ansible/playbooks/vars/extdir/a.yml": {"a": 1},
    "ansible/playbooks/vars/extdir/b.conf": "a: 1\n",
    "ansible/playbooks/vars/extdir/c.json": {"a": 1},             # not among its `extensions:`
    "ansible/roles/r/vars/rv.yml": {"a": 1},
    "ansible/playbooks/vars/vf.yml": {"a": "{{ lookup('template', 'vft.j2') }}",
                                      "b": "{{ lookup('template', 'vfown.j2') }}"},
    "ansible/playbooks/vars/vfown.j2": "beside its vars file\n",    # found from the var's own directory only
    "ansible/playbooks/vars/iv.yml": {"a": 1},
    "ansible/playbooks/vars/ivf.yml": {"a": 1},
    "ansible/playbooks/vars/ivdir/one.yml": {"a": 1},
    "ansible/playbooks/vars/ivdir/three.yaml": {"a": 1},
    "ansible/playbooks/vars/ivdir/deep/two.json": {"a": 1},
    "ansible/playbooks/vars/ivdir/notes.txt": "not a vars file\n",
    "ansible/playbooks/group_vars/all.yml": {"a": 1},
    "ansible/playbooks/group_vars/web/deep.yml": {"a": 1},
    "ansible/playbooks/host_vars/localhost.json": {"a": 1},
    "ansible/playbooks/host_vars/bare": "a: 1\n",
    "instance.example/inventory/group_vars/all.yml": {"a": "{{ lookup('template', 'gvt.j2') }}"},
    "instance.example/inventory/group_vars/web/deep.yml": {"a": 1},
    "instance.example/inventory/host_vars/h.yml": {"a": 1},
    "instance.example/inventory/hosts.yml": {"all": {"vars": {"a": 1}, "hosts": {"h": {"b": 1}}}},
    "instance.example/inventory/more.json": {"all": {"vars": {"a": 1}}},
    "instance.example/inventory/site/more.yaml": {"all": {"vars": {"a": 1}}},      # a subdirectory's, at any depth
    "instance.example/inventory/site/group_vars/x.yml": {"a": 1},  # skipped below the top: Ansible reads none there
    "instance.example/inventory/README.md": "skipped\n",
    "instance.example/inventory/notes.txt": "skipped\n",
    "instance.example/inventory/.hidden": "skipped\n",
    "ansible/playbooks/templates/t.j2": "{% import 'mac.j2' as m with context %}"
                                        "{% from 'fr.j2' import x, y as z without context %}"
                                        "{%- include 'inc.j2' ignore missing with context -%}\n",
    "ansible/playbooks/templates/mac.j2": "{% macro m() %}{% endmacro %}\n",
    "ansible/playbooks/templates/fr.j2": "{% extends 'base.j2' %}\n",
    "ansible/playbooks/templates/base.j2": "base\n{# {% include 'gone.j2' %}\n{{ lookup('template', 'gone.j2') }} #}"
                                           "{% raw %}{% include 'rawinc.j2' %}\n{{ lookup('template', 'rawlk.j2') }}"
                                           "{% endraw %}\n",     # a comment never runs; a raw block's text may again
    "ansible/playbooks/templates/rawinc.j2": "a raw block's include\n",
    "ansible/playbooks/templates/rawlk.j2": "a raw block's lookup\n",
    "ansible/playbooks/t.j2": "beside\n",                           # templates/ first; both read
    "ansible/playbooks/templates/inc.j2": "{% if false %}{% include 't.j2' %}{% endif %}included\n",   # a cycle
    "ansible/playbooks/inc.j2": "beside\n",                         # an include under templates/ first
    "ansible/playbooks/files/pd.j2": "by playbook_dir\n{% include 'pdi.j2' %}{% include 'fsub/n.j2' %}\n",
    "ansible/playbooks/files/pdi.j2": "in its own directory\n",      # the including template's directory last
    "ansible/playbooks/templates/pdi.j2": "found\n",
    "ansible/playbooks/files/fsub/n.j2": "{% include 'top-only.j2' %}\n",
    "ansible/playbooks/files/top-only.j2": "the top template's directory\n",
    "ansible/playbooks/templates/lt.j2": "looked up\n{{ lookup('template', 'lt2.j2') }}\n"   # a lookup in one
                                         "{{ lookup('template', 'deep/ld.j2') }}\n",
    "ansible/playbooks/templates/deep/ld.j2": "{% include 'ldi.j2' %}\n",
    "ansible/playbooks/templates/deep/ldi.j2": "the inner lookup's own directory\n",
    "ansible/playbooks/templates/lt2.j2": "looked up by a template\n",
    "ansible/playbooks/templates/lq.j2": "queried\n",
    "ansible/playbooks/templates/lq2.j2": "queried by q\n",
    "ansible/playbooks/templates/l1.j2": "a legacy lookup\n",
    "ansible/playbooks/templates/own/lo.j2": "{% include 'loi.j2' %}\n",
    "ansible/playbooks/templates/own/loi.j2": "the looked-up template's own directory\n",
    "ansible/playbooks/templates/at.j2": "under args\n",
    "ansible/playbooks/templates/act.j2": "under action\n",
    "ansible/playbooks/templates/la.j2": "under local_action\n",
    "ansible/playbooks/templates/wt.j2": "with_template\n",
    "ansible/playbooks/templates/wt1.j2": "with_template, one name\n",
    "ansible/playbooks/templates/wtb.j2": "with_ansible.legacy.template\n",
    "ansible/playbooks/templates/pvt.j2": "a play var's\n",
    "ansible/playbooks/templates/vft.j2": "a vars file's\n",
    "ansible/roles/r/templates/gvt.j2": "the inventory's, found in a role\n",
    "ansible/playbooks/templates/rt.j2": "the playbook's\n",       # the role's own templates/ first; both read
    "ansible/roles/r/templates/rt.j2": "{% include 'rinc.j2' %}{% include 'pbinc.j2' %}\n",
    "ansible/playbooks/templates/rinc.j2": "the playbook's\n",     # a role template's include in the role first
    "ansible/roles/r/templates/rinc.j2": "role template\n",
    "ansible/playbooks/templates/pbinc.j2": "a role template's include in the playbook's\n",
    "ansible/roles/r/templates/parent.j2": "a dependent's template\n",
    "ansible/roles/r/meta/main.yaml": {"dependencies": ["dep", {"role": "dep2",
                                                                "p": "{{ lookup('template', 'dpt.j2') }}"}]},
    "ansible/roles/r/templates/dpt.j2": "a dependency's param\n",
    "ansible/roles/r/tasks/main.yaml": [_task("role-main"), {"include_vars": "rv.yml"}, {"include_vars": "pv.yml"},
                                         {"include_vars": "pb.yml"}, {"include_vars": "rboth.yml"},
                                         {"ansible.builtin.include_vars": {"dir": "rdir"}},
                                         {"ansible.builtin.include_vars": {"dir": "vars/rdir2"}},
                                         {"name": "rtpl", "ansible.builtin.template": "src=rt.j2 dest=/y"},
                                         {"name": "rsub", "ansible.builtin.include_tasks": "sub/n1.yml"},
                                         {"name": "rinc", "ansible.builtin.include_role": "name=inc_r"},
                                         {"name": "rcwd", "ansible.builtin.include_tasks": "cwdonly.yml"},
                                         {"name": "rcsub", "ansible.builtin.include_tasks": "csub/cl.yml"}],
    "ansible/tasks/cwdonly.yml": [_task("role-include-in-the-working-directorys-tasks")],
    "ansible/tasks/csub/cl.yml": [{"name": "rcl", "ansible.builtin.set_fact": {
                                      "w": "{{ lookup('template', 'cl.j2') }}"}},
                                  {"name": "rcn", "ansible.builtin.include_tasks": "n3.yml"}],
    "ansible/templates/cl.j2": "a role's lookup from a task file below the working directory's tasks/\n",
    "ansible/roles/r/tasks/csub/n3.yml": [_task("role-nested-under-its-tasks-and-its-parents-directory")],
    "ansible/roles/r/tasks/sub/n1.yml": [{"name": "rn2", "ansible.builtin.include_tasks": "n2.yml"},
                                         {"name": "rsl", "ansible.builtin.set_fact": {
                                             "z": "{{ lookup('template', 'rsl.j2') }}"}}],
    "ansible/roles/r/tasks/rsl.j2": "the role's tasks/ for a lookup below it\n",
    "ansible/roles/r/tasks/n2.yml": [_task("role-nested-from-its-tasks")],          # the role's tasks/ first
    "ansible/roles/inc_r/tasks/main.yml": [{"include_vars": "fromr.yml"}, {"name": "again", "when": "false",
                                                                          "ansible.builtin.include_role": {
                                                                              "name": "inc_r"}},
                                           {"name": "iix", "ansible.builtin.include_tasks": "ix/a.yml"}],
    "ansible/roles/inc_r/tasks/ix/a.yml": [{"name": "iib", "ansible.builtin.include_tasks": "ib.yml"}],
    "ansible/roles/inc_r/ix/ib.yml": [_task("under-an-include-roles-root")],
    "ansible/roles/r/vars/fromr.yml": {"a": 1},                     # an include_role from a role: the includer's
    "ansible/roles/r/tasks/rboth.yml": {"a": 1},                    # the role's vars/ before the task's directory
    "ansible/roles/r/vars/rboth.yml": {"a": 1},
    "ansible/roles/r/tasks/rdir/x.yml": {"a": 1},                   # a role's `dir:` under its vars/ first
    "ansible/roles/r/vars/rdir/x.yml": {"a": 1},
    "ansible/roles/r/vars/rdir2/y.yml": {"a": 1},                   # `vars/rdir2` is the role's vars/rdir2
    "ansible/roles/r/tasks/other": [_task("role-other-bare")],
    "ansible/roles/r/tasks/other.yml": [_task("role-other-yml")],
    "ansible/roles/r/handlers/main.yml": [_task("role-handler"), {"name": "rh", "ansible.builtin.include_tasks":
                                                                    "rhx.yml"}],
    "ansible/playbooks/handlers/rhx.yml": [_task("role-handler-include-in-the-playbooks-handlers")],
    "ansible/handlers/rhx.yml": [_task("role-handler-include-in-the-working-directorys-handlers")],
    "ansible/roles/r/handlers/fb4.yaml": [_task("role-handler-fb4")],
    "ansible/roles/r/defaults/main.yml": {"a": 1},
    "ansible/roles/r/defaults/dfb4.json": {"a": 1},
    "ansible/roles/r/vars/main/one.yml": {"a": 1},
    "ansible/roles/r/vars/main/two.yaml": {"a": 1},
    "ansible/roles/r/vars/fb4.yaml": {"a": 1},
    "ansible/roles/dep/tasks/main.yml": [_task("dep-main"), {"name": "deptpl", "ansible.builtin.template": {
        "src": "parent.j2", "dest": "/p"}}],                         # a meta dependency: its dependent's dirs
    "ansible/roles/dep/defaults/main/a.yml": {"a": 1},
    "ansible/roles/dep/meta/main/x.yml": {"dependencies": []},      # Ansible reads no meta/main/ directory
    "ansible/roles/dep2/tasks/main.yml": [_task("dep2-main")],
    "ansible/roles/dep_user/tasks/main.yml": [_task("dep-user-yml")],
    "ansible/roles/dep_user/tasks/main": [_task("dep-user-bare")],
    "ansible/playbooks/roles/pr/tasks/main.yml": [_task("pr-beside-the-playbook")],
    "ansible/roles/pr/tasks/main.yml": [_task("pr-roles-path")],   # <playbook dir>/roles/ first; all three read
    "ansible/playbooks/pr/tasks/main.yml": [_task("pr-in-the-playbook-dir")],
    "ansible/playbooks/pdir/tasks/main.yml": [_task("pdir-in-the-playbook-dir")],   # the playbook's own, last
}


def test_the_deploy_walk_reaches_each_form_ansible_loads(tmp_path):
    """Over a tree holding one of each form, the walk reaches every task and handler a run would (an imported
    playbook's, pre_tasks, post_tasks and play handlers, a block's rescue and always, an imported and an included task
    file under `ansible.legacy.` and in the k=v `file=` form, a nested include named from the playbook's directory, a
    role's meta dependencies, by name and as a mapping, whose params it records, a role listed by `role:`, `tasks_from`
    and `handlers_from` in the k=v form, a `.yaml` handlers file), records which include brought each in, and records as
    vars files the inventory file's own (`all: vars:`, a host's) and its group_vars and host_vars as shipped
    (instance.example/inventory/), the play's vars_files (a first-found list's entry that is there too), include_vars as
    a string, a `file:`, a k=v `file=` and a `dir:` (its `.yml`, `.yaml` and `.json` files at any depth, not a `.txt`,
    and under `extensions:` the ones it names), group_vars (a group's directory too) and host_vars in JSON or with no
    extension, a role's defaults (a `defaults/main/` directory too), its `defaults_from` JSON file, every file of its
    `vars/main/` directory and its `vars_from` `.yaml` file, and an include_vars file in the including file's `vars/`,
    the role's `vars/` or an including role's, ansible/playbooks/ or its `vars/`; and as each task's templates a
    `template` task's `src` (the k=v form, under `args:`, `action:` and `local_action:`, and one under `{{ playbook_dir
    }}`), each term of a template lookup (`lookup`, `query` and `q`, under `ansible.builtin.` and `ansible.legacy.`, a
    keyword after the terms), a `with_template`, and each file a template includes, imports, imports from, extends or
    looks up (a cycle read once; an include in a comment not followed, one in a raw block followed, as a lookup's text
    may be rendered again (2.18 renders a fact's value again where it is read): for a template task, an over-read); and
    a template a lookup in a play var, a vars file or the inventory's group_vars renders, read by `expand`.

    Where two places hold a name it reads the file in each place the walk lists from Ansible's search orders: an
    include_vars or vars_files name's `vars/<name>` and `<name>`, a role's `vars/` and its task's directory, a role's
    `dir:` under its `vars/` and beside its task; a role in ansible/playbooks/roles/, under ansible/roles/ and in the
    playbook's own directory, and one beside an imported playbook in a directory of its own; a task file beside its
    includer and in the playbook's directory, and a role's nested include in its `tasks/`; a template under `templates/`
    and beside it, a role's own and the playbook's, a meta dependency's in its dependent role; a template's include in
    the including template's directory, the top template's, the task file's and the looked-up template's own. Where two
    spellings hold a role's file it takes the one Ansible's role loader takes: `main.yml` before a bare `main`, a bare
    `other` before `other.yml` for a `tasks_from`; and no `meta/main/` directory. It reads a `.json` file as JSON,
    tab-indented as JSON may be, which YAML refuses.

    Why: the two pins read only what the walk reaches; each form it skipped let a leak or a third platform-core domain
    pass them (fold 4's reads 90, N6, N7 and 100, N3, N4), and they had no self-test; the walk's own order took a decoy
    where Ansible loads the other file, read no template and no inventory, and five of its branches had no row (fold 5's
    reads 110, S2, N3 and 111, S2); and the order fold 6 modelled was not Ansible's for a lookup's include, a meta
    dependency, an include_role from a role, a role's nested include or an imported playbook's roles, and the walk read
    no template a lookup in a var, in a template or in a second term rendered, no `with_template`, no template task
    under `args:` or `action:`, no `ansible.legacy.` lookup, not the inventory file's own vars and no `extensions:`
    (fold 6's reads 120, S1, S2, N3 and 121, S2, N1, N4, N5); and fold 7's own mutants dropped each form of the
    paragraph above with every row green.

    Fold 7's rows: a playbook a nested playbook imports, found in the importer's directory (Ansible's PlaybookInclude
    joins the name to it alone); a nested playbook's role that reads a vars file in its playbook's directory and one in
    the top playbook's, and its include_role of a role in the top playbook's `roles/` (two places Ansible does not
    search for a nested playbook, which the walk's union reads too much of: fold 7's read 130, N2); a role named by a
    path, whose meta dependency is found beside it; a second inventory file, in JSON; every entry of a first-found list
    that is there; a `with_template` of one name; a template lookup in a meta dependency's param, and one in a vars file
    whose template is beside the file; a template a template looks up from a directory below, whose include is found in
    that directory; an include with `ignore missing` and a context clause, an import with context and a from-import of
    two names; a comment over two lines holding an include and a lookup, not followed; and `expand` of a task's string,
    which reads the templates its lookup rendered.

    Fold 8's rows: an include under its parent's written directory, in the playbook's directory and in the working
    directory (`ansible/`); a role's include in the working directory's `tasks/`; a role handler's include in the
    playbook's and the working directory's `handlers/`; a role's lookup below its `tasks/`, found in its `tasks/`; a
    nested playbook's `{{ playbook_dir }}`, its own directory; a `with_ansible.legacy.template`, and a template lookup
    with a space before its parenthesis; an import_playbook entry's `name`; an inventory file in a subdirectory, and
    those Ansible skips (a README, a `.txt`, a hidden file, a subdirectory's group_vars), not read; and a role that
    includes itself under `when: false`, read once. Why: fold 7's reads found Ansible loading each from a place the walk
    did not search, beside a decoy it read (130, S2; 131, S6), a prefixed `with_template` or a spaced lookup unread
    (131, S2) and an inventory subdirectory's file unread (131, S5); and a walk that followed a role into itself again
    passed every row (130, N3: K1).

    Fold 8's mutant run added, each a form ansible-core 2.19.4 and 2.18.19 load from that place alone: an include under
    its parents' written directories joined (`cq/cr/`, its includer found in the playbook's directory); one under a
    parent above an absolute one (`{{ playbook_dir }}/…`), from which the dynamic loader starts again; one under an
    absolute parent in a nested playbook, whose `{{ playbook_dir }}` is its own directory; a role's include the role
    lacks, found in the working directory's `tasks/csub/`, whose lookup is found in the working directory's `templates/`
    and whose nested include under the role's `tasks/csub/`; and an include below an include_role's tasks, found under
    the role's root. Why: fold 8's own mutants dropped each of those places (the parents but the nearest, their join,
    the loader that starts again, the written directory's `{{ playbook_dir }}` or its nested playbook's, the parent of a
    `tasks/`, the role's `tasks/` under the parents, an include_role's root) with every row green."""
    w = Walk(_tree(tmp_path, _FORMS))
    marks = {t["name"] for t in w.tasks if t.get("ansible.builtin.debug") == {"msg": t.get("name")}}
    assert marks == {"imported-play", "pre", "plain", "in-block", "in-rescue", "in-always", "from-import",
                     "from-include", "from-nested", "from-beside", "beside-in-the-playbook-dir",
                     "from-a-k-v-include", "role-main", "role-nested-from-its-tasks", "role-handler", "dep-main",
                     "dep2-main", "dep-user-yml", "role-other-bare", "role-handler-fb4", "pr-beside-the-playbook",
                     "pr-roles-path", "pr-in-the-playbook-dir", "pdir-in-the-playbook-dir",
                     "subr-beside-the-imported-playbook", "post", "play-handler", "sib-beside-its-importer",
                     "toprole-in-the-top-playbooks-roles", "nested-role", "beside-its-dependent",
                     "under-its-parents-directory", "under-its-parents-directory-in-the-working-directory",
                     "role-include-in-the-working-directorys-tasks", "role-handler-include-in-the-playbooks-handlers",
                     "role-handler-include-in-the-working-directorys-handlers",
                     "under-its-parents-joined-directories", "under-a-parent-above-an-absolute-one",
                     "under-an-absolute-parent-in-a-nested-playbook",
                     "role-nested-under-its-tasks-and-its-parents-directory", "under-an-include-roles-root"}, marks
    by_name = {t.get("name"): t for t in w.tasks}
    for mark, include in (("from-import", "imp"), ("from-include", "inc"), ("from-nested", "nest"),
                          ("role-other-bare", "kv"), ("from-a-k-v-include", "kvinc"), ("from-beside", "beside"),
                          ("beside-in-the-playbook-dir", "beside"), ("role-nested-from-its-tasks", "rn2"),
                          ("under-its-parents-directory", "pleaf"), ("role-include-in-the-working-directorys-tasks",
                                                                     "rcwd"),
                          ("role-handler-include-in-the-playbooks-handlers", "rh"),
                          ("under-its-parents-joined-directories", "cqc"),
                          ("under-a-parent-above-an-absolute-one", "cad"),
                          ("under-an-absolute-parent-in-a-nested-playbook", "sbd"),
                          ("role-nested-under-its-tasks-and-its-parents-directory", "rcn"),
                          ("under-an-include-roles-root", "iib")):
        assert w.via[id(by_name[mark])] == id(by_name[include]), mark
    assert len(w.plays) == 4 and w.plays[0]["tasks"] == [_task("imported-play")]
    assert [(_rel(tmp_path, [m]), d) for m, d in w.deps] == [({"ansible/roles/r/meta/main.yaml"}, {
        "role": "dep2", "p": "{{ lookup('template', 'dpt.j2') }}"})], w.deps
    assert w.expand(w.deps[0][1]).endswith("a dependency's param\n")
    assert _rel(tmp_path, w.var_files) == {"instance.example/inventory/" + f for f in (
        "group_vars/all.yml", "group_vars/web/deep.yml", "host_vars/h.yml", "hosts.yml", "more.json",
        "site/more.yaml")} | {
            "ansible/playbooks/" + f for f in (
                "vars/vf.yml", "vfboth.yml", "vars/vfboth.yml", "vars/ff.yml", "vars/iv.yml", "vars/ivf.yml",
                "vars/ivdir/one.yml", "vars/ivdir/three.yaml", "vars/ivdir/deep/two.json", "both.yml",
                "vars/both.yml", "vars/kvv.yml", "vars/extdir/a.yml", "vars/extdir/b.conf", "group_vars/all.yml",
                "group_vars/web/deep.yml", "host_vars/localhost.json", "host_vars/bare", "tasks/vars/near.yml",
                "tasks/near.yml", "pb.yml", "vars/pv.yml", "vars/ff2.yml", "sub/subpb.yml", "topv.yml")} | {
                    "ansible/roles/r/" + f for f in (
                    "defaults/main.yml", "defaults/dfb4.json", "vars/main/one.yml", "vars/main/two.yaml",
                    "vars/fb4.yaml", "vars/rv.yml", "vars/rboth.yml", "tasks/rboth.yml", "vars/rdir/x.yml",
                    "tasks/rdir/x.yml", "vars/rdir2/y.yml", "vars/fromr.yml")} | {
                        "ansible/roles/dep/defaults/main/a.yml"}
    templates = {t.get("name"): _rel(tmp_path, w.templates[id(t)]) for t in w.tasks if id(t) in w.templates}
    pb = "ansible/playbooks/"
    assert templates == {
        "tpl": {pb + f for f in ("templates/t.j2", "t.j2", "templates/mac.j2", "templates/fr.j2", "templates/base.j2",
                                 "templates/inc.j2", "inc.j2", "templates/rawinc.j2", "templates/rawlk.j2")},
        "pdtpl": {pb + f for f in ("files/pd.j2", "templates/pdi.j2", "files/pdi.j2", "files/fsub/n.j2",
                                   "files/top-only.j2")},
        "lk": {pb + "templates/" + f for f in ("lt.j2", "lt2.j2", "deep/ld.j2", "deep/ldi.j2", "lq.j2", "lq2.j2")},
        "lk2": {pb + "templates/" + f for f in ("l1.j2", "own/lo.j2", "own/loi.j2")},
        "argstpl": {pb + "templates/at.j2"}, "acttpl": {pb + "templates/act.j2"}, "latpl": {pb + "templates/la.j2"},
        "wt": {pb + "templates/wt.j2"}, "wt1": {pb + "templates/wt1.j2"}, "wtb": {pb + "templates/wtb.j2"},
        "subpd": {pb + "sub/subpd.j2"}, "rsl": {"ansible/roles/r/tasks/rsl.j2"}, "rcl": {"ansible/templates/cl.j2"},
        "ttpl": {pb + "tasks/templates/tt.j2", pb + "tasks/tinc.j2"},
        "rtpl": {"ansible/roles/r/templates/rt.j2", pb + "templates/rt.j2", "ansible/roles/r/templates/rinc.j2",
                 pb + "templates/rinc.j2", pb + "templates/pbinc.j2"},
        "deptpl": {"ansible/roles/r/templates/parent.j2"}}, templates
    assert w.text(by_name["tpl"]).endswith("beside\n") and "role template\n" in w.text(by_name["rtpl"])
    assert w.expand(w.plays[1]["vars"]).endswith("a play var's\n")
    lk2 = by_name["lk2"]["ansible.builtin.set_fact"]["y"]
    assert w.expand(lk2).endswith("the looked-up template's own directory\n")
    for f, said in (("ansible/playbooks/vars/vf.yml", "a vars file's\nbeside its vars file\n"),
                    ("instance.example/inventory/group_vars/all.yml", "the inventory's, found in a role\n")):
        assert w.expand(load(os.path.join(str(tmp_path), *f.split("/")))).endswith(said), f
    assert _rel(tmp_path, w.template_files) == {f for f in _FORMS if f.endswith(".j2")}
    unread = {"ansible/playbooks/vars/ivdir/notes.txt", "ansible/playbooks/vars/extdir/c.json",
              "ansible/roles/r/tasks/other.yml", "ansible/roles/dep_user/tasks/main",
              "ansible/roles/dep/meta/main/x.yml", "instance.example/inventory/site/group_vars/x.yml",
              "instance.example/inventory/README.md", "instance.example/inventory/notes.txt",
              "instance.example/inventory/.hidden"}
    assert _rel(tmp_path, w.opened) == {f for f in _FORMS if not f.endswith(".j2")} - unread, w.opened


def test_kv_and_args_read_a_modules_arguments_as_ansible_does():
    """`kv` splits a k=v string where Ansible's parse_kv does: not inside quotes, `{{ }}` or `{% %}`, one pair of outer
    quotes off a value, the free words as `_raw_params`; `args` finds a module's arguments under its key (bare,
    `ansible.builtin.`, `ansible.legacy.`), in `action:` and `local_action:` (a string's first word, a mapping's
    `module:`), with `args:` under them, and None for a task that runs no such module. Why: the URL pin split a k=v
    debug on whitespace, so `msg="{{ vars }}"` read as a `msg` of `"{{` and passed while Ansible printed every var
    (fold 6's read 120, S3), and the walk read no template task under `args:` or `action:` (121, S2: Q14); and a value
    held together by an escaped quote inside its quotes or by a `{# #}` comment was split with every row green, the
    pin then reading no `{{ vars }}` in the `msg` (fold 7's read 130, N3: K5, K6); and a comment read as never
    closing passed every row (fold 8's mutant run)."""
    assert kv('msg="{{ vars }}" var=x free words') == {"msg": "{{ vars }}", "var": "x", "_raw_params": "free words"}
    assert kv(r'msg="say \" {{ vars }}"') == {"msg": r'say \" {{ vars }}'}    # the backslash kept: the pin reads Jinja
    assert kv("msg={# a note #}{{ vars }}") == {"msg": "{# a note #}{{ vars }}"}
    assert kv("msg={# a note #} other=1") == {"msg": "{# a note #}", "other": "1"}    # the comment closed
    assert kv("msg={{ x | default('a b') }} y='c d'") == {"msg": "{{ x | default('a b') }}", "y": "c d"}
    assert kv("x.yml") == {"_raw_params": "x.yml"}
    assert args({"action": "debug var=x", "args": {"y": 1}}, "debug") == {"y": 1, "var": "x"}
    assert args({"local_action": {"module": "ansible.legacy.fail", "msg": "m"}}, "fail") == {"msg": "m"}
    assert args({"ansible.builtin.template": None, "args": {"src": "a"}}, "template") == {"src": "a"}
    assert args({"ansible.legacy.set_fact": "_k=v"}, "set_fact") == {"_k": "v"}
    assert args({"action": "ansible.builtin.debugger x"}, "debug") is None and args({"x": 1}, "debug") is None


_PB = "ansible/playbooks/"
_PARSED = {
    _PB + "deploy-stack.yml": [
        {"name": "imp", "ansible.builtin.import_playbook": "o.yml", "tags": ["t"], "when": "x is defined"},
        {"hosts": "localhost", "roles": ["g5"], "tasks": [
            {"name": "qc", "ansible.builtin.template": {"src": "qc.j2", "dest": "/q"}},
            {"name": "hdr", "ansible.builtin.template": {"src": "hdr.j2", "dest": "/h"}},
            {"name": "brace", "ansible.builtin.template": {"src": "br.j2", "dest": "/b"}},
            {"name": "cond", "ansible.builtin.debug": {"msg": "c"},
             "when": "lookup('template', 'cond.j2') | length > 0"},
            {"name": "lit", "ansible.builtin.set_fact": {"f": "{{ '{{ lookup(\"template\", \"lit.j2\") }}' }}"}},
            {"name": "raw", "ansible.builtin.set_fact": {
                "g": "{% raw %}{{ lookup('template', 'raw.j2') }}{% endraw %}"}},
            {"name": "unparsed", "ansible.builtin.debug": {"msg": "{{ 'a {{ b' }}"}},
            {"name": "tvar", "ansible.builtin.debug": {"var": "lookup('template', 'tvar.j2')"}},
            {"name": "tilde", "ansible.builtin.set_fact": {"t": "{{ '{{ lookup(' ~ \"'template', 'tilde.j2') }}\" }}"}},
            {"name": "plus", "ansible.builtin.set_fact": {"p": "{{ '{{ lookup(' + \"'template', 'plus.j2') }}\" }}"}},
            {"name": "mul", "ansible.builtin.set_fact": {"m": "{{ '{' * 2 ~ \" lookup('template', 'mul.j2') }}\" }}"}},
            {"name": "join", "ansible.builtin.set_fact": {
                "j": "{{ ['{{ look', \"up('template', 'join.j2') }}\"] | join }}"}},
            {"name": "list", "ansible.builtin.set_fact": {"l": "{{ ['{{ lookup(\"template\", \"list.j2\") }}', 1] }}"}},
            {"name": "map", "ansible.builtin.set_fact": {"d": "{{ {'k': '{{ lookup(\"template\", \"map.j2\") }}'} }}"}},
            _task("q"), {"name": "argv", "ansible.builtin.command": {"argv": ["pip", "-q", "x"]}}]}],
    _PB + "o.yml": [{"hosts": "localhost", "tasks": [_task("imported")]}],
    _PB + "templates/qc.j2": "{{ '{#' }}{% include 'qinc.j2' %}{{ '#}' }}\n"
                             "{{ '{#' }}{{ lookup('template', 'qlk.j2') }}\n",
    _PB + "templates/qinc.j2": "an include behind a quoted comment open\n",
    _PB + "templates/qlk.j2": "a lookup behind one\n",
    _PB + "templates/hdr.j2": "#jinja2: trim_blocks: False, lstrip_blocks: True\n{% include 'hinc.j2' %}\n",
    _PB + "templates/hinc.j2": "under a header that sets whitespace\n",
    _PB + "templates/br.j2": "{% include 'a{b}.j2' %}\n",
    _PB + "templates/a{b}.j2": "a name with a brace\n",
    _PB + "templates/cond.j2": "a condition's\n",
    _PB + "templates/lit.j2": "a literal's\n",
    _PB + "templates/raw.j2": "a raw block's\n",
    _PB + "templates/tvar.j2": "a debug var's\n",
    _PB + "templates/tilde.j2": "two literals joined by a tilde\n",
    _PB + "templates/plus.j2": "two literals added\n",
    _PB + "templates/mul.j2": "a brace doubled\n",
    _PB + "templates/join.j2": "a list of literals joined\n",
    _PB + "templates/list.j2": "a list's literal\n",
    _PB + "templates/map.j2": "a mapping's literal\n",
    "ansible/roles/g5/tasks/main.yml": [{"name": "g5a", "ansible.builtin.include_tasks": "x/tasks/a.yml"}],
    "ansible/roles/g5/tasks/x/tasks/a.yml": [{"name": "g5b", "ansible.builtin.include_tasks": "b.yml"}],
    "ansible/roles/g5/tasks/x/b.yml": [_task("in-the-roles-tasks-x")],
    _PB + "x/tasks/b.yml": [_task("the-decoy-in-the-playbooks-x-tasks")],
}


def test_the_deploy_walk_reads_what_jinja_parses_where_a_pattern_misread_it(tmp_path):
    """Over a tree of its own, the walk follows a template's include and lookup that sit behind a quoted comment open
    (`{{ '{#' }}`), an include under a `#jinja2:` header that sets whitespace alone, and one whose name holds a brace; a
    lookup in a `when:` condition, which Ansible runs as an expression without delimiters; a set_fact's literal and raw
    block that hold a lookup, which 2.18 renders again where the fact is read; an import_playbook entry with `tags:` and
    `when:`, kept in `imports` and its playbook read; and a role's include under `x/tasks/` whose own include Ansible
    loads from `<role>/tasks/x/` (DataLoader's RE_TASKS takes a `tasks` directory for its parent), the decoy under the
    playbook's `x/tasks/` read too; and it reads past a debug's literal that holds an unclosed `{{` (`{{ 'a {{ b' }}`),
    text that never runs, where a mutant that refused it passed every row (fold 9's own mutants); and, fold 10, a debug
    `var:` that is a template lookup alone, the expression's own top node, which Jinja's `find_all` leaves out; a
    set_fact's literals joined by `~`, `+`, a doubled brace (`'{' * 2`) and a `join` into a template lookup, and one in
    a list's or a mapping's literal, which Jinja computes, each read whole and 2.18 renders again where the fact is
    read; and it passes a debug's bare `q` and an argv's `-q`, plain text Jinja reads as a lookup's name that calls
    nothing. Why: a pattern took the quoted `'{#'` for a comment and the include behind it for unrun text, so the URL
    rode a template past both pins on both cores (fold 8's read 140, S1: G1b, G1d), read no lookup in a condition, and
    refused forms Ansible runs as written (140, N1: G4, G4b, G12); the walk's places lacked the parent of a role's
    nested `tasks/`, so it read the decoy where Ansible loads the other file (140, S2: G5); and the walk read no lookup
    at an expression's top, and none that two literals spelled, so a debug `var:` and a joined set_fact carried the URL
    past both pins (fold 10's own find; fold 9's read 151, S1); and the refusal of a literal that does not parse but
    names a lookup (read 150, S-B) refused the tilde's and the plus's halves, where the whole parses (fold 10's own
    find)."""
    w = Walk(_tree(tmp_path, _PARSED))
    marks = {t["name"] for t in w.tasks if t.get("ansible.builtin.debug") == {"msg": t.get("name")}}
    assert marks == {"imported", "in-the-roles-tasks-x", "the-decoy-in-the-playbooks-x-tasks", "q"}, marks
    by_name = {t.get("name"): t for t in w.tasks}
    assert w.via[id(by_name["in-the-roles-tasks-x"])] == id(by_name["g5b"])
    assert w.imports == [_PARSED[_PB + "deploy-stack.yml"][0]] and len(w.plays) == 2
    templates = {t.get("name"): _rel(tmp_path, w.templates[id(t)]) for t in w.tasks if id(t) in w.templates}
    assert templates == {
        "qc": {_PB + "templates/" + f for f in ("qc.j2", "qinc.j2", "qlk.j2")},
        "hdr": {_PB + "templates/hdr.j2", _PB + "templates/hinc.j2"},
        "brace": {_PB + "templates/br.j2", _PB + "templates/a{b}.j2"},
        "cond": {_PB + "templates/cond.j2"}, "lit": {_PB + "templates/lit.j2"},
        "raw": {_PB + "templates/raw.j2"}, "tvar": {_PB + "templates/tvar.j2"},
        "tilde": {_PB + "templates/tilde.j2"}, "plus": {_PB + "templates/plus.j2"}, "mul": {_PB + "templates/mul.j2"},
        "join": {_PB + "templates/join.j2"}, "list": {_PB + "templates/list.j2"},
        "map": {_PB + "templates/map.j2"}}, templates


_T = "ansible/playbooks/templates/t.j2"
_TPL = {"tasks": [{"ansible.builtin.template": {"src": "t.j2", "dest": "/x"}}]}
_HERE = {_T: "x\n", "ansible/playbooks/templates/a.j2": "a\n", "ansible/playbooks/templates/b.j2": "b\n"}


@pytest.mark.parametrize("play,files,error", [
    ({"tasks": [{"ansible.builtin.include_role": {"name": "r", "tasks_from": "nothere"}}]}, {}, FileNotFoundError),
    ({"tasks": [{"ansible.builtin.include_role": {"name": "r", "vars_from": "nothere"}}]}, {}, FileNotFoundError),
    ({"tasks": [{"ansible.builtin.include_role": {"name": "nothere"}}]}, {}, FileNotFoundError),
    ({"tasks": [{"ansible.builtin.include_tasks": "vonly.yml"}]},
     {"ansible/playbooks/vars/vonly.yml": [_task("v")], "ansible/vars/vonly.yml": [_task("v")]}, FileNotFoundError),
    ({"vars_files": ["nothere.yml"]}, {}, FileNotFoundError),
    ({"vars_files": [["nothere.yml", "vars/nothere.yml"]]}, {}, FileNotFoundError),
    ({"tasks": [{"ansible.builtin.template": {"src": "nothere.j2", "dest": "/x"}}]}, {}, FileNotFoundError),
    (_TPL, {_T: "{% include 'nothere.j2' %}\n"}, FileNotFoundError),
    ({"vars": {"v": "{{ lookup('template', 'nothere.j2') }}"}}, {}, FileNotFoundError),
    ({"tasks": [{"ansible.builtin.include_tasks": "{{ x }}.yml"}]}, {}, Unfollowed),
    ({"tasks": [{"ansible.builtin.include_tasks": {"apply": {"tags": "x"}}}]}, {}, Unfollowed),
    ({"tasks": [{"ansible.builtin.include_vars": {"dir": "{{ x }}"}}]}, {}, Unfollowed),
    ({"tasks": [{"ansible.builtin.include_vars": "{{ x }}.yml"}]}, {}, Unfollowed),
    ({"tasks": [{"ansible.builtin.include_role": {"name": "{{ x }}"}}]}, {}, Unfollowed),
    ({"tasks": [{"ansible.builtin.template": {"src": "{{ x }}.j2", "dest": "/x"}}]}, {}, Unfollowed),
    (_TPL, {_T: "{% include x %}\n"}, Unfollowed),
    (_TPL, dict(_HERE, **{_T: "{% include 'a.j2' if x else 'b.j2' %}\n"}), Unfollowed),
    (_TPL, dict(_HERE, **{_T: "{% include ['a.j2', 'b.j2'] %}\n"}), Unfollowed),
    (_TPL, dict(_HERE, **{_T: "{% include 'a.j2' ~ x %}\n"}), Unfollowed),
    (_TPL, dict(_HERE, **{_T: "#jinja2: variable_start_string:'[%'\n[% x %]\n"}), Unfollowed),
    ({"tasks": [{"ansible.builtin.template": {"src": "t.j2", "dest": "/x", "variable_start_string": "[%"}}]}, _HERE,
     Unfollowed),
    ({"tasks": [{"ansible.builtin.debug": {"msg": "{{ lookup('template', x) }}"}}]}, {}, Unfollowed),
    ({"tasks": [{"ansible.builtin.debug": {"msg": "{{ lookup('template', 'a.j2', x) }}"}}]}, _HERE, Unfollowed),
    ({"tasks": [{"ansible.builtin.debug": {"msg": "{{ lookup('template', 'a.j2' ~ x) }}"}}]}, _HERE, Unfollowed),
    ({"tasks": [{"ansible.builtin.debug": {"msg": "{{ lookup('template', 'a.j2', variable_start_string='[%') }}"}}]},
     _HERE, Unfollowed),
    ({"tasks": [{"ansible.builtin.debug": {"var": "item"}, "with_template": ["{{ x }}"]}]}, {}, Unfollowed),
    ({"vars": {"v": "{{ lookup('template', x) }}"}}, {}, Unfollowed),
    ({"vars_files": [["{{ x }}.yml", "vars/ff.yml"]]}, {"ansible/playbooks/vars/ff.yml": {"a": 1}}, Unfollowed),
    ({"tasks": [{"ansible.builtin.debug": {"msg": "{{ lookup('template', 'a.j2', x == y) }}"}}]}, _HERE, Unfollowed),
    ({"tasks": [{"ansible.builtin.include_vars": {"dir": "vars/d", "extensions": "yml"}}]},
     {"ansible/playbooks/vars/d/a.yml": {"a": 1}}, Unfollowed),
    ({"tasks": [{"ansible.builtin.include_tasks": ["a.yml"]}]}, {"ansible/playbooks/a.yml": [_task("a")]}, Unfollowed),
    ({"tasks": [{"ansible.builtin.debug": {"msg": "{% include 'templates/a.j2' %}"}}]}, _HERE, Unfollowed),
    ({"vars": {"v": "{% import 'templates/a.j2' as m %}{{ m.x() }}"}}, _HERE, Unfollowed),
    ({"tasks": [{"ansible.builtin.include_vars": "iv.yml"}]},
     dict(_HERE, **{"ansible/playbooks/iv.yml": {"v": "{%- from 'templates/a.j2' import x -%}"}}), Unfollowed),
    ({"tasks": [{"name": "#jinja2: variable_start_string:'<<'\nsay << x >>", "ansible.builtin.debug": {"msg": "hi"}}]},
     {}, Unfollowed),
    ({"vars": {"v": "#jinja2: line_statement_prefix: '%'\n% include 'templates/a.j2'\n"}}, _HERE, Unfollowed),
    ({}, {"ansible/playbooks/deploy-stack.yml": [{"ansible.builtin.import_playbook": "o.yml", "vars": {"x": 1}}],
          "ansible/playbooks/o.yml": [{"hosts": "localhost"}]}, Unfollowed),
    ({}, {"ansible/playbooks/deploy-stack.yml": [{"name": "o", "import_playbook": "o.yml", "environment": {"X": "1"}}],
          "ansible/playbooks/o.yml": [{"hosts": "localhost"}]}, Unfollowed),
    ({}, {"instance.example/inventory/hosts": "[all:vars]\nx=1\n"}, Unfollowed),
    ({}, {"instance.example/inventory/site/hosts.ini": "[all:vars]\nx=1\n"}, Unfollowed),
    ({"tasks": [{"ansible.builtin.debug": {"var": "item"}, "with_ansible.builtin.template": ["{{ x }}"]}]}, {},
     Unfollowed),
    ({"tasks": [{"ansible.builtin.debug": {"msg": "{{ '{#' }}{% include 'templates/a.j2' %}{{ '#}' }}"}}]}, _HERE,
     Unfollowed),
    ({"tasks": [{"ansible.builtin.debug": {"msg": "{{ '{% raw %}' }}{% include 'templates/a.j2' %}"
                                                  "{{ '{% endraw %}' }}"}}]}, _HERE, Unfollowed),
    ({"tasks": [{"ansible.builtin.debug": {"msg": "{{ '{% include \"templates/a.j2\" %}' }}"}}]}, _HERE, Unfollowed),
    ({"tasks": [{"ansible.builtin.debug": {"msg": "{{ lookup(_k, 'a.j2') }}"}}]}, _HERE, Unfollowed),
    ({"tasks": [{"ansible.builtin.debug": {"msg": "{% set l = lookup %}{{ l('template', 'a.j2') }}"}}]}, _HERE,
     Unfollowed),
    ({"tasks": [{"ansible.builtin.debug": {"msg": "{{ lookup('template', 'a.j2', **o) }}"}}]}, _HERE, Unfollowed),
    ({"tasks": [{"ansible.builtin.debug": {"msg": "{{ lookup('template', *o) }}"}}]}, _HERE, Unfollowed),
    ({"tasks": [{"ansible.builtin.debug": {"msg": "{{ x "}}]}, {}, Unfollowed),
    ({"tasks": [{"ansible.builtin.debug": {"var": "[lookup][0]('template', 'a.j2')"}}]}, _HERE, Unfollowed),
    ({"tasks": [{"ansible.builtin.debug": {"msg": "m"}, "when": "'a' in {'l': q}['l']('template', 'a.j2')"}]}, _HERE,
     Unfollowed),
    ({"tasks": [{"ansible.builtin.debug": {"msg": "{{ '{% inc' ~ \"lude 'templates/a.j2' %}\" }}"}}]}, _HERE,
     Unfollowed),
    ({"tasks": [{"ansible.builtin.debug": {"var": "(lookup if true else none)('template', 'a.j2')"}}]}, _HERE,
     Unfollowed),
    ({"tasks": [{"ansible.builtin.debug": {"msg": "hi"}, "when": "{{ 'look' ~ 'up(1) is string' }}"}]}, _HERE,
     Unfollowed),
    ({"tasks": [{"ansible.builtin.assert": {"that": ["true", "{% if x %}true{% endif %}"]}}]}, {}, Unfollowed),
    ({"roles": [{"role": "r", "when": "{{ x }}"}]}, {}, Unfollowed),
    ({}, {"ansible/playbooks/deploy-stack.yml": [{"ansible.builtin.import_playbook": "o.yml", "when": "{{ x }}"}],
          "ansible/playbooks/o.yml": [{"hosts": "localhost"}]}, Unfollowed),
    ({"tasks": [{"ansible.builtin.debug": {"msg": "{{ '{{ lookup(\"template\", \"a.j2\") }}{{' }}"}}]}, _HERE,
     Unfollowed),
    ({"tasks": [{"ansible.builtin.debug": {"msg": "{{ '{{ lookup(' ~ x ~ ') }}' }}"}}]}, _HERE, Unfollowed),
    ({"vars": {"query": "up"}, "tasks": [{"ansible.builtin.debug": {"msg": "{{ query }}"}}]}, {}, Unfollowed),
    (_TPL, dict(_HERE, **{_T: "#jinja2: line_statement_prefix: '%'\n% include 'a.j2'\n"}), Unfollowed),
    ({}, {"instance.example/inventory/extra.yml": "h x=1\n"}, Unfollowed),
    ({"roles": ["r"]}, {"ansible/roles/r/meta/argument_specs.yml": {"argument_specs": {"main": {}}}}, Unfollowed),
    ({"roles": ["r"]}, {"ansible/roles/r/meta/main.yml": {"argument_specs": {"main": {}}}}, Unfollowed),
    ({"roles": ["r"]}, dict(_HERE, **{"ansible/roles/r/meta/main.yml": {"dependencies": [
        {"role": "d", "x": "{% include 'templates/a.j2' %}"}]}, "ansible/roles/d/tasks/main.yml": [_task("d")]}),
     Unfollowed),
], ids=["tasks_from", "vars_from", "role", "tasks-under-vars", "vars-files-missing", "vars-files-none-found",
        "template-missing", "template-include-missing", "var-lookup-missing", "templated-tasks", "include-no-file",
        "templated-vars-dir", "templated-vars-file", "templated-role", "templated-template",
        "template-include-by-a-var", "template-include-if-else", "template-include-a-list",
        "template-include-joined", "template-jinja2-header", "template-task-delimiters", "template-lookup-by-a-var",
        "template-lookup-second-term-by-a-var", "template-lookup-joined-term", "template-lookup-delimiters",
        "with-template-templated", "var-lookup-by-a-var", "vars-files-first-found-templated",
        "template-lookup-a-comparison-term", "include-vars-extensions-a-string",
        "include-tasks-a-list", "an-include-in-a-tasks-string", "an-import-in-a-play-var",
        "a-from-import-in-a-vars-file", "a-jinja2-header-in-a-tasks-name", "a-jinja2-header-in-a-play-var",
        "an-import-playbook-entrys-vars", "an-import-playbook-entrys-environment",
        "an-ini-inventory-file-with-no-extension", "an-ini-inventory-file-in-a-subdirectory",
        "with-a-prefixed-template-templated", "an-include-behind-a-quoted-comment-open",
        "an-include-behind-a-quoted-raw-open", "an-include-in-a-literal", "a-lookup-whose-plugin-is-a-var",
        "a-lookup-by-another-name", "a-template-lookup-unpacked", "a-template-lookup-unpacked-by-position",
        "a-string-jinja-cannot-parse", "a-lookup-called-through-a-subscript-in-a-debug-var",
        "a-lookup-called-through-a-dict-in-a-condition", "an-include-joined-from-two-literals",
        "a-lookup-called-through-a-conditional-in-a-debug-var", "a-condition-with-delimiters",
        "an-assert-that-with-a-block", "a-roles-entry-condition-with-delimiters",
        "an-import-playbook-entrys-condition-with-delimiters", "a-literal-that-does-not-parse-naming-a-lookup",
        "a-literal-naming-a-lookup-joined-to-a-var",
        "a-var-named-query-read-in-a-template",
        "template-jinja2-header-a-line-statement", "an-ini-inventory-file-named-yml", "a-roles-argument-specs-file",
        "a-roles-argument-specs-in-its-meta", "an-include-in-a-dependencys-param"])
def test_the_deploy_walk_fails_on_a_file_it_cannot_find(tmp_path, play, files, error):
    """A role, a named role file, a vars_files entry (a first-found list none of whose entries is there), a template, a
    file a template includes or a template a lookup in a var renders that the walk cannot find, and a task file that is
    only under a `vars/` directory (where Ansible looks for none), fail the walk with FileNotFoundError; a name it
    cannot read fails it with `Unfollowed`: an include path, a role or a template source that is templated, an include
    with no file, a template's include by a variable, by a conditional, by a list or joined to one, a template under
    delimiters of its own (a `#jinja2:` first line, a task's `variable_start_string`), a template lookup whose term is a
    variable, a later one, or joined to one, or under a delimiter argument, a `with_template` of a templated name, a
    lookup in a var by a variable, a lookup term that is a comparison (`x == y`, no keyword), an a first-found list that
    holds a templated name, an include_vars `extensions:` that is not a list and an include given a list. Why: a file
    the walk skipped in silence is one the pins never read; Ansible fails on a named role file it lacks and a run on a
    missing vars_files entry (fold 5's read 110, N3 and S2, 111, S2; fold 6's own mutants); and the walk took the first
    literal of an include's conditional or list and of a lookup's terms, read none past the first, and read a template
    under other delimiters as if Jinja's own were its (fold 6's reads 120, S1: C2, C4 and 121, S2: Q8, Q13, N5); fold
    7's own mutants passed every row reading the last five as names. And, fold 8, a form it does not model fails it with
    `Unfollowed`: an include, import or from in a task's string, a play var or a vars file; a `#jinja2:` header in a
    task's name or a play var; an import_playbook entry's `vars:` or `environment:`; an INI inventory file, with no
    extension or a `.ini` in a subdirectory; and a `with_ansible.builtin.template` of a templated name. Why: Ansible
    renders each (a string's include from the playbook's directory, a header's delimiters, an entry's vars for every
    play it imports, an INI file's vars), and each carried the URL or a third domain past both pins (fold 7's reads 130,
    S1, S3 and 131, S3, S4, S5); refusing the class holds its next spelling, where each spelling the walk modelled had
    passed the next fold's reads. And, fold 9, where Jinja's own parser reads the form: an include behind a quoted
    comment or raw open, or in a literal; a lookup whose plugin is a var, one read by another name (`{% set l = lookup
    %}`), a template lookup's unpacked arguments, by keyword or by position; a string Jinja cannot parse; a `#jinja2:`
    header that sets a line statement prefix, in a play var or a template; an INI inventory file named `.yml`; a role's
    `meta/argument_specs` file or meta key; and an include in a dependency's param. Why: a pattern took a quoted `'{#'`
    for a comment and passed the include behind it, read no lookup whose plugin was not quoted, and skipped an inventory
    file that was not a mapping, each carrying the URL past both pins on both cores (fold 8's read 140, S1: G1, G1c, G2,
    G3); a header's line statement runs text the walk reads as plain; the walk does not read argument_specs, whose
    refusal prints a value on 2.19 under no_log too (140, N3); and a dependency's param went unread by `_inline` with
    every row green (140, N2: W9). And, fold 10: a lookup called through a subscript in a debug `var:`, or through a
    dict in a condition, and an include joined from two literals in a task's string. Why: Ansible runs the call in each
    expression, on both cores, and 2.18 renders the joined include where a fact holding it is read; each passed both
    pins (fold 9's read 151, S1). And a lookup called through a conditional; a condition with delimiters of its own, in
    a task, an assert's `that:`, a role's entry or an import_playbook entry; a literal that does not parse but names a
    lookup, alone or joined to a variable; and a var named `query` read in a template, a name the walk reserves. Why:
    Ansible runs the call; 2.18 renders a delimited condition and runs what it renders (a built lookup, or one spliced
    past its `{% set %}`), which the walk does not compute, and 2.19 refuses it; a literal the walk lexed otherwise than
    Ansible failed to parse and was skipped, the lookup in it running (fold 9's read 150, S-C, S-A, S-B, N-D)."""
    tree = _tree(tmp_path, dict({"ansible/playbooks/deploy-stack.yml": [dict({"hosts": "localhost"}, **play)],
                                 "ansible/roles/r/tasks/main.yml": [_task("m")]}, **files))
    with pytest.raises(FileNotFoundError) as e:
        Walk(tree)
    assert type(e.value) is error, e.value


def test_a_missing_file_a_templates_raw_block_names_says_how_to_spell_it(tmp_path):
    """A template task's template whose raw block names a file that is not there fails the walk with
    FileNotFoundError, as any missing file does, and the error names the template and how a raw block spells its
    delimiters (`{{ '{%' }}`) so the walk reads it as text. Why: the walk follows a raw block's include, as a lookup's
    or a fact's text may be rendered again, and for a template task that is an over-read; the bare error named only
    the missing file, and no remedy (fold 9's read 151, N1)."""
    tree = _tree(tmp_path, {"ansible/playbooks/deploy-stack.yml": [{"hosts": "localhost", "tasks": [
        {"ansible.builtin.template": {"src": "t.j2", "dest": "/x"}}]}],
        _T: "{% raw %}{% include 'partial.html' %}{% endraw %}\n"})
    with pytest.raises(FileNotFoundError) as e:
        Walk(tree)
    assert type(e.value) is FileNotFoundError and "partial.html" in str(e.value), e.value
    assert "templates/t.j2" in str(e.value).replace("\\", "/") and "{{ '{%' }}" in str(e.value), e.value


def test_the_walk_reads_the_roles_path_and_inventory_ansible_cfg_names():
    """ansible/ansible.cfg names `roles_path = roles` and `inventory = ../instance/inventory`, the two the walk
    reads: ROLES_PATH, ansible/roles/, and INVENTORY, the instance.example/inventory/ that kontroll-init copies to
    instance/inventory; and it names no `jinja2_extensions`, so Jinja's own parser, which the walk reads every string
    with, parses what Ansible's does. Why: the walk takes both as given, so a cfg that named another roles path or
    inventory would have the two pins read files the deploy never loads, and miss the ones it does (fold 5's read 110,
    S2); and an extension (`jinja2.ext.do`, `loopcontrols`) adds a statement the walk's parser refuses or reads as
    text, where Ansible runs it (fold 9's parser)."""
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read(os.path.join(_ROOT, "ansible", "ansible.cfg"), encoding="utf-8")
    assert not any(cfg.has_option(s, "jinja2_extensions") for s in cfg.sections()), cfg.sections()
    assert os.path.join("ansible", cfg["defaults"]["roles_path"]) == os.path.join(*ROLES_PATH)
    assert os.path.normpath(os.path.join("ansible", cfg["defaults"]["inventory"])) == \
        os.path.join("instance", *INVENTORY[1:]) and INVENTORY[0] == "instance.example"


_RUNS_THE_DEPLOY = re.compile(r"ansible-playbook\s+(\S*)deploy-stack\.yml")


def test_every_command_that_runs_the_deploy_runs_it_from_the_walks_working_directory():
    """Each command the tree runs the deploy with (the installer's `init` and `check`, and each operator command the
    API hands out) names it `playbooks/deploy-stack.yml`, so it runs from `ansible/`, the walk's CWD; the installer
    changes into `$ANSIBLE_DIR` first. Why: Ansible resolves an include or a lookup it finds nowhere else against the
    working directory (`tasks/<name>` there: fold 7's reads 130, S2 and 131, S6: I1, I2), so the walk searches
    `ansible/` too, which holds only while every run starts there; a command that ran the deploy from the repo root
    would have Ansible load files the two pins never read."""
    found = {}
    for path in sorted(glob.glob(os.path.join(_ROOT, "scripts", "kontroll", "service", "*.py"))) + [
            os.path.join(_ROOT, "docker", "installer", "entrypoint.sh")]:
        with open(path, encoding="utf-8") as fh:
            found[os.path.relpath(path, _ROOT).replace(os.sep, "/")] = _RUNS_THE_DEPLOY.findall(fh.read())
    assert found["docker/installer/entrypoint.sh"] and sum(map(len, found.values())) > 2, found
    assert {d for ds in found.values() for d in ds} == {"playbooks/"}, found
    with open(os.path.join(_ROOT, "docker", "installer", "entrypoint.sh"), encoding="utf-8") as fh:
        text = fh.read()
    assert re.search(r'^ANSIBLE_DIR="[^"\n]*/ansible"$', text, re.M) and text.count('cd "$ANSIBLE_DIR"') >= 2
    assert CWD == ("ansible",)


def test_strings_reads_a_block_scalars_hash_line_and_not_a_comment():
    """`strings` yields each key and string value of a parsed document: a block scalar's `#` line is part of its
    string, which Ansible templates, and a YAML comment is none. Why: the drift pin stripped every `#`-led line as
    a comment, so a decrypt written in a block scalar passed it (fold 4's read 90, S2)."""
    doc = yaml.safe_load("a: |\n  # {{ lookup('x') }}\n# lookup('y')\nb: [c, {d: e}]\n")
    assert list(strings(doc)) == ["a", "# {{ lookup('x') }}\n", "b", "c", "d", "e"]


# fold 10: read 150's rows (fold 9's 8631cb3), each a form probe12 shows Ansible running, or one the walk refuses
# because a core does not take it, where an m19 mutant passed every row of the suite or the walk read nothing
_BS = chr(92)    # a backslash, kept out of the source's own escapes
_A = {_PB + "templates/a.j2": "a\n", _PB + "templates/b.j2": "b\n"}


def _walk_of(tmp_path, tasks, files=None, play=None):
    """The walk over a tree of its own: `tasks` in deploy-stack.yml's one play (or `play`, its plays), beside the
    templates a.j2 and b.j2 and `files`."""
    return Walk(_tree(tmp_path, dict({_PB + "deploy-stack.yml": play or [{"hosts": "localhost", "tasks": tasks}]},
                                     **_A, **(files or {}))))


@pytest.mark.parametrize("task", [
    {"name": "c", "ansible.builtin.debug": {"msg": "hi"}, "when": "lookup('template', 'a.j2')"},
    {"name": "c", "ansible.builtin.assert": {"that": ["lookup('template', 'a.j2')"]}},
    {"name": "c", "ansible.builtin.debug": {"msg": "hi"}, "until": "lookup('template', 'a.j2')", "retries": 1},
    {"name": "c", "ansible.builtin.debug": {"msg": "hi"}, "changed_when": ["lookup('template', 'a.j2')"]},
], ids=["when", "assert-that", "until", "changed-when"])
def test_a_lookup_call_that_is_a_whole_condition_is_read(tmp_path, task):
    """A template lookup call that is the whole of a condition has its template read: Ansible renders it on both
    cores (probe12 p21b-p21d). Why: the expression tree's top node is that call, which Jinja's `find_all` leaves out,
    so fold 9's walk read none, where fold 8's pattern read each (fold 9's read 150, S-E)."""
    w = _walk_of(tmp_path, [task])
    assert _PB + "templates/a.j2" in _rel(tmp_path, w.template_files)


def test_a_tuple_condition_is_read(tmp_path):
    """A condition that is a tuple (`true, lookup('template', 'a.j2')`) has its template read: 2.18 splices it into a
    `{% set %}` and builds each item (probe12 p4). Why: the walk parsed a condition as one expression, which stops at
    the comma, and dropped the string as none (fold 9's read 150, S-A)."""
    w = _walk_of(tmp_path, [{"name": "c", "ansible.builtin.debug": {"msg": "hi"},
                             "when": "true, lookup('template', 'a.j2')"}])
    assert _PB + "templates/a.j2" in _rel(tmp_path, w.template_files)


def test_a_playbook_strings_literal_keeps_its_backslashes_as_ansible_keeps_them(tmp_path):
    """A set_fact's literal and a file lookup's term that hold a template lookup and then an escaped brace (a backslash
    and `x7b`) have the template read, the backslash kept as Ansible keeps it in a playbook's `{{ }}`; and a template
    file's own literal is decoded as Jinja decodes it (a backslash and `x2e` for the dot of b.j2). Why: Jinja alone
    decoded the escape into a brace, the literal then failed to parse and was skipped, where both cores render it
    (probe12 p1b, p1c; fold 9's read 150, S-B)."""
    lit = "{{ lookup(\"template\", \"a.j2\") }}" + _BS + "x7b" + _BS + "x7b"
    w = _walk_of(tmp_path, [
        {"name": "f", "ansible.builtin.set_fact": {"f": "{{ '" + lit + "' }}"}},
        {"name": "t", "ansible.builtin.debug": {"msg": "{{ lookup('ansible.builtin.file', '" + lit + "') }}"}},
        {"name": "tpl", "ansible.builtin.template": {"src": "t.j2", "dest": "/x"}}],
        {_T: "{{ lookup('template', 'b" + _BS + "x2ej2') }}\n"})
    got = {t.get("name"): _rel(tmp_path, w.templates[id(t)]) for t in w.tasks if id(t) in w.templates}
    assert got == {"f": {_PB + "templates/a.j2"}, "t": {_PB + "templates/a.j2"},
                   "tpl": {_T, _PB + "templates/b.j2"}}, got


def test_a_template_name_with_a_backslash_is_opened_as_written(tmp_path):
    """A template lookup in a playbook's string whose name holds a backslash escape (a backslash and `x2e` for a dot)
    fails the walk for that name, as written, and does not read the decoy a.j2 beside it. Why: Jinja alone decoded the
    name, so the walk read the decoy where Ansible opens the other file (probe12 p12; fold 9's read 150, S-B)."""
    with pytest.raises(FileNotFoundError) as e:
        _walk_of(tmp_path, [{"ansible.builtin.debug": {"msg": "{{ lookup('template', 'a" + _BS + "x2ej2') }}"}}])
    assert type(e.value) is FileNotFoundError and "a" + _BS + "x2ej2" in str(e.value), e.value


def test_a_block_literal_in_a_playbook_string_is_decoded_as_ansible_decodes_it(tmp_path):
    """A template lookup inside `{% %}` in a playbook's string that holds `{{ }}` too, whose name spells its dot as a
    backslash and `x2e`, has b.j2 read: both cores decode an escape inside a block, as Jinja does (fold 9's read 150,
    S-B). Why: a mutant that kept each backslash of a block's literal as well passed every row (fold 10's own
    mutants)."""
    w = _walk_of(tmp_path, [{"name": "d", "ansible.builtin.debug": {
        "msg": "{{ 1 }}{% if lookup('template', 'b" + _BS + "x2ej2') %}{% endif %}"}}])
    got = {t.get("name"): _rel(tmp_path, w.templates[id(t)]) for t in w.tasks if id(t) in w.templates}
    assert got == {"d": {_PB + "templates/b.j2"}}, got


def test_a_debug_var_with_a_backslash_is_read_as_a_playbooks_string_too(tmp_path):
    """A debug `var:` that is a template lookup alone, whose name spells its dot as a backslash and `x2e`, fails the
    walk for that name as written: 2.18 wraps a debug var in `{{ }}` and lexes it as a playbook's string, keeping the
    backslash, and 2.19 compiles it as an expression and decodes it; the walk, which cannot tell a var from a
    condition, reads such a string both ways, so it holds on each core and fails loud where a spelling names no file
    (fold 9's read 150, S-B; fold 10's read 160, N-1). Why: a mutant that parsed it decoded alone read the decoy a.j2
    and passed every row (fold 10's own mutants)."""
    with pytest.raises(FileNotFoundError) as e:
        _walk_of(tmp_path, [{"ansible.builtin.debug": {"var": "lookup('template', 'a" + _BS + "x2ej2')"}}])
    assert type(e.value) is FileNotFoundError and "a" + _BS + "x2ej2" in str(e.value), e.value


_LOOKS_A = "lookup(\"template\", \"a.j2\")"
_INCLUDED = {_PB + "t9.yml": [_task("t9")], "ansible/roles/r9/tasks/main.yml": [_task("r9")]}


def _conditioned(key, cond):
    """A task that runs `cond` as a condition under `key`: a task key of `_CONDITIONS`, an assert's `that:` (under the
    bare `assert` too), a loop's `loop_control.break_when`, or an include_tasks or include_role `apply:` `when:` (of
    t9.yml or the role r9, in `_INCLUDED`)."""
    if key in ("that", "bare-assert-that"):
        return {"name": "c", "ansible.builtin.assert" if key == "that" else "assert": {"that": [cond]}}
    if key in ("include_tasks.apply", "include_role.apply"):
        ref = {"file": "t9.yml"} if key == "include_tasks.apply" else {"name": "r9"}
        return {"name": "c", "ansible.builtin." + key.split(".")[0]: dict(ref, apply={"when": cond})}
    task = {"name": "c", "ansible.builtin.debug": {"msg": "hi"}, "retries": 1}
    if key == "loop_control.break_when":
        task.update(loop=[1, 2], loop_control={"break_when": [cond]})
    else:
        task[key] = cond
    return task


@pytest.mark.parametrize("key", ["when", "changed_when", "failed_when", "until", "that", "bare-assert-that",
                                 "loop_control.break_when", "include_tasks.apply", "include_role.apply"])
def test_a_condition_with_delimiters_fails_the_walk_under_each_key(tmp_path, key):
    """A condition with delimiters of its own (`"{{ x }}"`) fails the walk under each key Ansible reads as a
    condition: a task's, an assert's `that:` under its bare name too, a loop's `loop_control.break_when` and an
    include's `apply:` `when:`: 2.18 renders it and runs what it renders, and 2.19 refuses it (fold 9's read 150,
    S-A). Why: a mutant that dropped `until` from `_CONDITIONS` passed every row, the fails_on table holding a `when:`
    alone (fold 10's own mutants); the walk read no `break_when` and no `apply:` `when:`, each of which 2.18 renders
    and runs (`'{{ ''lookup("pipe", …) == "yes"'' }}'` ran its lookup and broke the loop, a computed and a raw `apply:`
    `when:` ran theirs, with the walk green: fold 11's reads 171, S4 and 170, S-2); and a mutant that read the
    FQCN assert alone passed every row (170, N-1)."""
    with pytest.raises(Unfollowed):
        _walk_of(tmp_path, [_conditioned(key, "{{ x }}")], _INCLUDED)


@pytest.mark.parametrize("key,cond", [
    ("when", "'{{' in banner"), ("when", "'{#' not in line"), ("when", "'{%' in line"),
    ("when", "x == '{{ y }}'"), ("when", "x {# a note #}"), ("when", "{% if x %}true{% endif %}"),
    ("when", "{% raw %}lookup('template', 'a.j2') is string{% endraw %}"),
    ("when", "'{#' != '{{ " + _LOOKS_A + " }}'"), ("when", "'{{' in '{{ " + _LOOKS_A + " }}'"),
    ("when", "'{#' not in ['{{ " + _LOOKS_A + " }}']"), ("when", "['{#', '{{ " + _LOOKS_A + " }}'] | length == 2"),
    ("changed_when", "'{{' in '{{ " + _LOOKS_A + " }}'"), ("until", "'{#' != '{{ " + _LOOKS_A + " }}'"),
    ("that", "'{#' != '{{ " + _LOOKS_A + " }}'"), ("loop_control.break_when", "'{#' != '{{ " + _LOOKS_A + " }}'"),
    ("when", "'" + _BS + "x7b" + _BS + "x7b " + _LOOKS_A + " }}' != 'x'"),
    ("changed_when", "'" + _BS + "x7b" + _BS + "x7b " + _LOOKS_A + " }}' != 'x'"),
    ("when", "'" + _BS + "x7b" + _BS + "x7b " + _LOOKS_A + " }}'"),
    ("when", " #jinja2: variable_start_string: '[[', variable_end_string: ']]'\n[[ " + _LOOKS_A + " ]]"),
    ("when", "\n#jinja2: variable_start_string: '[[', variable_end_string: ']]'\n[[ " + _LOOKS_A + " ]]"),
    ("that", "\t#jinja2: variable_start_string: '[[', variable_end_string: ']]'\n[[ " + _LOOKS_A + " ]]"),
    ("when", "'{# a note #}' != line"), ("when", "'{% if true %}{% endif %}' != line"), ("when", "x == 'a #jinja2: b'"),
    ("when", "'a" + _BS + "x7b" + _BS + "x7b " + _LOOKS_A + " }}' != 'x'"),
], ids=["quoted-variable", "quoted-comment", "quoted-block", "closed-variable", "comment", "block", "raw",
        "folded-compare", "folded-in", "folded-not-in-list", "folded-list-length", "folded-in-changed-when",
        "folded-compare-until", "folded-compare-that", "folded-compare-break-when", "decoded-braces",
        "decoded-braces-changed-when", "decoded-braces-alone", "header-past-a-space", "header-past-a-newline",
        "header-past-a-tab-that", "quoted-closed-comment", "quoted-closed-block", "quoted-header-past-its-start",
        "decoded-braces-past-its-start"])
def test_a_condition_that_holds_a_delimiter_fails_the_walk(tmp_path, key, cond):
    """A condition that holds a delimiter or a header, in a literal too, closed or not, at its start or past it (`'{{'
    in banner`, `x == '{{ y }}'`, `'{# a note #}' != line`, `x == 'a #jinja2: b'`), a comment, a block or a raw block, a
    literal whose braces are written as their codes (a backslash and `x7b`, which Jinja decodes), or a `#jinja2:` header
    past leading whitespace, fails the walk. 2.18 renders a condition that is a template and runs what it renders (a raw
    block's text too: fold 11's probe); 2.19 renders each literal of a condition that holds a delimiter as a template,
    so `'{#' != '{{ lookup(…) }}'`, whose expression folds to a bool the walk reads no literal of, ran its lookup under
    each key here (fold 11's read 170, S-1), as the decoded braces did under `when:` and `changed_when:` (fold 12's
    probe, ansible-core 2.19.4; 2.18.19 ran neither; a condition that is the literal alone is read as one too, which
    Jinja's `find_all` leaves out: fold 12's own row), and it honours a header past the whitespace it strips from a
    condition (170, S-3); and neither core runs to its end a condition whose literal alone holds a delimiter (170, S-4;
    171, S1). Why: fold 11 refused a condition only where 2.18's `is_template` reads a template and read the rest as its
    expression, so each folded and header row passed the walk while 2.19 ran its lookup; a text pattern alone passes the
    decoded braces; and a closed comment, a closed block and a header in a literal, and decoded braces past a literal's
    start, each passed a mutant of the check with every row green, the rows before failing Jinja's parse as well (fold
    13's own mutants)."""
    with pytest.raises(Unfollowed):
        _walk_of(tmp_path, [_conditioned(key, cond)], _INCLUDED)


@pytest.mark.parametrize("key,cond", [
    ("when", "lookup('template', 'a.j2') %}x"), ("changed_when", "lookup('template', 'a.j2') == 'x' -%}"),
    ("when", "x | to_datetime %}x"), ("until", "lookup('template', 'a.j2') %}x"),
    ("that", "lookup('template', 'a.j2') %}x"), ("loop_control.break_when", "lookup('template', 'a.j2') %}x"),
], ids=["a-close-after-a-lookup", "a-trimmed-close-changed-when", "a-close-after-a-filter", "until", "that",
        "break-when"])
def test_a_condition_that_does_not_parse_as_one_expression_fails_the_walk(tmp_path, key, cond):
    """A condition that does not parse as one expression (a lookup, or a filter, then a block's close and more text)
    fails the walk under each key, and its refusal says so and how a literal one is written. 2.19 fails such a
    condition; 2.18 splices it into a `{% set %}` tag, which its close ends, and runs its head (`lookup(…) %}x` ran
    its lookup under each condition key on 2.18.19). Why: the walk read a condition that does not parse as holding
    no delimiter, so each row passed it while 2.18 ran the lookup (fold 12's reads 181, S3 and 180, S-A)."""
    with pytest.raises(Unfollowed) as raised:
        _walk_of(tmp_path, [_conditioned(key, cond)], _INCLUDED)
    assert "does not parse as one expression" in str(raised.value) and "written in pieces" in str(raised.value)


@pytest.mark.parametrize("cond", ["x is not match('^' ~ '#' ~ 'jinja2:')", "('{' ~ '{') not in banner"],
                         ids=["a-header-in-pieces", "a-delimiter-in-pieces"])
def test_a_condition_that_spells_a_delimiter_in_pieces_passes_the_walk(tmp_path, cond):
    """A condition that writes a `#jinja2:` header or a delimiter's start in pieces (`'#' ~ 'jinja2:'`, `'{' ~
    '{'`), the remedy the walk's refusal names, passes it: neither core reads a template in the pieces. Why: a
    refusal whose remedy the walk refused too would leave a sound condition no spelling (fold 12's read 181, N1)."""
    _walk_of(tmp_path, [_conditioned("when", cond)])


@pytest.mark.parametrize("task", [
    {"name": "m", "action": "{{ _m }} src=a.j2 dest=/x", "vars": {"_m": "template"}},
    {"name": "m", "action": {"module": "{{ _m }}", "src": "a.j2", "dest": "/x"}, "vars": {"_m": "template"}},
    {"name": "m", "local_action": "{{ _m }} src=a.j2 dest=/x", "vars": {"_m": "template"}},
    {"name": "m", "action": "temp{{ 'late' }} src=a.j2 dest=/x"},
    {"name": "m", "action": "{% if true %}template{% endif %} src=a.j2 dest=/x"},
], ids=["action-string", "action-mapping", "local-action", "a-word-split-by-a-block",
        "a-word-a-statement-writes"])
def test_a_module_named_by_a_template_fails_the_walk(tmp_path, task):
    """A task whose `action:` or `local_action:` names its module by a template, as a string's first word or a mapping's
    `module:`, whole or in part, by an expression or a statement, fails the walk: the module is the template's
    value, which the walk cannot read, so it cannot read the module's arguments either. Why: an `action: "{{ _m }}
    src=a.j2 …"` with `_m: template` rendered a.j2 on both cores while the walk read no template, so the URL's pin
    passed a template that printed it (fold 12's read 181, S2); and a word a statement writes passed a mutant that read
    an expression's start alone (fold 13's own mutants)."""
    with pytest.raises(Unfollowed, match="a module named by a template"):
        _walk_of(tmp_path, [task])


def test_a_module_named_as_it_is_under_action_is_read(tmp_path):
    """An `action:` that names its module as it is (`ansible.builtin.template src=a.j2 dest=/x`) passes the walk,
    which reads its template. Why: a refusal of every `action:` string would fail the sound form with it (fold 13's
    own row)."""
    w = _walk_of(tmp_path, [{"name": "m", "action": "ansible.builtin.template src=a.j2 dest=/x"}])
    assert _PB + "templates/a.j2" in _rel(tmp_path, w.template_files)


def test_a_mapping_modules_words_past_the_first_are_its_arguments(tmp_path):
    """An `action:` mapping whose `module:` holds the module and its k=v arguments (`ansible.builtin.template src=a.j2
    dest=/x`) has its template read, its words read over the mapping's other keys, and one whose arguments hold a
    template (`copy content={{ _s }} dest=/x`) passes the walk. Why: Ansible splits a mapping's `module:` as it splits
    a string and runs the module its first word names, while the walk took the whole string for the module's name, so
    it read no template such a task renders, which printed its diff on both cores, and refused a sound copy as a
    module named by a template (fold 13's reads 191, S-b and 190, N-1: M5)."""
    w = _walk_of(tmp_path / "t", [{"name": "m", "action": {"module": "ansible.builtin.template src=a.j2 dest=/x"}}])
    assert _PB + "templates/a.j2" in _rel(tmp_path / "t", w.template_files)
    assert args({"action": {"module": "copy dest=/a", "dest": "/b", "mode": "0600"}}, "copy") == {
        "dest": "/a", "mode": "0600"}
    _walk_of(tmp_path / "c", [{"name": "c", "action": {"module": "copy content={{ _s }} dest=/x"},
                               "vars": {"_s": "y"}}])


def test_a_condition_that_holds_a_delimiter_past_its_start_is_delimited():
    """`_delimited` finds a delimiter anywhere in a condition, past its start too, where the condition parses as one
    expression that holds no string (`x == {{1: 2}: 3}`, a mapping keyed by a mapping). Why: a mutant that looked for
    the delimiter at the condition's start alone survived every row, the walk's own string check refusing each first
    (fold 13's reads 190 and 191: X-delimited-match-not-search)."""
    assert _delimited("x == {{1: 2}: 3}")


def test_a_condition_that_is_not_a_string_is_read_not_refused(tmp_path):
    """A `when:` that is a list of a list of a template lookup passes the walk and has its template read: 2.19
    refuses a condition that is not a string, and 2.18 renders the templates in it, which the walk reads, and runs no
    computed one (fold 11's read 170, N-1: P3). Why: a mutant that refused a non-string condition's text passed every
    row (170, N-1)."""
    w = _walk_of(tmp_path, [{"name": "c", "ansible.builtin.debug": {"msg": "hi"},
                             "when": [["{{ lookup('template', 'a.j2') }}"]]}])
    assert _PB + "templates/a.j2" in _rel(tmp_path, w.template_files)


def test_a_literals_text_is_read_as_a_template_not_an_expression(tmp_path):
    """A literal whose text is a template lookup handed a variable, `{{ 'lookup("template", name)' }}`, passes the
    walk: Ansible renders a literal's text again as a template, and there the call is text, not an expression. Why: a
    mutant that read a nested string as an expression too refused it with every row green (fold 11's read 170,
    N-1)."""
    _walk_of(tmp_path, [{"name": "c", "ansible.builtin.debug": {"msg": "{{ 'lookup(\"template\", name)' }}"}}])


def test_a_string_jinja_cannot_parse_as_a_template_fails_the_walk(tmp_path):
    """A task's `msg:` that Jinja cannot parse as a template fails the walk, whether its first delimiter closes or not,
    and whether it parses as an expression or not (`'{{' ~ lookup('template', 'a.j2')`, `'{{' x y`, `'{{' ~ x ~ '}}' ~
    '{%'`): 2.19 fails the task on it, and a condition that holds a delimiter fails the walk before. Why: fold 11 read
    such a string as its expression alone where its first delimiter does not close, and with it a condition whose
    expression folds, whose literal 2.19 renders (fold 11's read 170, S-1)."""
    for msg in ["'{{' ~ lookup('template', 'a.j2')", "'{{' x y", "'{{' ~ x ~ '}}' ~ '{%'"]:
        with pytest.raises(Unfollowed):
            _walk_of(tmp_path, [{"name": "c", "ansible.builtin.debug": {"msg": msg}}])


def test_a_template_file_jinja_cannot_parse_fails_the_walk_though_it_parses_as_an_expression(tmp_path):
    """A template file whose text Jinja cannot parse as a template fails the walk, though the text parses as one
    expression (`'{{' ~ x`): Ansible renders a file as a template alone, never as an expression. Why: fold 11 read a
    string whose first delimiter does not close as its expression alone, and a mutant that let a file take that
    reading passed every row (fold 11's mutants)."""
    with pytest.raises(Unfollowed):
        _walk_of(tmp_path, _TPL["tasks"], {_T: "'{{' ~ x\n"})


def test_a_template_files_text_is_not_read_as_an_expression(tmp_path):
    """A template file whose whole text is a template lookup call, `lookup('template', 'nothere.j2')`, passes the walk:
    Ansible renders a file as a template, and there the call is text. Why: the walk read a file's text as one
    expression too, so it looked up a template Ansible never renders (fold 11's read 170, N-2)."""
    _walk_of(tmp_path, _TPL["tasks"], {_T: "lookup('template', 'nothere.j2')\n"})


def test_a_header_of_the_other_whitespace_keys_is_read(tmp_path):
    """A `#jinja2:` header that sets keep_trailing_newline and newline_sequence, which both cores take (probe12
    p17c), is read past and its include followed. Why: a mutant that dropped either key from `_WHITESPACE` refused
    it with every row green (fold 9's read 150, N-E: m19)."""
    w = _walk_of(tmp_path, _TPL["tasks"], {_T: "#jinja2: keep_trailing_newline: True, newline_sequence: '" + _BS
                                           + "n'\n{% include 'a.j2' %}\n"})
    assert _PB + "templates/a.j2" in _rel(tmp_path, w.template_files)


@pytest.mark.parametrize("text", ["#jinja2: trim_blocks\n{% include 'a.j2' %}\n",
                                  "#jinja2: variable_start_string: '[%' "], ids=["no-colon", "no-newline"])
def test_a_header_the_walk_cannot_model_fails_it(tmp_path, text):
    """A header pair with no `:` (both cores refuse it, probe12 p17b) and a header with no newline after it (2.19
    refuses it; 2.18 reads the line less its last character and renders the whole line, p17) fail the walk. Why: a
    mutant that took either passed every row (fold 9's read 150, N-E: m19)."""
    with pytest.raises(Unfollowed):
        _walk_of(tmp_path, _TPL["tasks"], {_T: text})


def test_a_literal_two_deep_is_read(tmp_path):
    """A set_fact's literal that holds a raw block that holds a lookup has the template read: 2.18 renders the
    literal at each read of the fact (probe12 p1f). Why: a mutant that recursed into literals one level only passed
    every row (fold 9's read 150, N-E: m19)."""
    w = _walk_of(tmp_path, [{"name": "f", "ansible.builtin.set_fact": {
        "f": "{{ '{% raw %}{{ lookup(\"template\", \"b.j2\") }}{% endraw %}' }}"}}])
    got = {t.get("name"): _rel(tmp_path, w.templates[id(t)]) for t in w.tasks if id(t) in w.templates}
    assert got == {"f": {_PB + "templates/b.j2"}}, got


@pytest.mark.parametrize("key", ["comment_start_string", "block_start_string", "variable_end_string"])
def test_a_template_task_under_each_delimiter_fails_the_walk(tmp_path, key):
    """A template task handed a comment or block delimiter, or an end string, fails the walk: the template action
    takes each (under `comment_start_string: '[#'` a lookup inside `{# #}` runs on both cores, probe12 p15). Why: a
    mutant that dropped the comment or block kind from `_DELIMITERS`, or its end strings, passed every row (fold 9's
    read 150, N-E: m19)."""
    with pytest.raises(Unfollowed):
        _walk_of(tmp_path, [{"ansible.builtin.template": {"src": "t.j2", "dest": "/x", key: "[#"}}], {_T: "x\n"})


def test_a_template_lookups_later_delimiter_argument_fails_the_walk(tmp_path):
    """A template lookup handed a delimiter after another keyword argument fails the walk. Why: a mutant that read
    the first keyword argument alone passed every row (fold 9's read 150, N-E: m19)."""
    with pytest.raises(Unfollowed):
        _walk_of(tmp_path, [{"ansible.builtin.debug": {
            "msg": "{{ lookup('template', 'a.j2', convert_data=False, variable_start_string='[%') }}"}}])


def test_an_import_entrys_tags_list_lookup_is_read(tmp_path):
    """A template lookup in an import_playbook entry's `tags:` list, which both cores render (probe12 p16b), is read.
    Why: a mutant that held the entry's `when:` alone passed every row (fold 9's read 150, N-E: m19)."""
    w = _walk_of(tmp_path, None, {_PB + "o.yml": [{"hosts": "localhost", "tasks": [_task("o")]}]},
                 play=[{"ansible.builtin.import_playbook": "o.yml", "tags": ["{{ lookup('template', 'a.j2') }}"]}])
    assert _PB + "templates/a.j2" in _rel(tmp_path, w.template_files)


def test_a_directory_ending_in_tasks_is_not_a_tasks_directory(tmp_path):
    """A role's include under `x/mytasks/` whose own include names a file only under `x/` fails the walk:
    DataLoader's RE_TASKS takes a directory named `tasks` for its parent, not one whose name ends so. Why: a mutant
    that took `endswith("tasks")` read the file with every row green (fold 9's read 150, N-E: m19)."""
    with pytest.raises(FileNotFoundError) as e:
        _walk_of(tmp_path, None, {
            "ansible/roles/g6/tasks/main.yml": [{"ansible.builtin.include_tasks": "x/mytasks/a.yml"}],
            "ansible/roles/g6/tasks/x/mytasks/a.yml": [{"ansible.builtin.include_tasks": "b.yml"}],
            "ansible/roles/g6/tasks/x/b.yml": [_task("b")]}, play=[{"hosts": "localhost", "roles": ["g6"]}])
    assert type(e.value) is FileNotFoundError, e.value


def test_a_prefix_expression_in_text_is_not_a_condition(tmp_path):
    """A debug message whose words start with a lookup call (`lookup('template', 'nothere.j2') and its words`) is
    text, not one expression, so the walk looks up no template. Why: a mutant that took an expression's prefix
    refused it (FileNotFoundError) with every row green (fold 9's read 150, N-E: m19)."""
    _walk_of(tmp_path, [{"ansible.builtin.debug": {"msg": "lookup('template', 'nothere.j2') and its words"}}])


@pytest.mark.parametrize("ext", [".yaml", ".json"])
def test_argument_specs_under_each_extension_fails_the_walk(tmp_path, ext):
    """A role's `meta/argument_specs.yaml` or `.json`, which Ansible loads (C.YAML_FILENAME_EXTENSIONS), fails the
    walk as `.yml` does. Why: a mutant that checked `.yml` alone, and one that checked `.yml` and `.yaml`, passed
    every row (fold 9's read 150, N-E: m19)."""
    with pytest.raises(Unfollowed):
        _walk_of(tmp_path, None, {"ansible/roles/r/tasks/main.yml": [_task("m")],
                                  "ansible/roles/r/meta/argument_specs" + ext: {"argument_specs": {}}},
                 play=[{"hosts": "localhost", "roles": ["r"]}])
