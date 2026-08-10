# Architecture

## The premise

**EC2 does not report filesystem fullness.** `AWS/EC2` gives CPU and network only.
`AWS/EBS` metrics measure **I/O activity, not occupancy** — a completely full disk
generates near-zero EBS activity, because nothing can be written, so `BurstBalance`
looks healthy while the application dies.

Filesystem occupancy is an operating-system fact that AWS cannot see. **Therefore an
in-guest agent is mandatory**, and every other decision follows from it.

---

## 1. End-to-end architecture

```mermaid
flowchart TB
    subgraph MON["🔵 MONITORING ACCOUNT"]
        direction TB
        CTRL["<b>Ansible Controller</b> (EC2)<br/>dynamic inventory · event-triggered<br/>NOT in the data path"]
        S3T["<b>S3 module-transfer bucket</b><br/>central · versioning OFF · 1-day expiry"]
        SINK["<b>OAM Sink</b><br/>org-scoped · CWAgent namespace only<br/>metric sharing is FREE"]
        ALARM["<b>Metrics Insights alarms</b><br/>per account × per environment<br/>warn 80 · critical 90"]
        GUARD["PARTIAL_DATA guard"]
        DASH["<b>Dashboard</b><br/>SEARCH — self-populating"]
        LAM["<b>Enrichment Lambda</b><br/>instance → path → vol-id"]
        REM["<b>SSM Automation</b><br/>snapshot → ModifyVolume"]
        SNSW["SNS: warning"]
        SNSC["SNS: critical"]
    end

    subgraph WL["🟢 WORKLOAD ACCOUNTS — N, auto-enrolled by OU"]
        direction TB
        EB1["EventBridge Rule 1<br/>instance → running"]
        EB2["EventBridge Rule 2<br/>tag change"]
        CFG["<b>AWS Config</b><br/>required-tags + profile<br/>auto-remediated"]
        subgraph VPC["VPC — private subnets, NO internet egress"]
            direction TB
            EP["<b>VPC endpoints</b><br/>ssm · ssmmessages · ec2messages<br/><b>monitoring</b> ← the data path<br/>s3 gateway (free)"]
            EC2["<b>EC2 instance</b><br/>SSM Agent (prerequisite)<br/>CloudWatch Agent"]
        end
        CW["CloudWatch<br/>(local)"]
    end

    subgraph ORG["🟠 ORGANIZATION"]
        SS["<b>CloudFormation StackSet</b><br/>SERVICE_MANAGED · AutoDeployment→OU"]
        EB3["EventBridge Rule 3<br/>stack CREATE_COMPLETE"]
    end

    SS ==>|"account joins OU ⇒<br/>everything appears"| WL
    SS --> EB3
    EB3 -->|"bulk run: all instances<br/>in the new account"| CTRL

    EB1 -->|"Run Command"| CTRL
    EB2 -->|"Run Command"| CTRL
    CFG -->|"applies tag ⇒ fires Rule 2"| EB2

    CTRL ==>|"<b>Session Manager</b><br/>amazon.aws.aws_ssm<br/>no SSH · no inbound ports"| EC2
    CTRL -.->|"stage module files"| S3T
    S3T -.->|"presigned URL"| EC2

    EC2 ==>|"<b>disk_used_percent</b><br/>every 60s"| CW
    EC2 -.->|"via monitoring endpoint"| EP
    CW ==>|"<b>OAM link</b> — free, no data movement"| SINK

    SINK --> ALARM
    SINK --> DASH
    SINK --> GUARD
    ALARM -->|"≥80%"| SNSW
    ALARM -->|"≥90%"| REM
    ALARM --> LAM
    LAM -->|"+ mount + volume id"| SNSC
    REM -->|"cross-account role"| EC2
    GUARD --> SNSW

    classDef mon fill:#e8f0fe,stroke:#1a73e8,stroke-width:2px
    classDef wl fill:#e6f4ea,stroke:#188038,stroke-width:2px
    classDef org fill:#fef7e0,stroke:#f29900,stroke-width:2px
    class MON mon
    class WL wl
    class ORG org
```

---

## 2. The two paths — configuration vs. data

The most important structural idea: **Ansible configures, the agent collects.**
These are separate paths with different lifetimes, and Ansible never carries a
measurement.

