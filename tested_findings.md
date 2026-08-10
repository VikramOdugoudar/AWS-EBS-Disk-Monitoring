# Tested findings — live pilot in AWS

**Date:** 2026-08-10
**Accounts:** workload `<1111111111>` → monitoring `<2222222222>` · **Region:** us-east-1
**Method:** a working subset of the design was deployed and exercised against real AWS APIs.
Every statement below was **observed**, not inferred. Commands and outputs are quoted.

Companion to `findings.md`, which is the *desk review* (documentation and code read without
deploying). This file is the *empirical* record: what deployment proved, disproved, and
discovered that no amount of reading would have found. **Where the two disagree, this file
wins** — it has evidence.

## What was deployed

| Component | Detail |
|---|---|
| Instances | 2 × `t3.medium` Amazon Linux 2023, **no public IP**, IMDSv2 required |
| Network | Default VPC, private subnets on a **route table with no IGW route**, no NAT |
| Endpoints | `ssm`, `ssmmessages`, `ec2messages`, **`monitoring`** (4 interface × 2 AZ) + free `s3` gateway |
| IAM | One instance profile: `AmazonSSMManagedInstanceCore` + `CloudWatchAgentServerPolicy` |
| Storage | Root + 3 extra gp3 volumes mounted `/data`, `/data2`, `/data3` |
| Collection | CloudWatch agent, `disk_used_percent`, 60 s, `drop_device: true` |
| Centralization | **OAM sink in the monitoring account, link from the workload account** |
| Alarming | 2 Metrics Insights alarms **created in the monitoring account** |

---

## 1. What the pilot PROVED — previously all inference

| Design claim | Evidence observed |
|---|---|
| SSM reachability with no public IP and no NAT | both instances `PingStatus: Online` **10 s** after launch |
| Metrics reach CloudWatch over the `monitoring` endpoint | 20 datapoints retrieved with real values |
| The metric tracks filesystem reality | wrote 6 GiB → `/data` moved `1.02%` → **`61.40%`**; on-host `df` read 62% |
| Cardinality is `instances × mounts` | 2 instances × 2 mounts = **exactly 4 metrics** |
| OAM shares metrics cross-account | monitoring account queried **all 7** workload metrics |
| One alarm covers many hosts across accounts | warning 80% entered **`ALARM`**: *"1 out of 2 time series evaluated to ALARM"* |
| `MAX` not `AVG` | `GROUP BY InstanceId` collapsed 3 mounts to 1 contributor reporting its fullest |
| The `validate:`-style JSON guard | a malformed config is rejected before the agent restarts |

**Finding §2.1 CONFIRMED.** `Environment` appears as a dimension **only** because it was placed
inside the **`disk` section's** `append_dimensions`. The repo template's `metrics`-level
placement is silently dropped by AWS, exactly as §2.1 states. The one-line fix is correct and
necessary.

---

## 2. NEW CRITICAL — `fstype` is emitted; `SCHEMA()` names only three dimensions

**The agent emits four dimensions:** `InstanceId, path, Environment, fstype`.

`drop_device: true` removes `device` but **not** `fstype`. The repo assumed it removed both.

`SCHEMA()` is an **exact-set** match, so the project's three-dimension clause matches nothing.
Proven by running both alarms side by side on identical data:

```
SCHEMA("CWAgent", InstanceId, path, Environment)          -> "No time series were returned by the query."
SCHEMA("CWAgent", InstanceId, path, Environment, fstype)  -> "2 time series evaluated to OK"
```

### It fails WORSE than the desk review predicted

`findings.md §2.1` says a mismatch leaves the alarm in `INSUFFICIENT_DATA`. **It does not.**
With the design's own `TreatMissingData: notBreaching`, a query matching zero series reports a
reassuring green **`OK` — forever**:

```
State  : OK
Reason : "No time series were returned by the query. Treat missing data is configured as [NonBreaching]."
```

