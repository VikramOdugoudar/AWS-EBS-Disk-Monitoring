#!/usr/bin/env bash
#
# Render one Ansible inventory file per AWS account, from the live account list.
#
# WHY ONE FILE PER ACCOUNT
#   The aws_ec2 plugin holds one credential context per config file — see the
#   rationale above `assume_role_arn` in ansible/inventory/aws_ec2.yml.template.
#   Ansible merges every source in the directory, so N files is how N identities get
#   expressed. No per-account editing: the list comes from ListAccounts at runtime.
#
# WHAT IT DOES
#   1. aws organizations list-accounts   (ACTIVE only, management account excluded)
#   2. substitute __ACCOUNT_ID__ in aws_ec2.yml.template -> <id>.aws_ec2.yml
#   3. remove stale files for accounts that have left the org
#
# USAGE
#   ./scripts/render_inventory.sh                       # all ACTIVE accounts in the org
#   ./scripts/render_inventory.sh --ou ou-abcd-1111      # only accounts in one OU
#   ./scripts/render_inventory.sh --accounts 111111111111,222222222222
#   ./scripts/render_inventory.sh --dry-run
#
# Then:
#   ansible-playbook -i ansible/inventory ansible/site.yml
#
# IAM: requires organizations:ListAccounts (and ListAccountsForParent for --ou) in the
# monitoring account. The controller instance profile grants both.
#
# NETWORK: needs the `organizations` interface endpoint (12-monitoring-endpoints.yaml;
# control-plane Region only, hence us-east-1). Without it every call below fails,
# `set -euo pipefail` aborts, NO INVENTORY IS PRODUCED — and the next playbook run
# silently matches no hosts. See docs/01.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INVENTORY_DIR="${REPO_ROOT}/ansible/inventory"
TEMPLATE="${INVENTORY_DIR}/aws_ec2.yml.template"

OU_ID=""
ACCOUNT_LIST=""
DRY_RUN="false"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ou)       OU_ID="$2"; shift 2 ;;
    --accounts) ACCOUNT_LIST="$2"; shift 2 ;;
    --dry-run)  DRY_RUN="true"; shift ;;
    # Prints the header comment verbatim; keep this range in sync with it.
    -h|--help)  sed -n '2,31p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)          echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

[[ -f "${TEMPLATE}" ]] || { echo "ERROR: template not found: ${TEMPLATE}" >&2; exit 1; }
command -v aws >/dev/null || { echo "ERROR: aws CLI not found" >&2; exit 1; }

# --- 1. Discover accounts ---------------------------------------------------
if [[ -n "${ACCOUNT_LIST}" ]]; then
  echo "Using explicitly supplied accounts."
  ACCOUNTS="$(echo "${ACCOUNT_LIST}" | tr ',' '\n' | tr -d ' ' | grep -E '^[0-9]{12}$' || true)"
elif [[ -n "${OU_ID}" ]]; then
  echo "Discovering ACTIVE accounts in OU ${OU_ID}..."
  ACCOUNTS="$(aws organizations list-accounts-for-parent \
                --parent-id "${OU_ID}" \
                --query 'Accounts[?Status==`ACTIVE`].Id' \
                --output text | tr '\t' '\n')"
else
  echo "Discovering ACTIVE accounts in the organization..."
  # Exclude the management account: it holds no monitored workloads and has no
  # DiskMonitoringInventoryRole, so an inventory file for it would fail on every run.
  MGMT_ACCOUNT="$(aws organizations describe-organization \
                    --query 'Organization.MasterAccountId' --output text)"
  ACCOUNTS="$(aws organizations list-accounts \
                --query 'Accounts[?Status==`ACTIVE`].Id' \
                --output text | tr '\t' '\n' | grep -v "^${MGMT_ACCOUNT}$" || true)"
  echo "  (management account ${MGMT_ACCOUNT} excluded)"
fi

if [[ -z "${ACCOUNTS//[[:space:]]/}" ]]; then
  echo "ERROR: no accounts discovered. Check IAM permissions and --ou if supplied." >&2
  exit 1
fi

ACCOUNT_COUNT="$(echo "${ACCOUNTS}" | grep -c . || true)"
echo "Found ${ACCOUNT_COUNT} account(s)."

# --- 2. Render one file per account -----------------------------------------
RENDERED=""
for ACCOUNT_ID in ${ACCOUNTS}; do
  TARGET="${INVENTORY_DIR}/${ACCOUNT_ID}.aws_ec2.yml"
  RENDERED="${RENDERED} ${ACCOUNT_ID}.aws_ec2.yml"

  if [[ "${DRY_RUN}" == "true" ]]; then
    echo "  [dry-run] would render ${TARGET##*/}"
    continue
  fi

  sed "s/__ACCOUNT_ID__/${ACCOUNT_ID}/g" "${TEMPLATE}" > "${TARGET}"
  echo "  rendered ${TARGET##*/}"
done

# --- 3. Remove inventory for accounts no longer in scope --------------------
# Without this, an account removed from the org leaves a stale file whose
# assume_role no longer resolves, and every subsequent run reports a failure for it.
#
# ONLY PRUNE ON A FULL ORG RENDER. RENDERED holds just this invocation's accounts while
# the loop scans EVERY *.aws_ec2.yml on disk, so a scoped run would delete the rest —
# logging "account no longer in scope" as it went. Load-bearing because the event-driven
# path calls `--accounts <account>` per instance event (00-monitoring-account.yaml): one
# launch would otherwise wipe the whole estate.
if [[ "${DRY_RUN}" != "true" && -z "${ACCOUNT_LIST}" && -z "${OU_ID}" ]]; then
  for EXISTING in "${INVENTORY_DIR}"/*.aws_ec2.yml; do
    [[ -e "${EXISTING}" ]] || continue
    BASENAME="$(basename "${EXISTING}")"
    if [[ " ${RENDERED} " != *" ${BASENAME} "* ]]; then
      rm -f "${EXISTING}"
      echo "  removed stale ${BASENAME} (account no longer in scope)"
    fi
  done
fi

# --- 4. Report --------------------------------------------------------------
cat <<EOF

Inventory rendered into ${INVENTORY_DIR}

Verify hosts resolve (requires the DiskMonitoringInventoryRole in each account):
  ansible-inventory -i ansible/inventory --graph

Then run:
  export DISK_MONITORING_TRANSFER_BUCKET=<from stack output>
  ansible-playbook -i ansible/inventory ansible/site.yml --check --diff
EOF