```mermaid
flowchart LR
    subgraph P1["PATH 1 — CONFIGURATION (occasional)"]
        direction TB
        A1["Controller"] -->|"Session Manager"| A2["Instance"]
        A2 --> A3["reads <b>ansible_mounts</b><br/><i>which filesystems exist?</i>"]
        A3 --> A4["renders agent config<br/>listing only real mounts"]
        A4 --> A5["starts the agent"]
        A5 --> A6["Ansible leaves.<br/>Nothing further."]
    end

    subgraph P2["PATH 2 — DATA (continuous, forever)"]
        direction TB
        B1["CloudWatch Agent<br/>on the instance"] --> B2["reads filesystem usage<br/><i>how full are they NOW?</i>"]
        B2 --> B3["PutMetricData<br/><b>every 60 seconds</b>"]
        B3 --> B4["CloudWatch"]
        B4 -.->|"buffers through<br/>network blips"| B1
    end

    A5 -.->|"hands off"| B1
```

| | Path 1 — Ansible | Path 2 — Agent |
|---|---|---|
| Question | *Which* filesystems exist? **structural** | *How full* are they? **temporal** |
| Frequency | On trigger | **Every 60s, always** |
| Produces | A config file | A metric stream |
| If it stops | Config goes stale | **Metrics stop — you are blind** |

Ansible *has* the usage numbers (`ansible_mounts` includes `size_available`) and
deliberately discards them: a scheduled tool produces datapoints only when it runs.
There is also **no Ansible module for `PutMetricData`** in any collection — it was
never intended as a metrics pipeline.

---

## 3. Enrollment — three triggers, no scheduler

```mermaid
sequenceDiagram
    participant LT as Launch Template
    participant EC2 as Instance
    participant CFG as AWS Config
    participant EB as EventBridge
    participant CTRL as Controller
    participant CW as CloudWatch

    Note over LT,CW: Path A — launched correctly tagged
    LT->>EC2: profile + DiskMonitoring=enabled
    EC2->>EC2: SSM Agent registers
    EC2->>EB: state → running (Rule 1)
    EB->>CTRL: Run Command: --limit i-abc
    CTRL->>EC2: configure agent
    EC2->>CW: disk_used_percent

    Note over LT,CW: Path B — launched untagged
    LT->>EC2: (tag missing)
    EC2->>EB: state → running (Rule 1)
    EB->>CTRL: Run Command
    CTRL--xEC2: not in inventory — no-op
    CFG->>EC2: AWS-SetRequiredTags applies tag
    EC2->>EB: tag change (Rule 2)
    EB->>CTRL: Run Command
    CTRL->>EC2: configure agent
    EC2->>CW: disk_used_percent
```

**Rule 2 is not optional.** With no scheduled sweep, an instance tagged *after* launch
would never be enrolled — the launch event already fired, found the instance absent
from tag-filtered inventory, did nothing, and never fires again. Rule 2 makes Config's
own remediation the enrollment trigger.

**Rule 3** covers a third case Rules 1 and 2 cannot: an acquired account's *existing*
instances. No launch event will ever fire for them, and a tag-change event fires only
if a tag was *missing* — so an instance that **already** carried `DiskMonitoring=enabled`
would generate no event at all.

---

## 4. Why one alarm covers the fleet

```mermaid
flowchart TB
    Q["<b>ONE Metrics Insights alarm</b><br/>SELECT MAX(disk_used_percent)<br/>FROM SCHEMA(CWAgent, InstanceId, path, Environment, fstype)<br/>WHERE AWS.AccountId=… AND Environment=…<br/>GROUP BY InstanceId · ORDER BY MAX() DESC"]

    Q --> C1["contributor: i-aaa<br/>45% · OK"]
    Q --> C2["contributor: i-bbb<br/><b>94% · ALARM</b>"]
    Q --> C3["contributor: i-ccc<br/>30% · OK"]
    Q --> CN["… N more, added<br/><b>automatically</b>"]

    C2 ==>|"any contributor breaches<br/>⇒ alarm fires"| ACT["Action"]

    NEW["new instance launches"] -.->|"metrics match the query ⇒<br/><b>becomes a contributor.</b><br/>No alarm created, ever."| Q
```

**The three alternatives are dead ends:**

| Approach | Why it fails |
|---|---|
| One alarm per VM per mount | 3,000 alarms at 1,000 VMs; a missed creation = **silently unmonitored** |
| `SEARCH()` in an alarm | *"A search expression cannot be used within an Alarm"* — an alarm must resolve to ONE state |
| Metric math | *"maximum of 10 metrics … cannot be increased"* ≈ 3 instances |

**`MAX`, never `AVG`:** a host at 45/94/20% averages to 53% — under an 80% threshold
while a filesystem is nearly full.

**`ORDER BY` is correctness:** past 500 series it decides *which* are evaluated;
descending guarantees the fullest disks are seen.