So `InsufficientDataActions` — the design's compensating control for exactly this failure —
**never fires**. "Looking calm while monitoring nothing" is literal, and there is no signal at all.

**Required change:** add `fstype` to every `SCHEMA()` clause in
`cloudformation/20-alarms-dashboard.yaml`, `cloudformation/30-dashboard.yaml` and
`lambda/enrich_disk_alarm.py`. Update the expected dimension set to
`{InstanceId, path, Environment, fstype}`.

---

## 3. NEW CRITICAL — a fleet alarm carries NO identity, at any grouping

`GROUP BY InstanceId, path` was expected to name the breaching filesystem in the alarm. **It
does not.** Read from a firing alarm:

```
StateReason     : "1 out of 7 time series evaluated to ALARM"
StateReasonData : {"version": "1.0", "queryDate": "2026-08-09T22:57:06.458+0000"}
```

No instance, no path, no volume — only a count. The detail exists in the **query result**, never
in the **alarm**.

### Consequence for the design

| | `GROUP BY InstanceId` | `GROUP BY InstanceId, path` |
|---|---|---|
| Contributors (2 hosts, 6 filesystems) | 2 | 6 |
| Detects the 84.55% breach | yes | yes |
| Names the instance in the alarm | **no** | **no** |
| 500-series cap binds at | **~500 instances** | ~160 instances |

Finer grouping costs 3× the contributors and returns nothing operationally. **`GROUP BY
InstanceId` is correct** — which vindicates the repo's original choice — and the **enrichment
Lambda is mandatory, not a nice-to-have.** It is the only path from "something breached" to
"this volume needs growing."

---

## 4. NEW — the agent-side denylist leaks 9 pseudo-filesystems

With `resources: ["*"]`, `ignore_file_system_types` suppressed **25 of 28** mounts — but
**`vfat` (`/boot/efi`) leaked through and became a billable metric.**

Observed live on AL2023 and **absent** from the repo's 18-entry denylist:

```
vfat  ramfs  efivarfs  pstore  bpf  selinuxfs  securityfs  hugetlbfs  rpc_pipefs
```

Adding these (29 entries total) reduced 28 mounts → 2 real filesystems, and `/boot/efi` **stopped
publishing** (0 datapoints after the change, verified against a window strictly after it).

**Note the asymmetry:** the Ansible role's *allowlist* (`ext*/xfs/btrfs`) is immune to this by
construction — it can only ever admit known-good types. Only the agent-side *denylist* has the
gap. A denylist fails **open**; an allowlist fails **closed**.

---

## 5. CONFIRMED — there is no display-time filtering anywhere

Filtering is **agent-side only**:

- Once published, a metric is **stored and billed for 15 months**. Neither CloudWatch nor OAM can
  un-bill it.
- OAM filters by resource **type** (`AWS::CloudWatch::Metric`) — not namespace, not filesystem.
- `WHERE fstype = 'xfs'` in an alarm, or omitting a series from a dashboard, hides junk from
  **view** only. The metric still exists and still costs.

**So the ~11× cost control has no downstream escape hatch**, which is why `docs/06` is right to
call the fstype filter "the difference between a $1,500 and a $17,000 monthly bill" rather than a
tuning knob.

---

## 6. Finding §1.3 reproduced live — and a fix that needs no scheduler

Attaching a volume fires **no** EventBridge rule (not a launch, not a tag change, not a new
account), so a new mount stays unmonitored indefinitely.

**Reproduced:** `/data2` was attached, formatted, mounted and filled to **40%** on instance 1. It
was **invisible in CloudWatch** — 2 paths indexed, `/data2` absent — until the agent config was
re-rendered by hand. A 5 GiB volume at 40% full, entirely unmonitored, with nothing reporting a
problem and AWS Config still COMPLIANT.

