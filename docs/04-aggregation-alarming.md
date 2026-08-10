# 04 — Aggregation, Alarming, Remediation

Steps 1–3 put a metric in every account. This step answers what to do with 50 accounts'
worth of it: **where does it get looked at, what decides something is wrong, and what
happens next.**

## 4a — Centralization via OAM

### The problem

CloudWatch is **regional and per-account**. A metric published in account
`111122223333` exists only there. Fifty accounts therefore means fifty consoles, and
alarms would have to be created — and maintained — **in all fifty**. Nothing in
CloudWatch spans accounts by default.

### Decision: CloudWatch Observability Access Manager (OAM)

The monitoring account hosts an `AWS::Oam::Sink`. Each workload account creates an
`AWS::Oam::Link` pointing at it. From then on the monitoring account can query that
account's metrics as if they were local.

**Bilateral consent by design.** The sink policy says who *may* attach; the link says
who *does*. Neither alone suffices:

| Half | Lives in | States |
|---|---|---|
| `Oam::Sink` policy | Monitoring account | Which principals **may** link |
| `Oam::Link` | Workload account | That this account **does** link |

So **account isolation is preserved** — the monitoring account cannot reach in and
harvest data from an account that has not opted in — and **both halves are auditable**,
each as a resource in its own account's stack.

The sink policy is scoped with **`aws:PrincipalOrgID`** rather than an enumerated
account list, and filtered to `AWS::CloudWatch::Metric` in the `CWAgent` namespace. The
consequence that matters operationally: **a new account links without any policy edit**.

```yaml
Condition:
  StringEquals:
    aws:PrincipalOrgID: !Ref OrganizationId      # org membership, not a list
  ForAllValues:StringEquals:
    oam:ResourceTypes:
      - AWS::CloudWatch::Metric                  # metrics only — no logs, no traces
```

Why OAM specifically:

| Property | Consequence |
|---|---|
| Metric sharing is **free** | Centralization adds no per-metric cost |
| **No data movement** — query access, not copies | Nothing to fall behind, no backfill, no pipeline to operate |
| Native alarming and dashboards | Cross-account alarms and widgets work with no extra machinery |
| **100,000 source accounts** per sink | 50 accounts is not near any boundary |

---

### Rejected — CloudWatch Metrics Centralization

Organization-level rules that **physically replicate** metrics into a destination
account, which then owns the copy. GA June 2026 — genuinely the newer, more
purpose-built answer to "centralize metrics."

**Deciding factor**, verbatim: *"all metrics from source accounts are centralized.
Selective metric filtering is not supported at this time."* We could not scope to
`CWAgent`, so **every** custom, EMF, and OTLP metric in every account would replicate.

| Consequence | Detail |
|---|---|
| **Metric quota risk** in the destination | The account's metric count is driven by metrics unrelated to this project |
| **Excludes AWS service metrics** | No `AWS/EC2`, no `AWS/EBS` — so central correlation of CPU or EBS I/O alongside disk fullness is impossible |
| Headline advantage unused | Its differentiator is **cross-Region**; at single-Region scope that buys nothing |
| Heavier governance | Organizations trusted access plus a service-linked role, configured from the management or delegated-admin account |
| Very new | ~2 months GA at time of writing |
| A pipeline to watch | Replication health is only `HEALTHY` / `UNHEALTHY` / `PROVISIONING`, plus `CentralizationError` metrics |

**It becomes the right answer for multi-Region** — replication is how you get metrics
into one Region for alarming, and OAM cannot do that. See future work.

### Rejected — cross-account metric push

Give each instance credentials to call `PutMetricData` directly into the monitoring
account. No sink, no links, no OAM at all. Superficially the simplest possible design.

**Decisive objection: blast-radius inversion.** Every instance in every account would
hold credentials to write into central monitoring. One compromised instance can then
flood or poison monitoring **for the entire estate** — and **monitoring data is exactly
what an attacker wants to suppress**. The design would hand that capability to the
least-trusted tier of the system.

Also:

- **Local visibility lost.** The metrics no longer exist in the workload account, so
  account owners cannot see their own instances, and central monitoring becomes a
  bottleneck for every local question.
- **No trustworthy attribution.** With OAM, `AWS.AccountId` is applied **by AWS**. Here
  it would be a dimension the *instance* sets — and therefore **spoofable by a
  compromised host**.
- **Fights the agent's design.** The CloudWatch agent publishes locally via the instance
  role; cross-account publishing means credential plumbing the agent does not want.
- **Concentrates all accounts' metrics against one account's API limits.**

