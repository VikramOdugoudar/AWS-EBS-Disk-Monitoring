# 05 — Scalability

## The requirement

The brief asks: *"How will your solution handle growth as more accounts or VMs are added
over time?"*

That is five distinct growth events, and each one *should* be **zero-touch** — no file to
edit, no list to append to, no runbook to follow. Three of the five are:

| # | Growth event | Zero-touch? |
|---|---|---|
| 1 | **A new instance launches** | Yes — launch template + event-driven enrollment |
| 2 | **A new account joins** | Yes — one action: move it into the OU |
| 3 | **A new Region comes into use** | **No** — one-time per Region, then zero-touch within it |
| 4 | **An instance that *was* monitored *stops* being monitored** | Yes — and this is the one easily missed |
| 5 | **A new volume is mounted on an already-monitored instance** | **No** — needs a re-run, and no event fires |

Event 4 is the interesting one. Growth is usually framed as "does it keep up when things
are added", but the failure that actually hurts is an instance that **silently leaves
coverage** while everything reports healthy. That is the thread running through this
document.

**Event 5 was added after the pilot**, which reproduced it live (`tested_findings.md` §6):
attaching, formatting, mounting and filling a volume to 40% fires **no** EventBridge rule —
it is not a launch, not a tag change, not a new account — so the mount stayed **invisible in
CloudWatch** until the config was re-rendered by hand, with AWS Config still reporting
COMPLIANT throughout. It is event 4 wearing different clothes: coverage silently failing to
extend rather than silently disappearing. See *Configuration drift* below.

---

## A new instance

An instance launches with the **instance profile** and the **`DiskMonitoring=enabled`
tag**, both set in the **launch template** — the same enforcement point. So a correctly
provisioned instance is covered **by construction**, not by anything noticing it.

### Path A — launched correctly tagged

```
launch template sets profile + DiskMonitoring=enabled
   │
   ├─→ instance reaches `running`
   │      └─→ EventBridge Rule 1
   │             └─→ Run Command on the controller
   │                    `ansible-playbook --limit i-abc`
   │                       └─→ dynamic inventory confirms the tag  ✅
   │                              └─→ cw_agent role installs + configures the agent
   │                                     └─→ disk_used_percent flows every 60s
   │                                            └─→ the alarm ADOPTS the instance
   │                                                (no alarm created — see below)
```

### Path B — launched untagged

```
launch template omits the tag
   │
   ├─→ instance reaches `running`
   │      └─→ EventBridge Rule 1 fires
   │             └─→ NOT in tag-filtered inventory → NO-OP  ⚠️
   │
   ├─→ AWS Config `required-tags` detects the missing tag (change-triggered)
   │      └─→ remediation: AWS-SetRequiredTags applies DiskMonitoring=enabled
   │             └─→ EventBridge Rule 2 (TAG CHANGE) fires
   │                    └─→ Run Command on the controller
   │                           └─→ now in inventory → configured  ✅
```

**Config's remediation generates the enrollment trigger.** Without Rule 2 this instance
would **never** be configured: the launch event already fired, found nothing to do, and
**there is no scheduled sweep to catch it later**. The tag arriving late is only useful
because a tag arriving is itself an event.

### The tag is the entire interface

Ansible never holds a host list. Inventory is *derived* from tags at runtime, on every
run — which is what makes enrollment **O(1) to operate at any fleet size**. Tag it and it
is in scope; **un-tag it and it is out**, which is a clean off switch for maintenance
windows and the intended answer to growth event 4.

### The alarm needs no update either

Because coverage is a **Metrics Insights query** rather than a set of per-instance alarms,
a new instance's metrics simply **match the query and become a new contributor**. **No
alarm is ever created, updated, or deleted** — so there is no per-instance lifecycle
management step that could be missed, and no window during which a live instance has no
alarm watching it.

---

## AWS Config makes coverage default-on

Two rules, both **change-triggered** rather than periodic, so correction happens promptly
instead of waiting up to 24 hours for a sweep:

| Rule | Detects | Remediation |
|---|---|---|
| `required-tags` | Instance missing `DiskMonitoring=enabled` | **`AWS-SetRequiredTags`** applies the tag → fires Rule 2 |
| `ec2-instance-profile-attached` | Instance with no instance profile | `associate-iam-instance-profile` — takes effect in minutes, **no reboot** |

