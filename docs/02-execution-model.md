# 02 — Ansible Execution Model

## The problem

Step 1 established *how* to reach an instance: Systems Manager, no SSH. That leaves the
harder question. Ansible needs a **host list** and a **transport**, and this estate can
supply neither by hand — instances live in accounts nobody has enumerated, the account
set changes when the company acquires a company, and there are no SSH keys to reach any
of them with.

So: **what runs the playbook, how does it find hosts, and what makes it run at the
right moment?**

## Decision: a standing EC2 Ansible controller

One controller instance in the monitoring account, pushing configuration **over SSM
Session Manager**. It reuses Step 1's access model unchanged — no SSH, no inbound ports,
no bastion.

| Component | Choice |
|---|---|
| Connection | `amazon.aws.aws_ssm` |
| Inventory | `amazon.aws.aws_ec2` per account with `assume_role_arn`, filtered to `tag:DiskMonitoring=enabled` + `instance-state-name: running` |
| Accounts | Enumerated at runtime via `organizations:ListAccounts` — **no static account list and no host list anywhere**. Requires the `organizations` VPC endpoint (`docs/01`), which is why the monitoring account is in us-east-1 |
| Trigger | **EventBridge only. There is no schedule.** |

Controller prerequisites: `session-manager-plugin`, `ansible-core` (**version pinned** —
a controller-specific benefit, since exactly one host has to be right), boto3/botocore
≥ 1.35.0, and the `amazon.aws` collection.

**Network prerequisites are equally hard requirements**, and they are the ones most easily
missed because the controller lives in the *monitoring* account rather than a workload one.
It sits in a private subnet with no egress, so `12-monitoring-endpoints.yaml` gives it
private paths to `organizations` (runtime account enumeration), `sts` (the cross-account
connection credentials below), `ec2` (dynamic inventory), the SSM trio (so it is itself a
managed node and can be invoked by Run Command), `monitoring`, and S3 via the free gateway
endpoint. Plus `AWS_STS_REGIONAL_ENDPOINTS=regional`, because the global STS endpoint
bypasses its VPC endpoint. See `docs/01`.

The connection plugin **moved from `community.aws` into `amazon.aws` in 6.0.0**. The old
`community.aws.aws_ssm` FQCN still resolves, but only as a **redirect** — so an example
using it is a reliable signal that the documentation you are reading is stale, and stale
documentation is exactly where the transfer-bucket confusion below comes from.

**The tag is the entire interface.** Inventory is derived from tags on every run, so
tagging an instance puts it in scope and un-tagging takes it out. Nothing is O(fleet) to
operate.

**There is no background "inventory refresh."** Inventory is computed fresh at the start
of every run, which is why a new account needs no configuration change here — only a
trigger.

---

## How Ansible actually works

This is not background trivia. Two later decisions — the S3 bucket and the Python
requirement — are consequences of it, and both look arbitrary until you know it.

**Ansible does not run commands remotely.** For each task it:

1. takes the module's **Python source**,
2. wraps it together with that task's parameters into **one self-contained file** (~50KB),
3. **copies that file to the target**,
4. executes it with the *target's* Python,
5. reads back a single line of JSON,
6. deletes the file.

So `ansible.builtin.package` is not an instruction sent over a wire — it is a **program
that ships to the host and runs there**. That is why Python is required on managed nodes,
and it is why the role's operations are **identical under any execution model**: modules
always execute on the target regardless of where the playbook was launched from.

---

## The S3 module-transfer bucket is unavoidable

SSH carries those module files natively — it has a file-transfer channel built in.
**Session Manager has no file-transfer channel at all.** So the connection plugin stages
them through S3:

```
controller uploads module file → generates a presigned URL → instance curls it
```

From the AWS documentation, the bucket is *"required even for modules which do not
explicitly send files (such as `shell` or `command`)"* — because **the module's own code
is the payload**, regardless of what the task does. A `command` task still ships a Python
program that runs a command.

**There is no way to turn this off.** No `file_transfer_method` option exists, and all
five transfer-related options *configure* the bucket rather than bypass it:

| Option | What it controls |
|---|---|
| `bucket_name` | Which bucket |
| `bucket_endpoint_url` | Which endpoint to reach it through |
| `bucket_sse_mode` | Encryption at rest while staged |
| `bucket_sse_kms_key_id` | Which KMS key, if SSE-KMS |
| `s3_addressing_style` | Path vs virtual-hosted addressing |

**The target needs no S3 credentials.** The presigned URL carries the authorization, so
the instance role stays minimal — consistent with Step 1.

### Bucket configuration, and why

**One central bucket** in the monitoring account with an **org-scoped read policy**.

