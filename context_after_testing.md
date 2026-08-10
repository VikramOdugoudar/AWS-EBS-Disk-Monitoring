# context_after_testing.md — what changed once the design was actually deployed

Third companion to `context.md` (the brief) and `context_2.md` (the design and decision
record). Those two describe what was **designed**. `findings.md` describes what a **desk
review** found by reading it. This file describes what happened when a working subset was
**deployed into real AWS accounts** — the corrections that followed, and the state the repo
was left in.

It exists because the gap between "reasoned carefully" and "observed working" turned out to
matter more than expected. **Four defects were found that no amount of reading would have
caught**, and three claims made during the review were themselves disproved.

**Reading order:** `context.md` → `context_2.md` → `findings.md` → `tested_findings.md` →
this file. `tested_findings.md` is the raw evidence; this file is the narrative.

---

## 1. What was deployed, and what was not

A deliberately narrow slice: enough to exercise **collection, centralization and alarming**
end to end, and nothing else.

| Deployed | Not deployed |
|---|---|
| 2 × `t3.medium` AL2023, no public IP, IMDSv2 | The Ansible controller |
| Private subnets, **no IGW route, no NAT** | Event-driven enrollment (3 EventBridge rules) |
| 4 interface endpoints + free S3 gateway | AWS Config rules and remediation |
| CloudWatch agent, `disk_used_percent`, 60 s | The StackSet |
| 3 extra EBS volumes (`/data`, `/data2`, `/data3`) | The enrichment Lambda |
| **OAM sink + link across two accounts** | Remediation (no volume was grown) |
| 2 Metrics Insights alarms **in the monitoring account** | SNS topics |

Accounts are written as `<1111111111>` (workload) and `<2222222222>` (monitoring)
throughout the repo; real identifiers were deliberately kept out of every committed file.

**Configuration was applied by SSM Run Command, not Ansible.** That was the right trade for
testing the *agent config and alarm logic*, which is where the design's analytical work
lives. It also means everything in `findings.md §1` — the cross-account connection
credentials, playbook delivery, the controller's existence — remains **unverified**.

Everything was torn down afterwards. Both accounts verified clean.

---

## 2. The four things deployment found that reading did not

### 2.1 `fstype` is a fourth dimension, and its absence fails silently

The agent emits **`InstanceId, path, Environment, fstype`**. The design assumed three:
`drop_device: true` removes `device`, and the repo took that to mean the dimension set was
predictable and small. It removes `device` **only**.

`SCHEMA()` is an exact-set match, so a three-dimension clause matched nothing. Both were run
side by side on identical data:

```
SCHEMA("CWAgent", InstanceId, path, Environment)          -> "No time series were returned by the query."
SCHEMA("CWAgent", InstanceId, path, Environment, fstype)  -> "2 time series evaluated to OK"
```

**And the failure mode is worse than the design's own documentation of it.** Every doc said
a mismatch leaves the alarm in `INSUFFICIENT_DATA` — visibly abnormal, and actioned via
`InsufficientDataActions`. It does not. With this design's own
`TreatMissingData: notBreaching`, a query matching zero series reports a reassuring green
**`OK`, indefinitely**:

```
State  : OK
Reason : "No time series were returned by the query. Treat missing data is configured as [NonBreaching]."
```

So the compensating control wired into both alarms **never fires**. "Looking calm while
monitoring nothing" was a good phrase for it; it turned out to be literal.

This is the single most valuable result of the exercise, and it validates why
`context_2.md §9` insisted on the dimension gate before finalising alarms. That open item is
now **answered** — and neither documented candidate (`Partition`, or
`device`/`fstype`/`path`) was correct.

### 2.2 A fleet alarm carries no identity — at any grouping

`GROUP BY InstanceId, path` was expected to name the breaching filesystem in the alarm.
Read from an actually-firing alarm:

```
StateReason     : "1 out of 7 time series evaluated to ALARM"
StateReasonData : {"version": "1.0", "queryDate": "..."}
```

No instance, no path, no volume. The detail exists in the query **result** and is never
copied into the **alarm**.

**Consequence: the enrichment Lambda moved from optional to mandatory.** `limitations.md`
had framed it as "an extra component in the alert path — if it fails, the notification still
fires, with less detail." That was wrong twice over: it is the *only* route to identity, and
because it is not deployed, notifications **always** lack detail.

It also settles a design question empirically. Finer grouping triples contributors (6 vs 2
on two hosts) against the 500-series cap — binding at ~160 instances instead of ~500 — and
buys nothing. **`GROUP BY InstanceId` was right all along**, for a reason the design had
only guessed at.

### 2.3 The agent-side denylist leaks, and a denylist fails open

With `resources: ["*"]`, `ignore_file_system_types` suppressed **25 of 28** mounts — but
`vfat` (`/boot/efi`) leaked through and became a billable metric. Nine types observed live on
AL2023 were absent from the 18-entry list: `vfat`, `ramfs`, `efivarfs`, `pstore`, `bpf`,
`selinuxfs`, `securityfs`, `hugetlbfs`, `rpc_pipefs`.