**No reboot is needed** for the profile fix because SSM Agent retries registration on a
loop and picks up the new credentials on its own. Per AWS: *"If you use AWS Systems
Manager, then wait for the AWS Systems Manager Agent (SSM Agent) to detect the new IAM
role. Or, restart SSM Agent."* Restarting is the impatient option, not a requirement.

Worth knowing: **a Config remediation action *is* an SSM Automation document.** AWS's own
framing — *"Create your own custom remediation actions using AWS Systems Manager
Automation documents"* — means the commonly proposed "Lambda or SSM document that applies
tags per Config finding" pattern is **exactly this**, except AWS ships the document. There
is **no custom code to write, test, or own**.

Also note that non-compliance is **recorded and auditable even if remediation fails** —
*"If a resource is still non-compliant after auto remediation, you can set the rule to try
auto remediation again."* That matters for a control whose whole purpose is preventing
silent gaps: a failed fix still leaves a visible NON_COMPLIANT finding rather than
nothing.

### Rejected — a custom Lambda for tagging

A Lambda is only needed if the tag **value requires logic**: enable prod but skip sandbox,
derive the value from another tag, honour an opt-out list. `AWS-SetRequiredTags` applies a
**fixed value**, which is precisely what unconditional enrollment wants.

Noted here as **the swap-in if conditional enrollment is ever wanted** — the remediation
*wiring* is identical (Config rule → remediation configuration → SSM document), only the
`TargetId` changes from an AWS document to a custom one. That is a small, contained change
later, which is why the simpler option is right now.

### Rejected — an SCP blocking untagged `RunInstances`

Hard prevention, and it genuinely closes Path B. But it will eventually **block a
legitimate deployment at an inconvenient moment**, and *"you forgot a monitoring tag"* is
thin justification for failing someone's launch during an incident.

**Detect-and-fix beats prevent-and-break** for a monitoring concern. The cost of the
detect path is a few minutes of unmonitored instance; the cost of the prevent path is a
failed deployment. The asymmetry is clear.

### Rejected — targeting all managed nodes instead of requiring the tag

This removes the untagged gap entirely. It also:

- gives **no opt-out** — no way to exclude a host under investigation,
- runs against **every managed node in the account**, including untested and third-party
  appliances,
- means **a bad change hits everything at once** rather than a bounded, tagged subset.

The tag is not bureaucracy; it is the scoping mechanism that makes `serial: 10%` meaningful.

---

## Profile and tag: jointly necessary, neither sufficient

This is the most common *"why isn't this instance monitored?"* cause, so it is worth
stating flatly:

| State | What happens | Symptom |
|---|---|---|
| **Tagged, no profile** | Not an SSM managed node. Inventory targets it, the connection cannot be established | Ansible task **failure** on a host that looks correctly configured |
| **Profiled, no tag** | Is a managed node, but **invisible to inventory** | Total silence — nothing fails, nothing runs, nothing reports |

The second is worse, because nothing complains. Diagnose both with:

```bash
aws ssm describe-instance-information \
  --filters "Key=InstanceIds,Values=i-abc"   # empty result ⇒ not a managed node
```

Both Config rules exist because both halves are required.

### ⚠️ Caveat — auto-remediation can fight IaC

If a Terraform or CloudFormation stack defines an instance **without** the tag:

```
Config remediation adds DiskMonitoring=enabled
   → next `terraform plan` reports drift
      → apply removes the tag
         → Config detects non-compliance again
            → remediation re-adds it …  ⟳
```

A loop that burns API calls and produces a permanently noisy plan. Mitigation, in order:

1. **The launch template is the primary source of the tag** — remediation is only a
   backstop, so in the healthy case it never fires at all.
2. `ignore_changes = [tags["DiskMonitoring"]]` (or the CloudFormation equivalent) on that
   one tag, so the IaC stops asserting ownership of a value it does not set.

---

## A new account

**One step: move the account into the OU.**

A **service-managed** StackSet with `AutoDeployment: Enabled` then creates everything, in
that account, automatically:

