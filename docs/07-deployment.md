# 07 — Deployment and Verification

## Deployment order

Order matters because several dependencies are hard, and **some failures are silent
rather than loud**. A component deployed too early does not error — it deploys cleanly
and then does nothing.

| Phase | What | Blocking dependency |
|---|---|---|
| 0 | Build and validate the Ansible role, templates, inventory | — |
| 1 | Monitoring account: OAM sink + sink policy, SNS topics, central S3 module-transfer bucket, controller IAM, event bus policy | — |
| **1b** | **Monitoring-account VPC endpoints** (`12-monitoring-endpoints.yaml`): `organizations`, `sts`, `ec2`, SSM trio, `monitoring`, free S3 gateway | **Controller VPC/subnets exist** |
| 2 | StackSets trusted access with Organizations; **AWS Config configuration recorder** in target accounts/Regions | — |
| 3 | Workload baseline StackSet: instance profile, VPC endpoints, cross-account roles, OAM link, Config rules + remediation, EventBridge Rules 1–2 | Sink ARN (1), trusted access (2) |
| 4 | Controller: provision, harden, install `ansible-core` + `session-manager-plugin` + `amazon.aws`, deploy inventory config | **Endpoints (1b)**, cross-account inventory + session roles (3) |
| 5 | Attach profiles and tags to existing instances (**profile before tag**) | Profile exists (3) |
| 6 | First playbook run → **verify metrics arriving and confirm all four dimension names** | 4, 5 |
| 7 | Alarms, dashboard, **enrichment Lambda (required, not optional)**, remediation runbook, EventBridge Rule 3 | **Dimensions confirmed (6)** |

### Why this order

- **Phase 1 before 3** — workload accounts create OAM links that *reference the sink
  ARN*, so the sink must exist first. Links fail outright otherwise. This is the one
  ordering constraint that announces itself.
- **Phase 1b before 4, or the controller is inert.** In a no-egress subnet the controller
  cannot call Organizations (so `render_inventory.sh` exits and produces **no inventory**),
  cannot call STS (so the cross-account credential step in `site.yml` fails every task),
  and cannot register as an SSM managed node (so EventBridge `SendCommand` matches **zero
  targets and returns `Success`**). None of those announce themselves as a networking
  problem, which is why the endpoints come first.
- **Phase 2's Config recorder is a classic silent failure.** Config **rules** deploy
  successfully into an account with no configuration recorder and then **never
  evaluate**, with no error to indicate it. Tag and profile remediation would silently
  do nothing, so instances would never enroll and nothing would say why.
- **Phase 5: profile before tag** — a tagged instance without a profile is **not an SSM
  managed node**, so it *looks* enrolled while nothing runs. Tag-filtered inventory
  picks it up, the connection cannot succeed, and the failure counts toward
  `max_fail_percentage`. Attaching the profile first avoids that confusing intermediate
  state entirely.
- **Phase 7 last, deliberately** — alarms need metrics to evaluate against, and **arming
  irreversible volume growth before validating the alarm** risks it firing on bad data.
  **EBS volumes cannot be shrunk**, so there is no undo for a wrong remediation.
- **Phase 6 before 7 is a hard gate, not a courtesy.** A `SCHEMA()` clause that does not
  match the emitted dimensions **does not error and does not go
  `INSUFFICIENT_DATA`** — with `TreatMissingData: notBreaching` it reports a green `OK`
  forever (`tested_findings.md` §2). So an alarm stack deployed before the dimensions are
  confirmed can look perfectly healthy while evaluating nothing at all, and nothing later in
  the process would reveal it.

---

## Rollout: pilot account first

Deploy all phases into **one non-production account**, prove the chain end to end, then
enable StackSet `AutoDeployment` to the OU.

This matters more than usual because **remediation grows volumes irreversibly** — far
better discovered in one account than fifty.

The **dimension-name question, previously the other reason, is now answered.** A live pilot
ran the gate and the agent emits `InstanceId, path, Environment, fstype`
(`tested_findings.md` §2). That does not retire Phase 6's check — the answer is specific to
an agent version, an OS and a config, and the *failure* is a green `OK` rather than an
error — but it does mean you now know what the gate should return before you run it, which
turns an open question into a confirmation.

