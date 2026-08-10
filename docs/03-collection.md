# 03 — Data Collection

## The problem: EC2 does not report filesystem fullness

Steps 1 and 2 built a way to reach every instance and run configuration on it. This step
answers what that configuration is *for* — and it exists at all only because **AWS does
not publish the metric this design needs.**

`AWS/EC2` covers CPU, network, and status checks. It has no disk-space metric. `AWS/EBS`
looks more promising and is not:

| `AWS/EBS` metric | Measures |
|---|---|
| `VolumeReadOps` / `VolumeWriteOps` | I/O operation counts |
| `VolumeReadBytes` / `VolumeWriteBytes` | Bytes transferred |
| `VolumeQueueLength` | Requests waiting |
| `VolumeTotalReadTime` / `VolumeTotalWriteTime` | Time spent servicing I/O |
| `VolumeIdleTime` | Time with no I/O |
| `VolumeThroughputPercentage` | Throughput against provisioned |
| `BurstBalance` | Remaining burst credits |
| `VolumeAvgIOPS` / `VolumeAvgThroughput` / `VolumeAvgReadLatency` | Performance averages |
| `VolumeIOPSExceededCheck` | Provisioned-IOPS breach signal |

Every one of these measures **I/O activity, not occupancy**. **None reports fullness.**
AWS's own EBS metrics documentation redirects the question elsewhere:

> *"To get information about the available disk space from the operating system on an
> instance, see View free disk space."*

### Why this is architectural, not an oversight

**EBS is a block device.** It serves numbered blocks and has no concept of a file, a
directory, or a filesystem. "How full is it?" is a *filesystem* question — it depends on
which filesystem was created on those blocks, its metadata overhead, its reserved-blocks
setting, and what the guest OS has written. **That knowledge exists only inside the guest
OS.** EBS could not answer the question even if AWS wanted it to.

The vivid case makes it concrete: **a completely full disk generates near-zero EBS
activity.** No write can succeed, so no write is reported. `VolumeWriteOps` falls,
`VolumeIdleTime` rises, `BurstBalance` sits at 100% — every EBS metric looks *healthier
than usual* — while the application is dying on `ENOSPC`.

**Therefore an in-guest agent is mandatory.** There is no AWS-side substitute, and no
amount of clever alarming on `AWS/EBS` recovers the signal.

### EBS metrics are still valuable — for a different failure mode

Worth stating so they are not dismissed entirely. They are the right tool for
**performance**, not **capacity**:

- `BurstBalance` trending to 0 on gp2 means **imminent throttling** — the volume is about
  to fall to its baseline IOPS.
- Sustained non-zero `VolumeQueueLength` means an **I/O bottleneck** — requests are
  queueing faster than the volume drains them.

Separate alarms, separate concern. Neither one tells you a filesystem is full.

---

## The four Ansible tasks

The role does exactly four things to the host. Everything else in `tasks/main.yml` is
verification or fact computation.

### 1. `ansible.builtin.package` — install the agent

```yaml
ansible.builtin.package:
  name: "{{ cw_agent_package_name }}"
  state: present
```

Amazon Linux repositories are **S3-backed**, so this succeeds in a no-egress VPC via the
**free S3 gateway endpoint** from Step 1 — no NAT, no internet route. `package` is the
generic module, so it resolves to dnf/yum/apt per distribution rather than pinning the
role to one package manager.

**First run:** `changed`. **Repeat run:** `ok` — `state: present` is satisfied, and
Ansible does not reinstall or upgrade.

### 2. `ansible.builtin.template` — render the config, validated first

```yaml
ansible.builtin.template:
  src: amazon-cloudwatch-agent.json.j2
  dest: "{{ cw_agent_config_path }}"
  validate: "python3 -c \"import json,sys; json.load(open('%s'))\""
notify: Apply CloudWatch agent configuration
```

