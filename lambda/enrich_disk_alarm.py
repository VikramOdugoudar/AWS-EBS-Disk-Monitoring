"""
Turn a fleet disk alarm into an actionable alert, and drive remediation at 90%.

WHY THIS EXISTS
    A Metrics Insights alarm carries NO identity. Verified on a firing alarm during the
    live pilot (tested_findings.md 3):

        StateReason     : "1 out of 7 time series evaluated to ALARM"
        StateReasonData : {"version": "1.0", "queryDate": "..."}

    No instance, no path, no volume — at ANY GROUP BY. Finer grouping puts detail in the
    query RESULT, never in the ALARM. So this function rebuilds the whole chain:

        alarm fires -> re-run the query WITH path -> instance + path
                    -> resolve path -> EBS volume id ON THE HOST
                    -> 80%: notify with volume id
                    -> 90%: StartAutomationExecution to grow that volume

    That makes this function MANDATORY, not an enhancement. Without it an operator learns
    only that "something breached" somewhere in the fleet.

WHY VOLUME RESOLUTION RUNS ON THE HOST
    EC2 cannot map a mount to a volume. DescribeVolumes reports the ATTACHMENT device
    name, and on Nitro the guest renames it — verified in the pilot:

        EC2 says   : vol-0ccc...ccc -> /dev/sdf
        guest says : /data          -> /dev/nvme1n1

    There is no /dev/sdf block device in the guest, only a symlink. So matching on
    Attachments[].Device CANNOT work (this disproves findings.md 17.2's proposed fix).
    `ebsnvme-id` is the AWS-provided tool that reports the volume id directly; the NVMe
    controller serial is the fallback. Note /sys/block/<disk>/serial returns NOTHING on
    AL2023 — that method was tried and failed.
"""

import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import ClientError

log = logging.getLogger()
log.setLevel(logging.INFO)

NAMESPACE = "CWAgent"
METRIC_NAME = "disk_used_percent"

# fstype is NOT optional. The agent emits InstanceId, path, Environment AND fstype;
# drop_device removes `device` but not `fstype`. SCHEMA() is an exact-set match, so
# omitting it matches nothing — and with TreatMissingData: notBreaching the alarm then
# reports a green OK forever rather than INSUFFICIENT_DATA (tested_findings.md 2).
SCHEMA_DIMENSIONS = "InstanceId, path, Environment, fstype"

READ_ROLE_NAME = os.environ.get("READ_ROLE_NAME", "DiskMonitoringReadRole")
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "")
REGION = os.environ.get("AWS_REGION", "us-east-1")

# Thresholds come from the environment so they cannot drift from the CloudFormation
# WarningThreshold/CriticalThreshold parameters. Hardcoding them meant a dev stack at
# 90/95 was filtered at 80/90, reporting non-breaching filesystems as breaching.
WARNING_THRESHOLD = float(os.environ.get("WARNING_THRESHOLD", "80"))
CRITICAL_THRESHOLD = float(os.environ.get("CRITICAL_THRESHOLD", "90"))

# .get, not [...]: a module-scope subscript raises KeyError at import, which surfaces as
# a cold-start failure with no log line from this code. Checked in the handler instead.
SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN", "")
REMEDIATION_DOCUMENT = os.environ.get("REMEDIATION_DOCUMENT", "DiskSpace-GrowVolume")
REMEDIATION_ROLE_ARN = os.environ.get("REMEDIATION_ROLE_ARN", "")
# Growth is irreversible, so remediation is opt-in per deployment as well as per volume.
ENABLE_REMEDIATION = os.environ.get("ENABLE_REMEDIATION", "false").lower() == "true"

cloudwatch = boto3.client("cloudwatch")
sns = boto3.client("sns")
sts = boto3.client("sts")


