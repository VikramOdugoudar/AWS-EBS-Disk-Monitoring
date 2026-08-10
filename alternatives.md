# Alternatives considered

Every option evaluated during design, with the reason it was not chosen. This is a
consolidated reference — each decision is also argued in context in `docs/01`–`docs/07`.

Two conventions used throughout:
- **Rejected** — evaluated and not chosen, with the deciding reason stated.
- **Deferred** — a good idea whose value does not yet justify its cost. Listed in
  `limitations.md` under future work rather than here.

Four decisions were **reversed** mid-design. Those are marked ⟲ and the reversal is
explained, because the reasoning matters more than the conclusion.

---

## 1. Access to instances

### Chosen — AWS Systems Manager (SSM)
Outbound-only agent, IAM-based authorization, CloudTrail attribution per command, works in
private subnets with no inbound rules.

| Rejected | Why |
|---|---|
| **SSH with bastion hosts** | Key generation, distribution and rotation across ~50 accounts; inbound port 22; a bastion fleet that must itself be patched and monitored; no per-command audit trail without building one. The failure mode that matters in practice: an ex-employee's key still present on a host nobody remembers. |
| **EC2 Instance Connect** | Solves key *distribution* (push-on-demand, 60-second validity) but is still SSH, still needs network reachability, and is **interactive-only** — no scheduled automation. |
| **Third-party agent** (Datadog, Zabbix, …) | The brief explicitly says evaluate before buying. It is also a second credential system, a second agent to deploy and patch, and a second bill. |

---

## 2. Instance credentials

### Chosen — one standardized IAM instance profile
`AmazonSSMManagedInstanceCore` + `CloudWatchAgentServerPolicy`. One profile serves unlimited
instances (one-to-many), so there is no per-instance IAM object; the quota is **one profile
*per instance***, which is why a single profile carries both policies.

| Rejected | Why |
|---|---|
| **Default Host Management Configuration (DHMC)** ⟲ | Reconsidered on merit after IMDSv2 became a stated assumption, and still lost. **They conflict rather than compose**: *"SSM Agent attempts to use instance profile permissions **before** using the Default Host Management Configuration permissions"* — so a profile takes precedence and DHMC is bypassed wherever one exists. Also all-or-nothing per account/Region (*"applies to **all** managed EC2 instances"*), activation needed per account **and** Region with up to 30 minutes' propagation, breaks if `/var/lib/amazon/ssm` is removed, and hides permissions in an invisible account-level setting rather than the instance's own visible configuration. Finally, it is unverified whether `AmazonSSMManagedEC2InstanceDefaultPolicy` includes `cloudwatch:PutMetricData` — if not, a profile is needed anyway, collapsing the benefit entirely. |

⟲ **The reversal:** DHMC was first rejected because it requires IMDSv2 and an M&A estate holds
IMDSv1 instances, making migration a prerequisite. Once IMDSv2 became a stated assumption that
objection vanished, so it was re-examined — and rejected again for the stronger reason above.
An earlier version of this design assumed *"DHMC grants SSM permissions regardless of an existing
role"*; the docs say the opposite.

---

## 3. Cross-account trust

### Chosen — `aws:PrincipalOrgID` on trust policies, `aws:ResourceOrgID` on the caller

**Both keys are needed, on opposite sides, and they are not interchangeable.**
`aws:PrincipalOrgID` describes the org of the *caller*; `aws:ResourceOrgID` describes the
org of the *resource being accessed*. Workload role trust policies bound the caller, so
they use the former. The controller's `sts:AssumeRole` policy names a **wildcard account**
(`arn:aws:iam::*:role/DiskMonitoring*Role`) and must bound the *target*, so it uses the
latter — `aws:PrincipalOrgID` there would be **tautologically true** (the caller is always
the controller, always in the org) and would restrict nothing while appearing to.

| Rejected | Why |
|---|---|
| **`sts:ExternalId`** | ExternalId solves the **confused deputy** problem, which is inherently a *third-party* scenario: a vendor serving many customers, where without a per-customer secret one customer could trick the vendor into acting on another's account. The vendor is the "deputy" that can be confused. **Inside one Organization there is no third party and no deputy.** `aws:PrincipalOrgID` is stronger — enforced by AWS from org membership and impossible to leak, whereas an ExternalId is a shared string sitting in IaC. Adding it would cargo-cult a control past its threat model and imply a threat that does not exist. |

