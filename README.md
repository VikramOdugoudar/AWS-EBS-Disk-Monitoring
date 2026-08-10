# Disk Utilization Monitoring at Scale — AWS + Ansible

Detect low disk space early across many AWS accounts, before it causes downtime.

---

## The premise everything rests on

**EC2 does not report filesystem fullness.**

- `AWS/EC2` provides CPU and network metrics only.
- `AWS/EBS` metrics measure **I/O activity, not occupancy** — a completely full disk
  generates near-zero EBS activity, because nothing can be written. `BurstBalance`
  looks perfect while the application dies.

Filesystem occupancy is an operating-system fact that AWS cannot see from outside.
**Therefore an in-guest agent is mandatory**, and every other decision follows from it.

---

## How it works, in one paragraph

An **Ansible controller** in the monitoring account reaches instances over **SSM
Session Manager** — no SSH keys, no inbound ports, no bastion. It discovers them from
**tag-filtered dynamic inventory**, so there is no host list anywhere. On each host it
installs the **CloudWatch agent** and generates that host's config from its own
filesystems (`ansible_mounts`), which is both the correct result and the cost control.
The agent then publishes `disk_used_percent` **every 60 seconds, continuously** —
Ansible never carries a measurement. **OAM** shares those metrics into the monitoring
account for free, where **Metrics Insights alarms** cover every instance and adopt new
ones automatically. At 80% an email goes out; at 90% an SSM Automation runbook
snapshots and grows the EBS volume.

📐 **[Full architecture with diagrams →](architecture/architecture.md)**

---

## Repository layout

```
monitoring-disk/
├── README.md                     ← you are here
├── alternatives.md               every option considered, and why it lost
├── limitations.md                what this cannot do, and deferred work
├── quotas.md                     AWS quotas: which bind, and when
├── tested_findings.md            ← live-pilot record
├── context_after_testing.md      what deployment changed, and why
│
├── architecture/
│   └── architecture.md           diagrams: end-to-end, two paths, enrollment, cost
│
├── ansible/                      ← the configuration engine
│   ├── ansible.cfg
│   ├── requirements.yml          amazon.aws (controller only)
│   ├── site.yml                  entry point; serial/max_fail_percentage
│   ├── group_vars/all.yml        SSM connection settings
│   ├── inventory/
│   │   └── aws_ec2.yml.template  rendered per account by scripts/render_inventory.sh
│   └── roles/cw_agent/
│       ├── defaults/main.yml     cardinality controls live here
│       ├── tasks/main.yml        4 tasks — install, template, service
│       ├── handlers/main.yml     reload only on config change
│       └── templates/amazon-cloudwatch-agent.json.j2   ← THE COST CONTROL
│
├── scripts/
│   └── render_inventory.sh       ListAccounts → one inventory file per account
│
├── cloudformation/
│   ├── 00-monitoring-account.yaml   OAM sink, SNS, transfer bucket, controller IAM,
│   │                                EventBridge Rule 3 + forwarded-event receiver
│   ├── 10-workload-iam.yaml         StackSet: profile, roles, OAM link, Config,
│   │                                Rules 1–2 — parameter-free, auto-deploys to an OU
│   ├── 11-workload-endpoints.yaml   VPC endpoints — per account (VPC IDs are
│   │                                account-specific, so these cannot auto-deploy)
│   ├── 12-monitoring-endpoints.yaml monitoring-account endpoints for the controller:
│   │                                organizations, sts, ec2, ssm×3, monitoring, s3.
│   │                                us-east-1 only — Organizations' endpoint exists
│   │                                only in the control-plane Region
│   ├── 20-alarms-dashboard.yaml     alarms + PARTIAL_DATA guard + remediation trigger
│   └── 30-dashboard.yaml            cross-account dashboard
│
├── lambda/
│   └── enrich_disk_alarm.py      resolves instance → mount → EBS volume id
│
├── ssm-documents/
│   └── DiskSpace-GrowVolume.yaml  guarded volume growth (AWS-side only)
│
└── docs/
    ├── 01-access-management.md
    ├── 02-execution-model.md
    ├── 03-collection.md
    ├── 04-aggregation-alarming.md
    ├── 05-scalability.md
    ├── 06-cost.md
    └── 07-deployment.md
```

---

## Quick start

```bash
# 1. Monitoring account foundation
aws cloudformation deploy --template-file cloudformation/00-monitoring-account.yaml \
  --stack-name disk-monitoring-foundation --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides OrganizationId=o-xxxx WarningEmail=… CriticalEmail=…

# 2. Workload IAM/enrollment as a StackSet, auto-deployed to an OU.
#    Parameter-free, so onboarding an account is one action: move it into the OU.
#    Then 11-workload-endpoints.yaml per account, with that account's VPC parameters.
#    (see docs/07-deployment.md for the full ordered runbook)

# 3. On the controller — render inventory, then run
ansible-galaxy collection install -r ansible/requirements.yml
export AWS_REGION=us-east-1
export DISK_MONITORING_TRANSFER_BUCKET=<from stack output>

# MANDATORY with VPC endpoints: the GLOBAL endpoint sts.amazonaws.com bypasses the
# sts interface endpoint entirely, so site.yml's cross-account credential step would
# try to reach the internet and hang. Regional STS is what routes it privately.
export AWS_STS_REGIONAL_ENDPOINTS=regional

./scripts/render_inventory.sh            # ListAccounts → one file per account
ansible-inventory -i ansible/inventory --graph          # confirm hosts resolve
ansible-playbook -i ansible/inventory ansible/site.yml --check --diff   # dry run
ansible-playbook -i ansible/inventory ansible/site.yml
```

