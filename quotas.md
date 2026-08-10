# Service quotas and limits

Every AWS quota this design touches, whether it binds, and what to do when it does.

All figures are **per account per Region** unless stated otherwise, and were verified against
AWS documentation. Quotas change — re-check with
`aws service-quotas list-service-quotas --service-code <cloudwatch|ssm|ec2>` before scaling.

---

## ⚠️ The quota that binds first — and it is not the one you would expect

### SSM managed nodes per account per Region: **2,400** (adjustable)

> *"Approximate maximum number of nodes managed by Systems Manager (per AWS account per Region).
> … We do not recommend scaling past this without a limit increase because **instances could stop
> communicating with Systems Manager**."*

**This is the real first ceiling of the whole design, and it was previously undocumented here.**

An instance that stops communicating with SSM is not merely unmanaged — it becomes
**unreachable by Ansible**, so its agent config can never be updated. It would keep publishing
metrics (the CloudWatch agent is independent), but it would silently drop out of configuration
management.

Two properties make this worse than a normal quota:
- **It degrades rather than erroring.** There is no clean rejection; nodes just stop reporting.
- **Utilization is measured from `UpdateInstanceInformation` calls in a 5-minute window**, so
  staggered launches can mask it and you may exceed 100% utilization without noticing.

**Mitigation:** request an increase *before* approaching 2,400 per account/Region, and monitor
utilization in Service Quotas. Note this is a **per-account** limit, so an estate of many
accounts each holding a few hundred instances is unaffected — it binds on large single accounts.

---

## CloudWatch

| Quota | Default | Adjustable | Binds here? |
|---|---|---|---|
| **Metrics Insights alarms per Region** | **200** | **No** ⚠️ | **Yes — see §Alarm arithmetic** |
| Metrics per Metrics Insights query | 10,000 | No | Yes — ~3,300 VMs at 3 mounts |
| Time series returned per query | 500 | No | Yes — drives grouping by instance, **verified §below** |
| Alarm evaluation window | last 3 hours | No | Yes — blocks trend alarming |
| Contributors reported in ALARM | 100 | No | Cosmetic **only because the alarm names nothing anyway** — see below |
| Metrics in a **metric math** alarm | 10 | **No** | Fatal — why metric math was rejected |
| Dimensions per metric | 30 | No | No — we use **4**: `InstanceId, path, Environment, fstype` |
| Metrics per `PutMetricData` request | 1,000 | No | No — the agent batches |
| `PutMetricData` payload | 1 MB | No | No |
| Standard alarms per Region | 5,000 | Yes | Not used — we use Metrics Insights alarms |
| Dashboards (free) | 3 | — | No — we use 1 |
| High-resolution data in Metrics Insights | Unsupported | No | No — we collect at 60s |

**The 200-alarm limit is explicitly `Adjustable: No`** in the CloudWatch quota table. That is
unusual and it matters, because it cannot be raised by request.

### Alarm arithmetic — where the ceiling actually is

Two quotas interact, and the interaction is the constraint:

- To escape the **10,000-metric query limit** you shard alarms into narrower scopes.
- But each shard consumes alarms against the **non-adjustable 200-per-Region limit**.

At 3 alarms per account-environment pair (warn + critical + `PARTIAL_DATA` guard):

```
200 alarms ÷ 3 per scope ≈ 65 account-environment scopes per Region
```

⚠️ **That figure is arithmetic from two documented quotas, not a documented combined limit.**
Treat it as a number to sanity-check, not a hard AWS boundary.

**When you approach it:** consolidate scopes (drop the per-environment split), drop the guard
alarm, use composite alarms to roll up, or move to a second Region.

### The 500-series cap — where it binds depends entirely on `GROUP BY`

The 500-series return cap is not a fleet-size limit; it is a **contributor-count** limit, and the
alarm's `GROUP BY` decides how many contributors a host produces. Measured live on 2 hosts with
6 filesystems [`tested_findings.md §3`]:

| `GROUP BY` | Contributors per host | 2 hosts produced | 500-cap binds at |
|---|---|---|---|
| `InstanceId` | **1** — mounts collapsed to the fullest | 2 | **~500 instances** |
| `InstanceId, path` | **3** — one per real filesystem | 6 | **~160 instances** |

So the finer grouping costs **3× the contributors and a 3× lower ceiling**. It was expected to buy
something in return — naming the breaching filesystem in the alert — and it **buys nothing**: the
alarm carries no identity at any grouping (`limitations.md §1.5`). `GROUP BY InstanceId` is
therefore correct on both axes, and this is the empirical basis for the choice rather than the
projected 3,000-contributor arithmetic in `alternatives.md §8`.

Two consequences worth carrying forward:

- **Past the cap, `ORDER BY` is a correctness control, not a presentation one.** It decides *which*
  500 series are evaluated, so `ORDER BY MAX() DESC` is what guarantees the fullest disks are the
  ones seen. Ascending order would silently evaluate the emptiest.
- **The 100-contributor `StateReason` cap is cosmetic only because nothing useful is in
  `StateReason` to begin with.** A firing alarm reports *"1 out of 7 time series evaluated to
  ALARM"* and an otherwise-empty `StateReasonData`, so truncation at "100+" loses nothing that was
  not already absent.

### The silent-degradation risk
Past 10,000 matched metrics an alarm sets `EvaluationState: PARTIAL_DATA` and **keeps reporting a
state derived from incomplete data** — healthy-looking while monitoring part of the fleet. The
guard alarm exists specifically because this failure is otherwise invisible.

⚠️ **A second, closely related silent failure is not a quota at all but behaves like one.** If the
alarm's `SCHEMA()` clause does not name the metric's exact dimension set, the query matches **zero**
series and — with `TreatMissingData: notBreaching` — the alarm reports green **`OK` forever**, not
`INSUFFICIENT_DATA` [`tested_findings.md §2`]. So the same *"looks healthy, monitors nothing"*
outcome as `PARTIAL_DATA` is reachable through a one-word config error, and no guard alarm catches
it. Worth mentioning here because anyone auditing this design's silent-degradation modes from the
quota table alone would miss it.

---

## Systems Manager

| Quota | Default | Adjustable | Binds here? |
|---|---|---|---|
| **Managed nodes per account/Region** | **2,400** | Yes | **Yes — first real ceiling** ⚠️ |
| State Manager associations | 2,000 | — | No |
| Associations targeting a single node | **20** | No | No — we use 0–1 |
| Concurrent Automation executions | **100** | Yes (to 500 via adaptive concurrency) | **Possibly — see below** |
| Queued Automation executions | 1,000 | — | Possibly |
| `StartAssociationsOnce` API | 2 TPS | — | No |
| `CreateAssociation` API | 3 TPS | — | No |
| Hybrid-activated machines | 1,000 standard | — | N/A — AWS-only design |

### ⚠️ Automation concurrency and the remediation storm

**100 concurrent Automation executions** is a real exposure for the 90% remediation path.

A correlated event — a bad deployment filling logs across a fleet — could breach the critical
threshold on hundreds of instances at once. EventBridge would then attempt one Automation
execution per breaching instance, and past 100 concurrent they queue (up to 1,000) and then fail
with `AutomationExecutionLimitExceeded`.

**Three mitigations already in the design, plus one gap:**
1. ✅ The alarm is **per account per environment**, so a breach fans out per scope rather than
   fleet-wide at once.
2. ✅ EBS itself throttles: **6 hours minimum between modifications** and **4 per 24 h** per
   volume, so repeat attempts on the same volume are rejected cheaply.
3. ✅ `DiskAutoGrow=true` is opt-in, so only tagged volumes are eligible.
4. ⚠️ **Not mitigated:** first-time correlated breach across many *distinct* opt-in volumes.
   Adaptive concurrency (raising to 500) or an SQS-buffered invocation would address it. Recorded
   as a limitation rather than solved.

---

## EBS — remediation constraints