**The fix, proven:** `resources: ["*"]` plus the hardened denylist from §4 closes this **without
a scheduled re-run**. On instance 2, `/data3` was attached, formatted, mounted and filled to
**30.01%** and appeared in CloudWatch **with no agent reconfiguration and no restart** — the
agent's `ActiveEnterTimestamp` was unchanged throughout.

| Approach | New volume picked up | Junk excluded |
|---|---|---|
| Ansible allowlist (repo design) | yes, but **only on the next run — needs a trigger** | fully |
| `resources: ["*"]` + hardened denylist | **yes, automatically, no trigger** | yes, if the denylist is complete |

This is a better answer than `limitations.md`'s deferred item 3 ("scheduled drift repair") for
the *new volume* case, though a periodic run is still needed for a stopped agent or an edited
config.

---

## 7. NEW — how the enrichment Lambda must resolve `path` → EBS volume

The alarm gives the Lambda nothing (§3), so it must rebuild the entire chain. Each hop was traced
live.

### Step 1 — recover instance and path from the query

Re-run the alarm's query **with `path` in the `GROUP BY`** and filter to breaching values.

⚠️ **The label format includes a rank prefix**, which the current Lambda mis-parses:

```
label = '1 - i-0aaa...aaa /data'      value = 84.55%
```

`enrich_disk_alarm.py` does `parts = label.split()` then reads `parts[0]`/`parts[1]` — which
yields `"1"` and `"-"`, not the instance and path. **Parse from the end** (`parts[-2]`,
`parts[-1]`) or strip the `N - ` prefix first.

### Step 2 — EC2 alone CANNOT map a mount to a volume

`DescribeVolumes` reports the **attachment** device name, and on Nitro the guest renames it:

```
EC2 says     : vol-0ccc...ccc  ->  /dev/sdf
guest says   : /data  ->  /dev/nvme1n1
```

There is no `/dev/sdf` block device in the guest — only a symlink. **So `Attachments[].Device`
matching cannot work**, which disproves the fix proposed in `findings.md §17.2`.

### Step 3 — resolution must run on the host

Three working methods, verified. **`/sys/block/$DISK/serial` returns nothing on AL2023** — so the
method written into `ssm-documents/DiskSpace-GrowVolume.yaml` **does not work** and must be
replaced.

| # | Method | Result | Notes |
|---|---|---|---|
| A | `readlink -f /dev/sdf` | `/dev/nvme1n1` | maps EC2's name to the guest's; needs EC2 data first |
| B | `nvme id-ctrl` | **unavailable** | `nvme-cli` not installed on AL2023 by default |
| C | `/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_vol…` | **works** | volume id is in the filename |
| D | `/sys/class/nvme/nvme1/serial` | **works** — `vol0ccc...ccc` | note: **controller** path, not `/sys/block/*/serial` |
| E | `/sbin/ebsnvme-id <dev>` | **works** — `Volume ID: vol-0ccc...ccc` | AWS-provided, most explicit |

**Recommended: E with D as fallback.** The complete chain, verified end to end:

```bash
SRC=$(findmnt -no SOURCE --target /data)          # /dev/nvme1n1
DISK=$(lsblk -no pkname "$SRC" | head -1)          # parent disk if a partition
[ -n "$DISK" ] || DISK=$(basename "$SRC")
/sbin/ebsnvme-id "/dev/$DISK"                      # -> Volume ID: vol-0ccc...ccc
# fallback: sed 's/^vol/vol-/' < /sys/class/nvme/${DISK%n[0-9]*}/serial
```

Cross-checked: the resolved id matched `DescribeVolumes` exactly — `vol-0ccc...ccc`,
10 GiB, `/dev/sdf`, on `i-0aaa...aaa`. Two independent methods agreed.

---

## 8. Required changes, ready to apply

### `lambda/enrich_disk_alarm.py`