---

## 4. Ansible execution model

### Chosen — standing EC2 controller pushing over Session Manager ⟲
`amazon.aws.aws_ssm` connection plugin + `amazon.aws.aws_ec2` dynamic inventory, accounts
enumerated at runtime via `organizations:ListAccounts`.

| Rejected | Why |
|---|---|
| **`AWS-ApplyAnsiblePlaybooks` on-node** (Ansible runs on each instance, triggered by State Manager) ⟲ | Genuinely attractive: zero-touch enrollment within minutes and no controller at all. But **change management was materially heavier** — package the playbook, upload it to every account's bucket, then update a version pointer to trigger rollout, versus editing the playbook and running it. Ad-hoc and single-host runs were awkward. It required Ansible on **every node** with no version pinning (`InstallDependencies: True` installs whatever the distro repo ships). Linux-only: *"Associations that run Ansible playbooks aren't supported on macOS."* And with no-egress VPCs, `SourceType: GitHub` is unreachable (`aws:downloadContent` runs *on the node*, and there is no VPC endpoint for GitHub), so it would have forced `SourceType: S3` regardless. |
| **Ephemeral CI runner** | No host to patch, no standing credentials, version pinned in the image — the strongest alternative. Rejected in favour of a standing host always available for ad-hoc runs. Worth revisiting if controller maintenance becomes a burden. |
| **Ansible as the metrics collector** (run `df` on a schedule and publish) | Ansible runs at intervals; the agent runs continuously. A scheduled tool produces datapoints only when it runs, so a disk filling between runs is invisible. Confirming evidence: **there is no Ansible module for `cloudwatch:PutMetricData`** in `amazon.aws`, `community.aws`, or `amazon.cloud` — it would have to shell out to the CLI on every host on every run. Ansible was never intended as a metrics pipeline. |
| **Native SSM documents only, no Ansible** (`AWS-ConfigureAWSPackage` + `AmazonCloudWatch-ManageAgent`) | Simplest possible design and fully AWS-maintained. Loses **fact-driven mount enumeration**: Parameter Store holds a *static* config, so you would need one config per host shape or fall back to `resources: ["*"]` and pay ~11× the metric cost. Also does not satisfy the brief's request for Ansible artifacts. |

⟲ **The reversal:** on-node was chosen first, then reversed. Two objections were overstated and
corrected in the second pass: the controller is **not a SPOF for monitoring** (it is not in the
data path — if it is down, configuration cannot be deployed, but the agent keeps publishing,
alarms keep evaluating, remediation keeps working), and enrollment latency is a **scheduled-run
delay, not a monitoring gap**.

---

## 5. Playbook and module distribution

### Chosen — one central S3 module-transfer bucket
Unavoidable, not a design choice: Ansible wraps each task's module source into one
self-contained ~50 KB file and **copies it to the target**. SSH carries those files natively;
**Session Manager has no file-transfer channel**, so they stage through S3. Docs: *"required
even for modules which do not explicitly send files (such as `shell` or `command`)."* There is
no `file_transfer_method` option — all five transfer-related options configure the bucket rather
than bypass it.

| Rejected | Why |
|---|---|
| **Per-account transfer buckets** ⟲ | Smallest blast radius and no cross-account bucket policy. But contents are **transient** — seconds of lifetime, ~4 files per host per run, nothing durable — so the isolation benefit is small, while one bucket means one config value and nothing per-account to maintain. |
| **Versioning enabled on the bucket** | Module files carry **interpolated task parameters**, which can embed secrets. With versioning on, deleted objects persist in version history in cleartext. Versioning is therefore deliberately **off**, with SSE, a ~1-day expiry and a TLS-only policy instead. |
| **GitHub as the playbook source** | `aws:downloadContent` runs *on the node*, and there is no VPC endpoint for GitHub, so a no-egress VPC cannot reach it. |

---

## 6. Multi-account inventory

### Chosen — `scripts/render_inventory.sh` renders one inventory file per account
The account list is fetched from `organizations:ListAccounts` at runtime, so **nothing is
hand-maintained** and a new account is picked up with no edits.