Inventory files are **generated, never committed** — the account list comes from
`organizations:ListAccounts` at runtime, so a new account is picked up with no edits.

📋 **[Ordered deployment runbook with verification →](docs/07-deployment.md)**

---

## How the brief is answered

| Requirement | Where |
|---|---|
| Architecture diagram | [`architecture/architecture.md`](architecture/architecture.md) |
| Ansible playbooks / roles | [`ansible/`](ansible/) — role, template, inventory, `site.yml` |
| Secure multi-account access | [`docs/01`](docs/01-access-management.md) — SSM, instance profiles, endpoints |
| Reliable data collection | [`docs/03`](docs/03-collection.md) — agent installed and configured from host facts |
| Centralize & present | [`docs/04`](docs/04-aggregation-alarming.md) — OAM, alarms, dashboard |
| Scalability | [`docs/05`](docs/05-scalability.md) — StackSet auto-deploy, tag-driven enrollment |
| VM discovery & enrollment | [`docs/05`](docs/05-scalability.md) — tag + 3 EventBridge rules + Config |

---

## Stated assumptions

These are assumptions about the target environment, **not verified facts**. Each is
listed with what breaks if it is wrong. Where a live pilot has since **exercised** the
assumption rather than merely stating it, that is marked ✅ and recorded in
`tested_findings.md`.

| # | Assumption | If wrong |
|---|---|---|
| 1 | Fleet is **Amazon Linux 2/2023 with SSM Agent running** | An instance without the agent is unreachable. Nothing here can install it — the Ansible connection *is* SSM, and no AWS API can run commands inside an instance without it. Fix at the AMI or userdata level. |
| 2 ✅ | Instances **and the controller** are in **private subnets with no internet egress, and no NAT** | VPC endpoints are therefore **mandatory**, not an optimization — `11-workload-endpoints.yaml` for instances, `12-monitoring-endpoints.yaml` for the controller. **This holds for every AWS API the design calls, Organizations included**: PrivateLink for Organizations exists, so no NAT is needed anywhere. If a VPC does have NAT, the interface endpoints become optional — but still add the **free S3 gateway endpoint**, which diverts the highest-volume traffic (Ansible module transit, agent package, OS repos) away from NAT's $0.045/GB at no cost. **✅ Exercised:** two instances with **no public IP, no IGW route and no NAT** reached SSM (`Online` within **10 s**) and published metrics through the `monitoring` endpoint. |
| 3 ✅ | All instances use **IMDSv2** | No IMDSv1 handling exists anywhere in the design. **✅ Exercised** with IMDSv2 required — the design's own path works under it. |
| 4 | All accounts are in **one AWS Organization** | Required for the org-scoped sink policy and StackSet auto-deployment. |
| 5 | **Single Region**, and the monitoring account is in **us-east-1** | Alarms cannot watch another Region's metrics; multi-Region needs per-Region stacks or Metrics Centralization. The us-east-1 part is a separate constraint: the **Organizations interface endpoint exists only in the control-plane Region**, so `12-monitoring-endpoints.yaml` asserts it. Running the monitoring account elsewhere needs Transit Gateway to a us-east-1 endpoint — still no egress. Workload accounts are unaffected and may be in any Region the alarms cover. |
| 6 | Pricing figures are **single-Region list prices** | Must be re-verified at implementation — the Pricing API needs credentials. |

---

## What a live pilot confirmed

A working subset of this design was deployed into two real accounts — workload
`<1111111111>` sharing into monitoring `<2222222222>` — and exercised against real AWS
APIs. The following were **inference before and are observation now**. Full record and
quoted outputs in **[`tested_findings.md`](tested_findings.md)**.

| Claim | Observed |
|---|---|
| SSM reachability with **no public IP and no NAT** | both instances `Online` **10 s** after launch |
| Metrics reach CloudWatch over the **`monitoring` endpoint** | 20 datapoints with real values |
| The metric tracks **filesystem reality** | wrote 6 GiB → `/data` moved 1.02% → **61.40%**; on-host `df` read 62% |
| Cardinality is **instances × mounts** | 2 instances × 2 mounts = **exactly 4 metrics** |
| **OAM shares metrics cross-account** | the monitoring account queried **all 7** workload metrics |
| **One alarm covers hosts across an account boundary** | the warning 80% alarm entered `ALARM` on workload-account data |
| **`MAX`, not `AVG`** | `GROUP BY InstanceId` collapsed 3 mounts to 1 contributor reporting its fullest |

**What it did not cover**, so the pilot is not read as broader validation than it is: the
Ansible controller and playbook path (configuration was applied via SSM Run Command),
event-driven enrollment, remediation, the enrichment Lambda, SNS delivery, and anything at
scale — it ran on **two instances**.