---

## Phase 0 — pre-deployment validation

| Check | What it catches | How |
|---|---|---|
| `ansible-lint` + `yamllint` | Syntax and practice issues | tooling, no credentials needed |
| `aws cloudformation validate-template` | Template errors | once per template |
| `ansible-playbook --check --diff` | Exactly what would change, before it does | dry run, ideally `--limit` one host |
| **Rendered agent config** | **The ~11× cost error** — confirm `resources` is a real mount list and never `["*"]`, and that `tmpfs` and the other pseudo-filesystems are excluded | inspect the rendered JSON on the first host |
| **Dimension contract** | The agent template and the alarm `SCHEMA()` clause must name the same four dimensions: `InstanceId, path, Environment, fstype` | compare the two files by eye, then confirm live at Phase 6 |
| Idempotence | Configuration drift or a non-converging task | run the playbook twice; the second run must report `changed=0` |

**The rendered-config check is the highest-value one here.** `resources: ["*"]` looks harmless
in review and mints one metric per overlay filesystem on a container host — the bill arrives a
month later, and doc 04 explains that **there is no way to un-bill a metric once published.**

**The dimension contract is a close second**, and its value rose sharply after the pilot:
because the runtime symptom of a mismatch is a **green `OK`** rather than an error
(`tested_findings.md` §2), this review and the Phase 6 gate below are the only two places the
failure can be caught at all.

⚠️ **Both are manual review steps today, not automated gates.** Adding two checks to CI — a
template render asserting `resources` is never `["*"]`, and an assertion that the alarm
`SCHEMA()` dimension set matches the agent's — is the highest-value hardening available for
this repo. Both are cheap, need **no AWS credentials**, and each guards a failure mode that is
otherwise entirely silent.

---

## Phases 1–3 — infrastructure

### Phase 1 — monitoring account foundation

```bash
aws cloudformation deploy \
  --template-file cloudformation/00-monitoring-account.yaml \
  --stack-name disk-monitoring-foundation \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
      OrganizationId=o-xxxxxxxxxx \
      WarningEmail=ops-chat@example.com \
      CriticalEmail=oncall@example.com
```

Capture the outputs — every later phase consumes them:

```bash
aws cloudformation describe-stacks \
  --stack-name disk-monitoring-foundation \
  --query 'Stacks[0].Outputs'
```

| Output | Consumed by |
|---|---|
| `SinkArn` | Phase 3, as `MonitoringSinkArn` |
| `ModuleTransferBucketName` | Phase 4, as `DISK_MONITORING_TRANSFER_BUCKET` |
| `ControllerInstanceProfileName` | Phase 4, attached to the controller instance |
| `WarningTopicArn` / `CriticalTopicArn` | Phase 7, as alarm actions |

Email subscriptions require manual confirmation. **Confirm them now** — an unconfirmed
subscription means Phase 7's alarms fire into nothing.

### Phase 1b — monitoring-account VPC endpoints

```bash
aws cloudformation deploy \
  --template-file cloudformation/12-monitoring-endpoints.yaml \
  --stack-name disk-monitoring-controller-endpoints \
  --region us-east-1 \
  --parameter-overrides \
      VpcId=vpc-0controller \
      ControllerSubnetIds='subnet-0a,subnet-0b' \
      ControllerSecurityGroupId=sg-0controller \
      RouteTableIds='rtb-0a,rtb-0b'
```

**`--region us-east-1` is not incidental.** The template's `Rules` section asserts it,
because the **Organizations interface endpoint exists only in the control-plane Region**.
A deploy elsewhere is rejected at validation with the reason stated, rather than failing
opaquely at resource creation or — worse — succeeding without the one endpoint
`render_inventory.sh` depends on.

To run the monitoring account in another Region, remove the Organizations endpoint and
reach a us-east-1 endpoint over **Transit Gateway**. **Not NAT** — the controller holds
cross-account credentials for the whole estate, so giving it a route to the entire internet
is the one place that trade is least worth making.