def _assume(account_id: str, service: str):
    """Assume the read role in a workload account.

    Cross-account is required even though metrics arrive via OAM: OAM shares metric QUERY
    ACCESS, but EC2 and SSM resources still live in the workload account, and OAM does not
    share resource tags at all.
    """
    resp = sts.assume_role(
        RoleArn=f"arn:aws:iam::{account_id}:role/{READ_ROLE_NAME}",
        RoleSessionName="disk-alarm-enrichment",
    )
    c = resp["Credentials"]
    return boto3.client(
        service,
        region_name=REGION,
        aws_access_key_id=c["AccessKeyId"],
        aws_secret_access_key=c["SecretAccessKey"],
        aws_session_token=c["SessionToken"],
    )


def _scope_from_alarm_name(alarm_name: str):
    """Extract account id and environment from `disk-{level}-{account}-{env}`."""
    m = re.match(r"^disk-(?:warning|critical)-(\d{12})-(.+)$", alarm_name)
    if not m:
        raise ValueError(f"Unrecognised alarm name format: {alarm_name}")
    return m.group(1), m.group(2)


def _parse_label(label: str):
    """Pull instance id and path out of a Metrics Insights series label.

    THE LABEL CARRIES A RANK PREFIX. Observed in the pilot:

        '1 - i-0aaa...aaa /data'

    An earlier version split on whitespace and read parts[0]/parts[1], yielding "1" and
    "-". Index from the END instead: GROUP BY InstanceId, path puts exactly those two
    values last, in order.
    """
    parts = (label or "").split()
    if len(parts) < 2:
        return None, None
    instance_id, path = parts[-2], parts[-1]
    if not instance_id.startswith("i-"):
        return None, None
    return instance_id, path


def find_breaching_mounts(account_id: str, environment: str, threshold: float):
    """Re-run the alarm's query WITH `path`, recovering the detail it grouped away.

    The alarm groups by InstanceId alone to stay inside the 500-series return cap, so it
    knows only which INSTANCE breached. Adding `path` here is what makes the alert name a
    filesystem — and it is safe because this runs once per alarm, not continuously.
    """
    query = (
        f"SELECT MAX({METRIC_NAME}) "
        f'FROM SCHEMA("{NAMESPACE}", {SCHEMA_DIMENSIONS}) '
        f"WHERE AWS.AccountId = '{account_id}' AND Environment = '{environment}' "
        f"GROUP BY InstanceId, path "
        f"ORDER BY MAX() DESC"
    )

    now = datetime.now(timezone.utc)
    resp = cloudwatch.get_metric_data(
        MetricDataQueries=[
            {"Id": "breaching", "Expression": query, "Period": 300, "ReturnData": True}
        ],
        # Metrics Insights alarms evaluate only the last 3 hours; a short window keeps the
        # response small and matches what the alarm actually saw.
        StartTime=now - timedelta(minutes=15),
        EndTime=now,
        ScanBy="TimestampDescending",
        MaxDatapoints=500,
    )

    breaching = []
    for series in resp.get("MetricDataResults", []):
        if not series.get("Values"):
            continue
        latest = series["Values"][0]
        if latest < threshold:
            continue
        instance_id, path = _parse_label(series.get("Label"))
        if not instance_id:
            log.warning("Unparseable series label: %r", series.get("Label"))
            continue
        breaching.append(
            {"instance_id": instance_id, "path": path, "used_percent": round(latest, 1)}
        )
    return breaching