**Why one file per account is unavoidable:** AWS APIs are per-account —`DescribeInstances`
returns instances in *the account whose credentials were used*, and no "list instances across
the org" API exists. The plugin's config expresses **multiple Regions but only one identity**,
because Regions are just a parameter on the same call with the *same* credentials (so the plugin
loops them internally) whereas each account needs *different* credentials via `sts:AssumeRole`
and its own authenticated client. Verified: `assume_role_arn` and `profile` are **single
strings**; only `regions` is a list. Ansible merges every source in a directory, so N files is
simply **how N identities get expressed**.

| Rejected | Why |
|---|---|
| **Custom inventory plugin** | ~100 lines of Python holding multiple credential contexts would give one file with no generation step — genuinely closer to "fully dynamic". Rejected because it replaces the AWS-maintained plugin with code we own, test and patch. Documented as the cleaner refinement. |
| **Hand-committed inventory per account** | Works, but "add an account" becomes a manual edit — the exact failure mode this design exists to prevent. |

---

## 7. Cross-account metric centralization

### Chosen — CloudWatch cross-account observability (OAM)
**Bilateral consent:** the sink policy says who *may* attach; the link says who *does*. Metric
sharing is **free**, and no data moves — the monitoring account gains query access rather than
copies, so there is no pipeline to fall behind or backfill.

| Rejected | Why |
|---|---|
| **CloudWatch Metrics Centralization** (org rules that physically replicate metrics; GA June 2026) | **Deciding factor:** *"all metrics from source accounts are centralized. Selective metric filtering is not supported at this time."* We could not scope to `CWAgent`, so **every** custom/EMF/OTLP metric in every account would replicate — with destination metric-quota risk from metrics unrelated to this project. It also **excludes AWS service metrics** (no `AWS/EC2`, `AWS/EBS`), so central correlation of CPU or EBS I/O alongside disk is impossible. Its headline advantage — cross-Region — is unused at single-Region scope. Heavier governance (Organizations trusted access + a service-linked role from the management/delegated-admin account), very new, and replication is a pipeline to watch. **It becomes the right answer for multi-Region.** See `docs/08-alternative-centralization.md` for the full evaluation. |
| **Cross-account metric push** (instances call `PutMetricData` into the monitoring account) | **Blast-radius inversion** — every instance in every account would hold credentials to write into central monitoring, so one compromised host can flood or poison monitoring for the entire estate, and **monitoring data is exactly what an attacker wants to suppress**. Also loses local visibility (account owners could not see their own instances), and destroys trustworthy attribution: with OAM `AWS.AccountId` is applied by AWS, whereas here it would be a dimension the *instance* sets and therefore spoofable. Contradicts the whole model of instances holding minimal, local permissions. |
| **Metric streams → Firehose → S3/OpenSearch/third-party** | Costs per metric update **plus** per-GB Firehose **plus** destination storage, where OAM is free. You own the pipeline (delivery failures, buffering, retries, backfill) and rebuild alarming in the destination. Right only for retention beyond CloudWatch's 15 months or non-AWS correlation. |
| **Per-account alarms only** (each account alarms into a shared SNS topic) | Sidesteps the metric ceiling, but gives alerts with **no unified view** — failing the brief's "centralize and present" — multiplies alarm management by N accounts, and permits no cross-account ranking. A reasonable *complement*, not a replacement. |
| **Legacy `CloudWatch-CrossAccountSharingRole`** | Pre-OAM. Enables dashboards and cross-account alarms but **not** the unified Metrics Insights query surface the alarm design depends on, with more per-account IAM to manage. |

---

## 8. Alarm mechanism

### Chosen — Metrics Insights alarms, per account per environment
The **only** alarm type accepting multi-series queries, and it **auto-adopts resources**: *"any
resource that matches your query definition… joins the alarm monitoring scope"*, so a new
instance needs no alarm created.