What lives there: Ansible module code plus interpolated task parameters · lifetime of
**seconds** · ~4 files per host per run for our role · **nothing durable**. It is a
**transient staging area, not a repository** — which is what makes the security posture
simple to reason about.

| Setting | Why |
|---|---|
| **Versioning OFF** | Module files carry interpolated task parameters, which for a general-purpose plugin can embed secrets. With versioning on, **deleted objects persist in version history in cleartext** — a delete stops being a delete |
| SSE (AES256) | Encrypt at rest for the seconds they exist |
| ~1-day lifecycle expiry | Reap objects left behind by an ungraceful controller termination |
| TLS-only bucket policy | No plaintext fetch of a presigned URL |
| Block Public Access | Presigned URLs are the only intended access path; nothing else should be |

Our role's parameters are innocuous — package names, paths, mount lists — but **the
plugin is general-purpose**, so the bucket is locked down regardless of what we happen to
put through it today.

Instances fetch the presigned URL through their **Region-scoped** S3 gateway endpoint.
Cross-account access therefore works without the instance holding any credential, provided
the bucket policy permits the org and the endpoint policy does not block it.

### Rejected — a bucket per account

The isolation benefit is small for a staging area whose contents live for seconds, and
one bucket means **one configuration value in the controller** with nothing per-account to
keep in step with the account list. Per-account buckets would add a maintenance surface
proportional to N to protect data that does not persist.

---

## Triggers: three EventBridge rules, and no schedule

All three target **Run Command on the controller** (`ansible-playbook --limit
<instance-id>`). Run Command is the natural target because **the controller is itself an
SSM managed node** — no Lambda is needed just to shell out.

### Rule 1 — instance launch

```yaml
source: aws.ec2
detail-type: "EC2 Instance State-change Notification"
detail: { state: ["running"] }
```

**Implementation detail that bites:** the event fires when **EC2** reports `running`, but
**SSM registration takes a further ~30–60 seconds**. The invocation must **retry briefly**
rather than fail on first attempt — otherwise the normal path fails on a race that has
nothing to do with the instance being wrong.

### Rule 2 — tag change (**required, not optional**)

```yaml
source: aws.tag
detail-type: "Tag Change on Resource"
detail:
  service: ec2
  resource-type: instance
  changed-tag-keys: ["DiskMonitoring", "Environment"]
```

With no scheduled sweep, an instance tagged *after* launch would **never** be enrolled.
The launch event has already fired, found the instance absent from tag-filtered inventory,
done nothing, and **will never fire again**:

```
T+0     launches untagged  → Rule 1 fires → not in inventory → no-op
T+~2m   Config remediation applies DiskMonitoring
                           → Rule 2 fires → configured ✅
```

Rule 2 makes **Config's own remediation the enrollment trigger**, closing the loop with no
scheduler.

**Why `Environment` is watched too.** `Environment` is not merely an inventory filter — it
is a **metric dimension** (Step 3). If it arrives late and nothing re-runs, metrics keep
publishing `Environment=unscoped` **permanently**, so the instance sits **outside** its
environment-scoped alarm. It is **monitored but not covered — and it looks fine**, which
is worse than being visibly unmonitored. A tag-change re-run re-renders the config, the
handler restarts the agent, and metrics carry the correct dimension. The stranded
`unscoped` metrics simply age out after ~15 months of no data.

### Rule 3 — new account onboarded

Lives **in the monitoring account**, not per workload account.

```yaml
source: aws.cloudformation
detail-type: "CloudFormation Stack Status Change"
detail:
  status-details: { status: ["CREATE_COMPLETE"] }
# → bulk run: ansible-playbook against all instances in the new account
```

**Why it is needed.** Rules 1 and 2 are per-instance and event-driven, so for an acquired
account's *existing* instances:

- **No launch event will ever fire** — they launched long ago.
- **A tag-change event fires only if a tag was missing.** An instance that **already
  carries `DiskMonitoring=enabled`** — because the acquired team happened to use that tag,
  or a previous onboarding attempt set it — **generates no event at all** and would never
  be configured.

Rule 3 closes that hole with a **bulk** run scoped to the new account, `serial: 10%`
bounding the batch.

**Why stack completion rather than the account joining the Organization:**

| Reason | Detail |
|---|---|
| **Naturally ordered** | At `CREATE_COMPLETE` the account's IAM roles, VPC endpoints, and OAM link already exist. Organizations events (`MoveAccount` / `CreateAccount` / `AcceptHandshake`) can fire **before the StackSet has finished**, so the controller would try to reach hosts it has no role to reach |
| **Stays in-Region** | Organizations events are emitted **only in us-east-1**, so that route needs a cross-Region event bus |
| **One rule, not N** | **One rule in the monitoring account** instead of one per workload account |

