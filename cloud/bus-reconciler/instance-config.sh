#!/usr/bin/env bash
# The declared configuration of redis-central, as infrastructure state in the form this fleet uses.
#
# WHY THIS FILE EXISTS
# Gemini, as architect lead, on 2026-10-06 14:34:52Z: "Eviction: Explicitly declare volatile LRU in
# your infrastructure state. Relying on cloud defaults introduces drift risk."
#
# It was unset. The instance therefore ran Memorystore's implicit default, which happens to be the
# behaviour we want and was chosen by nobody - and an implicit policy is a decision no one made, so it
# can change under us with a provider release and nothing would notice.
#
# WHY NOT TERRAFORM, since that is what "infrastructure state" usually means
# There is no Terraform state anywhere in this fleet: every deploy here is a gcloud script followed by
# a read-back. Introducing a state file for one instance means a second source of truth that nothing
# else uses and that can drift from what gcloud did - which is the very failure the ruling is about,
# relocated. So the declaration lives here, in the same shape as chair/deploy.sh and
# cloud/bus-reconciler/deploy.sh, and `verify` is what makes it state rather than a comment.
#
#   bash cloud/bus-reconciler/instance-config.sh verify   # what IS set, against what is declared
#   bash cloud/bus-reconciler/instance-config.sh apply    # set the declared config (OWNER-GATED)
#
# `apply` changes a running instance the owner pays for. It is his to run, or mine on his word, and it
# is deliberately a separate verb from `verify` so that reading the state can never change it.

set -euo pipefail

PROJECT="${PROJECT:-sfdc24}"
REGION="${REGION:-us-central1}"
INSTANCE="${INSTANCE:-redis-central}"
GCLOUD="${GCLOUD:-gcloud}"

# THE DECLARATION. One line per setting, each with the reason it is that value.
#
# maxmemory-policy=volatile-lru
#   Only keys WITH a TTL may ever be evicted. Our probe and synthetic keys carry TTLs and are
#   therefore the evictable ones; progress and mirror keys do not and cannot vanish under memory
#   pressure. allkeys-lru would be actively dangerous here: it can evict a progress key, and a
#   progress key that disappears makes a CLOSED item look OPEN, which is the exact failure the whole
#   live-progress design exists to end.
DECLARED_CONFIG="maxmemory-policy=volatile-lru"

# Facts about the instance that are NOT configuration but shape every design decision, recorded here
# so the next reader does not have to go and measure them again:
#   persistence   DISABLED - no RDB, no AOF. A failover or restart loses EVERYTHING in this instance.
#                 Gemini's ruling of 14:34:52Z follows from it: "Because Redis is volatile, progress
#                 state must be reconstructible from the repository. Redis acts only as an ephemeral
#                 accelerator." Nothing here may be the only copy of anything.
#   tier          STANDARD_HA, replicaCount 1, read replicas DISABLED. The replica is for failover,
#                 not for reads. Kept at Standard because it resizes without interruption; Basic does
#                 not, and the owner named that himself.
#   capacity      1 GiB. The ENTIRE board history measures 4.74 MB of cells, about 6.34 MB as Redis
#                 hashes - 0.62 percent. Capacity is not a constraint and will not become one.

verify() {
  echo "instance ${INSTANCE} in ${REGION}, project ${PROJECT}"
  local actual
  actual="$("$GCLOUD" redis instances describe "$INSTANCE" --region "$REGION" --project "$PROJECT" \
    --format='value(redisConfigs)')"
  echo "declared : ${DECLARED_CONFIG}"
  if [ -z "$actual" ]; then
    echo "actual   : (none set - the instance runs the provider default, which nobody chose)"
  else
    echo "actual   : ${actual}"
  fi
  # Reported, never corrected. A verify that silently fixes what it finds is a deploy wearing a
  # read-only name, and this fleet has been bitten by a check that changed the thing it checked.
  case "$actual" in
    *"maxmemory-policy"*"volatile-lru"*) echo "VERDICT: declared policy is set" ;;
    "") echo "VERDICT: DRIFT - nothing is declared on the instance. Run apply (owner-gated)." ;;
    *) echo "VERDICT: DRIFT - the instance carries a policy that is not the declared one." ;;
  esac
  echo "persistence / tier / replicas, for the record:"
  "$GCLOUD" redis instances describe "$INSTANCE" --region "$REGION" --project "$PROJECT" \
    --format='value(persistenceConfig.persistenceMode,tier,replicaCount,readReplicasMode,memorySizeGb)'
}

apply() {
  echo "setting ${DECLARED_CONFIG} on ${INSTANCE}. This changes a running instance."
  "$GCLOUD" redis instances update "$INSTANCE" --region "$REGION" --project "$PROJECT" \
    --update-redis-config "$DECLARED_CONFIG"
  echo "--- read back, because an apply that is not read back is an intention ---"
  verify
}

case "${1:-}" in
  verify) verify ;;
  apply) apply ;;
  *) sed -n '1,22p' "$0"; exit 2 ;;
esac