It contradicts the whole model of Step 1: instances hold **minimal, local** permissions.

### Rejected — metric streams → Firehose → S3 / OpenSearch / third-party

Also **moves data**, and pays three times where OAM is free: per metric update, **plus**
per-GB Firehose, **plus** destination storage. You own the pipeline — delivery failures,
buffering, retries, backfill — and you **rebuild alarming** in the destination, because
CloudWatch alarms do not evaluate data that has left CloudWatch.

Right only for retention beyond CloudWatch's 15 months, or correlation with non-AWS
telemetry.

### Rejected — per-account alarms only

Each account alarms on its own metrics and publishes to a shared SNS topic. This
sidesteps the metric ceiling entirely and needs no cross-account read at all. But it
gives alerts with **no unified view** — failing the brief's "centralize and present" —
multiplies alarm management by N accounts, and permits no cross-account ranking ("which
five instances in the estate are worst?"). A reasonable **complement**, not a
replacement.

### Rejected — legacy `CloudWatch-CrossAccountSharingRole`

Pre-OAM cross-account sharing. It enables dashboards and cross-account alarms, but
**not** the unified Metrics Insights query surface that 4b depends on, and it requires
more per-account IAM to manage. Superseded.

---

### Cross-account works; cross-Region does not

These are **independent axes**, and conflating them is easy — "cross-account alarms"
sounds like the same problem as "cross-Region alarms." It is not.

Cross-account alarming is exactly what OAM provides: **one alarm covers all linked
accounts.** The precise rule: *"When creating resources on cross account data like
CloudWatch Alarms, the resource must be created in the same **Region** for which the
telemetry resides"* — **same Region, not same account.**

Two distinct cross-account alarm mechanisms exist, and only one fits:

| Mechanism | Cross-account? | Fit |
|---|---|---|
| OAM + Metrics Insights query alarm | Yes | **Chosen** — query-based, auto-adopts new resources |
| Legacy `PutMetricAlarm` + sharing role | Yes | Targets **one named metric per alarm**, reintroducing exactly the per-VM sprawl 4b exists to avoid |

### OAM limitations

- Sink and link must be **same-Region**.
- Max **5 monitoring accounts** per source account.
- **Resource tags are not shared** — so "which team owns this instance?" needs another
  mechanism (which is part of why `Environment` is a metric *dimension*; see 4b).
- **Not retroactive.** Sharing starts at link creation; there is no historical import.

---

## 4b — The alarms

### Granularity: one warning + one critical alarm, per account, per environment

Each alarm is scoped with `WHERE AWS.AccountId = '…' AND Environment = '…'`.

| Environment | Warning | Critical |
|---|---|---|
| prod | 80% | 90% |
| dev / sandbox | 90% | 95% |

**Differentiated thresholds are the point.** A single global threshold is either too
noisy for dev — where a build box sitting at 85% is normal and healthy — or too late for
prod. Scoping the query is what makes two thresholds possible from one design.

### The query

```sql
SELECT MAX(disk_used_percent)
FROM SCHEMA("CWAgent", InstanceId, path, Environment, fstype)
WHERE AWS.AccountId = '111122223333' AND Environment = 'prod'
GROUP BY InstanceId
ORDER BY MAX() DESC
```

**Reading it outward:**

- `SCHEMA("CWAgent", InstanceId, path, Environment, fstype)` is a **filter describing
  which metrics exist**. Every dimension the agent emits must be named, or **nothing
  matches**. Writing `SCHEMA("CWAgent", InstanceId)` asks for metrics carrying *only* that
  dimension — none exist — and the query returns zero series.
  **All four dimensions are mandatory, `fstype` included**: `drop_device: true` removes
  `device` but leaves `fstype`, verified live (`tested_findings.md` §2). The
  three-dimension form that earlier drafts of this document used matches nothing.
- `WHERE` scopes the alarm **and bounds its cost**, because billing follows what the
  filter matches.
- `GROUP BY InstanceId` — `path` is deliberately **absent**, so all mounts on a host
  collapse into one contributor: 1,000 contributors instead of 3,000, comfortably inside
  the 500-series return cap once split per account.
- `MAX` gives each contributor its **fullest mount**.
- `ORDER BY MAX() DESC` sorts fullest first.

**`SCHEMA` vs `GROUP BY`** is the distinction to hold onto: the first **describes the
data**, the second **aggregates it**. So `path` and `fstype` appearing in `SCHEMA` are *not
choices* — they are facts about what the agent publishes. `path` being absent from
`GROUP BY` **is** a choice, and a deliberate one; §3 of `tested_findings.md` vindicates it,
because finer grouping buys nothing (see below).

---

### Why nothing simpler works — three dead ends

**1. One alarm per VM per mount.** 3,000 alarms at 1,000 VMs × 3 mounts, created on
launch and deleted on terminate, reconciled constantly against Auto Scaling churn. The
fatal flaw is not tedium — it is that a **missed creation leaves an instance silently
unmonitored**. No error, no red state, nothing to notice. Discovered during the outage.

**2. `SEARCH()` inside an alarm.** *"A search expression cannot be used within an
Alarm."* The reason is structural: an alarm must resolve to **one deterministic state**
to decide whether an action fires, and `SEARCH` returns an arbitrary, unordered number of
series.

**3. Metric math.** *"Alarms based on metric math expressions can reference a maximum of
10 metrics. This is a hard limit that cannot be increased."* Ten metrics is about **three
instances**.

### Why Metrics Insights is the only fit

- It is the **sole alarm type accepting multi-series queries** — *"You can create an
  alarm on any Metrics Insights query, including queries that return multiple time
  series."*
- It **auto-adopts resources**: *"the alarm automatically adjusts as resources are added
  to or removed from your monitored group… any resource that matches your query
  definition… joins the alarm monitoring scope."* So **a new instance needs no alarm
  created** — which eliminates dead-end 1's entire failure mode rather than mitigating it.
- Each returned series is a **contributor** with its own state, so one alarm resource
  performs N independent evaluations and fires if **any** contributor breaches.

Both `GROUP BY` and `ORDER BY` are **mandatory** for multi-series alarms.

---

### Load-bearing details

- **`ORDER BY` is correctness, not cosmetics.** Past 500 series it decides *which* series
  are evaluated. Without it *"you can't control which 500 matching metrics are
  returned"*; descending order guarantees the **fullest disks are the ones seen**.
- **Never `AVG` across instances.** 999 hosts at 20% plus one at 100% averages to ~20%:
  the alarm never fires while a disk is completely full. The same trap applies within a
  host — 45 / 94 / 20% averages to 53%, sailing under an 80% threshold. **Confirmed live**
  (`tested_findings.md` §1): `GROUP BY InstanceId` with `MAX` collapsed three mounts to one
  contributor reporting its fullest, which is exactly the intended behaviour.
- **M-of-N** (warning 3 periods / 2 datapoints; critical 2 / 2). A build writing a large
  temp file can breach for a single 5-minute period and then clean up. Requiring 2 of 3
  means the condition must **persist ~10 minutes**. Critical is tightened because at 90%
  you would rather be early.
- **`TreatMissingData: notBreaching`** matters especially here because contributors churn
  constantly. A terminated instance stops reporting, and that silence **must not read as
  a full disk** — otherwise every scale-in event alarms. **But understand what it costs**:
  it cannot distinguish "this instance went away" from "this query matches nothing at all",
  so a broken query reports `OK` rather than `INSUFFICIENT_DATA`. See the dimension
  contract below — that is not a hypothetical, it is what the pilot observed.

### Granularity is free; overlap is not

Billing follows **what the filter matches**: *"a Metric Insights query alarm that
references a query whose filter matches ten metrics incurs ten metrics analyzed cost per
hour."*

So partitioning 3,000 metrics across 20 alarms costs the same as 2 alarms over all 3,000
— roughly **$600/month at 1,000 VMs**. **What does cost more is *overlapping* scopes**,
where the same metrics are billed once under each alarm that matches them. **Clean
partitioning is the rule**: every metric in exactly one alarm's scope.

Quota: **200 Metrics Insights alarms per Region** (raisable). 10 accounts × 2
environments × 2 thresholds = **40**, comfortable; revisit past ~50 accounts.

Operational trade-off: an estate-wide event now pages **once per scope** rather than
once. Composite alarms can roll them up, and cost a flat rate regardless of how many
alarms they evaluate.

### Hard limits

| Limit | Value | Meaning here |
|---|---|---|
| Metrics per query | **10,000** | ≈3,300 VMs at 3 mounts |
| Series returned | **500** | Why `GROUP BY InstanceId`, not `InstanceId, path` |
| Evaluation window | **last 3 hours only** | No long-horizon expressions |
| Contributors in ALARM | **100** | `StateReason` shows "100+" |
| Metrics Insights alarms | **200 / Region** | 40 in use |
| Resolution | **no high-resolution data** | 60s is the floor |

**The ceiling fails by silent degradation.** Past it, the alarm sets
`EvaluationState: PARTIAL_DATA` and **keeps reporting a state derived from incomplete
data** — healthy-looking while monitoring only part of the fleet. Hence the metric-count
guard alarm in the template: **without it, the design reproduces the exact
silent-blind-spot flaw that disqualified per-VM alarms.** Mitigation once crossed: shard
by account group, or by an `Application` dimension.

### ⚠️ The guard alarm's threshold is DATAPOINTS, not metrics

The guard's expression is `SELECT COUNT(disk_used_percent) FROM SCHEMA(…)` with **no
`GROUP BY`** — because you cannot count *distinct metrics* from inside an alarm expression.
`COUNT()` counts **datapoints**. So the threshold has to be expressed in the same unit:

```
threshold = metrics_target × (Period / collection_interval)
```

At `Period: 3600` and the agent's 60-second interval that is 60 datapoints per metric per
hour, so a **3,000-metric warning line is 180,000 datapoints — not 3,000.**

An earlier version of the template used **400**, which is what a metric count looks like if
you forget the conversion. **Roughly 7 metrics exceed 400 in an hour**, so the guard would
have been in `ALARM` from first deploy at any real fleet size. That is worse than having no
guard: **a guard that cries wolf gets muted, and a muted guard leaves the silent
degradation mode completely unwatched.** Recompute the number if `Period` or the collection
interval changes, and do not "simplify" it back to a metric count.

The **3-hour window** is also why predictive "days until full" cannot be an alarm
expression — the trend it needs is outside what an alarm can see.

### Cost is not higher than per-VM alarms

A common misreading, worth correcting explicitly. Like for like, both thresholds, 1,000
VMs × 3 mounts:

| Approach | Billing basis | Monthly |
|---|---|---|
| One alarm per VM per mount | 3,000 metrics × 2 thresholds = 6,000 alarms × $0.10 | **$600** |
| Metrics Insights, per account/env | 2 alarms × 3,000 metrics analyzed × $0.10 | **$600** |

**Identical.** So auto-adoption and the absence of lifecycle churn come at **no
premium** — they are free improvements, not a paid upgrade.

And no alarm type is cheaper: standard metric alarms are also $0.10, high-resolution is
$0.30, and composite alarms are flat-rate **but the alarms they reference still bill
separately**. **The only real lever is reducing metric count** — which is Step 3's job
(dropping `device`, filtering filesystem types), not this one's.

---

### Also rejected — filesystem UUID as a dimension

The motivating concern is real: `device` names (`nvme1n1`) genuinely shift across
reboots. But `drop_device: true` in Step 3 already removes that dependency, so UUID
solves a problem already solved. **Note precisely what `drop_device` removes** — `device`,
and nothing else. `fstype` stays (`tested_findings.md` §2), so the emitted set is four
dimensions; the argument against UUID does not depend on which of the survivors remain.

Rejected because a UUID **changes on every reformat or instance replacement**, creating a
**brand-new billable metric** each time while the old one lingers for 15 months. Under
immutable infrastructure — AMI rebuilds, ASG instance refresh — abandoned metrics
accumulate, and all of them are billed. Also: unreadable alerts (`a1b2c3d4-…` instead of
`/var`) and no central predictability for `SCHEMA()`.

**The framing that settles it:** a UUID answers *"is this the same physical
filesystem?"* while disk monitoring asks *"is the thing my application writes to running
out of space?"* — a **mount-point** question. `path` is the operationally meaningful
identifier; UUID is the physically precise one. **Alerting wants meaning.**

### Also rejected — EBS `VolumeId` as a dimension

**No fullness metric exists.** `AWS/EBS` is entirely I/O — a full disk generates
near-zero EBS activity, because nothing can be written. And the agent runs **in the OS**,
where there is no concept of `vol-xxx`.

The mapping is also not 1:1:

| Layout | Relationship |
|---|---|
| LVM / RAID | Many volumes → one filesystem |
| Partitioning | One volume → many filesystems |
| Instance store | No `vol-` id at all |
| tmpfs | No volume whatsoever |

Volume identity is therefore resolved **at alert time** (4d), not carried in every
datapoint.

### Also rejected — per-application tag scoping

**`WHERE tag.X` does not work for `CWAgent`.** CloudWatch's "resource tags for telemetry"
enrichment covers only an allowlist — roughly 70 `AWS/*` namespaces plus
`ContainerInsights`, `Glue`, `LambdaInsights`, and `CloudWatchSynthetics` — and
`CWAgent` is **not on it**. So the intuitive approach fails **silently**: a valid query
that matches nothing.

Scope must therefore be a metric **dimension**. Which is exactly what `Environment` is,
and why Step 3 emits it as an `append_dimensions` value rather than relying on an EC2 tag.

### ⚠️ Step 3 ↔ Step 4 dimension contract

`SCHEMA()` must match the agent's emitted dimensions **exactly** — for this agent version
and configuration, all four of `InstanceId, path, Environment, fstype`. Verify before
finalizing:

```bash
aws cloudwatch list-metrics --namespace CWAgent --metric-name disk_used_percent
```

This check is not optional paranoia: **AWS docs disagree** on whether the dimension is
`Partition` or `device` / `fstype` / `path`. Nothing enforces the contract automatically, so
the agent template and this template must be edited as a pair — see doc 03.

**And the runtime failure is not the one you would expect.** A mismatch does *not* leave
the alarm in `INSUFFICIENT_DATA`. Because this design sets `TreatMissingData:
notBreaching`, a query matching zero series resolves to a green **`OK`**, forever —
verified live (`tested_findings.md` §2):

```
State  : OK
Reason : "No time series were returned by the query. Treat missing data is configured as [NonBreaching]."
```

| | Expected on a mismatch | Actually observed |
|---|---|---|
| Alarm state | `INSUFFICIENT_DATA` | **`OK`** |
| `InsufficientDataActions` | Fires — someone is told | **Never fires** |
| Dashboard appearance | Grey / unknown | **Green** |

**So `InsufficientDataActions` is not the compensating control for this failure.** It is
still worth configuring for a fleet-wide agent outage, but the *dimension mismatch* case it
was chosen to catch slips straight past it. The controls that actually catch it are the CI
dimension-contract test (before deploy) and the Phase 6 `list-metrics` gate in doc 07
(after deploy) — which is why doc 07 refuses to arm alarms until that call has been made.

---

## 4c — Dashboard

**Alarms decide; dashboards explain.** Neither replaces the other — an alarm cannot show
a trend, and a dashboard cannot wake anyone at 3am.

| | Alarm | Dashboard |
|---|---|---|
| Direction | **Pushes to you**, 24/7 | **You go to it** |
| Output | A state and an action | A picture |
| Question answered | *Is anything wrong right now?* | *Where exactly, and where is this heading?* |
| Cost | ~**$600/month**, evaluating continuously | **$0** — first three dashboards free, console queries free |

### Why `SEARCH()` is banned in alarms but legal here

The **same rule**, not an inconsistency. An alarm must produce **one definite state** to
decide whether an action fires. A widget has no such requirement — it just draws whatever
it is given, however many series, in any order.

**Consequence: the two use different queries over the same data.** The dashboard keeps
`path`, so **the mount-level detail the alarm groups away lives here.** That is why
notifications carry a dashboard deep link, and why per-instance grouping in 4b is
*acceptable* rather than *lossy* — the information is not discarded, it is looked up
somewhere else.

```
SORT(SEARCH('{CWAgent,InstanceId,path,Environment,fstype} MetricName="disk_used_percent"', 'Maximum'), MAX, DESC, 20)
```

The `{…}` dimension list is subject to the **same exact-set rule as `SCHEMA()`**, so it
carries `fstype` for the same reason the alarms do (`tested_findings.md` §2). A dashboard
widget with the three-dimension list draws an empty graph — which at least *looks* empty,
unlike the alarm, which looks green.

This widget **self-populates**: a new instance's metrics match the search and appear with
no template edit — the dashboard inherits the same auto-adoption property as the alarms.

`SORT(..., 20)` is **load-bearing, not cosmetic**: the 500-series cap applies to `SEARCH`
too, and **`SORT` only orders what was returned**. Bounding the result is what keeps the
widget readable instead of a wall of 500 lines.

### ⚠️ Filtering here is cosmetic; filtering at the agent is the only real filter

Worth stating plainly because the dashboard is where the temptation arises. **There is no
display-time filtering anywhere in this stack** (`tested_findings.md` §5):

- Once published, a metric is **stored and billed for 15 months.** Neither CloudWatch nor
  OAM can un-bill it, and there is no delete API for a custom metric.
- **OAM filters by resource *type*** (`AWS::CloudWatch::Metric`) — not by namespace, not by
  filesystem. A link cannot decline to share junk.
- `WHERE fstype = 'xfs'` in a query, or omitting a series from a widget, hides junk from
  **view** only. The metric still exists and still costs.

**So the agent-side filters in Step 3 have no downstream escape hatch.** That is why doc 06
calls the fstype filter "the difference between a $1,500 and a $17,000 monthly bill" rather
than a tuning knob, and why the denylist gap in `tested_findings.md` §4 mattered enough to
fix: every leaked `vfat` mount is 15 months of billing that no later query can undo.

### Widgets and the question each answers

| Widget | Question |
|---|---|
| Worst 20 mounts, fleet-wide, with 80/90 annotations | *Which filesystems are closest to full, and are they above threshold?* |
| Series count by environment (1h period) | *Is the problem growing, or is one host noisy?* |
| `MAX` grouped by `AWS.AccountId`, ordered desc | *Which account is worst?* — cross-account ranking, the thing per-account alarms cannot do |
| `MAX` grouped by `Environment` | *Is this a prod issue or dev noise?* |
| Alarm-state widget | *What is currently firing?* |
| **Coverage note** | *Are we actually watching everything we think we are?* |

The **coverage** widget states a known gap plainly rather than hiding it: AWS Config
verifies *configuration* (tag present, instance profile attached) but cannot verify the
*outcome* (metrics arriving). An instance whose agent crashed is fully Config-compliant
while sending nothing, and the alarms' `INSUFFICIENT_DATA` fires only if metrics stop
**everywhere** — one silent instance among many leaves them in `OK`. Closing it needs a
check comparing running instances against instances publishing metrics.

---

## 4d — Notification and enrichment

**Two SNS topics**, so warnings and pages route differently — **a warning that pages at
3am trains people to ignore alerts.** Warnings go to chat and email; critical goes to the
paging path and to remediation.

### ⚠️ A fleet alarm carries NO identity — at any `GROUP BY`

The alarm does not name the instance. It says *"12 out of 1000 time series evaluated to
ALARM"*, because a Metrics Insights alarm holds many contributors rather than watching one
metric. And **`VolumeId` can never be a metric dimension** (4b).

The natural assumption is that this is a *grouping* problem — that putting `path` back into
the `GROUP BY` would put the breaching filesystem into the alarm message. **It does not.**
Read directly from a firing alarm in the pilot (`tested_findings.md` §3):

```
StateReason     : "1 out of 7 time series evaluated to ALARM"
StateReasonData : {"version": "1.0", "queryDate": "2026-08-09T22:57:06.458+0000"}
```

No instance, no path, no volume — **only a count**, and a `StateReasonData` payload with no
contributor detail whatsoever. The identity exists in the **query result**; it never reaches
the **alarm**. Grouping changes how the data is aggregated, not what the alarm records
about it.

| | `GROUP BY InstanceId` | `GROUP BY InstanceId, path` |
|---|---|---|
| Contributors (2 hosts, 6 filesystems) | 2 | 6 |
| Detects the breach | Yes | Yes |
| **Names the instance in the alarm** | **No** | **No** |
| 500-series cap binds at | **~500 instances** | ~160 instances |

**So `GROUP BY InstanceId` is correct** — the choice made in 4b, now vindicated
empirically. Finer grouping triples the contributor count, consumes the 500-series budget
three times faster, and returns **nothing** operationally.

### The enrichment Lambda is MANDATORY, not a nice-to-have

This is the consequence, and it deserves stating without hedging. Because no grouping and
no alarm field carries identity, **the Lambda is the only path from "something breached" to
"this volume needs growing."** Without it the alert is unactionable: an operator receives a
count and must go find the breaching host by hand, during an incident, across accounts.

EventBridge on alarm state change → Lambda, which:

1. **Re-runs the Metrics Insights query with `path` in the `GROUP BY`**, recovering exactly
   what the alarm grouped away — and what the alarm could never have carried anyway.
2. **Resolves `path` → EBS volume on the host** (see below — this cannot be done from the
   EC2 API alone).
3. Posts: instance · account · mount · % used · volume id · size · dashboard link.
4. At the critical tier, **invokes the remediation runbook**, supplying the `InstanceId`
   the alarm cannot.

⚠️ **Label parsing is a real trap here.** The re-run returns labels with a **rank prefix**:

```
label = '1 - i-0aaa...aaa /data'      value = 84.55%
```

Splitting on whitespace and reading fields 0 and 1 yields `"1"` and `"-"`. **Parse from the
end** (`parts[-2]`, `parts[-1]`) or strip the `N - ` prefix first (`tested_findings.md` §7).

It reports **all** attached volumes when the mapping is ambiguous (LVM/RAID) and says so,
rather than asserting a single answer. **A scheme assuming 1:1 would mislead precisely on
the hosts where storage is most complex** — the worst place to be confidently wrong.

Note the cost framing: volume identity is resolved **once per alert**, not carried in
every datapoint forever.

### ⚠️ EC2 cannot map a mount to a volume — resolution must run on the host

`ec2:DescribeVolumes` looks like the obvious answer and is not. It reports the
**attachment** device name, and on Nitro instances the guest kernel renames the device
(`tested_findings.md` §7 step 2):

```
EC2 says   : vol-0ccc...ccc  ->  /dev/sdf
guest says : /data           ->  /dev/nvme1n1
```

There is no `/dev/sdf` block device in the guest — only a symlink. **Matching
`Attachments[].Device` against what `findmnt` reports therefore cannot work.** The mapping
has to be resolved **in the guest**, via SSM.

Two on-host methods were verified, and two documented-looking ones do not work at all:

| Method | Result |
|---|---|
| `/sbin/ebsnvme-id <dev>` | **Works** — prints `Volume ID: vol-0ccc...ccc`. AWS-provided, most explicit |
| `/sys/class/nvme/<ctrl>/serial` | **Works** — `vol0ccc...ccc`, needs a `vol` → `vol-` fixup |
| `/sys/block/<disk>/serial` | **Empty on AL2023** — returns nothing at all |
| `nvme id-ctrl` | **Unavailable** — `nvme-cli` is not installed on AL2023 by default |

Note the third row carefully: the working sysfs path is the **controller** path
(`/sys/class/nvme/nvme1/serial`), *not* the block-device path. The distinction is one
directory and the difference between an answer and an empty string.

**Recommended: `ebsnvme-id`, with the controller sysfs path as fallback.** The complete
chain, verified end to end and cross-checked against `DescribeVolumes` — two independent
methods agreed on `vol-0ccc...ccc`:

```bash
SRC=$(findmnt -no SOURCE --target /data)           # /dev/nvme1n1
DISK=$(lsblk -no pkname "$SRC" | head -1)          # parent disk if a partition
[ -n "$DISK" ] || DISK=$(basename "$SRC")
/sbin/ebsnvme-id "/dev/$DISK"                      # -> Volume ID: vol-0ccc...ccc
# fallback: sed 's/^vol/vol-/' < /sys/class/nvme/${DISK%n[0-9]*}/serial
```

`DescribeVolumes` is still called — for **size, type and the ceiling check** — but it is
the *second* step, confirming a volume id the host supplied, not the source of it.

### Manual fallback, retained deliberately

So the Lambda is **not a single point of failure in the alert path** — if it fails, SNS
still fires and the `AlarmDescription` still carries the dashboard link. A responder can
run the same chain by hand, on the host:

```bash
findmnt -no SOURCE --target /data                  # -> /dev/nvme1n1
/sbin/ebsnvme-id /dev/nvme1n1                      # -> Volume ID: vol-0ccc...ccc
aws ec2 describe-volumes --volume-ids vol-0ccc...ccc   # size, type, attachment
```

**`lsblk -o NAME,MOUNTPOINT,SERIAL` is not sufficient** — it is the intuitive one-liner and
it does not yield a usable volume id on AL2023, because the underlying
`/sys/block/<disk>/serial` is empty (`tested_findings.md` §7 step 3). The `ebsnvme-id` call
is the one that works.

### Two further notes

- **Contributor cap.** Beyond 100 breaching contributors, `StateReason` shows *"100+ time
  series evaluated to ALARM"*. Fine for alerting — but the message is **not a complete
  inventory**, and should not be read as one. Given that even a single contributor is
  reported only as a count, treat `StateReason` as a *trigger*, never as data.
- **Notify on `INSUFFICIENT_DATA` too, not only `ALARM`** — but do not rely on it. That
  state means the alarm is evaluating nothing, which is worth paging on. However, the
  failure it was chosen to catch — the Step 3 dimension mismatch — **does not produce
  `INSUFFICIENT_DATA`**: with `TreatMissingData: notBreaching` it produces a green `OK`
  (see 4b, and `tested_findings.md` §2). Keep the action for the fleet-wide-agent-failure
  case; look to CI and the Phase 6 gate for the mismatch case.

---

## 4e — Remediation at 90%

### Flow

```
critical alarm (90%)
  → EventBridge rule on ALARM state change
  → enrichment Lambda            ← resolves InstanceId + path + volume id
  → SSM Automation runbook, invoked PER BREACHING INSTANCE
       with DryRun: 'false' passed explicitly
  → pre-flight guards
  → SNAPSHOT
  → ModifyVolume
  → poll DescribeVolumesModifications
  → notify
```

**The Lambda sits in the middle by necessity, not by preference.** An EventBridge rule
cannot invoke the runbook directly with a target, because **the alarm event contains no
`InstanceId`** — only a count of breaching contributors (§4d). Something has to re-run the
query and resolve identity before a runbook that takes `InstanceId` and `MountPath` can be
called at all. That something is the enrichment Lambda, which is why it is on the critical
path rather than beside it.

Two consequences worth pinning down:

- **The Lambda invokes once per breaching instance**, not once per alarm. One alarm
  transition can mean twelve volumes.
- **`DryRun: 'false'` must be passed explicitly.** The document **defaults to `'true'`**
  (deliberately — see below), so an invocation that omits the parameter evaluates every
  guard, reports success, and **grows nothing**. That is the correct default for a human
  running the document by hand and the wrong one for the automated path, and the failure is
  silent in the reassuring direction: a green execution history with no volume changed.

An SSM Automation document rather than a Lambda for the *action* itself: it is the native
home for multi-step, guarded, auditable AWS-side actions, with execution history per run.

### Verified AWS constraints, and what each forces

| Constraint | Consequence for design |
|---|---|
| *"must wait at least 6 hours between modifications"* | Check modification history; **abort if too recent** — turning an API failure into a clean, explainable skip |
| **4 modifications per rolling 24h** per volume | Acts as a **circuit breaker** — a runaway loop cannot grow a volume indefinitely |
| A modification must reach `completed` before the next | **Sequential only** — detect `modifying`/`optimizing` and skip, no concurrent attempts |
| **Volumes can only grow, never shrink** | **Every action is irreversible** → opt-in `DiskAutoGrow=true` tag **plus** a size ceiling (`MaxVolumeSizeGiB`) |
| Modification is online on current-generation instances | **No stop or detach needed** — remediation is non-disruptive |
| *"You can't use these steps for partitions, the root file system, RAID devices, or Logical Volume Manager (LVM)"* | Those are **out of scope** — detect and **notify a human** instead of guessing |

Two more choices follow from irreversibility:

- **Snapshot before modifying** is AWS's documented best practice and, because volumes
  cannot shrink, **the only rollback that exists.**
- **`DryRun` defaults to `true`.** Irreversible actions should be **opt-in per run**, not
  the default behaviour of a document someone invokes to see what it does.

The guards and the modification live in **one `aws:executeScript` step** so they share a
single view of state. Splitting them would risk acting on stale data: the 6-hour and
4-per-24h checks must reflect the moment of modification, not a moment several steps
earlier.

### ⚠️ Two-step reality, stated clearly

`ModifyVolume` grows the **volume**. **The filesystem must then be extended** —
`growpart` plus `resize2fs` or `xfs_growfs` — before the OS can use the new space. Until
then, `df` reports the same 90%.

That OS-side step is **deferred**, so **the runbook notifies rather than claiming the
problem is solved.** **Do not treat a successful run as resolution.** Both the
`AlarmDescription` and the outcome notification say so explicitly, because a remediation
that silently half-works is worse than one that does nothing.

### Deferred refinements worth knowing

- **Reclaim space before growing.** A disk at 90% is often 90% logs, and growing the
  volume makes a **permanent cost commitment to a recurring problem**. AWS Managed
  Services' own disk remediation cleans up first for exactly this reason.
- **An expansion counter.** `LastAutoGrowth` tags are written today; a count is the
  signal that matters — **repeated growth of one volume means an application leak needs
  fixing rather than feeding.**

---

## Files

- [`cloudformation/00-monitoring-account.yaml`](../cloudformation/00-monitoring-account.yaml) — OAM sink, org-scoped sink policy, warning and critical SNS topics
- [`cloudformation/10-workload-iam.yaml`](../cloudformation/10-workload-iam.yaml) — per-account OAM link, `DiskMonitoringReadRole`
- [`cloudformation/20-alarms-dashboard.yaml`](../cloudformation/20-alarms-dashboard.yaml) — warning and critical Metrics Insights alarms, PARTIAL_DATA guard, remediation EventBridge rule
- [`cloudformation/30-dashboard.yaml`](../cloudformation/30-dashboard.yaml) — cross-account dashboard, `SORT(SEARCH(...))` widgets, coverage note
- [`lambda/enrich_disk_alarm.py`](../lambda/enrich_disk_alarm.py) — re-query with `path`, cross-account `DescribeVolumes`, enriched notification
- [`ssm-documents/DiskSpace-GrowVolume.yaml`](../ssm-documents/DiskSpace-GrowVolume.yaml) — guards, pre-growth snapshot, `ModifyVolume`, outcome notification
