#!/usr/bin/env bash
set -euo pipefail

terraform -chdir="$(dirname "$0")" init
terraform -chdir="$(dirname "$0")" apply -auto-approve