The asymmetry is the lesson, and it was not in the design: **the Ansible role's allowlist
(`ext*/xfs/btrfs`) fails closed; the agent's denylist fails open.** They reach the same
result on a known host and diverge on an unknown one.

### 2.4 Mount → volume cannot be resolved from EC2, and the documented method does not work

`DescribeVolumes` reports the **attachment** device name; the Nitro guest renames it:

```
EC2 says   : vol-0ccc...ccc -> /dev/sdf
guest says : /data          -> /dev/nvme1n1
```

There is no `/dev/sdf` block device in the guest, only a symlink. So `Attachments[].Device`
matching **cannot work** — which disproves the fix `findings.md §17.2` proposed, and which I
had written into the SSM runbook.

Five methods were tested. **`/sys/block/<disk>/serial` returns empty on AL2023** (the method
in the runbook), and `nvme-cli` is not installed. What works: **`/sbin/ebsnvme-id`**, with
`/sys/class/nvme/<controller>/serial` as fallback — note the *controller* path. The verified
chain, cross-checked against `DescribeVolumes`:

```bash
SRC=$(findmnt -no SOURCE --target /data)      # /dev/nvme1n1
DISK=$(lsblk -no pkname "$SRC" | head -1)     # parent disk if a partition
/sbin/ebsnvme-id "/dev/$DISK"                 # Volume ID: vol-0ccc...ccc
```

---

## 3. Finding §1.3 reproduced — and a better fix than the one deferred

`limitations.md §1.3` says drift is neither repaired nor detected because enrollment is
purely event-driven. Attaching a volume fires **none** of the three events this design
listens for.

**Reproduced:** a 5 GiB volume was attached, formatted, mounted and filled to **40%** on a
running instance. It was **absent from CloudWatch entirely** until the config was re-rendered
by hand. Nothing reported a problem; AWS Config stayed COMPLIANT. That is §1.2 and §1.3
compounding — compliant, monitored, and blind to the filesystem most likely to fill.

**But a fix exists that needs no scheduler.** With `resources: ["*"]` and the hardened
denylist, a second volume filled to **30%** appeared in CloudWatch with **no agent
reconfiguration and no restart** — the agent's `ActiveEnterTimestamp` was unchanged. The
agent enumerates mounts continuously; it is *Ansible's* enumeration that is a point-in-time
snapshot.

| Approach | New volume | Junk excluded |
|---|---|---|
| Ansible allowlist (shipped) | only on the next run — **needs a trigger** | fully, fails **closed** |
| `resources: ["*"]` + hardened denylist | **automatically, no trigger** | only if complete — fails **open** |

So deferred item 3 (scheduled drift repair) is **no longer the only answer for new volumes**,
though it remains the only answer for a stopped agent or a hand-edited config.

---

## 4. What deployment confirmed

Recorded because a review that only reports failures is not calibrated. All of these were
inference and are now observation:

| Claim | Evidence |
|---|---|
| SSM works with no public IP and no NAT | both instances `Online` in **10 s** via the endpoints |
| Metrics reach CloudWatch over the `monitoring` endpoint | 20 datapoints with real values |
| The metric tracks the filesystem | wrote 6 GiB → `/data` 1.02% → **61.40%**; `df` said 62% |
| Cardinality is `instances × mounts` | 2 × 2 = **exactly 4 metrics** |
| **OAM shares metrics cross-account** | monitoring account queried **all 7** workload metrics |
| **One alarm spans an account boundary** | warning 80% entered `ALARM` on real data |
| `MAX` not `AVG` | `GROUP BY InstanceId` collapsed mounts to the fullest per host |
| No display-time filtering exists | filtering is agent-side only; a published metric bills for 15 months |

That last one strengthens the design's own argument: **the fstype filter has no downstream
escape hatch**, so `docs/06` is right to call it the difference between a $1,500 and a
$17,000 bill rather than a tuning knob.

---

## 5. Corrections to claims made during review

Recorded because they were stated with more confidence than the evidence supported. Three
were mine, made while fixing the desk-review findings.

