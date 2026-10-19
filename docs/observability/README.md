# docs/observability/ — design records of the observability and capability track

This directory holds the design records of the observability and capability track: how telemetry, dashboards,
logging and the secondary-capability dialog were designed. They are **history**. Each says so on its first line,
and says whether it was built, superseded or parked. Where a record disagrees with the code or with
[SECURITY.md](../../SECURITY.md), the code and SECURITY.md are right.

Two documents here are **current references**, not history:

| Document | What it describes |
|---|---|
| [secondary-capability-dialog.md](secondary-capability-dialog.md) | the shipped seam every secondary capability (telemetry, backup, logging) rides |
| [logging-capability-vector.md](logging-capability-vector.md) | the built logging capability |

What describes the tree today: [telemetry/README.md](../../telemetry/README.md),
[capabilities/README.md](../../capabilities/README.md), [prometheus/README.md](../../prometheus/README.md) and
[docs/logging-architecture.md](../logging-architecture.md). The master design that ties these records together is
[docs/observability-onboarding-flow.md](../observability-onboarding-flow.md), itself history.