**A new account needs no configuration change — it needs a trigger.** Because inventory is
computed fresh per run and accounts come from `ListAccounts`, there is nothing to edit; the
only missing ingredient is something to start a run, which is what Rule 3 supplies.

### ⚠️ Accepted consequence of having no schedule

Enrollment is **purely event-driven**, so **a missed or failed event means that instance is
never configured, with nothing to catch it.** There is also no periodic re-run, so
configuration drift — agent stopped, config hand-edited — is neither repaired nor detected.
Ad-hoc runs remain available (`ansible-playbook --limit`).

Related: if both tags are missing, both remediations may fire and produce two runs.
Harmless — Ansible is idempotent — just wasteful. Applying `Environment` before
`DiskMonitoring` avoids it, since `DiskMonitoring` is what gates inventory.

---

## `instance-state-name: running` is a correctness filter

It reads like an optimization. It is not.

Without it, `stopped` / `stopping` / `terminated` / `pending` instances enter inventory.
Ansible then attempts Session Manager connections that **cannot** succeed, and each one
becomes a **task failure — not a skip**. Failures count toward `max_fail_percentage`, so
**enough dead instances abort the run for the healthy ones.**

**Terminated instances linger in `describe-instances` for up to an hour**, so this is not
an edge case; it is routine in any fleet with churn. The filter is what keeps a scale-in
event from breaking the next deployment.

### ⚠️ The filter reads *cached* state, so a window remains

The filter removes stopped instances **as of the last inventory query** — and inventory is
cached for **300 seconds**, set in two places (`cache_timeout` in `ansible.cfg` and again in
`inventory/aws_ec2.yml.template`). Inside that window an instance that has just stopped is
still listed as `running`, so the filter cannot help:

```
T+0     instance stops
T+10s   run starts → cached inventory still says `running` → instance is targeted
        → pre_tasks assume-role SUCCEEDS  (delegate_to: localhost — the dead target
          is never touched, so this step cannot detect the problem)
        → setup task opens ssm:StartSession → TargetNotConnected → host unreachable
        → consumes the max_fail_percentage budget
```

**The assume-role step succeeding is what makes this hard to read in a log.** The first three
tasks pass and the failure lands on *fact gathering*, which looks like a problem with the
target — a broken endpoint, a missing profile, an agent that died — rather than what it is:
inventory describing a fleet that has since changed.

**The window can be narrowed but never closed.** Even with caching disabled entirely,
`DescribeInstances` reports state *at query time*, and an instance can stop moments later,
mid-run. **Any push-based configuration model over a live fleet has this race**; caching only
sets how wide it is.

**Availability is not the concern — signal is.** `max_fail_percentage: 5` absorbs a handful of
these and `serial` confines them to one batch, so a few stopped instances will not break a
run. The real objection is that **the failure budget exists to stop a systematically broken
change**, and routine instance churn spending it conflates two unrelated problems: at enough
churn, a legitimate run aborts for reasons that have nothing to do with the change being
deployed.

| Mitigation | Effect | Cost |
|---|---|---|
| `ANSIBLE_INVENTORY_CACHE=False` on the single-instance event path | Narrows the window to the run itself | One extra `DescribeInstances` per event — negligible for a `--limit` run |
| Lower `cache_timeout` | Narrows proportionally | More EC2 API calls on bulk runs, which is exactly what the cache exists to prevent |
| Pre-flight filter on `ssm:DescribeInstanceInformation` `PingStatus=Online` | **Eliminates the class** — a stopped instance is never `Online` | An extra call plus a step in `scripts/render_inventory.sh` |

**`ignore_unreachable: true` is deliberately not recommended.** It would make these hosts
skip silently — but it would equally mask the case where *every* host is unreachable, from a
missing `monitoring` endpoint or a security group that does not admit the instance SG. The run
would then report success having configured nothing, which is the silent-success failure mode
this design works hardest to avoid.

**The same cache causes the opposite failure**, and that direction is already flagged in
`cloudformation/00-monitoring-account.yaml`: a 300-second-old inventory *predates* a
just-launched instance, so `ansible-playbook --limit <new-instance>` matches **zero hosts and
still exits `0`** — enrollment silently does nothing. The two failures are mirror images of one
stale read, and **one change fixes both**: disabling the inventory cache on the event-driven
path, where a single `DescribeInstances` costs nothing and freshness is the entire point.

---

## Rejected — `AWS-ApplyAnsiblePlaybooks` on-node

The genuine alternative: Ansible runs **on each instance**, triggered by a State Manager
association, with no controller at all. It was attractive — zero-touch enrollment within
minutes, no host to own — and it still lost.

