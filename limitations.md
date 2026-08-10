# Limitations of the current design

What this design **cannot** do, stated plainly. A design that hides its limits is harder to
trust than one that names them.

Organised by severity:
- **§1 Functional gaps** — things that will not work as an operator might reasonably expect
- **§2 Scaling ceilings** — where it stops working, with numbers
- **§3 Operational constraints** — things that work but need care
- **§4 Assumptions** — what must be true, and what breaks if it is not
- **§5 Known cost characteristics** — not defects, but easy to get wrong
- **§6 Deferred work** — ordered by value

---

## §1 Functional gaps

### 1.1 Volume growth does not free space inside the guest ⚠️
`ModifyVolume` grows the **volume**. The **filesystem must then be extended** (`growpart` +
`resize2fs`/`xfs_growfs`) before the OS can use the new space. That step is **not implemented**.

**Consequence:** a successful remediation run does **not** resolve the incident. The runbook is
written to *notify* rather than claim resolution, and the SSM document carries an explicit
`filesystem_warning` in its output. Do not read "action: grown" as "problem fixed".

**Why it was scoped out:** the AWS-side actions are safe and idempotent; the OS-side extension
needs per-filesystem logic and careful handling of edge cases. Deferring it was deliberate, but
it leaves the most visible gap in the design.

### 1.2 AWS Config verifies configuration, not outcome
Config confirms the *inputs* — the tag is present, an instance profile is attached. It **cannot**
confirm the *outcome* — that metrics are actually arriving.

These states are **fully Config-compliant while producing no monitoring**:
- the playbook run failed
- the agent installed, then crashed
- someone ran `systemctl stop amazon-cloudwatch-agent`
- the S3 gateway endpoint broke, so module transit fails
- the rendered agent config is malformed
- the `monitoring` endpoint is misconfigured, so `PutMetricData` fails

**And the alarms do not cover it either:** `INSUFFICIENT_DATA` fires only if metrics stop
**everywhere**. One silent instance among 500 leaves the alarm in `OK`, because the other 499
still report.

⚠️ **And `INSUFFICIENT_DATA` is weaker than that even in the total-silence case.** It was
verified live that with the design's own `TreatMissingData: notBreaching`, an alarm whose query
matches **zero** series does not enter `INSUFFICIENT_DATA` at all — it reports a reassuring green
**`OK`, indefinitely** [`tested_findings.md §2`]:

```
State  : OK
Reason : "No time series were returned by the query. Treat missing data is configured as [NonBreaching]."
```

So the `InsufficientDataActions` wired into the alarms **never fires**, and "monitoring nothing"
and "everything is fine" are indistinguishable from the console. Wherever this design leans on
`INSUFFICIENT_DATA` as a safety net, that net does not exist — which promotes the coverage check
in §6.2 from *desirable* to *the only outcome verification there is*.

**Net effect:** an instance can be silently unmonitored — the exact failure mode that
disqualified per-VM alarms. Closing it requires the coverage check in §6.2.

### 1.3 No periodic re-run, so drift is neither repaired nor detected ✅ reproduced live
Enrollment is purely event-driven (instance launch, tag change, new account). There is no
scheduled sweep.

**Consequences:**
- a **missed or failed event** leaves that instance unconfigured, with nothing to catch it
- a **stopped agent** stays stopped
- a **hand-edited config** stays edited
- a **newly mounted volume** is not picked up until something else triggers a run

**The last one was reproduced in the pilot** [`tested_findings.md §6`]. Attaching a volume fires
**no** event this design listens for — it is not a launch, not a tag change, not a new account. A
5 GiB volume was attached, formatted, mounted and filled to **40%** on a running instance and was
**absent from CloudWatch entirely** — 2 paths indexed, the new mount missing — until the agent
config was re-rendered by hand. Nothing reported a problem and AWS Config remained COMPLIANT
throughout, which is §1.2 and §1.3 compounding: the instance is compliant, monitored, and blind
to the filesystem most likely to fill.

**But the new-volume case has a fix that needs no scheduler.** `resources: ["*"]` with the
hardened 29-entry denylist was also proven live: a second volume, filled to **30%**, appeared in
CloudWatch **with no agent reconfiguration and no restart** — the agent's `ActiveEnterTimestamp`
was unchanged. The agent enumerates mounts continuously; it is *Ansible's* enumeration that is a
point-in-time snapshot.

