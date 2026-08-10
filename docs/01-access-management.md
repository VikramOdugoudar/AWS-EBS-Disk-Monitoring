# 01 — Access Management

## The problem

VMs across many AWS accounts. Something must reach them to install and configure
software, and that reach must be secure, auditable, and revocable without touching
every host.

## Decision: AWS Systems Manager, not SSH

The SSM Agent on each instance makes an **outbound** connection to AWS and polls for
work. Nothing connects inward.

| Property | Consequence |
|---|---|
| Outbound-only | **No inbound rules** on instance security groups; no public IP needed |
| IAM-based authorization | Revoking access is one policy change, not a visit to every VM |
| CloudTrail records every command | Attributed to an IAM principal, by default |
| Works in private subnets | No bastion, no NAT required (with endpoints) |

### Rejected — SSH with bastion hosts

Key generation, distribution, and rotation across ~50 accounts; inbound port 22; a
bastion fleet that must itself be patched and monitored; and no per-command audit trail
without building one. The failure mode that matters in practice: an ex-employee's key
still present on a host nobody remembers.

### Rejected — EC2 Instance Connect

Solves key *distribution* (push-on-demand, 60-second validity) but is still SSH, still
needs network reachability, and is interactive-only — no scheduled automation.

---

## Prerequisite: SSM Agent must already be running

**This design cannot install SSM Agent, and that is structural rather than a
preference.**

- Ansible's connection *is* SSM (`amazon.aws.aws_ssm`), so reaching a host requires the
  agent to be running already. Installing it over that connection is circular —
  **Ansible cannot bootstrap its own transport.**
- A Lambda cannot do it either: **no AWS API can run commands inside an instance
  without SSM Agent.** That is precisely the gap the agent exists to fill.

In practice this is a non-issue — the agent is preinstalled on Amazon Linux 2/2023,
Ubuntu LTS, and Windows AMIs. Where a hardened AMI strips it, installation belongs in
the image build or userdata.

The Ansible role therefore **verifies** the agent is running and fails loudly if not,
rather than attempting an impossible install.

---

## Instance identity: one standardized instance profile

```yaml
InstanceProfile: DiskMonitoringInstanceProfile
Role: DiskMonitoringInstanceRole
  ManagedPolicyArns:
    - AmazonSSMManagedInstanceCore   # agent registers and receives commands
    - CloudWatchAgentServerPolicy    # agent calls PutMetricData — the data path
```

Two facts that matter:

- **One profile serves unlimited instances.** It is a one-to-many relationship, so there
  is no per-instance IAM object to create. 200 instances all reference the same profile.
- **Exactly one profile per instance** is the quota. You cannot attach a second
  alongside — which is why a single profile carries *both* policies.

AWS-managed policies are used deliberately: AWS updates them as the services evolve, so
the design does not silently drift into breakage.

### Rejected — Default Host Management Configuration (DHMC)

DHMC makes every instance in an account SSM-managed with no profile attached, which
would be attractive for onboarding an acquired account's existing instances. It was
reconsidered on merit after IMDSv2 became a stated assumption, and still loses:

| Reason | Detail |
|---|---|
| **They conflict rather than compose** | *"SSM Agent attempts to use instance profile permissions **before** using the Default Host Management Configuration permissions."* A profile takes precedence, so DHMC is bypassed wherever one exists |
| All-or-nothing | *"Any changes made to the IAM role … applies to **all** managed EC2 instances in the Region and account"* — no per-instance opt-out |
| Per Region | Must be activated in each account **and** Region, with up to 30 minutes before instances pick up credentials |
| Fragile | Deleting `/var/lib/amazon/ssm` breaks registration and then *requires* a profile anyway |
| Weaker auditability | Permissions come from an invisible account-level service setting rather than the instance's own visible configuration |
| Unverified | Whether `AmazonSSMManagedEC2InstanceDefaultPolicy` includes `cloudwatch:PutMetricData`. If not, a profile is needed regardless — collapsing the benefit |

---

## Cross-account roles

Two roles per workload account, both created by the StackSet:

| Role | Grants | Used by |
|---|---|---|
| `DiskMonitoringInventoryRole` | `ec2:DescribeInstances`, `DescribeTags` | Controller's dynamic inventory |
| `DiskMonitoringReadRole` | `DescribeVolumes`, `CreateSnapshot`, `ModifyVolume` (tag-gated) | Enrichment Lambda, remediation runbook |

Trust is scoped with **`aws:PrincipalOrgID`** on the **workload side** (the role's trust
policy, where the caller is what needs bounding) and **`aws:ResourceOrgID`** on the
**controller side** (its `sts:AssumeRole` policy, where the *target* is what needs
bounding). **The two are not interchangeable, and using the wrong one silently restricts
nothing:**

| Key | Describes | Correct use here |
|---|---|---|
| `aws:PrincipalOrgID` | Org of the **caller** | Workload role trust policies — "only principals in my org may assume this" |
| `aws:ResourceOrgID` | Org of the **resource being accessed** | Controller's `sts:AssumeRole` — "only roles in my org may be assumed" |

The controller's policy names `arn:aws:iam::*:role/DiskMonitoring*Role` — a **wildcard
account** — so something must bound which accounts it may assume into.
`aws:PrincipalOrgID` cannot do that job: the caller is always the controller, always in the
org, so the condition is **tautologically true**. It *looks* like a target-account
restriction while permitting any account outside the org that creates a same-named role
trusting this controller. `aws:ResourceOrgID` is the key that actually constrains the
target.

### Rejected — `sts:ExternalId`