| Rejected | Why |
|---|---|
| **One alarm per VM per mount** | 3,000 alarms at 1,000 VMs × 3 mounts, created on launch and deleted on terminate, reconciled constantly against ASG churn. The fatal flaw is not tedium — **a missed creation leaves an instance silently unmonitored**: no error, discovered during the outage. |
| **`SEARCH()` inside an alarm** | *"A search expression cannot be used within an Alarm."* An alarm must resolve to **one deterministic state** to decide whether an action fires, and `SEARCH` returns an arbitrary, unordered number of series. (It *is* used on the dashboard, where no such constraint exists.) |
| **Metric math** | *"Alarms based on metric math expressions can reference a maximum of 10 metrics. This is a hard limit that cannot be increased."* Ten metrics is about three instances. |
| **`AVG` as the statistic** | Hides the failure it is meant to catch: 999 hosts at 20% plus one at 100% averages to ~20% and never fires. A single host with mounts at 45/94/20% averages to 53%, under an 80% threshold while a filesystem is nearly full. **`MAX` always.** |
| **Fleet-wide alarms instead of per-account-per-environment** ⟲ | A single global threshold is either too noisy for dev or too late for prod. Granularity is **free** — billing follows what the filter *matches* — so partitioning costs nothing and enables differentiated thresholds plus per-team routing. |
| **Grouping by mount rather than instance** | 3,000 contributors at 1,000 VMs **exceeds the 500-series return cap**, so `ORDER BY` would silently decide which are evaluated. Grouping by instance gives 1,000 contributors, comfortably inside it; the mount detail is recovered by the dashboard and the enrichment Lambda. **Vindicated live**: on 2 hosts with 6 filesystems, `GROUP BY InstanceId` gave 2 contributors and `GROUP BY InstanceId, path` gave 6 — and **both named the instance equally: not at all.** Finer grouping triples the contributor count and returns nothing operationally, because identity lives in the query *result*, never in the alarm. |
| **Filesystem UUID as a dimension** | `device` names (`nvme1n1`) do shift across reboots, but `drop_device: true` already removes that dependency — **and only that one**: it removes `device`, *not* `fstype`, so the emitted set is `InstanceId, path, Environment, fstype`, confirmed against a live instance during the pilot. A UUID changes on every reformat or instance replacement, creating a **brand-new billable metric** each time while the old one lingers — under immutable infrastructure abandoned metrics accumulate and are all billed. Also unreadable alerts, and **an extra, unstable member of `SCHEMA()`'s exact-set match** — which the pilot showed is unforgiving: one wrong dimension matches nothing and the alarm reports green `OK` forever. **The framing:** a UUID answers *"is this the same physical filesystem?"* while disk monitoring asks *"is the thing my application writes to running out of space?"* — a **mount-point** question. |
| **EBS `VolumeId` as a dimension** | **No fullness metric exists** — `AWS/EBS` is entirely I/O. The agent runs in the OS and has no concept of `vol-xxx`. And the mapping is not 1:1: LVM/RAID means many volumes → one filesystem, partitioning means one volume → many filesystems, instance store has no `vol-` id, tmpfs has no volume at all. |
| **Per-application scoping via resource tags** | **`WHERE tag.X` does not work for `CWAgent`.** CloudWatch's tag-enrichment covers only an allowlist (~70 `AWS/*` namespaces plus `ContainerInsights`, `Glue`, `LambdaInsights`, `CloudWatchSynthetics`), so the intuitive approach fails **silently**. Scope must be a metric *dimension* instead — which is what `Environment` is. |
| **Per-instance alarms for granularity** | This is approach 1 again: 2,000 alarms, lifecycle churn, silent gaps. |

**Cost note, corrected:** Metrics Insights is **not** more expensive than per-VM alarms. Like
for like (both thresholds): per-VM = 3,000 metrics × 2 = 6,000 alarms = **$600**; Metrics
Insights = 2 alarms × 3,000 analyzed = **$600**. Identical, so auto-adoption is free. No alarm
type is cheaper — standard is also $0.10, high-resolution $0.30, composite flat-rate but
referenced alarms still bill.

---

## 9. Enrollment and discovery

### Chosen — `DiskMonitoring` tag + three EventBridge rules + AWS Config auto-remediation

