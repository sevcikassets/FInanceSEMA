#!/usr/bin/env bash
set -euo pipefail

git pull --ff-only
docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml ps