ExternalId solves the **confused deputy** problem, which is inherently a *third-party*
scenario: you give a vendor a role, the vendor serves many customers, and without a
per-customer secret one customer could trick the vendor into acting on another's
account. The vendor is the "deputy" that can be confused.

Inside your own Organization there is no third party and no deputy. `aws:PrincipalOrgID`
is the **stronger** control: enforced by AWS from org membership, and impossible to leak
— whereas an ExternalId is just a shared string sitting in your IaC. Adding it would be
cargo-culting a control past its threat model, and would imply a threat that does not
exist here.

---

## Network: VPC endpoints (mandatory here)

Instances **and the controller** are in private subnets with **no internet egress**, so
both need a private path to AWS services. That means **two endpoint stacks**, and the
distinction matters because the two sides need different services.

### Workload accounts — `11-workload-endpoints.yaml`

| Endpoint | Purpose |
|---|---|
| `ssm` | Agent registration and command polling |
| `ssmmessages` | Session Manager data channel — what Ansible's connection rides on |
| `ec2messages` | Run Command message delivery |
| **`monitoring`** | **The metric data path** |
| `s3` (gateway, **free**) | Ansible module transit · CloudWatch agent package · Amazon Linux repos |

### Monitoring account — `12-monitoring-endpoints.yaml`

Easy to forget, because the instinct is that endpoints are for the hosts being *managed*.
But the controller is itself in a private subnet, and without these it cannot function at
all — no inventory, no connection, and no way for EventBridge to invoke it.

| Endpoint | Purpose | What breaks without it |
|---|---|---|
| **`organizations`** | `render_inventory.sh` enumerates accounts at runtime | `set -euo pipefail` exits → **no inventory is ever produced** |
| **`sts`** | `site.yml`'s `pre_tasks` assume the per-account session role | Every task fails `AccessDeniedException` |
| `ec2` | Dynamic inventory's `DescribeInstances` | `ansible-inventory --graph` hangs, then fails — reads like an IAM fault |
| `ssm`, `ssmmessages`, `ec2messages` | Controller registers as a managed node; the connection plugin rides `ssmmessages` outbound | EventBridge `SendCommand` matches **zero** targets and returns `Success` |
| `monitoring` | Alarm/metric API calls | Verification steps fail |
| `s3` (gateway, **free**) | Controller *uploads* each module file; also agent/OS packages | Every task fails at module staging |

**⚠️ Two constraints specific to this stack.**

**`organizations` is Region-restricted, not unavailable.** A widely repeated belief is that
Organizations has no PrivateLink support and the controller therefore needs NAT. It does
have support — but *"you can create an interface VPC endpoint for AWS Organizations only in
the Region where the AWS Organizations control plane is located,"* which is **us-east-1** in
commercial partitions (also `cn-northwest-1` and `us-gov-west-1`). So the monitoring account
runs in us-east-1 and the template asserts it. Elsewhere, reach a us-east-1 endpoint over
**Transit Gateway** — **not NAT**. The no-egress posture holds estate-wide.

**The `sts` endpoint alone is not enough.** The *global* endpoint `sts.amazonaws.com`
**bypasses a VPC endpoint entirely**. The controller must target the regional endpoint:

```bash
export AWS_STS_REGIONAL_ENDPOINTS=regional
```

Without it the endpoint exists, is billed, and is silently unused while the credential step
tries to reach the internet.

Three things commonly get missed:

1. **The `monitoring` endpoint is easy to forget, and its absence is confusing to
   debug.** SSM works perfectly, Ansible succeeds, the agent runs — and no metric ever
   arrives, because `PutMetricData` cannot reach CloudWatch.
2. **"No inbound rules on instances" is not "no rules anywhere."** The *endpoints* need
   443 inbound from the instance security group.
3. **`PrivateDnsEnabled: true` plus VPC DNS attributes** (`enableDnsSupport`,
   `enableDnsHostnames`), or service names will not resolve to the endpoints.

**The S3 gateway endpoint is free and load-bearing for two consumers** — Ansible module
transit (the instance `curl`s a presigned URL) and the agent package plus OS repos,
both S3-backed. Omitting it silently breaks all configuration.

**Cost:** ~$7.30/AZ/month per interface endpoint.

| Stack | Endpoints | AZs | Monthly |
|---|---|---|---|
| `11-workload-endpoints.yaml` | 4 interface + free S3 gateway | 3 | ~**$88 per VPC** |
| `12-monitoring-endpoints.yaml` | 7 interface + free S3 gateway | 2 | ~**$102 once** |

The monitoring account uses **2 AZs, not 3**: the controller is a single instance, so two
AZs keep it relaunchable into either without editing the endpoints stack, and a third would
pay for idle ENIs. The monitoring cost is a **one-time addition, not per-VPC** — it scales
with neither fleet nor account count.

This is a stated consequence of the no-egress posture rather than a choice. NAT is often
*more* expensive (~$32/month per gateway **plus per-GB processing**). At many-VPC scale,
centralized endpoints shared via PrivateLink are the mitigation.

---

## Files

- [`cloudformation/00-monitoring-account.yaml`](../cloudformation/00-monitoring-account.yaml) — controller IAM, transfer bucket
- [`cloudformation/10-workload-iam.yaml`](../cloudformation/10-workload-iam.yaml) — instance profile, cross-account roles, Config rules, OAM link
- [`cloudformation/11-workload-endpoints.yaml`](../cloudformation/11-workload-endpoints.yaml) — the VPC endpoints (deployed per account)
- [`cloudformation/12-monitoring-endpoints.yaml`](../cloudformation/12-monitoring-endpoints.yaml) — monitoring-account endpoints for the controller: `organizations`, `sts`, `ec2`, SSM trio, `monitoring`, free S3 gateway. us-east-1 only