| Rejected | Why |
|---|---|
| **Scheduled sweep as the enrollment mechanism** ⟲ | Deferred rather than rejected outright: event-driven enrollment is faster and cheaper. But it means **a missed or failed event leaves an instance unconfigured with nothing to catch it**, and no periodic re-run means drift is neither repaired nor detected. **Reproduced live**: attaching a volume fires no event of any kind, so a volume filled to 40% on a running instance stayed unmonitored until a hand re-render. Note the sweep is **not** the only fix for that particular case — `resources: ["*"]` with a complete denylist picks up new volumes with no trigger at all (§12) — but it remains the only fix for a stopped agent or an edited config. See `limitations.md`. |
| **Custom Lambda for tag remediation** | Only needed if the tag **value** requires logic (enable prod, skip sandbox, honour an opt-out list). `AWS-SetRequiredTags` applies a fixed value, which suits unconditional enrollment. Worth knowing: **a Config remediation action *is* an SSM Automation document**, so the "Lambda or SSM doc that tags per Config finding" pattern is exactly what this is — AWS simply ships the document. |
| **SCP blocking untagged `RunInstances`** | Hard prevention, but it will eventually block a legitimate deployment at an inconvenient moment, and a monitoring tag is thin justification for failing a launch. **Detect-and-fix beats prevent-and-break** for a monitoring concern. |
| **Targeting all managed nodes instead of requiring the tag** | Removes the untagged gap but gives no opt-out, runs against every node including untested ones, and a bad change hits everything at once. |
| **ASG lifecycle hooks** | Cover only ASG-managed instances, missing standalone ones — and a failed hook can block scaling. |
| **Userdata as the configuration mechanism** | Runs once at boot, so no drift correction, and changing it requires new AMIs or launch-template versions. |
| **Cron on each instance** | Every host independently pulling config, with no central visibility of success or failure. |
| **EventBridge on Organizations events for new-account onboarding** | Conceptually the cleanest trigger, but Organizations events are emitted **only in us-east-1** (needing a cross-Region event bus) and could fire **before** the StackSet has finished, so the controller could not yet reach the instances. Stack `CREATE_COMPLETE` is naturally ordered and stays in-Region. |

---

## 10. Provisioning

### Chosen — service-managed CloudFormation StackSets, auto-deploy to an OU, **split into two templates** ⟲

| Rejected | Why |
|---|---|
| **Self-managed StackSet permissions** | Requires an `AWSCloudFormationStackSetExecutionRole` **pre-created in every target account** — itself a manual per-account step, defeating the purpose. |
| **One combined workload template** ⟲ | VPC IDs are **globally unique**, so a parameter naming `vpc-aaa` is valid in exactly one account. Six VPC-specific resources **contaminated** twelve parameter-free ones, so the whole stack could not deploy without parameters valid in only one account — breaking the "move the account into the OU" claim. |
| **Per-account `--parameter-overrides`** | Works, but "move it into the OU" becomes "…then look up its VPC, subnets, route tables and security group" — the manual runbook this design exists to avoid. |
| **Custom-resource VPC auto-discovery** | An acquired account may have several VPCs, and picking the wrong one puts endpoints where no instances run: **the stack succeeds while instances silently cannot reach SSM**. A confident wrong answer is worse than a required input. |
| **Terraform** | Familiar and multi-cloud friendly, but needs remote state, a runner, and explicit per-account looping to match StackSets' auto-enrol. |
| **Ansible for account-level bootstrap** | Keeps everything in one tool, but Ansible is a poor fit for account-level provisioning and drift on org structure. |

---

## 11. Network path

### Chosen — VPC interface endpoints + free S3 gateway endpoint (no NAT assumed)

| Rejected | Why |
|---|---|
| **NAT Gateway** | Comparable fixed cost (~$98/mo for 3 AZs vs ~$88/mo for 4 endpoints × 3 AZs — contradicting the assumption that endpoints are pricier), but **4.5× more expensive per GB** ($0.045 vs $0.01) and it charges for S3 traffic that the gateway endpoint carries **free**. The stronger argument is security: NAT grants a route to **the entire internet**, whereas endpoints grant a route to four named AWS services and nothing else. For a monitoring agent, internet access is attack surface, not a feature. **Where NAT does exist, still add the free S3 gateway endpoint** — it diverts the highest-volume traffic (module transit, agent package, OS repos) away from NAT's per-GB charge at zero cost. |

---

## 12. Metric scope and cardinality

### Chosen — `disk_used_percent` only, mounts enumerated from `ansible_mounts`, per-mount metrics retained

