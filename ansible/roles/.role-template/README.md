# role template

Copy this directory to `roles/<device-class>` when adding a new managed device
class. Required structure (matches every real role in this repo):

```
roles/<class>/
├─ tasks/
│  ├─ check.yml     # read-only liveness probe — called by ping.yml  (REQUIRED)
│  └─ backup.yml    # read-only config capture into {{ config_backup_dir }}  (REQUIRED)
└─ README.md        # entrypoints + blast radius (per the §9 access-chain header)
```

Contract (verified by the scaffolding audit):
- Every role exposes **both** `check` (liveness, dispatched by `ping.yml`) and
  `backup` (capture, dispatched by `backup-configs.yml`) — both via `device_role`.
- `backup.yml` (state-touching captures) carries the four-line access-chain header
  (PLAN.md §9) and `no_log: true` on any task whose output could contain secrets.
- Roles never reference each other. Optional `tasks/main.yml` (apply intended
  config) is added when the role graduates from stub to implemented.

A new role also needs a `modules/<key>/module.yml` pointing at it, and the key
listed in `instance/fleet.yml`. See [modules/README.md](../../../modules/README.md).