def resolve_volume_on_host(account_id: str, instance_id: str, path: str):
    """Resolve a mount path to its EBS volume id BY ASKING THE HOST.

    Returns {'volume_id', 'device', 'method'} or None.

    Three methods were tested live; the two that work are used here in order.
    `/sys/block/<disk>/serial` is deliberately NOT used — it returned empty on AL2023.
    """
    script = (
        'set -e; '
        f'SRC=$(findmnt -no SOURCE --target {json.dumps(path)}); '
        'DISK=$(lsblk -no pkname "$SRC" 2>/dev/null | head -1); '
        '[ -n "$DISK" ] || DISK=$(basename "$SRC"); '
        # Method E: AWS-provided, reports the volume id explicitly.
        'VOL=$(/sbin/ebsnvme-id "/dev/$DISK" 2>/dev/null | sed -n "s/^Volume ID: *//p"); '
        # Method D fallback: NVMe CONTROLLER serial (note: /sys/class/nvme/<ctrl>/serial,
        # not /sys/block/<disk>/serial, which is empty on AL2023).
        '[ -n "$VOL" ] || VOL=$(cat /sys/class/nvme/$(echo $DISK | sed "s/n[0-9]*$//")/serial '
        '2>/dev/null | tr -d " " | sed "s/^vol/vol-/"); '
        'echo "DEVICE=$DISK"; echo "VOLUME=$VOL"'
    )
    try:
        ssm = _assume(account_id, "ssm")
        cmd = ssm.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": [script]},
            TimeoutSeconds=120,
            Comment="disk-monitoring: resolve mount to EBS volume",
        )["Command"]["CommandId"]

        for _ in range(20):
            time.sleep(3)
            inv = ssm.get_command_invocation(CommandId=cmd, InstanceId=instance_id)
            if inv["Status"] not in ("Pending", "InProgress", "Delayed"):
                break
        else:
            log.warning("Volume resolution timed out on %s", instance_id)
            return None

        if inv["Status"] != "Success":
            log.warning("Volume resolution failed on %s: %s", instance_id, inv["Status"])
            return None

        out = dict(
            line.split("=", 1)
            for line in inv["StandardOutputContent"].strip().splitlines()
            if "=" in line
        )
        vol = out.get("VOLUME", "").strip()
        if not vol.startswith("vol-"):
            log.warning("No volume id resolved for %s on %s", path, instance_id)
            return None
        return {
            "volume_id": vol,
            "device": out.get("DEVICE", "").strip(),
            "method": "ebsnvme-id/nvme-serial",
        }
    except ClientError as exc:
        # An unreachable host is the likely cause, and it is precisely when a disk is
        # full that a host may stop responding. Degrade to an alert without the volume id
        # rather than losing the alert entirely.
        log.error("SSM volume resolution error for %s: %s", instance_id, exc)
        return None


def describe_volume(account_id: str, volume_id: str):
    """Fetch size, type and the DiskAutoGrow opt-in tag for a resolved volume."""
    try:
        ec2 = _assume(account_id, "ec2")
        vols = ec2.describe_volumes(VolumeIds=[volume_id]).get("Volumes", [])
        if not vols:
            return {}
        v = vols[0]
        tags = {t["Key"]: t["Value"] for t in v.get("Tags", [])}
        return {
            "size_gib": v["Size"],
            "volume_type": v.get("VolumeType", "unknown"),
            "auto_grow_enabled": tags.get("DiskAutoGrow") == "true",
        }
    except ClientError as exc:
        log.error("describe_volumes failed for %s: %s", volume_id, exc)
        return {}


def start_remediation(account_id: str, finding: dict):
    """Grow the resolved volume. Only called on the CRITICAL path.

    This is the fix for findings.md 5: the alarm's own event cannot supply an InstanceId,
    so wiring the alarm directly to the SSM document could never work. This function has
    the resolved instance AND path, so it can.

    DryRun MUST be passed as 'false' explicitly — the document defaults to 'true' and is
    otherwise permanently inert while reporting success.
    """
    if not ENABLE_REMEDIATION:
        return {"status": "disabled", "detail": "ENABLE_REMEDIATION is not true"}
    if not REMEDIATION_ROLE_ARN:
        return {"status": "skipped", "detail": "REMEDIATION_ROLE_ARN not configured"}
    if not finding.get("volume_id"):
        return {"status": "skipped", "detail": "volume unresolved; cannot target growth"}
    if not finding.get("auto_grow_enabled"):
        # The volume-level opt-in is the real gate; the runbook checks it too.
        return {"status": "skipped", "detail": "volume is not tagged DiskAutoGrow=true"}

    try:
        ssm = _assume(account_id, "ssm")
        execution = ssm.start_automation_execution(
            DocumentName=REMEDIATION_DOCUMENT,
            Parameters={
                "InstanceId": [finding["instance_id"]],
                "MountPath": [finding["path"]],
                "AutomationAssumeRole": [REMEDIATION_ROLE_ARN],
                "SnsTopicArn": [SNS_TOPIC_ARN],
                "DryRun": ["false"],
            },
        )
        return {"status": "started", "detail": execution["AutomationExecutionId"]}
    except ClientError as exc:
        log.error("StartAutomationExecution failed: %s", exc)
        return {"status": "error", "detail": str(exc)[:200]}