| Quota | Value | Adjustable | Consequence |
|---|---|---|---|
| Wait between volume modifications | **6 hours** | No | Runbook checks history and aborts if too recent |
| Modifications per volume per 24 h | **4** | No | Acts as a circuit breaker |
| Concurrent modifications per volume | 1 (sequential) | No | Must reach `completed` first |
| Volume shrink | **Impossible** | — | **Every growth is irreversible** → opt-in + size ceiling |
| Max gp3 volume size | 16 TiB | — | Our ceiling parameter is far below |
| Snapshots per account | 100,000 | Yes | Remediation snapshots accumulate — see below |

**Snapshots are the quiet cost.** They are **not stack resources**, so they survive teardown and
keep billing. Audit with
`aws ec2 describe-snapshots --filters Name=tag:Project,Values=disk-monitoring`.

---

## Cross-account observability (OAM)

| Quota | Value | Adjustable | Binds here? |
|---|---|---|---|
| Source accounts per sink | 100,000 | — | No |
| Monitoring accounts per source account | **5** | No | No — we use 1 |
| Sinks per account per Region | **1** | No | No |
| Cross-Region sink/link | **Not supported** | — | **Yes — forces per-Region deployment** |

---

## CloudFormation StackSets

| Quota | Default | Adjustable | Binds here? |
|---|---|---|---|
| Stack instances per StackSet | 10,000 | Yes | No |
| Concurrent StackSet operations | 1 per StackSet | No | Large orgs deploy in waves |
| Stacks per account/Region | 2,000 | Yes | No |
| Resources per stack | 500 | No | No — largest template is 20 |
| Template body (S3) | 1 MB | No | No |

---

## Other services

| Service | Quota | Value | Binds here? |
|---|---|---|---|
| **AWS Config** | Rules per Region | 150 | No — we use 2 |
| **EventBridge** | Rules per event bus | 300 | No — we use 4 |
| | Targets per rule | 5 | No — we use 1 |
| | `PutEvents` | 10,000/s | No |
| **Lambda** | Concurrent executions | 1,000 | No — alarm-triggered only |
| **SNS** | Topics per account | 100,000 | No — we use 2 |
| **Organizations** | `ListAccounts` | 20 TPS | No — called once per run |
| **IAM** | Roles per account | 1,000 | No — 5 per workload account |
| | Instance profiles | 1,000 | No — 1 per account |
| | **Instance profiles per instance** | **1** | **Yes** — why one profile carries both policies |
| **VPC** | Interface endpoints per VPC | 50 | No — we use 4 |
| | Gateway endpoints per VPC | 20 | No — we use 1 |
| **EC2** | Tags per resource | 50 | No — we use 2–3 |

---

## Summary — the four quotas that actually matter

Ordered by which binds first as the estate grows:

| # | Quota | Value | Adjustable | Effect when reached |
|---|---|---|---|---|
| 1 | **SSM managed nodes per account/Region** | 2,400 | Yes | Nodes **silently stop communicating** — unreachable by Ansible |
| 2 | **Metrics Insights alarms per Region** | 200 | **No** | Cannot create further alarm scopes; ~65 account-environment pairs |
| 3 | **Metrics per query** | 10,000 | No | `PARTIAL_DATA` — **silent** partial coverage |
| 4 | **Concurrent Automations** | 100 | Yes | Remediation queues, then fails on a correlated breach |

**#1 is the one to watch, and it is per account** — so a wide estate of modest accounts is safe
while a single large account is not. **#2 is the one you cannot buy your way out of.**

### Recommended quota alarms
Service Quotas supports CloudWatch alarms on utilization. Worth setting for:
- SSM managed nodes at **80%** of 2,400 (~1,900) per account/Region
- Metrics Insights alarms at **80%** of 200 (160) in the monitoring account
- Concurrent Automation executions at **80%** of 100

That turns three silent-degradation failures into alerts — consistent with the rest of the
design's stance that a limit you cannot see is worse than one you can.

---

## See also

- `limitations.md` — functional gaps and deferred work
- `alternatives.md` — options considered and why they lost
- `docs/06-cost.md` — how cardinality drives cost, which interacts with quota #3