### Phase 2 — trusted access and the Config recorder

```bash
aws organizations enable-aws-service-access \
  --service-principal member.org.stacksets.cloudformation.amazonaws.com
```

Then, **in each target account and Region**, the silent-failure check:

```bash
aws configservice describe-configuration-recorders
aws configservice describe-configuration-recorder-status
```

An empty result means the Phase 3 Config rules will deploy and **never evaluate**. There
is no error, no non-compliant finding, and no signal at all — enrollment simply never
happens for anything that misses its tag or profile at launch.

### Phase 3 — workload baseline StackSet

```bash
aws cloudformation create-stack-set \
  --stack-set-name disk-monitoring-baseline \
  --template-body file://cloudformation/10-workload-iam.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --permission-model SERVICE_MANAGED \
  --auto-deployment Enabled=true,RetainStacksOnAccountRemoval=false \
  --parameters \
      ParameterKey=MonitoringAccountId,ParameterValue=111122223333 \
      ParameterKey=MonitoringSinkArn,ParameterValue=arn:aws:oam:ap-south-1:111122223333:sink/abcd1234 \
      ParameterKey=OrganizationId,ParameterValue=o-xxxxxxxxxx \
      ParameterKey=VpcId,ParameterValue=vpc-0abc123 \
      ParameterKey=PrivateSubnetIds,ParameterValue='subnet-0a,subnet-0b,subnet-0c' \
      ParameterKey=InstanceSecurityGroupId,ParameterValue=sg-0abc123 \
      ParameterKey=RouteTableIds,ParameterValue='rtb-0a,rtb-0b'
```

```bash
aws cloudformation create-stack-instances \
  --stack-set-name disk-monitoring-baseline \
  --deployment-targets OrganizationalUnitIds=ou-xxxx-xxxxxxxx \
  --regions ap-south-1 \
  --operation-preferences FailureToleranceCount=0,MaxConcurrentCount=5
```

`AutoDeployment Enabled=true` is what makes onboarding an acquired account **one
action**: move the account into the OU. `RetainStacksOnAccountRemoval=false` is the
matching exit — removing it deletes its stack.

The VPC parameters are per-account, so a heterogeneous estate needs either per-account
parameter overrides (`create-stack-instances --parameter-overrides`) or a StackSet per
network shape. **Pilot with a single account before assuming one parameter set fits.**

---

## Phase 4 — the controller

- EC2 instance in a **private subnet**, with the `DiskMonitoringControllerProfile`
  instance profile from Phase 1 attached.
- Install `ansible-core >= 2.15`, `session-manager-plugin`, and `boto3`/`botocore
  >= 1.35.0` (required by `amazon.aws` 11.x).
- `ansible-galaxy collection install -r ansible/requirements.yml` — collections are
  **controller-side only**; the role itself uses `ansible.builtin` modules exclusively,
  so nothing is installed on managed instances.
- Export `DISK_MONITORING_TRANSFER_BUCKET` (Phase 1 output) and `AWS_REGION`. Both are
  read by `group_vars/all.yml`; an unset bucket name fails every task, because Session
  Manager has no file-transfer channel and modules stage through S3.
- **Export `AWS_STS_REGIONAL_ENDPOINTS=regional`.** Mandatory whenever the controller sits
  behind VPC endpoints: the *global* endpoint `sts.amazonaws.com` **bypasses the `sts`
  interface endpoint entirely**, so `site.yml`'s cross-account credential step would try to
  reach the internet and hang in a no-egress subnet. The endpoint would exist, be billed,
  and go unused. Both EventBridge Run Command payloads already export it; the manual path
  needs it too. Put it in the controller's shell profile so an ad-hoc run cannot miss it.
- Confirm the controller is a **managed node** before going further — this is the
  precondition for every EventBridge-driven run, and its absence looks like success:

  ```bash
  aws ssm describe-instance-information \
    --filters Key=tag:Role,Values=disk-monitoring-controller \
    --query 'length(InstanceInformationList)'      # must return 1, not 0
  ```
