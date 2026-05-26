#!/usr/bin/env bash
set -euo pipefail

terraform -chdir="$(dirname "$0")" destroy -auto-approve