| Claim | Reality |
|---|---|
| "`GROUP BY InstanceId, path` gives identity without a Lambda" | **False** — no grouping puts identity in the alarm (§2.2) |
| "Match the volume via `Attachments[].Device`" (`findings.md §17.2`'s fix) | **Cannot work** on Nitro (§2.4) |
| "NVMe serial from `/sys/block/$DISK/serial`" (written into the runbook) | **Returns nothing** on AL2023 (§2.4) |
| "`/boot/efi` is still publishing after the denylist fix" | **Measurement error** — my query window straddled the config change |
| "`ap-south-1` is the repo's Region" | **Wrong** — a fallback default only; all templates use `${AWS::Region}` |
| "AWS Organizations does not support PrivateLink" (`findings.md §1.7`) | **Refuted** — it does, but only in the control-plane Region |

The last one is worth dwelling on: §1.7 concluded the controller subnet needed NAT. It does
not. The endpoint exists, is Region-restricted rather than unavailable, and the design ends
up **stronger** than the finding proposed — no egress anywhere, and README assumption 2
intact.

---

## 6. What changed in the repo

### Code and templates

| File | Change |
|---|---|
| `lambda/enrich_disk_alarm.py` | **Rewritten.** Host-side `ebsnvme-id` resolution; 4-dimension `SCHEMA()`; label parsed from the end (the `1 - ` rank prefix broke the old parse); thresholds from env; 80% → SNS naming instance/path/volume; 90% → `StartAutomationExecution` with `DryRun: 'false'` |
| `cloudformation/20-alarms-dashboard.yaml` | `fstype` in all `SCHEMA()`; guard threshold 400 → **180,000** (it counts datapoints, not metrics); remediation marked not-functional-as-wired |
| `cloudformation/30-dashboard.yaml` | `fstype` in all `SCHEMA()`; two widgets marked as unable to render |
| `cloudformation/12-monitoring-endpoints.yaml` | **New** — monitoring-account endpoints incl. `organizations` and `sts` |
| `cloudformation/10-workload-iam.yaml` | New `SessionRole` for the Ansible connection |
| `cloudformation/00-monitoring-account.yaml` | Event bus policy; `aws:ResourceOrgID`; corrected Run Command payload shape |
| `ansible/.../amazon-cloudwatch-agent.json.j2` | `Environment` moved into the `disk` section — at `metrics` level AWS **silently drops** it |
| `ansible/.../defaults/main.yml` | 9 denylist entries added |
| `ansible/site.yml` | `pre_tasks` obtaining cross-account STS credentials |
| `ssm-documents/DiskSpace-GrowVolume.yaml` | Resolver replaced; phantom 6-hour EBS guard deleted |
| `tests/test_agent_config.py` | Dimension contract now four dimensions |

### Documentation

`README.md`, `context_2.md`, `limitations.md`, `alternatives.md`, `quotas.md`,
`architecture/architecture.md`, `solution-2-centralization.md` and `docs/01`–`07` all carry
the corrections, each citing `tested_findings.md` by section. Two new files:
**`tested_findings.md`** (the evidence) and this one.

Also corrected along the way, from live AWS API checks rather than the pilot:

- **Cost figures.** 2 of 3 rows were wrong — 100 VMs is **$150** not $120; 10,000 VMs is
  **$11,000** not $8,000. Rates verified against the Pricing API: metric tiers
  $0.30/$0.10/$0.05/**$0.02**, Metrics Insights alarms $0.10 per metric analyzed, interface
  endpoints $7.30/AZ/month. The 11.3× wildcard ratio is confirmed exactly.
- **A deployment blocker.** `12-monitoring-endpoints.yaml` had a `Description` of 1,149
  characters against CloudFormation's **1024** limit — it could not have deployed. Caught by
  `validate-template`, which is why that check belongs in CI.

---

## 7. What is still open, in priority order

1. **Deploy the enrichment Lambda.** Now the highest-value item, not a refinement. Without it
   an alert says only "something breached" (§2.2). The code is written; no
   `AWS::Lambda` resource exists in any template.
2. **The chain in `findings.md §1` still does not close.** Nine CRITICAL items, none touched
   by the pilot: cross-account connection credentials, the event bus policy's downstream
   effects, the controller's existence in IaC, playbook delivery.
3. **`AWS-SetRequiredTags` does not work** with the `required-tags` rule (AWS says so on that
   rule's own page), so auto-enrollment is inert.
4. **Filesystem extension after volume growth.** `ModifyVolume` alone frees nothing in the
   guest.
5. **Coverage verification** — the only mechanism that would verify *outcome* rather than
   configuration, and now more important because `INSUFFICIENT_DATA` is not the backstop the
   design assumed (§2.1).

---

## 8. The honest summary

The design's **analytical work held up well**. Cardinality-not-frequency, `MAX` over `AVG`,
the three Metrics Insights dead ends, OAM's bilateral consent, SSM without egress, the
in-guest-agent premise — all confirmed, several of them observed working for the first time.

What did not hold up was **anything asserted about the runtime without running it**: which
dimensions the agent emits, what an alarm actually says when it fires, whether a documented
device-matching approach works on the instance types this design targets, and whether the
one endpoint everyone said was impossible exists.

Every one of those was a confident sentence in a document. Each took minutes to disprove once
something was deployed — and each would have produced a monitoring system that looked
healthy while seeing nothing.

## See also

- `tested_findings.md` — the pilot evidence, with commands and outputs
- `findings.md` — the desk review this pilot partly confirmed and partly disproved
- `context_2.md` — the design and decision record, §9 of which this exercise closed
- `limitations.md` — updated with what is now reproduced rather than predicted