- Render **one inventory file per account** in `ansible/inventory/`, each with that
  account's `DiskMonitoringInventoryRole` ARN as `assume_role_arn`. Ansible merges every
  file in the directory into one fleet. Accounts are enumerated at runtime via
  `organizations:ListAccounts`, so **no static account list exists anywhere.**

The controller **holds cross-account credentials**, which makes it a high-value target
by construction — it must be patched, hardened, and monitored like any other privileged
host. But it is **not in the data path**: if it is down, configuration deployment stops
while the CloudWatch agent keeps publishing, alarms keep evaluating, and remediation
keeps working.

---

## Phase 5 — existing instances

New instances get the profile from their launch template. Existing ones need it
attached:

```bash
aws ec2 associate-iam-instance-profile \
  --instance-id i-0abc123 \
  --iam-instance-profile Name=DiskMonitoringInstanceProfile
```

Effective in minutes, and **no reboot is required** — SSM Agent retries registration on
a loop and picks up the new credentials on its next attempt.

In practice the Phase 3 Config rules do this automatically
(`EC2_INSTANCE_PROFILE_ATTACHED` → remediation, then `REQUIRED_TAGS` →
`AWS-SetRequiredTags`); the command above is the manual equivalent for a pilot or a
one-off. Either way: **profile before tag.**

---

## Phase 6 — first run and the dimension gate

```bash
ansible-playbook -i ansible/inventory ansible/site.yml --check --diff   # dry run first
ansible-playbook -i ansible/inventory ansible/site.yml
```

Then **the gate that must not be skipped**:

```bash
aws cloudwatch list-metrics --namespace CWAgent --metric-name disk_used_percent
```

**Expect exactly these four dimensions:**

```
InstanceId    path    Environment    fstype
```

**This is no longer an open question.** A live pilot on Amazon Linux 2023 ran this call and
got that set (`tested_findings.md` §2). Two things about it are worth internalising, because
both contradict a reasonable reading of the docs:

- **`fstype` is present.** `drop_device: true` removes `device` and **nothing else** — it
  does not remove `fstype`, so the emitted set is four dimensions, not three.
- **`Environment` is present only because it lives inside the `disk` section's
  `append_dimensions`.** At `metrics` level AWS accepts only
  `ImageId`/`InstanceId`/`InstanceType`/`AutoScalingGroupName` and **silently drops**
  anything else — so a `metrics`-level `Environment` yields three dimensions and every
  `WHERE Environment = …` clause matches nothing.

Whatever this returns must match the alarm's `SCHEMA()` **exactly**.
`SCHEMA("CWAgent", InstanceId, path, Environment, fstype)` is a filter describing which
metrics exist, not a projection: if the agent emits `Partition` where the query says
`path`, zero series match and no error is raised anywhere.

### ⚠️ Why this gate is not optional paranoia

The intuition is that a mismatch is self-announcing — the alarm goes
`INSUFFICIENT_DATA` and `InsufficientDataActions` tells someone. **The pilot disproved
this.** With this design's `TreatMissingData: notBreaching`, a query matching zero series
reports a green **`OK`**, indefinitely:

```
State  : OK
Reason : "No time series were returned by the query. Treat missing data is configured as [NonBreaching]."
```

**`InsufficientDataActions` never fires.** There is no red state, no grey state, no error,
no log — the alarm is not uncertain, it is confidently wrong. This gate and the Phase 0
dimension-contract test are therefore **the only two places** a mismatch can be caught. Skip
the gate and the first evidence is an unmonitored disk filling up.

Do not proceed to Phase 7 until this is confirmed. If the names differ, fix
`ansible/roles/cw_agent/` and `cloudformation/20-alarms-dashboard.yaml` together — the
dimension-contract test in Phase 0 exists to keep them from drifting again.

---

## Phase 7 — alarms, dashboard, remediation

Deploy `20-alarms-dashboard.yaml` **once per account per environment** — prod at 80/90,
dev at 90/95. Granularity is free (billing is per metric *analyzed*, so partitioning
3,000 metrics across 20 alarms costs the same as 2 alarms over all 3,000), but
**overlapping scopes are not** — partition cleanly.