| Rejected | Why |
|---|---|
| **`resources: ["*"]`** | Collects every mount the OS reports. On a container host that is dozens of overlay filesystems — ~50 metrics per instance instead of 3, **~11× the bill at the same fleet size**, and it happens **silently**. ⚠️ **But this is now a conditional rejection — see below.** |
| **A static mount allowlist** | Cheapest and most predictable, but a data volume mounted at `/data` or `/opt` is silently unmonitored until someone updates the list. Enumerating from host facts adapts automatically. |
| **Aggregating to one metric per instance** (`aggregation_dimensions` + `drop_original_metrics`) | The **largest available saving** — ~$1,000/month at 1,000 VMs ($1,500 → ~$500), and detection is *not* weakened because the agent still sends a statistic set, so `Maximum` means "fullest mount on this host". Rejected because it **permanently discards `path`**: the dashboard would rank instances rather than filesystems, and the enrichment Lambda would need `df -h` via Run Command at alarm time. **This is the first lever to pull under cost pressure** — a config-template plus alarm-query change, no redesign. Note `drop_original_metrics` is essential or the agent publishes *both* sets and cost *increases*. |
| **`disk_inodes_free`** | In AWS's own Standard metric set, and inode exhaustion causes "No space left on device" while `disk_used_percent` reads low. Excluded to keep one metric per mount rather than two. |
| **High-resolution metrics (<60s)** | 3× the alarm cost and **not supported by Metrics Insights** — such data is aggregated to one-minute granularity anyway. Collection frequency does not affect metric cost at all; only cardinality does. |
| **OpenTelemetry metrics** | Priced at **$0.50/GB ingested with no per-series charge**, which *inverts* the economics at very high cardinality — the honest answer to "what would you do at 20,000 instances?". Rejected for now because it means PromQL-based alarming instead of Metrics Insights. |

### ⚠️ Reopened: `resources: ["*"]` is rejected for the cost, but it buys something real

The live pilot found that the ~11× figure is **conditional on the denylist being complete**, and
that the wildcard closes a gap the chosen design cannot.

**What it buys.** The chosen design enumerates mounts from `ansible_mounts` at *configuration*
time, so a volume attached later is invisible until something re-runs Ansible — and nothing does
(`limitations.md §1.3`). That was **reproduced live**: a 5 GiB volume was attached, formatted,
mounted and filled to 40% on a running instance and stayed **entirely absent from CloudWatch**,
with AWS Config still COMPLIANT, until the config was re-rendered by hand. With
`resources: ["*"]` plus a hardened denylist, a second volume filled to 30% **appeared in
CloudWatch on its own — no agent reconfiguration, no restart**, the agent's
`ActiveEnterTimestamp` unchanged throughout. The drift gap closes for free, with no scheduler.

**What it costs, and why the cost is not fixed.** The ~11× penalty is what happens when junk
mounts are *not* excluded. With a complete denylist, the wildcard produced the same 2 real
filesystems the allowlist would have — 28 mounts reduced to 2. So the two approaches converge on
identical cardinality **when the denylist is right**, and diverge by ~11× when it is not.

**The asymmetry is the whole argument, and it is a safety property, not an accounting one:**

| | Ansible allowlist (chosen) | `resources: ["*"]` + denylist |
|---|---|---|
| Names | the filesystem types you **want** (`ext*`, `xfs`, `btrfs`) | the ones you **don't** |
| A type nobody anticipated | **excluded** — fails **CLOSED** | **admitted and billed** — fails **OPEN** |
| A new volume on a running host | invisible until the next run — **needs a trigger** | picked up **automatically** |
| Cardinality if the list is wrong | under-monitors, visibly | over-bills, **silently** |

An allowlist can only ever admit known-good types, so its worst case is a missing metric someone
notices. A denylist's worst case is an unbounded bill nobody notices. And the denylist **was**
wrong: the repo's 18 entries missed **nine** pseudo-filesystems present on AL2023, and `vfat`
(`/boot/efi`) leaked through as a real billable metric:

```
vfat  ramfs  efivarfs  pstore  bpf  selinuxfs  securityfs  hugetlbfs  rpc_pipefs
```

Adding them (29 entries) stopped `/boot/efi` publishing. Note that this leak is **irreversible in
billing terms** — there is no display-time filter anywhere, and a published metric is stored and
billed for 15 months.