`validate:` parses the JSON **before the file is installed** — Ansible renders to a
temporary path, runs the validator, and only then moves it into place. The reason this
guard earns its keep: **a malformed config stops the agent from starting, and since the
agent produces our metrics, that failure is silent.** Nothing alarms, because the thing
that would have alarmed is the thing that stopped. This is **the cheapest guard against
the worst failure mode in the design** — one line, and it converts a silent monitoring
outage into a loud task failure on the controller.

**First run:** `changed`, handler notified. **Repeat run with identical facts:** `ok`,
handler **not** notified.

### 3. `ansible.builtin.service` — started *and* enabled

```yaml
ansible.builtin.service:
  name: "{{ cw_agent_service_name }}"
  state: started
  enabled: true
```

`state: started` is the obvious half. **`enabled: true` is the half that gets forgotten,
and without it an instance silently stops reporting after its next reboot** — the agent
was running when Ansible last visited, so nothing ever flagged it, and with no scheduled
re-run (Step 2) nothing will.

**First run:** `changed`. **Repeat run:** `ok`. **After someone stopped the agent by
hand:** `changed` — and only this task changes.

### 4. The handler — `fetch-config`

```yaml
ansible.builtin.command:
  cmd: >-
    {{ cw_agent_ctl }} -a fetch-config -m ec2 -s -c file:{{ cw_agent_config_path }}
```

Writing the config file does not make the agent read it; `fetch-config -s` loads it and
restarts the agent. Because it is a **handler**, it fires **only when the config actually
changed**, so **re-runs do not disturb a healthy agent.** A restart is a brief gap in the
metric stream — cheap, but not free, and there is no reason to pay it on every run.

Reading from `file:` rather than `ssm:` is deliberate: the playbook bundle is the single
source of truth for configuration, so Parameter Store is not in this path.

A second handler on the same `listen:` confirms the agent is running afterwards, because
a config the agent rejects at *load* time would leave it stopped.

### What a run actually looks like

**First run — four changes:**

```
TASK [cw_agent : Install the CloudWatch agent]           changed: [i-0abc123]
TASK [cw_agent : Render the CloudWatch agent config]     changed: [i-0abc123]
TASK [cw_agent : Ensure the agent is running and enabled] changed: [i-0abc123]
RUNNING HANDLER [cw_agent : Apply CloudWatch agent configuration]
                                                          changed: [i-0abc123]
RUNNING HANDLER [cw_agent : Confirm the agent is running] ok: [i-0abc123]

i-0abc123 : ok=9  changed=4  unreachable=0  failed=0
```

**Unchanged re-run — the signature of correct idempotence:**

```
TASK [cw_agent : Install the CloudWatch agent]           ok: [i-0abc123]
TASK [cw_agent : Render the CloudWatch agent config]     ok: [i-0abc123]
TASK [cw_agent : Ensure the agent is running and enabled] ok: [i-0abc123]

i-0abc123 : ok=8  changed=0  unreachable=0  failed=0
```

**No handler line at all** — that absence is the point. Nothing was restarted.

**After someone stopped the agent — only the service task changes:**

```
TASK [cw_agent : Render the CloudWatch agent config]     ok: [i-0abc123]
TASK [cw_agent : Ensure the agent is running and enabled] changed: [i-0abc123]

i-0abc123 : ok=8  changed=1  unreachable=0  failed=0
```

The config was correct, so it was left alone and the handler never fired; only the
service was brought back to its declared state. **Convergence, not redeployment.**

---

## `ansible_mounts` — where the filesystem list comes from

`ansible_mounts` is an Ansible **fact**, not something the role queries. It is gathered
once at the start of a run by reading **`/proc/mounts`** and calling **`statvfs()`** on
each entry — **the same source `df` uses**, so the numbers agree with what an operator
would see on the host.

A realistic excerpt:

```python
ansible_mounts = [
  {"mount": "/",        "device": "/dev/nvme0n1p1", "fstype": "xfs",
   "size_total": 8578932736,  "size_available": 3221225472},
  {"mount": "/var",     "device": "/dev/nvme1n1",   "fstype": "xfs",
   "size_total": 53687091200, "size_available": 9663676416},
  {"mount": "/dev/shm", "device": "tmpfs",          "fstype": "tmpfs",
   "size_total": 4104773632,  "size_available": 4104773632},
]
```