```bash
aws cloudformation deploy \
  --template-file cloudformation/20-alarms-dashboard.yaml \
  --stack-name disk-alarms-444455556666-prod \
  --parameter-overrides \
      TargetAccountId=444455556666 \
      EnvironmentName=prod \
      WarningThreshold=80 \
      CriticalThreshold=90 \
      WarningTopicArn=arn:aws:sns:ap-south-1:111122223333:disk-monitoring-warning \
      CriticalTopicArn=arn:aws:sns:ap-south-1:111122223333:disk-monitoring-critical \
      RemediationDocumentArn=arn:aws:ssm:ap-south-1:111122223333:document/DiskSpace-GrowVolume \
      RemediationRoleArn=arn:aws:iam::111122223333:role/DiskMonitoringRemediationRole
```

Then, in order:

```bash
# Dashboard — once for the whole estate, not per account
aws cloudformation deploy \
  --template-file cloudformation/30-dashboard.yaml \
  --stack-name disk-monitoring-dashboard \
  --parameter-overrides DashboardName=disk-monitoring

# Remediation runbook — DryRun=true until the fill test passes
aws ssm create-document \
  --name DiskSpace-GrowVolume \
  --document-type Automation \
  --document-format YAML \
  --content file://ssm-documents/DiskSpace-GrowVolume.yaml
```

Deploy the enrichment Lambda and EventBridge Rule 3 last. **Deploy remediation with
`DryRun=true` initially** — it evaluates every guard and reports the change it would
make without touching a volume, which is the only way to inspect the decision before it
becomes permanent.

### ⚠️ The enrichment Lambda is the runbook's invoker, and `DryRun` must be explicit

Two things about this wiring are easy to get wrong, and both fail quietly.

**The Lambda invokes the runbook — EventBridge cannot.** The critical alarm event carries
**no `InstanceId`**: a firing Metrics Insights alarm reports only *"1 out of 7 time series
evaluated to ALARM"*, with a `StateReasonData` payload containing nothing but a version and
a timestamp (`tested_findings.md` §3). Adding `path` to the `GROUP BY` does not change this.
So nothing can target the runbook until something re-runs the query, parses the breaching
labels, and resolves each mount to a volume **on the host** via `ebsnvme-id`. That is the
Lambda's job, and it is why it sits **on** the remediation path rather than beside it —
treat it as a required component, not an enhancement.

**Pass `DryRun: 'false'` explicitly on the automated path.** The document **defaults to
`'true'`**, which is right for a human invoking it to see what it would do and wrong for the
Lambda. An invocation that omits the parameter runs every guard, writes a clean execution
history, reports success — and **grows nothing.** The failure is silent in the reassuring
direction, which is the worst direction for a control whose whole purpose is acting when
nobody is watching. Flip it only after the warning-tier fill test below has passed.

---

## Post-deployment verification

### 1. Are instances managed?

```bash
aws ssm describe-instance-information \
  --query 'length(InstanceInformationList)'
```

Proves SSM Agent is running and the instance profile grants registration — the
precondition for Ansible reaching anything at all. Compare against
`aws ec2 describe-instances` count; a gap is a missing profile.

### 2. Are metrics arriving, and does OAM work?

```bash
# In a WORKLOAD account
aws cloudwatch list-metrics --namespace CWAgent

# Then the same call from the MONITORING account
aws cloudwatch list-metrics --namespace CWAgent
```

**Running it from both is the point.** It separates *"the agent is not publishing"* from
*"OAM is not sharing"*, which are different failures with different fixes — a missing
`monitoring` VPC endpoint versus a missing or misconfigured OAM link. A single call from
the monitoring account cannot tell them apart. Note also that OAM sharing is **not
retroactive**: metrics published before the link existed never appear centrally.

### 3. The controlled fill test — the only test that proves the whole chain

On a **non-production** instance:

```bash
fallocate -l 5G /var/tmp/fill-test
df -h /var
```

