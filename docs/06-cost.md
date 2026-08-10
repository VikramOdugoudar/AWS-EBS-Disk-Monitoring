# 06 — Cost

## The governing rule

*"CloudWatch treats each unique combination of dimensions as a separate metric, even if
the metrics have the same metric name."*

Billing is **per metric per month**, so cost tracks **cardinality, not frequency**.

**Counterintuitive consequence worth stating plainly: collecting every 60 seconds costs
exactly the same as every 5 minutes.** Frequency is free. Only more unique dimension
combinations increase the bill.

```
metrics = instances × monitored mounts
```

Every cost decision in this design follows from that one line.

---

## What we pay

At three real mounts per host (`/`, `/var`, `/data` — what the mount filter in doc 03
actually selects):

| Fleet | Mounts/host | Metrics | Metric storage/month |
|---|---|---|---|
| 100 VMs | 3 | 300 | ~**$90** |
| 1,000 VMs | 3 | 3,000 | ~**$900** |
| 1,000 VMs | **8** | **8,000** | ~**$2,400** |
| 10,000 VMs | 3 | 30,000 | ~**$5,000** |

The 8-mount row is the point of the table: **mount count multiplies as powerfully as
instance count** — a database fleet with eight data volumes costs more than 2.5× a
three-mount fleet of identical size. **And it is the variable we control.** Fleet size is
given to us; mounts per host is a filter we write.

Tiering:

| Metrics | Rate |
|---|---|
| First 10,000 | $0.30 |
| 10,001 – 250,000 | $0.10 |
| 250,001+ | $0.05 |

So **tiering favours scale**: 10× the fleet is ≈ **5.5×** the cost ($900 → $5,000), not
10×. Per-instance cost falls as the estate grows, which means the design gets cheaper per
host at exactly the point where an acquisition-heavy estate needs it to.

---

## The expensive mistake we avoid

`resources: ["*"]` tells the agent to watch **every** mount the OS reports. On a plain
instance that is 2–3. On a container host it includes every overlay filesystem — **dozens
to hundreds per host**, each one a distinct `path` dimension and therefore a distinct
billable metric.

| Configuration | Mounts/host | Metrics at 1,000 VMs | Metric storage | All-in (with 2 alarms) |
|---|---|---|---|---|
| Filtered real mounts | 2–4 | 2,000–4,000 | $600–1,200 | $1,000–2,000 |
| `resources: ["*"]`, container host | ~50 | **50,000** | **$7,000** | **~$17,000** |

**Same fleet size, roughly 11× the bill, and it happens silently** — the agent does not
warn you, the metrics look correct, the alarms work. The only signal is the invoice, a
month later.

The test suite prints this ratio on every run:

```
resources:['*'] on container hosts would cost 11.3x more ($17,000 vs $1,500)
```

**That single `fstype` filter in the Jinja template is the cost control.** Not a tuning
knob — the difference between a $1,500 and a $17,000 monthly bill.

The three guards in doc 03 each attack cardinality differently:

- **enumerate from `ansible_mounts`, not `*`** — bounds the set to what this host actually
  has, computed per host at render time
- **`ignore_file_system_types`** — kills the overlay explosion, and excludes `tmpfs`,
  which is RAM, not disk: monitoring it is meaningless **and** billable
- **`drop_device: true`** — **removes a dimension**, so it prevents multiplication rather
  than merely filtering. Without it, a mount reachable by two device paths bills twice

The third is the structurally strongest: filters reduce a count, but removing a dimension
removes a factor from the product.

---

## Total at 1,000 VMs ≈ $1,710/month

| Line item | Basis | Monthly |
|---|---|---|
| Custom metrics | 3,000 metrics, tier 1 | ~**$900** |
| Alarms | Metrics Insights, per metric analyzed | ~**$600** |
| VPC endpoints — workload | 4 interface endpoints × 3 AZs | ~$88 |
| VPC endpoints — monitoring | 7 interface endpoints × 2 AZs (`organizations`, `sts`, `ec2`, SSM×3, `monitoring`) | ~$102 |
| Controller instance | one `t3.medium`-class host | ~$20 |
| Dashboard | first 3 free | $0–3 |
| **OAM metric sharing** | cross-account observability | **$0** |
| Enrichment Lambda | invoked only on alarm | ~$0 |
| | | **≈ $1,710** |

**Metrics + alarms are roughly 88% of spend**, so that is still the only place optimization
matters. Halving the entire endpoint bill saves $95; halving the metric count saves $750.
And because **both** metric lines track metric count, reducing metrics cuts both at once —
the lever is singular.