`/dev/shm` is the instructive entry: `fstype: tmpfs` means it is **RAM, not disk**.
Monitoring it is both meaningless and billable — which is why the fstype filters exist.

---

## The two paths are separate

This is the key structural idea in Step 3, and the one most easily collapsed by accident.

```
  PATH 1 — CONFIGURATION (Ansible)          PATH 2 — DATA (CloudWatch agent)
  ────────────────────────────────          ──────────────────────────────────
  ansible_mounts (once per run)
        │  mount + fstype only
        ▼
  filter: real block devices                 agent reads config
        │                                          │
        ▼                                          ▼
  render amazon-cloudwatch-agent.json ──────▶ statvfs() every 60s
        │                                          │
        ▼                                          ▼
  handler: fetch-config -s                   PutMetricData → CWAgent
        │                                          │
        ▼                                          ▼
  Ansible exits. Nothing left running.       runs forever, alone
```

| | **Path 1 — Configuration (Ansible)** | **Path 2 — Data (CloudWatch agent)** |
|---|---|---|
| Question answered | *Which* filesystems exist | *How full* are they now |
| Nature of the answer | **Structural** — changes rarely | **Temporal** — changes constantly |
| Reads mounts | Once per run | **Every 60 seconds** |
| Produces | A config file | A continuous metric stream |
| Frequency | Occasionally, on trigger | **Always** |
| If it stops | Config goes stale | **Metrics stop — you are blind** |

**We use only `mount` and `fstype`.** `size_available` is deliberately **ignored** even
though Ansible hands it to us on a plate. **Ansible never carries a measurement** — it
writes a config file and leaves. The agent does all collection, independently and
indefinitely, and would keep doing it if the controller were deleted.

That separation is also what makes Step 2's "the controller is not in the data path"
claim true rather than aspirational.

---

## Why not let Ansible report usage, since it already has the numbers?

It is the obvious shortcut: `ansible_mounts` already contains `size_available`, so why not
compute a percentage and publish it?

**Because you would get a datapoint only at each run.** With event-driven-only triggering
(Step 2 — no schedule), a long-running instance may not be visited for weeks. A disk that
fills between runs is **invisible**: the alarm has no data covering the interval in which
the incident happened. Disk exhaustion is exactly the failure mode that develops on a
timescale of hours, which is the interval a config-management tool cannot see.

Confirming evidence from the ecosystem: **there is no Ansible module for
`cloudwatch:PutMetricData`** — not in `amazon.aws`, not in `community.aws`, not in
`amazon.cloud`. There are modules for alarms, log groups, and metric filters, but none for
publishing a datapoint. Doing it would mean shelling out to the AWS CLI on every host on
every run, which also means AWS CLI plus credentials on every host.

**Ansible was never intended as a metrics pipeline**, and the absence of the module is the
ecosystem saying so.

---

## The config, and the three cardinality guards

The template's loop enumerates mounts from the host's own facts:

```jinja
"metrics_collected": {
  "disk": {
    "measurement": ["used_percent"],
    "metrics_collection_interval": {{ cw_agent_collection_interval }},
    "drop_device": true,
    "ignore_file_system_types": {{ cw_agent_ignore_fstypes | to_json }},
    "resources": {{ cw_agent_monitored_mounts | to_json }}
  }
}
```

with `cw_agent_monitored_mounts` computed in `tasks/main.yml`:

```jinja
{{ ansible_mounts
   | selectattr('fstype', 'in', cw_agent_allowed_fstypes)
   | rejectattr('fstype', 'in', cw_agent_ignore_fstypes)
   | map(attribute='mount') | sort | list }}
```

CloudWatch bills per **unique dimension combination**, so metrics = instances × monitored
mounts. Three guards attack that product from different directions:

| Guard | Mechanism | Failure it prevents |
|---|---|---|
| **Enumerate from facts, never `resources: ["*"]`** | **Bounds the set** to known real filesystems | Unbounded metric count |
| **`ignore_file_system_types`** | Filters pseudo-filesystems at the agent | The overlay explosion on container hosts |
| **`drop_device: true`** | **Removes a dimension** | Multiplication, not just extra rows |

**On `resources: ["*"]`.** It looks harmless and self-maintaining. On a container host it
publishes **one metric per container layer — dozens to hundreds per instance**. At 1,000
instances the projection is roughly **11x the monthly bill** versus the filtered set, and
**nothing warns you.** A month later the invoice arrives.

**On `ignore_file_system_types`.** `overlay` is the important entry; `tmpfs` and
`squashfs` matter too (RAM and snap mounts respectively). This is a second line of defence
behind the fact-based enumeration — but it is **weaker than it looks**, because a denylist
can only exclude what it names. The pilot found nine types missing from it; see below.

**On `drop_device: true`.** This one is qualitatively different from the other two. The
others *filter rows*; `drop_device` **removes a dimension from the metric identity**, so a
filesystem that appears under two device paths collapses to one metric instead of two. It
**prevents multiplication rather than merely filtering.**

What it does **not** do — and the pilot settles this, `tested_findings.md` §2 — is leave a
three-dimension metric. **`drop_device: true` removes `device` only. `fstype` survives**,
so the emitted set is `InstanceId, path, Environment, fstype`. The guard is real; the
inference that it produces a minimal dimension set was wrong, and the alarm's `SCHEMA()`
clause has to name `fstype` too (see the dimension contract below).

**The config also adapts automatically.** Attach and mount a new volume, and the next run
includes it with no code change. A hand-maintained static list cannot do that — it either
drifts behind reality or gets replaced by `["*"]` out of frustration, which is precisely
the expensive mistake.

### ⚠️ The allowlist fails closed; a denylist fails open

The two filters look symmetric and are not, which the pilot demonstrated the hard way
(`tested_findings.md` §4). Run with `resources: ["*"]`, `ignore_file_system_types`
suppressed **25 of 28** mounts on Amazon Linux 2023 — and **`vfat` (`/boot/efi`) leaked
through and became a billable metric.** Nine pseudo-filesystem types present on AL2023
were absent from the original 18-entry denylist:

```
vfat  ramfs  efivarfs  pstore  bpf  selinuxfs  securityfs  hugetlbfs  rpc_pipefs
```

They are now in `defaults/main.yml` (29 entries), and after the change `/boot/efi`
**stopped publishing** — 0 datapoints in a window strictly after it.

| | `cw_agent_allowed_fstypes` (allowlist) | `ignore_file_system_types` (denylist) |
|---|---|---|
| Admits | Only `ext*`, `xfs`, `btrfs` | Everything not named |
| An unknown new fstype | **Excluded by construction** | **Admitted and billed** |
| Failure direction | **Closed** — a real filesystem might be missed | **Open** — junk becomes a metric |

**This is why the allowlist is the primary control and the denylist is defence in depth,
not the other way round.** An allowlist can only ever under-monitor, which is visible; a
denylist can only ever over-bill, which is not — and doc 06 explains why there is no way
to un-bill a metric once it has been published.

### The `resources: ["*"]` trade, now that it has been measured

The pilot also proved the *upside* that made `["*"]` tempting in the first place, and it is
larger than the desk review credited (`tested_findings.md` §6). A volume was attached,
formatted, mounted and filled to 30% on a running instance and **appeared in CloudWatch
with no agent reconfiguration and no restart** — the agent's `ActiveEnterTimestamp` never
moved. Contrast the fact-based enumeration, which is correct but is a **snapshot**: the
same test on the other instance left a 40%-full volume **entirely invisible** until the
config was re-rendered by hand.

| Approach | New mount picked up | Junk excluded |
|---|---|---|
| Enumerate from `ansible_mounts` (this design) | Yes — but **only on the next run, which needs a trigger** | Fully, by allowlist |
| `resources: ["*"]` + hardened denylist | **Yes, automatically, no trigger** | Only if the denylist is complete |