**The `SCHEMA()` clause names four dimensions, and the fourth is easy to miss.** The agent
emits `InstanceId, path, Environment, fstype` — `drop_device: true` removes `device` but
**not** `fstype`. `SCHEMA()` is an **exact-set** match, so a three-dimension clause matches
nothing at all. Verified live: the three-dimension form returned *"No time series were
returned by the query"* while the four-dimension form evaluated normally on the same data
(`tested_findings.md §2`). Worse, with `TreatMissingData: notBreaching` a zero-match query
reports a reassuring green **`OK` forever** rather than `INSUFFICIENT_DATA`, so
`InsufficientDataActions` never fires and the mismatch is completely silent.

**The alarm names no instance, at any grouping.** A firing fleet alarm carries only
*"1 out of 7 time series evaluated to ALARM"* and an empty `StateReasonData` — adding `path`
to `GROUP BY` does not change that, because the identity lives in the query **result**, never
in the alarm (`tested_findings.md §3`). The enrichment Lambda above is therefore the **only**
path from "something breached" to "this volume needs growing" — **mandatory, not an
enhancement.**

---

## 5. Cost — cardinality, not frequency

```mermaid
flowchart LR
    F["metrics =<br/>instances × mounts"] --> G["<b>Filtered mounts</b><br/>2–4 per host<br/>ansible_mounts + fstype filter"]
    F --> B["<b>resources: ['*']</b><br/>~50 per host on a<br/>container host"]
    G --> GC["1,000 VMs = 3,000 metrics<br/><b>~$1,500/mo</b>"]
    B --> BC["1,000 VMs = 50,000 metrics<br/><b>~$17,000/mo</b>"]
    BC -.->|"<b>11× the bill</b>, silently"| GC
```

`metrics_collection_interval: 60` costs the same as 300 — **frequency is free.** Only
unique dimension combinations are billed. That single `fstype` filter in the Jinja
template is the cost control.

**And it is the only cost control there is, because nothing downstream can undo it.** Once
published, a metric is stored and billed for **15 months**; neither CloudWatch nor OAM can
un-bill it, OAM filters by resource *type* rather than namespace, and `WHERE fstype='xfs'`
hides junk from **view** only (`tested_findings.md §5`). Filtering has to happen on the host
or not at all.

The cardinality model itself is now observed rather than modelled: 2 instances × 2 mounts
produced **exactly 4 metrics**, and the metric tracked filesystem reality — writing 6 GiB
moved `/data` from 1.02% to 61.40% while on-host `df` read 62% (`tested_findings.md §1`).

---

## 6. Key decisions

| Decision | Chosen | Rejected, and why |
|---|---|---|
| Access | **SSM** | SSH — key sprawl across accounts, inbound 22, bastion fleet, no native audit trail |
| Credentials | **Instance profile** | DHMC — profiles take *precedence* over it, so they conflict rather than compose |
| Execution | **Ansible controller** | On-node (`AWS-ApplyAnsiblePlaybooks`) — heavier change management, Ansible on every node, no version pinning |
| Centralization | **OAM** | Metrics Centralization — cannot filter to one namespace, replicates *all* custom metrics; cross-account push — every instance would hold central write credentials |
| Alarming | **Metrics Insights** | Per-VM alarms, `SEARCH()`, metric math — see §4 |
| Enrollment | **Tag + 3 event rules** | Scheduled sweep — dropped; consequence noted below |
| Remediation | **Snapshot → ModifyVolume** | Growth is irreversible ⇒ opt-in tag + size ceiling; root/LVM/RAID excluded by AWS's procedure |

---

## 7. Known limitations

1. **`ModifyVolume` grows the volume, not the filesystem.** Space is unusable by the OS
   until `growpart` + `resize2fs`/`xfs_growfs` run. Remediation therefore notifies
   rather than claiming resolution.
2. **Config verifies configuration, not outcome.** An instance whose agent crashed is
   fully compliant while sending nothing, and **`INSUFFICIENT_DATA` is not the safety net
   it looks like**: with `TreatMissingData: notBreaching` a query matching nothing reports
   green `OK`, verified live (`tested_findings.md §2`). Silence looks like health.
3. **No periodic re-run**, so configuration drift is neither repaired nor detected — a new
   volume on a running instance stayed unmonitored until the config was re-rendered by hand
   (`tested_findings.md §6`). `resources: ["*"]` with a **complete** denylist closes the
   new-volume case with no trigger at all; a stopped agent or an edited config still needs a
   scheduled run.