- the instance profile and role,
- VPC endpoints (`ssm`, `ssmmessages`, `ec2messages`, **`monitoring`**, plus the **free S3
  gateway endpoint**),
- the cross-account inventory and read roles,
- the OAM link to the monitoring sink,
- both Config rules with remediation,
- EventBridge Rules 1 and 2.

```
account joins the OU
   │
   └─→ StackSet AutoDeployment creates a stack instance
          │
          └─→ stack reaches CREATE_COMPLETE
                 │
                 ├─→ EventBridge Rule 3 (monitoring account)
                 │      └─→ BULK playbook run against ALL instances in that account
                 │             `serial: 10%` bounds the batch
                 │
                 └─→ Config rules evaluate every existing instance
                        └─→ remediate missing tags / profiles
                               └─→ Rule 2 fires per instance  ← second safety net
```

### Rule 3 is what makes this complete

Rules 1 and 2 are **per-instance and event-driven**, so an acquired account's *existing*
instances would otherwise be missed entirely:

- **No launch event will ever fire** — they launched long ago.
- **A tag-change event fires only if a tag was missing.** So an instance that **already**
  carried `DiskMonitoring=enabled` — because the acquired team happened to use that tag
  name, or a previous onboarding attempt set it — generates **no event at all** and would
  never be configured.

That second case is the trap. The instance looks *more* correct than its neighbours and is
the one that stays unmonitored. Rule 3 keys on **stack completion**, which is a real event
for every account regardless of tag history.

Stack completion is also the right ordering point: at `CREATE_COMPLETE` the account's IAM
roles, endpoints and OAM link already exist, whereas an Organizations event
(`MoveAccount` / `AcceptHandshake`) can fire **before the StackSet has finished** — the
controller would then try to reach hosts it has no role to reach. Organizations events are
also emitted **only in us-east-1**, which would drag a cross-Region event bus into the
design. See doc 02 for the full comparison.

### Why `SERVICE_MANAGED`

The self-managed permission model requires *"an execution role such as the
`AWSCloudFormationStackSetExecutionRole` in each of the accounts where you deploy stack
set instances"* — **a manual, per-account, pre-provisioned role**, which is itself the
per-account onboarding step this design exists to eliminate. It defeats the purpose
exactly.

Service-managed instead uses Organizations trusted access: *"you don't have to create the
necessary IAM roles because StackSets creates the IAM roles on your behalf. With this
model, you can also enable automatic deployments to accounts that are added to the
organization in the future."* **Nothing to pre-provision.**

### No account list exists anywhere

**Three independent mechanisms all key on org membership rather than an enumerated
account list:**

| Mechanism | How it scales without a list |
|---|---|
| StackSet `AutoDeployment` on the OU | Targets the **OU**, not accounts — membership is the input |
| OAM sink policy | `aws:PrincipalOrgID` condition — any org member **may** link |
| Controller inventory | Runtime `organizations:ListAccounts` — accounts enumerated per run, over the `organizations` VPC endpoint |

**So there is no account list to edit, in IaC or anywhere else.** That is the property
which makes acquisition onboarding **a single action rather than a runbook** — and, more
importantly, the property that makes it impossible to onboard an account *incompletely* by
updating two of three lists.

There is also **no background "inventory refresh"** to wait for: inventory is computed
fresh at the start of every run. **A new account therefore needs no configuration change —
only a trigger**, which is what Rule 3 supplies.

### The 200-existing-instances case

An acquired account arriving with 200 running instances is handled by the same machinery,
not a special path. Both Config rules **evaluate every existing instance** when deployed
and remediate missing tags and profiles, so those instances enroll **exactly as new
instances do** — Path B, at 200× concurrency.

**Prerequisite, and it is absolute: SSM Agent must already be present** (see doc 01).
Nothing in this design can install it — Ansible's connection *is* SSM, so installing the
agent over that connection is circular, and no AWS API can run commands inside an instance
without it. An instance without the agent is **unreachable** and must be fixed at the AMI
or userdata level. This is the one growth case that is not zero-touch and cannot be made
so.

---

## A new Region

**This is the one growth event that is not zero-touch**, and the honesty matters more than
the convenience.

Almost every component in the design is **Region-scoped**:

| Component | Region coupling |
|---|---|
| Metrics Insights alarms | **Cannot query another Region's metrics** — one alarm set per Region |
| OAM link | Per account **per Region** |
| VPC endpoints | `com.amazonaws.${AWS::Region}.ssm` — created where the instances are |
| Dynamic inventory | `regions:` list in `aws_ec2.yml` |
| Ansible SSM connection | `ansible_aws_ssm_region` |

So a first-time Region requires: adding the Region to the StackSet's target Regions,
adding it to the inventory `regions` list, and deploying an alarm stack there. **Once
done, growth *within* that Region is zero-touch again** — the StackSet, Config rules and
event rules all replicate per Region and behave identically.

This is a stated assumption (README assumption 5), not an oversight. The alternative —
CloudWatch Metrics Centralization to pull metrics into one Region — was rejected in doc 04
because it **replicates all custom metrics** and cannot be scoped to the `CWAgent`
namespace, so it would import unrelated cost and cardinality to solve a problem that
per-Region stacks solve for the price of a parameter.

---

## Rollout control

`serial: 10%` bounds the batch size and `max_fail_percentage: 5` stops a run that is
systematically failing — so **a bad change reaches a few hosts rather than all of them.**

For 200 instances arriving at once, this is the difference between discovering a problem
with the acquired account's network after **~20 hosts** instead of **200**. The failure
mode it protects against is real: a missing `monitoring` endpoint or an endpoint security
group that does not admit the instance SG fails *every* host in that account, identically,
and there is no reason to attempt it 200 times to learn that.

These are the controller-side equivalents of Run Command's `MaxConcurrency` and
`MaxErrors`. Because inventory is tag-scoped, `serial` is measured against **the monitored
fleet** rather than every node in the account — which is the concrete benefit of having
rejected "target all managed nodes" above.

---

## The scaling ceiling

**The alarm query binds first, not enrollment.**

Enrollment scales essentially indefinitely — tag-derived inventory, one shared instance
profile serving unlimited instances, event-driven triggers with no central queue. The
constraint is the alarm query. From the Metrics Insights quotas:

> *"A single query can process no more than 10,000 metrics. This means that if the
> **SELECT**, **FROM**, and **WHERE** clauses match more than 10,000 metrics, the query
> only processes the first 10,000 of these metrics that it finds."*

At 3 mounts per instance that is **≈3,300 VMs per alarm scope**. Because alarms are already
scoped **per account per environment**, each query matches around **300 metrics** — roughly
3% of the ceiling, so there is substantial headroom before this is a live concern. And
granularity is free: billing is per metric *analyzed*, so partitioning 3,000 metrics across
20 alarms costs the same as 2 alarms over all 3,000. What costs more is **overlapping**
scopes, so clean partitioning is the rule.

**⚠️ Two quotas bind before this one** (found in a later audit — see `quotas.md`):

1. **SSM managed nodes: 2,400 per account per Region.** AWS: *"We do not recommend scaling past
   this without a limit increase because **instances could stop communicating with Systems
   Manager**."* Such an instance becomes **unreachable by Ansible** while still publishing
   metrics — so it looks healthy but drops out of configuration management. It **degrades rather
   than erroring**, which makes it the most dangerous ceiling in the design. Adjustable on request.
2. **Metrics Insights alarms: 200 per Region, `Adjustable: No`.** Sharding to escape the
   10,000-metric limit *consumes* this quota, so at ~3 alarms per scope the practical ceiling is
   **≈65 account-environment scopes per Region** — and unlike the others, it cannot be raised.

Beyond that, shard further — add an `Application` dimension to the `WHERE` clause, or split
by mount class. The `PARTIAL_DATA` guard alarm in `20-alarms-dashboard.yaml` watches metric
count precisely so that approaching this ceiling is **visible rather than silent**: past
10,000 metrics a Metrics Insights alarm sets `EvaluationState: PARTIAL_DATA` and **keeps
reporting a state derived from incomplete data** — healthy-looking while monitoring only
part of the fleet.

### ⚠️ The guard's threshold is datapoints, and the unit matters

The guard is `SELECT COUNT(disk_used_percent)` with **no `GROUP BY`**, because a metric
*count* cannot be obtained from inside an alarm expression. `COUNT()` counts
**datapoints**, so the threshold must be stated in datapoints:

```
threshold = metrics_target × (Period / collection_interval)
```

At `Period: 3600` against the agent's 60-second interval, that is 60 datapoints per metric
per hour — so the **3,000-metric warning line is 180,000, not 3,000.** An earlier value of
**400** was a metric count written into a datapoint field: **about 7 metrics exceed it in an
hour**, so the guard would have sat in `ALARM` from the first deploy. **A guard that fires
constantly gets muted, and a muted guard means the silent-degradation mode is unwatched** —
strictly worse than not having one. Recompute if `Period` or the collection interval
changes.

| Limit | Value | Position |
|---|---|---|
| SSM associations per managed node | 20 | We use far fewer — not a constraint on the controller model |
| Instances per playbook / Run Command invocation | Governed by rate control (`serial`, `MaxConcurrency`) | A deliberate throttle, not a ceiling |
| StackSet concurrency | Throttled by AWS | **Large orgs deploy in waves** — not simultaneously |
| Metrics Insights query | *"no more than 10,000 metrics"* | **The binding constraint** ≈3,300 VMs per scope |
| Metrics Insights query return | *"no more than 500 time series"* | Why `path` is grouped away and `ORDER BY` is mandatory. Grouping by `path` too would bind at **~160 instances instead of ~500** and, per `tested_findings.md` §3, buy **no** identity in return |
| Metrics Insights alarms per Region | *"as many as 200 Metrics Insights alarms per Region"* | 3 alarms × accounts × environments — bounds how finely you may shard |

Note the last two interact: sharding to escape the 10,000-metric limit consumes alarms
against the 200-per-Region limit. At ~3 alarms per account-environment pair, that is
roughly **65 account-environment scopes per Region** before the alarm quota binds — which
is the *real* upper bound on this design, not the metric count.

---

## Known limitation — Config verifies configuration, not outcome

**AWS Config confirms the inputs. It cannot confirm the outcome.**

It verifies the tag is present and the profile is attached. It has no visibility into
whether **metrics are actually arriving**. Every one of these states is **fully
Config-compliant and produces no monitoring**:

| State | Config says |
|---|---|
| The playbook run failed | COMPLIANT |
| The agent installed, then crashed | COMPLIANT |
| Someone stopped the agent during troubleshooting and forgot | COMPLIANT |
| The S3 gateway endpoint broke, so Ansible module transit fails | COMPLIANT |
| The agent config file is malformed | COMPLIANT |
| The `monitoring` endpoint is misconfigured, so `PutMetricData` fails | COMPLIANT |
| **A new volume was mounted after the last run** | **COMPLIANT** — observed live |

The last row is not hypothetical. In the pilot (`tested_findings.md` §6) a 5 GiB volume was
mounted and filled to 40% on an enrolled, compliant, correctly tagged instance, and it was
**absent from the CloudWatch metric index entirely** — not a stale value, no index entry at
all. Config had nothing to report because nothing it inspects had changed.

**And the fleet alarm does not cover this either.** A missing instance or mount contributes
no series, and `TreatMissingData: notBreaching` means missing series read as healthy —
**one silent instance among 500 leaves the alarm in OK**, because the other 499 still report
and one of them is always the `MAX`. The alarm is answering *"is any disk full?"*, not
*"is every instance reporting?"*

Nor does `INSUFFICIENT_DATA` rescue this. The pilot showed that a query returning **zero**
series does not even reach `INSUFFICIENT_DATA` — with `notBreaching` it reports a green
`OK`, reason *"No time series were returned by the query"* (`tested_findings.md` §2). So the
worst case is not "the alarm is unsure"; it is **"the alarm is confidently green while
watching nothing."**

Combined with **no periodic re-run**, configuration drift is **neither repaired nor
detected**. This is the accepted consequence of purely event-driven enrollment, and it is
the exact inverse of the failure that disqualified per-VM alarms: there, a missed alarm
creation left an instance silently unmonitored; here, a missed *configuration* does.

### The new-volume case has a fix that needs no scheduler