1. **Add `fstype`** to the `SCHEMA()` clause (§2) or it resolves nothing.
2. **Fix label parsing** — index from the end to skip the `N - ` rank prefix (§7 step 1).
3. **80% warning → SNS naming instance, path and volume id.** Resolve the volume via
   `ssm:SendCommand` running `ebsnvme-id` on the host (§7 step 3), not `Attachments[].Device`.
4. **90% critical → invoke `DiskSpace-GrowVolume`** per breaching instance with the resolved
   `InstanceId` and `MountPath`, and **`DryRun: 'false'` explicitly** — the document defaults to
   `'true'` and is otherwise inert while reporting success. This also closes `findings.md §5`,
   where a fleet alarm cannot supply an `InstanceId`.
5. **Thresholds from environment variables**, not the hardcoded `80.0`/`90.0`, which diverge from
   the `WarningThreshold`/`CriticalThreshold` stack parameters.
6. **Remove the module-scope `os.environ["SNS_TOPIC_ARN"]`** or accept a `KeyError` on cold start.

### `ssm-documents/DiskSpace-GrowVolume.yaml`

7. **Replace the NVMe-serial resolver** — `/sys/block/$DISK/serial` returns nothing on AL2023.
   Use `ebsnvme-id`, with `/sys/class/nvme/<ctrl>/serial` as fallback.

### `cloudformation/`

8. **Add `fstype`** to every `SCHEMA()` in `20-alarms-dashboard.yaml` and `30-dashboard.yaml`.
9. **Keep `GROUP BY InstanceId`** — finer grouping adds no identity and triples contributors (§3).

### `ansible/roles/cw_agent/`

10. **Move `Environment`** into the `disk` section's `append_dimensions` (confirms §2.1).
11. **Add the 9 missing denylist entries** (§4) if `resources: ["*"]` is adopted.
12. **Consider `resources: ["*"]`** with the hardened denylist to close §1.3 for new volumes (§6).

### Not yet in the repo

13. **Add an automated dimension-contract check** asserting the alarm `SCHEMA()` set equals
    `{InstanceId, path, Environment, fstype}`. §2 showed a mismatch fails **green**, so no
    runtime signal catches it — this and the Phase 6 `list-metrics` gate are the only defences.

---

## 9. Corrections to earlier claims made during this session

Recorded because they were stated with more confidence than the evidence supported.

| Claim I made | Reality |
|---|---|
| "`GROUP BY InstanceId, path` gives identity without a Lambda" | **False.** No grouping puts identity in the alarm (§3) |
| "Match the volume via `Attachments[].Device`" (`findings.md §17.2` fix) | **Cannot work** on Nitro — the guest renames the device (§7 step 2) |
| "NVMe serial from `/sys/block/$DISK/serial`" (written into the runbook) | **Returns nothing** on AL2023; use `ebsnvme-id` (§7 step 3) |
| "`/boot/efi` is still publishing after the denylist fix" | **Measurement error** — my query window straddled the config change |
| "`ap-south-1` is the repo's Region" | **Wrong** — it is only a fallback default; all templates use `${AWS::Region}` |

---

## 10. Still untested

Named so the pilot is not read as broader validation than it is.

- **The Ansible controller and playbook path.** Configuration was applied via SSM Run Command, so
  `findings.md §1.1` (cross-account connection credentials), `§1.8` (playbook delivery) and `§1.9`
  (no controller in IaC) remain unverified.
- **Event-driven enrollment.** No EventBridge rules, no Config rules, no StackSet were deployed,
  so `§1.2`–`§1.7` and `§17.1` are untested.
- **Remediation.** `DiskSpace-GrowVolume` was never invoked; no volume was grown.
- **The enrichment Lambda.** Not deployed — the resolution chain in §7 was traced by hand.
- **Scale.** 2 instances. The 500-series cap, the 10,000-metric ceiling and the `PARTIAL_DATA`
  guard threshold are all untested at volume.
- **SNS.** No topics or subscriptions; alarm state transitions were the evidence.