| Approach | New volume picked up | Junk excluded |
|---|---|---|
| Ansible allowlist (shipped) | yes, but **only on the next run — needs a trigger** | fully, fails **closed** |
| `resources: ["*"]` + hardened denylist | **yes, automatically, no trigger** | only if the denylist is complete — fails **open** |

So §6 item 3 (scheduled drift repair) is **no longer the only answer for new volumes** — but it
is still the only answer for a **stopped agent** or a **hand-edited config**, neither of which the
wildcard touches. The trade is argued in `alternatives.md §12`; Ansible is idempotent and safe to
re-run either way, so §6.1 remains a one-property change. As shipped, nothing self-heals.

### 1.4 `VolumeId` can never be a metric dimension
The CloudWatch agent runs **inside the OS** and has no concept of an EBS volume. `AWS/EBS`
metrics do carry `VolumeId`, but they measure **I/O, not fullness**.

**Consequence:** the alarm cannot say which volume to extend. The enrichment Lambda resolves it at
alert time — but **not from `DescribeVolumes` alone**, as this section previously implied.
`DescribeVolumes` reports the *attachment* device name (`/dev/sdf`) and on Nitro the guest renames
it (`/dev/nvme1n1`), with no `/dev/sdf` block device present, so matching on
`Attachments[].Device` cannot work [`tested_findings.md §7`]. **Resolution has to run on the
host.** The verified method is `/sbin/ebsnvme-id /dev/<disk>`, with
`/sys/class/nvme/<controller>/serial` as a fallback — note the **controller** path;
`/sys/block/*/serial` (and therefore `lsblk -o SERIAL`) returns **nothing** on AL2023, which
disqualifies the fallback this section used to recommend.

**And the mapping is not always 1:1** — LVM/RAID means several volumes behind one filesystem, so
"the volume for `/var`" may legitimately be a list. The Lambda reports all attached volumes and
flags ambiguity rather than guessing.

### 1.5 Fleet alarms carry NO identity, at any grouping ⚠️ verified
A Metrics Insights alarm's raw message reports e.g. *"12 out of 1000 time series evaluated to
ALARM"* — a **count and nothing else**. Read from an actually-firing alarm in the pilot
[`tested_findings.md §3`]:

```
StateReason     : "1 out of 7 time series evaluated to ALARM"
StateReasonData : {"version": "1.0", "queryDate": "…"}
```

