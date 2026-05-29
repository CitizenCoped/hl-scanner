#!/usr/bin/env bash
set -euo pipefail

psql "$POSTGRES_URL" -f sql/001_init.sql