Note the monitoring-account endpoints are a **fixed, one-time** ~$102 — they scale with
neither fleet size nor account count, so their share of the bill shrinks as the estate
grows. At 10,000 VMs they are ~1% of spend.

OAM being free is a real design win rather than a rounding error. The alternative
cross-account pattern — CloudWatch metric streams into a central account — would have
added **per-GB ingestion charges plus destination storage**, a recurring cost that scales
with fleet size, to do the same job.

---

## Alarm cost — correcting a common misreading

Metrics Insights alarms are **not** more expensive than per-VM alarms. The intuition that
"one alarm watching 3,000 metrics must cost more than one alarm watching one metric" is
correct per alarm and irrelevant in aggregate. Compared like for like, both thresholds, at
$0.10 per metric per month:

| | Calculation | Monthly |
|---|---|---|
| Per-VM alarms | 3,000 metrics × 2 thresholds = 6,000 alarms | **$600** |
| Metrics Insights | 2 alarms × 3,000 metrics analyzed | **$600** |

**Identical.** So the operational properties that made Metrics Insights the choice in doc
04 — auto-adoption of new instances, no alarm lifecycle to manage, no silent coverage gaps
— come at **no premium**.

**No alarm type is cheaper:**

| Alarm type | Rate | Verdict |
|---|---|---|
| Standard metric alarm | $0.10 | same |
| High-resolution alarm | $0.30 | **3× worse** |
| Composite alarm | flat-rate | referenced alarms still bill separately |
| Metrics Insights | $0.10 per metric analyzed | same |

**So switching alarm type saves nothing — the only real lever is reducing metric count.**

**Granularity is free.** Billing follows what the query's filter *matches*, not how many
alarms exist: *"a Metric Insights query alarm that references a query whose filter matches
ten metrics incurs ten metrics analyzed cost per hour."* Partitioning 3,000 metrics across
20 per-account-per-environment alarms therefore costs exactly the same as 2 alarms over
all 3,000. Fine-grained routing is available at no charge.

**What does cost more is *overlapping* scopes.** If two alarms both match the same metric,
that metric is billed under each. An "all environments" alarm alongside a "prod only"
alarm double-bills every prod metric. **Clean partitioning is the rule** — every metric
matched by exactly one alarm at each threshold.

---

## The largest available saving — aggregate to one metric per instance with `MAX`

This is the first lever to pull under cost pressure, and it is worth more than everything
else combined.

The agent can publish an aggregate across mounts instead of one metric per mount:

```json
"metrics": {
  "aggregation_dimensions": [["InstanceId", "Environment"]],
  "metrics_collected": {
    "disk": {
      "measurement": ["used_percent"],
      "drop_original_metrics": ["disk_used_percent"]
    }
  }
}
```

`path` disappears from the dimension set, so cardinality drops from
`instances × mounts` to `instances`:

| | Per-mount (currently chosen) | Aggregated + `MAX` |
|---|---|---|
| Metrics at 1,000 VMs | 3,000 | **1,000** |
| Metric storage | $900 | **$300** |
| Alarm cost | $600 | **$200** |
| **Subtotal** | **$1,500** | **~$500** |

**~$1,000/month saved at 1,000 VMs**, and **the ratio holds at any fleet size** because it
removes the mount multiplier entirely rather than shaving a constant.

**`drop_original_metrics` is essential.** Without it the agent publishes **both** the
aggregated metric and the per-mount metrics — 4,000 metrics instead of 3,000, **increasing**
cost rather than reducing it. That is the trap: the config looks like an optimization and
is a 33% regression.

**Detection is not weakened.** The agent still measures every mount; it sends them as a
**statistic set** per period, so CloudWatch stores `Min`/`Max`/`Sum`/`SampleCount` and
**`Maximum` means "the fullest mount on this host"** — exactly what the per-mount design
detects. Nothing is sampled away.

The alarm query also simplifies. No `path` in `SCHEMA()`, and no `GROUP BY` needed to
collapse mounts, because there is nothing left to collapse:

```sql
SELECT MAX(disk_used_percent)
FROM SCHEMA("CWAgent", InstanceId, Environment)
WHERE AWS.AccountId = '<account>' AND Environment = '<env>'
ORDER BY MAX() DESC
```

**⚠️ The statistic must be `MAX`, never `Average`.** A host with mounts at 45/94/20%
reports **53%** under `Average` — sailing comfortably under an 80% threshold while a
filesystem is at 94%. It worsens as mounts multiply, because each healthy filesystem
dilutes the signal further: one full disk among ten averages to ~10%. `Average` over an
aggregated statistic set is not a slightly less sensitive alarm; it is an alarm that
**cannot fire on the failure it exists to catch**.