**The design keeps the allowlist**, because the cost failure is silent and the coverage
failure is at least discoverable. But `["*"]` plus the 29-entry denylist is now a
*measured* option rather than a rejected one, and it is the better answer specifically for
the new-volume case — see doc 05's drift discussion.

---

## The `Environment` dimension

`Environment` is emitted from **inside the `disk` section**, not from the `metrics`-level
`append_dimensions` where it looks like it belongs:

```json
"metrics": {
  "append_dimensions": {
    "InstanceId": "${aws:InstanceId}"
  },
  "metrics_collected": {
    "disk": {
      "append_dimensions": {
        "Environment": "{{ cw_agent_environment }}"
      }
    }
  }
}
```

### ⚠️ Why the placement is not a style choice

**The `metrics`-level `append_dimensions` block supports exactly four keys** —
`ImageId`, `InstanceId`, `InstanceType`, `AutoScalingGroupName` — because it is the
EC2-metadata enrichment hook, not a general dimension bag. **Anything else there is
silently dropped.** No parse error, no log line, no rejected config: the agent starts
happily and simply does not emit the dimension.

This was verified live (`tested_findings.md` §1). `Environment` appeared as a dimension
**only** when it was moved into the `disk` section's own `append_dimensions`. A
per-section `append_dimensions` block is a plugin-level feature and takes arbitrary
key/value pairs; the top-level one does not.

The failure compounds badly, which is why it is worth a warning block rather than a
footnote. A `metrics`-level `Environment` produces metrics carrying
`InstanceId, path, fstype` — three dimensions, not four — so **every `WHERE Environment =
'prod'` clause in doc 04 matches nothing**, and by doc 04's own `TreatMissingData:
notBreaching` the alarms report green. Two silent failures stacked: a dropped dimension
and an alarm that looks calm because it is measuring nothing.

**Why it exists:** doc 04 scopes alarms **per account and per environment**, which is what
allows differentiated thresholds — **prod 80/90, dev 90/95**. Without the dimension there
is nothing to write `WHERE Environment = 'prod'` against, and every environment would
share one threshold: either too noisy for dev or too slack for prod.

**Where the value comes from:**

```yaml
cw_agent_environment: "{{ ec2_tags.Environment | default('unscoped') }}"
```

- **`ec2_tags`, not `tags`.** The `tags` hostvar is deprecated in the `amazon.aws.aws_ec2`
  inventory plugin, with removal after **2026-12-01**. Note precisely what is deprecated:
  **that one variable name in that plugin** — hostvars themselves are not deprecated.
- **`default('unscoped')` is load-bearing.** An untagged instance would otherwise fail the
  template render, and **inconsistent tagging is expected in an acquisition-heavy estate**
  — an acquired account's instances arrive with whatever conventions that team used.
  Failing the run would mean the least-governed instances are the ones that get *no*
  monitoring, which is backwards. Instances landing in `unscoped` are then surfaced by AWS
  Config compliance, so the gap is visible rather than fatal.

**It costs nothing in cardinality.** `Environment` is **functionally dependent** on
`InstanceId` — one instance has exactly one value — so it **relabels** metrics that
already exist rather than creating new ones. Metric count stays instances × mounts.

Contrast a dimension that varies *independently* of the others — `process_name`, say, or a
request path. Those multiply: every distinct value against every instance and mount is a
new billable metric. **The test for whether a dimension is free is whether it can take
more than one value for a given instance.**

---

## Metric scope: one metric, 60 seconds

| Setting | Value | Reason |
|---|---|---|
| `measurement` | `used_percent` only | The one number the alarm needs |
| `metrics_collection_interval` | `60` | Standard resolution |
| `namespace` | `CWAgent` | The alarms query `SCHEMA("CWAgent", ...)` |

Collecting only `used_percent` is a deliberate narrowing. `inodes_free`, `used`, `total`,
and `free` are all available and each would be another billable metric per mount per
instance — for information the percentage already implies for our purposes.

**Why not faster than 60 seconds:**

- Anything below 60s becomes a **high-resolution** metric, which costs **3x per alarm**.
- High-resolution data is **not supported by Metrics Insights** — *"Metrics Insights does
  not support high-resolution data"* — and such data is **aggregated to one-minute
  granularity** in Metrics Insights queries anyway. Since doc 04's alarms are Metrics
  Insights queries, sub-minute collection would cost triple and deliver **nothing**.

**Collection frequency does not affect metric cost at all.** A metric costs the same
whether it receives one datapoint an hour or one a minute; **only the number of unique
dimension combinations is billed.** This is worth internalising because it inverts the
usual intuition — the lever is cardinality, never frequency.

---

## ⚠️ The Step 3 ↔ Step 4 dimension contract

**The dimensions the agent emits must match the alarm's `SCHEMA()` clause exactly.**

```
Step 3 emits:  InstanceId, path, Environment, fstype
                    (path and fstype are both added by the disk section)