No instance, no path, no volume. **And a finer `GROUP BY` does not help.** Adding `path` was
expected to name the breaching filesystem; it does not. The detail exists in the query **result**
and is never copied into the **alarm**, so `GROUP BY InstanceId, path` triples the contributor
count (6 vs 2 on the pilot's two hosts) and buys exactly nothing operationally.

**This changes the status of the enrichment Lambda from optional to mandatory.** This section
previously read that the Lambda "fills this in, but it is an extra component in the alert path —
if it fails, the notification still fires, with less detail." That understates it in both
directions:

- It is **not one of several routes** to identity. It is the **only** route. There is no grouping,
  no alarm setting and no `StateReasonData` field that yields the breaching instance — the Lambda
  must re-run the alarm's query itself to recover it.
- **It is not deployed.** So notifications do not merely risk lacking detail; they
  **always** lack it. Today an on-call responder receiving the 90% page learns that *n* of *m*
  series breached and must go find which, by hand, in the middle of an incident.

The mitigation is therefore not "make the Lambda more reliable" but "deploy it" — and while it is
absent, the alert is a prompt to investigate rather than a description of what is wrong. Its
resolution chain (alarm → query → instance + path → EBS volume id) is non-trivial and was traced
by hand in `tested_findings.md §7`; the label format carries a `N - ` rank prefix that the current
code mis-parses.

Two ceilings apply once it *is* deployed: beyond 100 breaching contributors `StateReason` shows
*"100+"*, so the count itself is **not a complete inventory**, and the alarm's 3-hour evaluation
window (§2.4) bounds how far back the Lambda's re-query can look.

### 1.6 SSM Agent cannot be installed by this design
Ansible's connection *is* SSM, so reaching a host requires the agent already running —
**Ansible cannot bootstrap its own transport**. A Lambda cannot do it either: **no AWS API can
run commands inside an instance without SSM Agent**, which is precisely the gap the agent fills.

**Consequence:** an instance without the agent is unreachable by any part of this design and must
be fixed at the AMI or userdata level. For ASG-managed instances an instance refresh works; for a
standalone instance with no agent and no reboot window, there is **no automated path**.

### 1.7 Linux only
`AWS-ApplyAnsiblePlaybooks` is Linux-only, and the role targets Linux paths and services.
Windows instances need the native SSM document path with `LogicalDisk % Free Space` — not
implemented.

### 1.8 Byte exhaustion only
`disk_used_percent` measures bytes. **Inode exhaustion** causes "No space left on device" while
`disk_used_percent` reads low — typical on hosts with millions of small files. Adding
`disk_inodes_free` is a one-line config change but doubles disk metric count.

---

## §2 Scaling ceilings

### 2.0 The first ceiling is SSM managed nodes: 2,400 per account per Region ⚠️
Discovered while auditing quotas, and it binds **before** anything in the alarm path.

> *"We do not recommend scaling past this without a limit increase because **instances could stop
> communicating with Systems Manager**."*

An instance that stops communicating is **unreachable by Ansible**, so its config can never be
updated — though it keeps publishing metrics, since the agent is independent. Two properties make
this nastier than a normal quota: it **degrades rather than erroring**, and utilization is measured
from `UpdateInstanceInformation` calls in a 5-minute window, so staggered launches can mask it.

It is **per account**, so a wide estate of modest accounts is unaffected; it binds on large single
accounts. Adjustable on request — see `quotas.md` for the recommended 80% alarm.

### 2.0b Concurrent SSM Automation executions: 100 ⚠️
A correlated event — a bad deployment filling logs fleet-wide — could breach the critical
threshold on hundreds of instances at once, and EventBridge would attempt one Automation execution
per instance. Past 100 concurrent they queue (to 1,000), then fail with
`AutomationExecutionLimitExceeded`.

Partly mitigated already: alarms are per account per environment (so fan-out is bounded), EBS
enforces 6 hours between modifications per volume, and `DiskAutoGrow` is opt-in. **Not mitigated:**
a first-time correlated breach across many distinct opt-in volumes. Adaptive concurrency (raising
to 500) or SQS-buffered invocation would address it.

### 2.1 The alarm ceiling is quota, not metric count
A single alarm scope caps at **~3,300 VMs** (the 10,000-metric query limit). But sharding to
escape that limit **consumes alarms** against the **200-per-Region quota**, so at ~3 alarms per
account-environment pair the real ceiling is roughly **65 account-environment scopes per Region**.

⚠️ That figure is **arithmetic from two documented quotas, not a documented combined limit** —
treat it as a number to sanity-check rather than a hard AWS boundary.

### 2.2 The metric ceiling fails by silent degradation
Past 10,000 matched metrics the alarm sets `EvaluationState: PARTIAL_DATA` and **keeps reporting
a state derived from incomplete data** — healthy-looking while monitoring only part of the fleet.
A guard alarm on metric count is included to make approach visible, but the underlying failure
mode is silent by design.

### 2.3 Full table of hard limits

| Limit | Value | Nature |
|---|---|---|
| Metrics per Metrics Insights query | 10,000 | Hard |
| Series returned per query | 500 | Hard — `ORDER BY` decides which |
| Alarm evaluation window | **last 3 hours only** | Hard |
| Contributors reported in ALARM | 100 | Hard |
| Metrics Insights alarms per Region | 200 | Soft (raisable) |
| High-resolution data in Metrics Insights | Unsupported | Hard |
| OAM monitoring accounts per source account | 5 | Hard |
| OAM sinks per account per Region | 1 | Hard |
| State Manager associations per node | 20 | Hard |
| EBS modifications per volume | 4 per rolling 24h | Hard |
| EBS wait between modifications | 6 hours | Hard |

### 2.4 The 3-hour evaluation window blocks trend-based alarming
Alarms see only the last 3 hours, and CloudWatch metric math has **no `TREND` or `FORECAST`
function**. So "days until full" cannot be an alarm expression — it needs a Lambda (§6.8).

---

## §3 Operational constraints

### 3.1 Single Region
Sink and link must be **same-Region**, and **alarms cannot watch another Region's metrics**
(*"the resource must be created in the same Region for which the telemetry resides"*).
Cross-*account* is fully supported; cross-*Region* is not. Multi-Region means per-Region stacks or
a switch to Metrics Centralization (§6.7).

### 3.2 OAM sharing is not retroactive
Sharing begins when the link is created. Metrics predating it are not visible. Acceptable here —
disk monitoring is forward-looking — but it means a newly onboarded account has no history.

### 3.3 Resource tags are not shared through OAM
The monitoring account sees metrics and dimensions, **not** the source account's EC2 tags.
Consequences: `Environment` had to be baked in as a **metric dimension** by the agent, and the
enrichment Lambda must **assume a role into the workload account** to resolve volumes.

### 3.4 The controller is a deployment SPOF (but not a monitoring one)
If the controller is down, configuration changes cannot be deployed. **Monitoring is unaffected**
— the agent keeps publishing, alarms keep evaluating, remediation keeps working. The controller
does hold cross-account credentials, making it a high-value target that must be patched and
monitored.

### 3.5 Ansible over Session Manager is slower than SSH
Every module transits S3 plus a Session Manager round trip. Fine for configuration management;
unusable as a metrics data path — which is independently why the agent collects.

### 3.6 The module-transfer bucket can retain secrets
Module files carry interpolated task parameters, which can embed secrets. The plugin deletes them
at task end, but ungraceful termination can leave objects behind. Mitigated with versioning
**off** (so deleted objects do not persist in history), SSE, ~1-day expiry and a TLS-only policy —
but the exposure window is non-zero.

### 3.7 No `ansible_user` / `remote_user` support
Commands run as the ssm-agent user (normally root); privilege changes need `become_user`. This
surprises anyone expecting SSH semantics.

### 3.8 Endpoint deployment is not zero-touch
VPC IDs are account-specific, so `11-workload-endpoints.yaml` must be deployed per account with
that account's parameters. **The IAM/enrollment stack auto-deploys to the OU; endpoints do not.**
Splitting the template stopped networking from *blocking* auto-deployment, but it cannot make
networking automatic in an unknown VPC layout.

### 3.9 StackSet operations can fail quietly
A stack instance can fail in a single account, leaving it unmonitored, without anything
surfacing. Needs an alarm on StackSet operation status.

### 3.10 Auto-remediation can fight IaC
If a Terraform/CloudFormation stack defines an instance without the `DiskMonitoring` tag, Config
remediation adds it, the next plan reports drift and may remove it — a loop. Mitigation: the
launch template is the primary source, remediation is the backstop, and the tag is added to
`ignore_changes`.

### 3.11 `RetainStacksOnAccountRemoval: false` deletes on OU move
Correct for offboarding, but **destructive if an account is moved between OUs for unrelated
reasons** — monitoring is silently stripped from a live account.

### 3.12 Remediation snapshots persist after teardown
Snapshots created by remediation are **not stack resources**, so they survive stack deletion and
keep costing money. Find them with
`aws ec2 describe-snapshots --filters Name=tag:Project,Values=disk-monitoring`.

### 3.13 An estate-wide event pages once per scope
Per-account-per-environment alarms mean a bad deployment filling logs everywhere pages once per
scope rather than once. Composite alarms can roll them up, at the cost of another layer.

### 3.14 Enrollment is not instantaneous
EventBridge fires when EC2 reports `running`, but SSM registration takes a further ~30–60s, so
the invocation retries. Enrollment is a matter of minutes, not seconds.

---

## §4 Assumptions, and what breaks if wrong

| # | Assumption | If wrong |
|---|---|---|
| 1 | Fleet is **Amazon Linux 2/2023 with SSM Agent running** | An instance without the agent is unreachable, and nothing here can install it (§1.6). Non-AL distros also change the package-install path — Ubuntu/RHEL repos are not S3-backed, so a no-egress VPC cannot reach them and Ansible itself could not be installed on-node. |
| 2 | Private subnets, **no internet egress, no NAT** — for the **controller as well as** instances | VPC endpoints are mandatory rather than optional: `11-workload-endpoints.yaml` for instances, `12-monitoring-endpoints.yaml` for the controller. **Every AWS API the design calls is reachable over PrivateLink, Organizations included** — so no NAT is needed anywhere. If NAT exists, interface endpoints become optional — but still add the **free S3 gateway endpoint**, which diverts the highest-volume traffic away from NAT's $0.045/GB at no cost. |
| 3 | **IMDSv2** everywhere | No IMDSv1 handling exists anywhere in the design. |
| 4 | All accounts in **one AWS Organization** | Required for the org-scoped sink policy, StackSet auto-deployment and runtime account discovery. Accounts outside the org need manual onboarding. |
| 5 | **Single Region**, monitoring account in **us-east-1** | See §3.1. The us-east-1 constraint is separate and narrower: the **Organizations interface endpoint exists only in the control-plane Region**, and `12-monitoring-endpoints.yaml` asserts it via a `Rules` section. Elsewhere, reach a us-east-1 endpoint over Transit Gateway — still no egress. Workload accounts are unconstrained. |
| 6 | Pricing is **single-Region list price** | Figures must be re-verified at implementation — the Pricing API needs credentials. Directionally reliable; **not quotable to finance**. |
| 7 | Instances have a **consistent security group** referenced by the endpoint SG | A heterogeneous estate needs per-account endpoint parameters. |
| 8 | `Environment` tagging is **reasonably consistent** | Untagged instances default to `Environment=unscoped` and fall outside every environment-scoped alarm — **monitored but not covered, which looks fine**. Surfaced by Config compliance rather than an alarm. |

---

## §5 Cost characteristics worth knowing

Not defects, but easy to get wrong:

1. **Cost tracks cardinality, not frequency.** Collecting every 60 s costs the same as every
   5 min. Only unique dimension combinations are billed. Verified live: 2 instances × 2 mounts
   produced **exactly 4 metrics** [`tested_findings.md §1`].
2. **`resources: ["*"]` costs ~11× more** on container hosts — $17,000 vs $1,500/month at
   1,000 VMs — and it happens **silently**. Guarded by the test suite. The pilot adds a
   qualification: the ~11× is what happens when the **denylist is incomplete**, and the repo's
   was — nine pseudo-filesystem types present on AL2023 were missing, and `vfat` (`/boot/efi`)
   leaked through as a real billable metric [`tested_findings.md §4`]. With all 29 entries the
   wildcard produced the same cardinality as the allowlist. See `alternatives.md §12`; the
   safety point is that an **allowlist fails closed while a denylist fails open**.
3. ⚠️ **There is no display-time filtering, anywhere — so agent-side filtering is the only cost
   control that exists** [`tested_findings.md §5`]. This is the one most likely to be
   misunderstood, because every instinct says an over-broad metric can be tidied up later. It
   cannot:
   - Once published, a metric is **stored and billed for 15 months**. Neither CloudWatch nor OAM
     can un-bill it, and there is no delete-metric API.
   - **OAM filters by resource *type*** (`AWS::CloudWatch::Metric`) — **not** by namespace and
     certainly not by filesystem. A sink cannot decline the junk.
   - `WHERE fstype = 'xfs'` in an alarm, or omitting a series from a dashboard, hides junk from
     **view** only. The metric still exists and still costs.

   So the fstype filter in the Jinja template is not a tuning knob applied at the wrong layer for
   convenience — it is the **only** layer at which the decision can be made, and a mistake there
   is billed for 15 months before it ages out.
4. **Alarms bill per metric *analyzed***, so alarm cost scales with metric count too. Reducing
   metrics cuts both lines.
5. **Overlapping alarm scopes double-bill.** Partitioning is free; overlap is not.
6. **Interface endpoints cost ~$88/month per workload VPC** (4 × 3 AZs), plus a **one-time
   ~$102** for the monitoring account (7 × 2 AZs — `organizations`, `sts`, `ec2`, SSM×3,
   `monitoring`). The workload figure is the one that multiplies; at many small VPCs it adds
   up, and centralized endpoints shared via PrivateLink are the mitigation. The monitoring
   figure is fixed regardless of fleet or account count.
7. **The dashboard is effectively free** ($0 for the first three), while alarms are ~$600/month at
   1,000 VMs because they evaluate continuously.

---

## §6 Deferred work, ordered by value

Each is additive; none requires redesign.

⚠️ **Ahead of all of them: deploy the enrichment Lambda.** It is listed last-ish nowhere because
the pilot moved it out of "deferred" entirely. §1.5 was written as though the Lambda added polish;
it is in fact the **only** mechanism that names the breaching instance, and it is **not deployed**,
so every notification the design would send today identifies nothing. `tested_findings.md §7`–`§8`
list the corrections it needs before deployment: add `fstype` to its `SCHEMA()` clause, parse the
`N - ` rank prefix out of result labels, and resolve the volume with `ebsnvme-id` on the host
rather than `Attachments[].Device`. A page nobody can act on is worse than drift nobody has hit
yet, so this outranks items 2 and 3 below.

1. **Filesystem extension after volume growth** — closes §1.1, the most visible gap.
   `growpart` + `resize2fs`/`xfs_growfs` then `df -h` verification.
2. **Coverage verification** — closes §1.2. Periodically compare running instances against
   instances publishing metrics; alarm on the difference. This is the only mechanism that verifies
   *outcome* rather than configuration.
3. **Scheduled drift repair** — closes §1.3. Adding `ScheduleExpression` to a State Manager
   association, or a periodic controller run, makes the design self-healing. **One property
   change.** Scope note after the pilot: this is still required for a **stopped agent** and a
   **hand-edited config**, but it is **no longer the only fix for a newly attached volume** —
   `resources: ["*"]` with the hardened 29-entry denylist picks those up with no trigger at all
   [`tested_findings.md §6`]. The two are complementary rather than alternatives, and the
   allowlist-vs-denylist safety trade is argued in `alternatives.md §12`.
4. **Reclaim before growing** — journal vacuum, logrotate, package cache, `docker system prune`,
   then re-measure and stop if resolved. A disk at 90% is often 90% logs, where growing the volume
   is a permanent cost for a recurring problem. AWS Managed Services' own remediation cleans up
   first.
5. **Expansion counter** — alert on repeated growth of one volume; that pattern is the signal an
   **application leak needs fixing rather than feeding**. Without it, auto-growth converts a
   visible incident into a silently rising bill.
6. **Root / LVM / RAID remediation** — excluded by AWS's documented procedure, yet `/` is
   frequently what fills.
7. **Multi-Region** — either per-Region sinks and alarm stacks converging on one SNS topic
   (dashboards are natively cross-Region), or **Metrics Centralization**, which replicates metrics
   into the destination Region so **one global alarm** spans all accounts and Regions (changing
   `GROUP BY` to `:@aws.account, :@aws.region`).
8. **Predictive "days until full"** — a Lambda fitting a slope over ~14 days, alarmed at `< 7`.
   Needs code because of §2.4. Catches slow leaks a static threshold misses until it is nearly too
   late — arguably closest to the CTO's actual ask.
9. **Per-application alarm scoping** — add `Application` to `append_dimensions` and split alarms
   with `WHERE`. Also the natural **sharding strategy** at the metric ceiling (§2.1).
10. **Windows support** — closes §1.7.
11. **`disk_inodes_free`** — closes §1.8, at the cost of doubling disk metric count.
12. **Custom inventory plugin** — would remove the per-account inventory file generation step.
13. **Composite alarms** — roll up per-scope alarms so an estate-wide event pages once (§3.13).
14. **Alarm on StackSet operation status** — closes §3.9.
15. **Agent-side aggregation** — not a gap but the **largest cost lever**: ~$1,000/month saved at
    1,000 VMs, at the cost of losing per-filesystem detail. First lever to pull under cost
    pressure.

---

## The honest summary

This design reliably detects **byte exhaustion on Linux EC2 instances in one Region**, across any
number of accounts, with no host list to maintain and no alarm to create per instance. **The
detection claim is now measured rather than argued** — the pilot confirmed SSM reachability with no
public IP and no NAT, metrics arriving over the `monitoring` endpoint, the metric tracking `df` to
within a percentage point, OAM sharing working cross-account, and **one alarm firing on hosts
across an account boundary** [`tested_findings.md §1`].

Its four real weaknesses:
1. **Remediation is incomplete** — it grows the volume but cannot yet make the space usable.
2. **It verifies configuration, not outcome** — a crashed agent looks compliant, and
   `INSUFFICIENT_DATA` is not the backstop it appears to be (§1.2).
3. **Nothing self-heals** — no periodic re-run means drift persists, reproduced live (§1.3).
4. **The alert names nothing** — a fleet alarm carries no identity at any grouping, and the only
   component that recovers it is not deployed (§1.5).

The first three are addressed by deferred items 1–3 and the fourth by deploying the enrichment
Lambda; **none requires the architecture to change.** What the pilot changed was the *severity
ordering*, not the design: weakness 4 was previously read as cosmetic and is not.

---

## See also

- `alternatives.md` — every option considered and why it was not chosen
- `quotas.md` — every AWS quota this design touches, and which ones bind
- `context_2.md` — the full decision record, including reversals and corrections
- `docs/01`–`docs/07` — each decision argued in its own context