| Expect | Proves |
|---|---|
| `disk_used_percent` rises in the monitoring account | Agent → CloudWatch → OAM |
| Alarm transitions to ALARM | Query matches; threshold and M-of-N correct |
| `StateReason` reads *"N out of M time series evaluated to ALARM"* **and names nothing** | Expected, not a bug — see below |
| Notification names instance, mount **and volume ID** | Enrichment Lambda works |
| Remediation snapshots and grows the **correct** volume | Runbook resolves the right target, and `DryRun: 'false'` was actually passed |
| `rm /var/tmp/fill-test` → alarm returns to OK | Recovery path |

**Do not read the alarm's own message as a failure of the fill test.** A firing fleet alarm
carries **no identity at any grouping** — in the pilot, `StateReason` was *"1 out of 7 time
series evaluated to ALARM"* and `StateReasonData` held only a version and a timestamp
(`tested_findings.md` §3). The instance, mount and volume must come from the **enrichment
Lambda's** notification, so row 4 above is the one that actually validates actionability. If
that notification is missing or unparsed, remediation has nothing to target.

**Test the warning tier first** — fill to roughly 85%, confirm the notification arrives
and is legible, and only then push into the critical tier. This validates notification
**before** triggering irreversible volume growth. There is no path back from a grown
volume, so the cheap half of the test goes first. It also validates the Lambda's **label
parsing** on real data, which is where a rank-prefixed label (`1 - i-0aaa...aaa /data`)
quietly turns into a lookup for an instance called `"1"`.

### 4. Verify all three EventBridge rules

This matters more than usual because **with no scheduled sweep, a broken rule has
nothing to mask it** — a missed enrollment stays missed indefinitely, and the instance
looks fine because it is simply absent.

| Rule | How to test | What it covers |
|---|---|---|
| 1 — instance launch | Launch a test instance with the tag and profile | New instances |
| 2 — tag change | Add `DiskMonitoring=enabled` to an untagged running instance | Instances tagged *after* launch, including by Config remediation |
| 3 — new account | Confirm the bulk run fires when a workload stack reaches `CREATE_COMPLETE` | Whole-account onboarding |

Rule 2 is not optional. Without it, an instance tagged after launch is never configured:
the launch event already fired, found the instance absent from tag-filtered inventory,
did nothing, and never fires again.

---

## Teardown

Reverse order, with three asymmetries that make it not a clean mirror of deployment.

- **Delete alarms and remediation first.** A partially dismantled system could still
  trigger volume growth — the critical alarm firing into a runbook whose guards have
  been removed is the worst possible half-state.
- **`RetainStacksOnAccountRemoval: false`** means removing an account from the OU
  deletes its stack, including its OAM link and cross-account roles. That is the
  intended exit path, but it is immediate and needs no separate action.
- **Remediation snapshots persist** after teardown and keep costing money — they are
  **not** stack resources, so nothing deletes them. List and clean them explicitly:

```bash
aws ec2 describe-snapshots \
  --owner-ids self \
  --filters Name=tag:Project,Values=disk-monitoring \
  --query 'Snapshots[].[SnapshotId,VolumeSize,StartTime]' \
  --output table
```

The module-transfer bucket is `DeletionPolicy: Retain`, so it survives stack deletion by
design; its lifecycle rule expires contents in a day regardless.

---

## Future work

1. **Filesystem extension after volume growth** — `growpart` then
   `resize2fs`/`xfs_growfs`, with `df -h` verification. **Required for the space to
   become usable**; until then `ModifyVolume` alone does not free space in the guest,
   which is why the runbook notifies rather than claiming resolution.
2. **Coverage verification** — periodically compare running instances against instances
   publishing metrics and alarm on the difference. Closes the one gap Config cannot see:
   **Config verifies configuration, not outcome**, so an instance whose agent crashed is
   fully compliant while sending nothing. **And the alarm will not tell you**: a query
   matching zero series reports a green `OK` under `TreatMissingData: notBreaching`, so
   `INSUFFICIENT_DATA` does not fire even in the total-blindness case
   (`tested_findings.md` §2). This is the highest-value remaining item for exactly that
   reason — it is the only proposed control that measures *outcome*.