| Reason | Detail |
|---|---|
| **Change management is materially heavier** | To ship a change: package the playbook, upload it to **every account's** bucket, then update a version pointer to trigger the rollout. With a controller you edit the playbook and run it |
| Ad-hoc and single-host runs are awkward | There is no natural equivalent of `--limit i-abc` against one host, right now |
| **Ansible on every node, unpinned** | An extra package on every instance with **no version pinning** — `InstallDependencies: True` installs whatever the distro repo happens to ship, so the fleet's Ansible version is decided per-AMI rather than by us |
| Linux-only | *"Associations that run Ansible playbooks aren't supported on macOS"* |
| **Its source options collapse under no-egress** | `SourceType: GitHub` is unreachable, because `aws:downloadContent` runs **on the node** and **there is no VPC endpoint for GitHub** — so it would have forced `SourceType: S3` regardless, i.e. the bucket-and-pointer workflow above rather than the simpler one it appears to offer |

(`AWS-RunAnsiblePlaybook`, the older document, is **explicitly deprecated by AWS** and was
never a candidate.)

The playbook changes far more often than anyone will care about a ~30-minute enrollment
delay, so the trade lands on the controller.

## Rejected — an ephemeral CI runner

Real advantages, and they are precisely the controller's weaknesses: **no host to patch**,
**no standing credentials**, and the Ansible version **pinned in the image**. Genuinely
attractive. Rejected in favour of **a standing host that is always available for ad-hoc
runs** — during an incident you want to run a playbook now, not wait for a pipeline. This
is the closest call in Step 2.

---

## The controller is not in the data path

Worth stating plainly, because a single standing instance looks like a single point of
failure for the whole design. It is not.

If the controller is down you lose **the ability to deploy configuration changes**. You do
not lose monitoring:

- the CloudWatch agent keeps publishing,
- alarms keep evaluating,
- remediation keeps working.

**That is a much smaller failure than "the monitoring system is down"** — it degrades
change management, not detection.

## Change management

| Task | Action |
|---|---|
| Ship a change | Edit the playbook and run it |
| Test on one host | `ansible-playbook site.yml --limit i-abc` |
| Dry run | `ansible-playbook site.yml --check --diff` |
| Roll back | **Re-run the previous version** — the playbook is the desired state |
| Bound a bad change | `serial: 10%` limits batch size; `max_fail_percentage: 5` stops a systematically failing run |

`serial` and `max_fail_percentage` are the controller-side equivalents of Run Command's
`MaxConcurrency` and `MaxErrors`: **a bad change reaches a few hosts, not all of them.**

---

## Limitations

- **The controller must be patched, hardened, and monitored**, and it **holds cross-account
  credentials** — a high-value target by construction. This is the cost of the model.
- **Slower than SSH.** Every module transits S3 *plus* a Session Manager round trip, per
  task. `ansible_aws_ssm_timeout` is raised to 120s because the 60s default is tight on a
  busy host.
- **No `ansible_user` / `remote_user` support.** Commands run as the ssm-agent user
  (normally root), so privilege escalation is via `become` / `become_user`. This surprises
  anyone expecting SSH semantics, where `remote_user` is the obvious knob.
- **Inventory must yield instance IDs.** If `ansible_aws_ssm_instance_id` is unset, the
  plugin uses the **connection host** as the SSM target ID — so `hostnames: [instance-id]`
  is load-bearing, and a Name tag as hostname breaks the connection. The inventory also
  sets `ansible_aws_ssm_instance_id` via `compose:` as belt and braces.
- **The `tags` hostvar is deprecated** (removal after 2026-12-01) → use **`ec2_tags`**.
  Note precisely what is deprecated: **that one variable name in the `aws_ec2` plugin**,
  **not** hostvars themselves.
- **Windows works, but treat it as less mature.** It needs `ansible_shell_type: powershell`
  set manually, and the plugin's Windows path took multiple 2025-era bugfixes.

---

## Files

- [`ansible/ansible.cfg`](../ansible/ansible.cfg) — forks, inventory plugins, fact caching
- [`ansible/group_vars/all.yml`](../ansible/group_vars/all.yml) — SSM connection settings, transfer bucket, rollout controls
- [`ansible/inventory/aws_ec2.yml.template`](../ansible/inventory/aws_ec2.yml.template) — dynamic inventory, tag + state filters, `hostnames`, `compose`
- [`ansible/site.yml`](../ansible/site.yml) — play, `serial`, `max_fail_percentage`
- [`ansible/requirements.yml`](../ansible/requirements.yml) — `amazon.aws`, controller-side only
- [`cloudformation/00-monitoring-account.yaml`](../cloudformation/00-monitoring-account.yaml) — controller IAM, module-transfer bucket and policy
- [`cloudformation/10-workload-iam.yaml`](../cloudformation/10-workload-iam.yaml) — per-account EventBridge Rules 1–2