**Standing decision: unchanged.** The allowlist stays, because failing closed on an unknown
filesystem type is worth more than free drift repair, and `limitations.md §6` item 3 already
offers a bounded fix for drift. **But the wildcard is now a genuine trade rather than a mistake**,
and it is the right choice for an estate where volumes are attached to long-lived instances more
often than new filesystem types appear — provided the 29-entry denylist ships with it and is
treated as a cost control that can regress.

---

## 13. Remediation

### Chosen — snapshot then `ModifyVolume`, opt-in per volume, AWS-side only

| Rejected | Why |
|---|---|
| **Growing immediately with no cleanup attempt** | Fastest resolution, but it permanently enlarges volumes that a log rotation would have fixed for free, and repeated growth silently masks application leaks. A disk at 90% is often 90% logs. |
| **Growing without a snapshot** | **Volumes cannot be shrunk**, so a snapshot is the only rollback that exists. AWS documents taking one first as best practice. |
| **Fleet-wide auto-growth by default** | Growth is irreversible, so it must be **opt-in** per volume (`DiskAutoGrow=true`) with a size ceiling. |
| **Including root / LVM / RAID volumes** | AWS's documented extend procedure explicitly excludes them: *"You can't use these steps for partitions, the root file system, RAID devices, or Logical Volume Manager (LVM)."* Those notify a human instead. |
| **`DryRun` defaulting to false** | Irreversible actions should be opt-in per run. |

---

## 14. Documentation and presentation

| Rejected | Why |
|---|---|
| **Dropping the dashboard, alarms only** | Considered, since the enrichment Lambda already covers incident response. Kept because it costs **$0** (first three free), answers the brief's "centralize and present" requirement, and provides the trend view that distinguishes a spike from steady growth — which changes whether you clean up or resize. |
| **Rendered PNG/SVG diagrams as the maintainable source** | Presentation-ready, but requires mermaid-cli or Graphviz to regenerate. Mermaid renders natively in GitHub, is diffable text, and remains the maintainable source; a rendered image is embedded in the README as well. |
| **An offline simulator** ⟲ | Would have demonstrated the logic without AWS credentials. Dropped in favour of real deployable artifacts and validation against a live account. |

---

## Summary of reversals

| # | First chosen | Changed to | Why |
|---|---|---|---|
| 1 | On-node Ansible | **Standing controller** | Change management far simpler; SPOF risk overstated (not in the data path) |
| 2 | Fleet-wide alarms | **Per account, per environment** | Granularity is free, and enables differentiated thresholds |
| 3 | One combined workload template | **Split IAM / endpoints** | VPC IDs are account-specific and blocked OU auto-deployment |
| 4 | Per-account transfer buckets | **One central bucket** | Contents are transient; isolation bought little |
| 5 | DHMC as an option | **Rejected outright** | Instance profiles take precedence — they conflict rather than compose |

Corrections recorded because they were load-bearing to earlier reasoning:
- *"Metrics Insights costs 2× per-VM alarms"* — **wrong**, they are identical; an earlier
  comparison used one threshold against two.
- *"Cross-Region alarming is impossible"* — **incomplete**; Metrics Centralization makes it
  possible by copying metrics into the alarm's Region.
- *"DHMC grants SSM permissions regardless of an existing role"* — **wrong**, the opposite is true.
- *"~8× cost for wildcard mounts"* — **~11×**; an earlier estimate omitted that alarms also bill
  per metric analyzed.
- *"`drop_device: true` removes the device-related dimensions"* — **incomplete**. It removes
  `device` and leaves `fstype`, so the emitted set has **four** members and every
  three-dimension `SCHEMA()` clause in the repo matched nothing.
- *"`resources: ["*"]` is simply ~11× more expensive"* — **conditional**. It is ~11× more
  expensive *when the denylist is incomplete*; with a complete one it produces the same
  cardinality as the allowlist **and** picks up new volumes automatically (§12).
- *"A finer `GROUP BY` would put the breaching filesystem in the alert"* — **wrong**. No grouping
  puts identity in the alarm; the enrichment Lambda is the only path to it.

---

## See also

- `limitations.md` — what the current design cannot do, and deferred work
- `quotas.md` — every AWS quota this design touches, and which ones bind
- `docs/01`–`docs/07` — each decision argued in its own context
- `docs/08-alternative-centralization.md` — evaluated-and-not-adopted alternative to the OAM aggregation layer