Step 4 queries: FROM SCHEMA("CWAgent", InstanceId, path, Environment, fstype)
```

**Four dimensions, not three.** This is now an empirical fact rather than a reading of the
documentation: the pilot ran both clauses side by side against identical data
(`tested_findings.md` §2) and got

| Dimensions named in `SCHEMA("CWAgent", …)` | What the query returned |
|---|---|
| `InstanceId, path, Environment` — three | *"No time series were returned by the query."* |
| `InstanceId, path, Environment, fstype` — four | *"2 time series evaluated to OK"* |

`SCHEMA()` is an **exact-set** match, not a subset match. `SCHEMA("CWAgent", InstanceId)`
asks for metrics with **only** that dimension — of which there are none. **A mismatch
matches nothing**, and the alarm has no metrics to evaluate.

### It fails worse than `INSUFFICIENT_DATA`

The intuitive expectation — and what earlier drafts of this document asserted — is that a
zero-match query leaves the alarm in `INSUFFICIENT_DATA`, where doc 04's
`InsufficientDataActions` catches it. **It does not.** Combined with `TreatMissingData:
notBreaching`, which this design sets deliberately so that scale-in does not page anyone, a
query matching zero series reports a reassuring green **`OK` — indefinitely**:

```
State  : OK
Reason : "No time series were returned by the query. Treat missing data is configured as [NonBreaching]."
```

So the compensating control **never fires**. There is no red state, no
`INSUFFICIENT_DATA`, no error — the alarm is not "unsure", it is *confident and wrong*.
"Looking calm while monitoring nothing" is literal, and this is **the most consequential
single failure mode in the design** precisely because nothing anywhere reports it.

`drop_device: true` still serves this contract — it removes `device`, which genuinely does
shift across reboots, so what remains is stable enough for a hardcoded `SCHEMA()` clause
to name. It just does not make that set as small as it appears.

⚠️ **This contract is not machine-enforced today.** Changing `append_dimensions` in the
agent template without making the matching change to `SCHEMA()` in
`cloudformation/20-alarms-dashboard.yaml` produces no error anywhere — and because the runtime
symptom is a green `OK` rather than a failure, nothing downstream reveals it either. **Treat
the two files as a single unit and always edit them together.** Asserting the dimension set in
CI is the single highest-value hardening available to this repo; until it exists, the Phase 6
`list-metrics` gate in doc 07 is the only place a mismatch is caught.

**Verify against a real instance before finalizing:**

```bash
aws cloudwatch list-metrics \
  --namespace CWAgent \
  --metric-name disk_used_percent
