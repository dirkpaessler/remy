#!/usr/bin/env bash
# Remy setup for Google Cloud Shell.
#
# Cloud Shell is a terminal that runs in the browser at
# https://shell.cloud.google.com — it already has gcloud installed and is
# already logged in as you. So this needs no software on your own computer
# and no login step.
#
# Paste this whole file into Cloud Shell, or — once this repository is public —
# run the one-liner:
#   bash <(curl -sL https://raw.githubusercontent.com/dirkpaessler/remy/main/skills/remy/scripts/cloudshell-setup.sh)
#
# While the repository is private that URL needs credentials, so paste the
# file contents into Cloud Shell instead.

set -euo pipefail
# Never die silently: name the line, and reassure that rerunning is safe.
trap 'echo "❌ Setup stopped unexpectedly (line $LINENO). Rerunning this script is safe — it picks up where it left off."' ERR

echo "🐀 Setting up Remy's Google account…"
echo

ACCOUNT="$(gcloud config get-value account 2>/dev/null)"
if [ -z "$ACCOUNT" ] || [ "$ACCOUNT" = "(unset)" ]; then
  echo "❌ Not logged in. This script is meant to run inside Google Cloud Shell,"
  echo "   which logs you in automatically. Open https://shell.cloud.google.com"
  exit 1
fi
echo "   Logged in as: $ACCOUNT"

# A project id must be globally unique; derive a stable one from the account.
SUFFIX="$(printf '%s' "$ACCOUNT" | shasum | cut -c1-6)"
LOCAL="$(printf '%s' "${ACCOUNT%%@*}" | tr -cd '[:alnum:]' | tr '[:upper:]' '[:lower:]' | cut -c1-12)"
PROJECT="${REMY_PROJECT_ID:-remy-${LOCAL:-docs}-${SUFFIX}}"
KEY="$HOME/remy-key.json"

# A deleted project blocks its id for ~30 days: describe still finds it,
# but every API call fails with RESOURCES_NOT_FOUND. Only ACTIVE counts —
# otherwise walk to a free (or active) sibling id.
BASE="$PROJECT"; N=1
while :; do
  STATE="$(gcloud projects describe "$PROJECT" --format='value(lifecycleState)' 2>/dev/null || true)"
  if [ -z "$STATE" ]; then
    echo "   Creating project $PROJECT…"
    gcloud projects create "$PROJECT" --name="Remy Docs Skill" >/dev/null
    break
  elif [ "$STATE" = "ACTIVE" ]; then
    if [ "$PROJECT" = "$BASE" ]; then
      echo "   Project $PROJECT already exists — reusing it."
    else
      echo "   Using project $PROJECT ($BASE is pending deletion)."
    fi
    break
  fi
  N=$((N+1))
  if [ "$N" -gt 9 ]; then
    echo "❌ No usable project id near $BASE. Pick one yourself:"
    echo "   REMY_PROJECT_ID=remy-<something-unique> bash <(curl -sL …)"
    exit 1
  fi
  PROJECT="${BASE}-${N}"
done
SA="remy-bot@${PROJECT}.iam.gserviceaccount.com"

echo "   Enabling the Google Docs and Drive APIs (this takes ~30 seconds)…"
gcloud services enable docs.googleapis.com drive.googleapis.com \
  --project="$PROJECT" >/dev/null

if ! gcloud iam service-accounts describe "$SA" --project="$PROJECT" >/dev/null 2>&1; then
  echo "   Creating the robot account…"
  gcloud iam service-accounts create remy-bot --display-name="Remy" \
    --project="$PROJECT" >/dev/null
fi

echo "   Creating the key file…"
rm -f "$KEY"
# A brand-new service account is eventually consistent: creating a key
# right away can 404. Retry briefly, and if it still fails, show the real
# error instead of dying silently.
CREATED=""
for _ in 1 2 3 4 5 6; do
  if ERR="$(gcloud iam service-accounts keys create "$KEY" \
      --iam-account="$SA" --project="$PROJECT" 2>&1)"; then
    CREATED=1
    break
  fi
  sleep 5
done
if [ -z "$CREATED" ]; then
  echo "❌ Could not create the key file. gcloud said:"
  echo "$ERR"
  echo
  echo "   If this mentions iam.disableServiceAccountKeyCreation, your"
  echo "   Google organisation forbids key files by policy — ask its admin."
  echo "   If it mentions a key limit, earlier setups left keys behind:"
  echo "   delete old keys for $SA"
  echo "   in the Cloud console (IAM & Admin → Service Accounts → Keys)."
  echo "   Otherwise, simply rerun this script; it picks up where it left off."
  exit 1
fi
chmod 600 "$KEY"

echo
echo "✅ Done. Downloading the key file to your computer now…"
echo
cloudshell download "$KEY" || {
  echo "   (Automatic download did not start. In the Cloud Shell toolbar click"
  echo "    the three dots ⋮ → Download, and enter this path:)"
  echo "    $KEY"
}
echo
echo "───────────────────────────────────────────────────────────────"
echo "Last step: go back to your Claude window and say"
echo
echo "    Remy, I downloaded the key file"
echo
echo "Claude will find it in your Downloads folder and put it in place."
echo "You can close this browser tab afterwards."
echo "───────────────────────────────────────────────────────────────"