It also found three defects worth naming here because they change how the design must be
read: the agent emits a **fourth dimension, `fstype`**, so every three-dimension `SCHEMA()`
clause matched nothing; a dimension mismatch fails **silently green**, not
`INSUFFICIENT_DATA`; and a fleet alarm carries **no identity at any grouping**. All three
are reflected in the limitations below.

---

## Known limitations

Stated plainly, because a design that hides these is harder to trust.

1. **`ModifyVolume` grows the volume, not the filesystem.** Space is unusable by the OS
   until `growpart` + `resize2fs`/`xfs_growfs` run on the host. The remediation runbook
   therefore **notifies rather than claiming resolution.**
2. **AWS Config verifies configuration, not outcome.** An instance whose agent crashed
   is fully compliant while sending nothing. And **`INSUFFICIENT_DATA` is not the safety
   net it looks like**: it fires only if metrics stop *everywhere*, and a query that
   matches **no** metrics at all reports a reassuring green **`OK` forever** — verified
   live, so `InsufficientDataActions` never fires (`tested_findings.md §2`).
3. **No periodic re-run**, so configuration drift is neither repaired nor detected.
   **Reproduced live:** a volume attached to a running instance and filled to 40% stayed
   invisible in CloudWatch, with AWS Config still COMPLIANT, until the agent config was
   re-rendered by hand (`tested_findings.md §6`).
4. **A fleet alarm names nothing, and the component that fixes that is not deployed.**
   A firing alarm carries only *"1 out of 7 time series evaluated to ALARM"* — no
   instance, no path, no volume — and **a finer `GROUP BY` does not change that**,
   because the detail lives in the query result rather than the alarm
   (`tested_findings.md §3`). The enrichment Lambda is therefore the **only** route from
   "something breached" to "this volume needs growing" — **mandatory, not optional** —
   and until it ships, notifications always lack detail.
5. **Scaling ceiling — the binding limit is alarm quota, not metric count.** A single alarm
   scope caps at ~3,300 VMs (the 10,000-metric query limit), and the `PARTIAL_DATA` guard
   alarm makes approach visible rather than silent. But sharding to escape that limit
   *consumes alarms* against the **200-per-Region quota**, so at ~3 alarms per
   account-environment pair the real ceiling is roughly **65 account-environment scopes per
   Region**. That figure is arithmetic from two documented quotas, **not a documented
   combined limit** — treat it as a number to sanity-check rather than a hard AWS boundary.
6. **Linux only.** Windows needs the native SSM document path with
   `LogicalDisk % Free Space`.

Items 1–4 are the highest-value next work, and **item 4 is now the first of them** — the
pilot reclassified it from cosmetic to functional.

📋 **[Full limitations, scaling ceilings and deferred work →](limitations.md)**
🔀 **[Every alternative considered and why it lost →](alternatives.md)**
📐 **[Service quotas — which bind, and when →](quotas.md)**

---

## Cost

| Fleet | Metrics | Metrics + alarms | All-in |
|---|---|---|---|
| 100 VMs | 300 | ~$150 | ~$360 |
| 1,000 VMs | 3,000 | ~$1,500 | **~$1,710** |
| 10,000 VMs | 30,000 | ~$11,000 | ~$11,210 |

All-in adds workload VPC endpoints (~$88/VPC), the **monitoring-account endpoints
(~$102: 7 interface × 2 AZs × ~$7.30)**, the controller (~$20) and the dashboard ($0–3).
**Metrics + alarms are ~93% of spend**, so that is the only place optimization matters —
and because both track metric count, reducing metrics cuts both lines at once.

**Cost tracks cardinality, not frequency** — collecting every 60 seconds costs the same
as every 5 minutes. The lever is metric count:

- `resources: ["*"]` on container hosts would cost **~11× more** — $17,000 vs $1,500 at
  1,000 VMs, because 50,000 metrics incur ~$7,000 storage **plus ~$10,000 in alarms**
  (alarms bill per metric *analyzed*). **The pilot qualified this:** the ~11× is what the
  wildcard costs when the *denylist* is incomplete — and the repo's was, by nine entries,
  one of which (`vfat` on `/boot/efi`) leaked through as a billable metric. With a complete
  denylist the wildcard matched the allowlist's cardinality **and** picked up new volumes
  automatically. The trade is real but asymmetric — an **allowlist fails closed, a
  denylist fails open** — and is argued in [`alternatives.md §12`](alternatives.md).
- Aggregating to one metric per instance with `MAX` would cut ~$1,000/month at 1,000 VMs,
  at the cost of losing per-filesystem detail — see [`docs/06`](docs/06-cost.md)

⚠️ **There is no display-time filtering, so this is the only place the decision can be
made.** A published metric is **stored and billed for 15 months**; neither CloudWatch nor
OAM can un-bill it (OAM filters by resource *type*, not namespace), and a `WHERE` clause
hides junk from **view** only (`tested_findings.md §5`). Get it wrong on the host and you
pay for 15 months.

📊 **[Cost model →](docs/06-cost.md)**