3. **Reclaim before growing** — journal vacuum, logrotate, package cache,
   `docker system prune`, then re-measure and stop if resolved. **A disk at 90% is often
   90% logs**, where growing the volume is a permanent cost for a recurring problem. AWS
   Managed Services' own remediation cleans up first.
4. **Expansion counter** — alert on repeated growth of one volume. That pattern is the
   signal an **application leak needs fixing rather than feeding**.
5. **Root / LVM / RAID remediation** — excluded by AWS's documented extend procedure
   ("can't use these steps for partitions, the root file system, RAID devices, or LVM"),
   yet `/` is frequently what fills.
6. **Per-application alarm scoping** — add `Application` to `append_dimensions` and split
   alarms with `WHERE`. This is also the natural **sharding strategy** at the
   10,000-metric ceiling, which is what the metric-count guard alarm exists to announce.
7. **Multi-Region** — either per-Region sinks and alarm stacks converging on **one SNS
   topic** (dashboards are natively cross-Region), or **CloudWatch Metrics
   Centralization**, which replicates metrics into the destination Region so **one global
   alarm** spans all accounts and Regions (changing `GROUP BY` to
   `:@aws.account, :@aws.region`).
8. **Predictive "days until full"** — needs a Lambda, because CloudWatch metric math has
   **no `TREND` or `FORECAST` function** and alarms evaluate only the **last 3 hours**.
   `RATE` is point-to-point and unreliable on sparse data.
9. **Windows support** — `LogicalDisk % Free Space` via the native SSM document path,
   since the Ansible role targets Linux.
10. **Scheduled drift repair** — periodic controller runs, so a stopped agent or a
    hand-edited config is re-corrected rather than persisting unnoticed. **Narrower than it
    was:** the pilot proved that `resources: ["*"]` plus the hardened denylist picks up a
    newly mounted volume **with no re-run and no agent restart** (`tested_findings.md` §6),
    so the schedule is no longer needed for the *new volume* case — only for a stopped
    agent, a failed run, or an edited config. Doc 05 has the trade-off, which turns on a
    denylist failing **open** where an allowlist fails **closed**.

---

## Files

- [`cloudformation/00-monitoring-account.yaml`](../cloudformation/00-monitoring-account.yaml) — Phase 1: sink, SNS topics, transfer bucket, controller IAM
- [`cloudformation/10-workload-iam.yaml`](../cloudformation/10-workload-iam.yaml) — Phase 3: profile, roles, OAM link, Config, Rules 1–2 (parameter-free, auto-deploys to the OU)
- [`cloudformation/11-workload-endpoints.yaml`](../cloudformation/11-workload-endpoints.yaml) — Phase 3b: VPC endpoints, deployed per account with that account's VPC parameters
- [`cloudformation/12-monitoring-endpoints.yaml`](../cloudformation/12-monitoring-endpoints.yaml) — Phase 1b: monitoring-account endpoints so the controller can reach Organizations, STS, EC2, SSM and S3 without egress. us-east-1 only
- [`cloudformation/20-alarms-dashboard.yaml`](../cloudformation/20-alarms-dashboard.yaml) — Phase 7: alarms, `PARTIAL_DATA` guard, remediation trigger
- [`cloudformation/30-dashboard.yaml`](../cloudformation/30-dashboard.yaml) — Phase 7: cross-account dashboard
- [`ansible/site.yml`](../ansible/site.yml) — Phase 6: the playbook run, `serial`, `max_fail_percentage`
- [`ansible/inventory/aws_ec2.yml.template`](../ansible/inventory/aws_ec2.yml.template) — Phase 4: rendered per account by [`scripts/render_inventory.sh`](../scripts/render_inventory.sh)
- [`ansible/requirements.yml`](../ansible/requirements.yml) — Phase 4: controller-side collections and Python prerequisites
- [`ssm-documents/DiskSpace-GrowVolume.yaml`](../ssm-documents/DiskSpace-GrowVolume.yaml) — Phase 7: guarded volume growth, `DryRun` default
- [`lambda/enrich_disk_alarm.py`](../lambda/enrich_disk_alarm.py) — Phase 7: instance → mount → volume ID resolution, and the runbook's invoker — **required**, because the alarm supplies no `InstanceId`