4. **Single Region.** Alarms cannot watch another Region's metrics; multi-Region needs
   per-Region stacks or Metrics Centralization.
5. **SSM Agent is a prerequisite.** Nothing here can install it — the Ansible
   connection *is* SSM, and no AWS API can run commands inside an instance without it.
6. **Scaling ceilings, in the order they bind:** (a) **SSM managed nodes — 2,400 per account per
   Region**, past which instances may *silently stop communicating* and become unreachable by
   Ansible; (b) **Metrics Insights alarms — 200 per Region, not adjustable**, giving ≈65
   account-environment scopes; (c) **10,000 metrics per query** ≈ 3,300 VMs per scope, where the
   `PARTIAL_DATA` guard makes approach visible. See `quotas.md`.

---

## 8. Alternative — Metrics Centralization and on-node Ansible

The evaluated-but-not-adopted alternative, kept here as the text-based counterpart to
`alternative-architecture.svg`. Metrics Centralization physically replicates metrics into a
destination account rather than federating queries as OAM does; the reasoning, cost arithmetic
and switch triggers are in README §10.

```mermaid
flowchart TB
    subgraph MGMT["🟣 MANAGEMENT / DELEGATED-ADMIN ACCOUNT — new participant"]
        direction TB
        RULE["<b>Centralization rule</b><br/>AWS::ObservabilityAdmin::<br/>OrganizationCentralizationRule<br/>scope: Organization | OU | Account"]
        TA["<b>Organizations trusted access</b><br/>+ service-linked role"]
    end

    subgraph DEST["🔵 DESTINATION ACCOUNT — owns the data"]
        direction TB
        COPY["<b>Centralized metric copy</b><br/>first copy FREE<br/>+ :@aws.account · :@aws.region"]
        ALARM["<b>Metrics Insights alarms</b><br/>evaluated on LOCAL data<br/>no query federation"]
        DASH["<b>Dashboard</b><br/>SEARCH now spans everything"]
        LAM["<b>Enrichment Lambda</b><br/>still assumes into workload<br/>account for DescribeVolumes"]
        REM["<b>SSM Automation</b><br/>snapshot → ModifyVolume"]
        HEALTH["<b>Rule health</b><br/>HEALTHY / UNHEALTHY /<br/>PROVISIONING"]
    end

    subgraph WL["🟢 WORKLOAD ACCOUNTS — N, unchanged from the chosen design"]
        direction TB
        CTRL2["Ansible controller reaches<br/>these over SSM — UNCHANGED"]
        subgraph VPC["VPC — private subnets, NO egress"]
            EP["<b>VPC endpoints</b><br/><b>monitoring</b> ← still mandatory"]
            EC2["<b>EC2 instance</b><br/>CloudWatch Agent"]
        end
        CW["CloudWatch<br/><b>local copy stays</b><br/>owners keep visibility"]
        OTHER["⚠️ <b>ALL other custom/EMF/OTLP<br/>metrics in the account</b><br/>cannot be filtered out"]
    end

    TA --> RULE
    CTRL2 ==>|"Session Manager<br/>configures agent"| EC2
    EC2 ==>|"disk_used_percent<br/>every 60s"| CW
    EC2 -.->|"via monitoring endpoint"| EP

    RULE ==>|"replicates"| COPY
    CW ==>|"<b>physical copy</b><br/>first copy $0"| COPY
    OTHER ==>|"<b>replicated too —<br/>NO namespace filter</b>"| COPY

    COPY --> ALARM
    COPY --> DASH
    RULE -.-> HEALTH
    ALARM --> LAM
    ALARM -->|"≥90%"| REM
    LAM -.->|"AssumeRole — tags/resource<br/>metadata NOT centralized"| WL
    REM -->|"cross-account role"| EC2

    classDef mgmt fill:#f3e8fd,stroke:#8430ce,stroke-width:2px
    classDef dest fill:#e8f0fe,stroke:#1a73e8,stroke-width:2px
    classDef wl fill:#e6f4ea,stroke:#188038,stroke-width:2px
    classDef warn fill:#fce8e6,stroke:#d13212,stroke-width:2px
    class MGMT mgmt
    class DEST dest
    class WL wl
    class OTHER warn
```

---

## Rendered diagrams

Presentation-ready exports of the two headline architectures, embedded in the README:

- [`main-architecture.svg`](main-architecture.svg) — the chosen hub-and-spoke design
- [`alternative-architecture.svg`](alternative-architecture.svg) — the alternative, showing
  both Metrics Centralization and on-node Ansible