Worth separating from the general drift problem, because it is the one sub-case with a
*measured* answer rather than deferred work. Setting `resources: ["*"]` in the agent config,
paired with the **hardened 29-entry denylist**, picks up a new mount with **no
reconfiguration and no restart** — proven on the second pilot instance, where a freshly
attached volume filled to 30% appeared in CloudWatch while the agent's
`ActiveEnterTimestamp` never moved.

| Approach | New volume picked up | Junk excluded | Needs a trigger |
|---|---|---|---|
| Enumerate from `ansible_mounts` (this design) | Yes | **Fully** — allowlist fails closed | **Yes** — and no event fires |
| `resources: ["*"]` + hardened denylist | **Yes, automatically** | Only if the denylist is complete — it fails **open** | **No** |

The asymmetry is the whole decision: **an allowlist can only under-monitor; a denylist can
only over-bill**, and doc 04 explains that an over-billed metric cannot be un-billed for 15
months. The pilot found `vfat` leaking through the original list and becoming a real billable
metric, which is what that risk looks like in practice.

**This does not retire the drift work**, it narrows it. A *stopped* agent, a *hand-edited*
config, or a *failed* run still need a periodic convergence run to repair — `["*"]` only
covers the "reality grew and the config did not" case.

Closing the general gap still needs a **coverage check** — compare the set of running
instances carrying the tag against the set of instances publishing `disk_used_percent`, and
alarm on the difference. That is the highest-value next work; see doc 07.

---

## Other limitations

- **OAM sharing is not retroactive.** Sharing starts at link creation, so an acquired
  account's metric history does not appear. Acceptable here — disk monitoring is
  forward-looking, and a filesystem's fullness last month is not actionable.
- **StackSet operations can fail quietly in a single account**, leaving it entirely
  unmonitored while the overall operation reports success. **Alarm on StackSet operation
  status**, not just on the console view.
- **Concurrency throttling means large orgs deploy in waves.** Onboarding is not
  instantaneous at scale, and Rule 3 fires per account as each stack completes rather than
  once for the batch.
- **`RetainStacksOnAccountRemoval: false`** — per AWS, *"If an account is removed from a
  target organization or OU, StackSets deletes stack instances from the account in the
  specified Regions"*, and *"If set to `false`, stack resources are deleted."* This is
  **correct for offboarding** (no orphaned cross-account roles trusting a monitoring
  account that no longer watches you) but **destructive if an account is moved between OUs
  for unrelated reasons** — a reorganisation would silently strip monitoring from a live
  account. Growth event 4, arriving from an unexpected direction.
- **The endpoint configuration assumes private subnets.** An acquired account with a
  different network layout — public subnets with an internet gateway, a shared-services
  VPC, centralized endpoints via PrivateLink — may need adjustment. This is **the most
  likely reason a StackSet deployment succeeds while instances still cannot reach SSM**,
  and the first thing to check when a newly onboarded account produces zero managed nodes.

---

## Files

- [`cloudformation/10-workload-iam.yaml`](../cloudformation/10-workload-iam.yaml) — the auto-deploying StackSet body: instance profile, cross-account roles, OAM link, Config rules with remediation, EventBridge Rules 1–2
- [`cloudformation/11-workload-endpoints.yaml`](../cloudformation/11-workload-endpoints.yaml) — VPC endpoints, per account (VPC IDs are account-specific, so these cannot auto-deploy)
- [`cloudformation/12-monitoring-endpoints.yaml`](../cloudformation/12-monitoring-endpoints.yaml) — monitoring-account endpoints; the `organizations` endpoint is what makes runtime account enumeration work without egress
- [`cloudformation/00-monitoring-account.yaml`](../cloudformation/00-monitoring-account.yaml) — org-scoped OAM sink policy, controller `organizations:ListAccounts` permission
- [`cloudformation/20-alarms-dashboard.yaml`](../cloudformation/20-alarms-dashboard.yaml) — Metrics Insights alarms that auto-adopt instances, plus the `PARTIAL_DATA` guard
- [`ansible/inventory/aws_ec2.yml.template`](../ansible/inventory/aws_ec2.yml.template) — tag-derived inventory; the reason no host list exists
- [`ansible/site.yml`](../ansible/site.yml) — `serial` and `max_fail_percentage` rollout bounds
- [`ansible/group_vars/all.yml`](../ansible/group_vars/all.yml) — rollout control defaults, Region setting