```

**AWS documentation disagrees with itself** on whether the disk dimension is `Partition`
or some combination of `device`/`fstype`/`path`, and the answer varies by agent version
and configuration. **This must not be guessed** — the emitted set is an empirical fact
about the deployed agent, and one `list-metrics` call settles it. For the agent version and
configuration in this repo, on Amazon Linux 2023, that call has now been made and the
answer is `InstanceId, path, Environment, fstype`.

---

## Build vs reuse: why a first-party role

`christiangda.amazon_cloudwatch_agent` is the strongest community option — roughly **492k
downloads**, actively maintained, and it even supports an `onPremise` mode for hybrid
fleets. It was evaluated seriously:

| Consideration | Finding |
|---|---|
| Licence | **GPLv3** — a licensing conversation we do not need for 4 tasks |
| Platform coverage | **Linux-only**; this estate includes Windows |
| Tested platforms | List stops at **RHEL 8 / Ubuntu 22.04** |
| Scope | Handles many collection scenarios; we need exactly one |

Our actual need is **one templated JSON block plus a service handler.** A thin first-party
role is smaller than the configuration surface required to bend a general-purpose role to
this shape, and it keeps the **cardinality guards — the part with real money attached —
in code we own and test.** This is a documented build-vs-reuse call, not an oversight.

---

## Role portability constraint

The role uses **only `ansible.builtin` modules** and depends on **no external
collections**.

Two consequences:

1. It is **independent of the controller's Ansible version** — builtin modules ship with
   `ansible-core`, so there is no collection version to keep in step.
2. It would **still work if the on-node execution model were ever revisited** — the
   `AWS-ApplyAnsiblePlaybooks` path rejected in Step 2 runs Ansible on the instance, where
   collections are not installed. Keeping the role builtin-only means that door stays
   open at zero cost.

Note this constrains the **role**, not the playbook: the *connection* is
`amazon.aws.aws_ssm` and the *inventory* is `amazon.aws.aws_ec2`, both controller-side.
The role itself stays clean.

This is a convention to hold on review: every FQCN in `tasks/main.yml` should start with
`ansible.builtin.`, and adding a collection-dependent module silently forfeits both properties
above.

---

## Limitations

- **The agent is a component that can fail.** If it stops, metrics stop, and with no
  scheduled re-run (Step 2) nothing repairs or detects it. Doc 04's
  `InsufficientDataActions` was described as the compensating control; the pilot showed
  that with `TreatMissingData: notBreaching` **it does not fire on a zero-series query**
  (`tested_findings.md` §2), so the real compensating controls are the metric-count guard
  alarm and the CI dimension-contract test. A periodic convergence run and an explicit
  coverage check (doc 07 future work) remain the honest answer here.
- **`used_percent` only.** Inode exhaustion is a real way to fill a filesystem while
  `used_percent` reads comfortably — a directory full of tiny files. Not covered here.
- **Configuration is a snapshot.** A volume mounted after the last Ansible run is not
  monitored until the next run. The config adapts automatically *when it runs*, and Step 2
  has no schedule. **Reproduced live** (`tested_findings.md` §6): a 5 GiB volume filled to
  40% was invisible in CloudWatch — the path simply absent from the index — while AWS
  Config still reported COMPLIANT. `resources: ["*"]` with the hardened denylist closes
  this case with no trigger at all; see the trade-off table above.
- **A 60-second interval means up to a minute of blindness**, plus CloudWatch ingestion
  latency, plus the m-of-n evaluation in doc 04. Detection is minutes, not seconds — which
  is appropriate for disk growth and would not be for a latency spike.
- **`Environment` accuracy depends on tagging.** `unscoped` instances publish metrics that
  no environment-scoped alarm evaluates. Step 2's tag-change trigger and AWS Config
  compliance both exist to shrink this window, not to eliminate it.

---

## Files

- [`ansible/roles/cw_agent/tasks/main.yml`](../ansible/roles/cw_agent/tasks/main.yml) — the four tasks, SSM verification, mount selection
- [`ansible/roles/cw_agent/handlers/main.yml`](../ansible/roles/cw_agent/handlers/main.yml) — `fetch-config -s` and the post-apply status check
- [`ansible/roles/cw_agent/defaults/main.yml`](../ansible/roles/cw_agent/defaults/main.yml) — fstype lists, interval, namespace, `Environment` sourcing
- [`ansible/roles/cw_agent/templates/amazon-cloudwatch-agent.json.j2`](../ansible/roles/cw_agent/templates/amazon-cloudwatch-agent.json.j2) — the rendered config and the cardinality guards
