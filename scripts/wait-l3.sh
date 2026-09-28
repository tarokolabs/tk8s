#!/usr/bin/env bash
# Wait for the L3 workflow on a commit. Green as soon as any run for the commit succeeded; red once
# every run is complete without a success. A tag push and a main push of the same commit each get
# their own run, and the concurrency group may cancel one of them.
#   wait-l3.sh <sha>      # WAIT_L3_ROUNDS (default 90) polls 30 s apart
set -eu
sha=$1; rounds=${WAIT_L3_ROUNDS:-90}
for _ in $(seq 1 "$rounds"); do
  runs=$(gh run list -R "${GITHUB_REPOSITORY:-tarokolabs/tk8s}" --workflow l3.yaml --commit "$sha" --json status,conclusion)
  if echo "$runs" | grep -q '"conclusion":"success"'; then echo "L3 green for $sha"; exit 0; fi
  if [ "$runs" != "[]" ] && ! echo "$runs" | grep -qE '"status":"(queued|in_progress|waiting|requested|pending)"'; then
    echo "L3 red for $sha: $(echo "$runs" | grep -oE '"conclusion":"[a-z_]+"' | cut -d'"' -f4 | paste -sd, -)" >&2; exit 1
  fi
  sleep 30
done
echo "L3 did not finish for $sha within $((rounds * 30)) s" >&2; exit 1