def build_message(alarm_name, state_reason, findings, level):
    lines = [
        f"DISK {level.upper()}: {alarm_name}",
        "",
        f"Alarm said: {state_reason}",
        "  (a fleet alarm names no instance — the detail below was resolved at alert time)",
        "",
        f"{len(findings)} breaching filesystem(s):",
        "",
    ]
    for f in findings:
        lines.append(
            f"  {f['instance_id']}  {f['path']}  at {f['used_percent']}%"
        )
        if f.get("volume_id"):
            size = f.get("size_gib")
            vt = f.get("volume_type", "")
            grow = "auto-grow ENABLED" if f.get("auto_grow_enabled") else "auto-grow off"
            lines.append(
                f"      volume: {f['volume_id']}"
                + (f"  {size} GiB {vt}" if size else "")
                + f"  device {f.get('device','?')}  ({grow})"
            )
        else:
            lines.append(
                "      volume: UNRESOLVED — host unreachable, or LVM/RAID. "
                "Run `/sbin/ebsnvme-id $(findmnt -no SOURCE --target <path>)` on the host."
            )
        if f.get("remediation"):
            r = f["remediation"]
            lines.append(f"      remediation: {r['status']} — {r['detail']}")
        lines.append("")

    if DASHBOARD_URL:
        lines += ["Dashboard (per-filesystem detail and trend):", f"  {DASHBOARD_URL}", ""]

    if level == "critical":
        lines += [
            "NOTE: ModifyVolume grows the VOLUME only. The filesystem must still be",
            "extended (growpart + resize2fs/xfs_growfs) before the OS can use the space.",
        ]
    return "\n".join(lines)


def lambda_handler(event, _context):
    log.info("Event: %s", json.dumps(event))

    if not SNS_TOPIC_ARN:
        log.error("SNS_TOPIC_ARN is not set; cannot notify")
        return {"statusCode": 500, "body": "SNS_TOPIC_ARN not configured"}

    detail = event.get("detail", {})
    alarm_name = detail.get("alarmName") or event.get("AlarmName", "")
    state_reason = (detail.get("state") or {}).get("reason", "")

    try:
        account_id, environment = _scope_from_alarm_name(alarm_name)
    except ValueError as exc:
        # The metric-count guard alarm has a different name shape and must not be treated
        # as a disk breach.
        log.error("%s", exc)
        return {"statusCode": 400, "body": str(exc)}

    is_critical = "critical" in alarm_name
    level = "critical" if is_critical else "warning"
    threshold = CRITICAL_THRESHOLD if is_critical else WARNING_THRESHOLD

    findings = []
    for item in find_breaching_mounts(account_id, environment, threshold):
        resolved = resolve_volume_on_host(account_id, item["instance_id"], item["path"])
        if resolved:
            item.update(resolved)
            item.update(describe_volume(account_id, resolved["volume_id"]))
        if is_critical:
            item["remediation"] = start_remediation(account_id, item)
        findings.append(item)

    if not findings:
        # The alarm fired but nothing is breaching now — usually a transient that
        # resolved, or the metric aged out of the 15-minute window.
        log.warning("No breaching series resolved for %s", alarm_name)
        return {"statusCode": 200, "body": "no breaching series resolved"}

    subject = (
        f"[{environment}] Disk {level} — {len(findings)} filesystem(s) in {account_id}"
    )
    sns.publish(
        TopicArn=SNS_TOPIC_ARN,
        Subject=subject[:100],  # SNS subject limit
        Message=build_message(alarm_name, state_reason, findings, level),
    )
    return {
        "statusCode": 200,
        "body": f"notified for {len(findings)} finding(s); level={level}",
    }