**What it costs you** — `path` leaves the metrics permanently, so:

- the dashboard's worst-20 widget ranks **instances** rather than **filesystems**: you
  learn *which host* is full, not *which mount*
- the enrichment Lambda must obtain the mount by running `df -h` via Run Command at alarm
  time rather than reading it from metrics

Arguably that second point is an improvement — `df` at alarm time is fresher than a
metric up to a minute old, and costs nothing — but it is an extra call in the alert path,
and it fails if the instance is unreachable precisely when the disk is full.

**Current decision: keep per-mount metrics**, retaining per-filesystem visibility in the
dashboard and metric-based mount lookup in the Lambda. At $1,500/month the diagnostic
detail is worth $1,000. This aggregation option is a **config-template change plus an
alarm-query change, with no redesign** — so it is available whenever cost pressure
increases, or immediately at fleet sizes where $1,000 becomes $10,000.

---

## At very high cardinality the economics invert

Publishing **OpenTelemetry metrics** to CloudWatch is priced at **$0.50/GB ingested with
no per-series charge**. Our cost is *entirely* per-series, so the two pricing models cross
over: below the crossover, per-series wins; above it, a fleet paying $0.05/series/month
for tens of thousands of series pays more than the bytes are worth.

Very high-cardinality fleets therefore eventually favour OTel. This is the honest answer
to *"what would you do at 20,000 instances?"* — not "the same thing, but bigger." The
trade is real: **PromQL-based alarming instead of Metrics Insights**, a different query
language, a different alarm model, and the loss of the auto-adoption property doc 04 was
chosen for.

---

## Cost is verified in CI

`tests/test_agent_config.py` includes a `TestCostProjection` class that computes the
**tiered** cost for the current mount-selection logic and prints both the projection and
the wildcard ratio, failing if the projection exceeds the expected envelope:

```
1,000 instances x 3 mounts = 3,000 metrics
Projected monthly cost (metrics + 2 alarms): $1,500
resources:['*'] on container hosts would cost 11.3x more ($17,000 vs $1,500)
```

The projection runs against the same `select_mounts()` logic the Jinja template uses, so
loosening the fstype filter moves the number. **A cardinality regression is caught before
it reaches a single instance**, not a month later on the bill — which is the only point at
which it is cheap to catch.

---

## Caveat on every figure

**Rates verified against the live AWS Price List API** (`us-east-1`, price-list publication
2026-08-06 / 2026-07-24). Every unit rate this document depends on was confirmed:

| Rate | API value | SKU / usage type |
|---|---|---|
| Custom metric tiers | $0.30 / $0.10 / $0.05 / **$0.02** | `CW:MetricMonitorUsage` |
| Metrics Insights alarm | $0.10 per metric analyzed per month | `CW:MetricInsightAlarmUsage` |
| Interface endpoint | $0.01/hr = **$7.30**/AZ/month | `USE1-VpcEndpoint-Hours` |
| Endpoint data processing | $0.01/GB (first 1 PB) | `USE1-VpcEndpoint-Bytes` |

Two notes on the tier table above: it omits a **fourth tier at $0.02 beyond 1,000,000
metrics**, which this design never reaches; and the boundaries are 10,000 / 250,000 /
1,000,000, so the "250,001+" row is really 250,001–1,000,000.

**Still to re-verify at implementation:** rates are **per-Region** and differ outside
`us-east-1`, and the EC2 controller line (~$20) is an instance-type estimate rather than a
queried rate. The ratios (11×, 3× vs 1×, the $1,000 aggregation saving) hold regardless of
absolute rates, because they derive from the same price list.

---

## Files

- [`ansible/roles/cw_agent/templates/amazon-cloudwatch-agent.json.j2`](../ansible/roles/cw_agent/templates/amazon-cloudwatch-agent.json.j2) — where the cost control lives: `resources`, `ignore_file_system_types`, `drop_device`
- [`ansible/roles/cw_agent/defaults/main.yml`](../ansible/roles/cw_agent/defaults/main.yml) — the fstype allowlist/denylist that sets cardinality
- [`cloudformation/20-alarms-dashboard.yaml`](../cloudformation/20-alarms-dashboard.yaml) — two Metrics Insights alarms; scope partitioning determines alarm spend
- [`tests/test_agent_config.py`](../tests/test_agent_config.py) — `TestCardinalityGuards` and `TestCostProjection`
