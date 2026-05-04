#!/bin/bash
# One-time Fly.io setup script.
# Run this once from the project root before the first deploy.
# Requires: fly CLI authenticated (fly auth login)

set -euo pipefail

APP="ops-intelligence-agent"
REGION="sin"

echo "==> Creating Fly app: $APP"
fly apps create "$APP" --machines

echo "==> Creating managed Postgres (this takes ~2 min)"
fly postgres create \
  --name "${APP}-db" \
  --region "$REGION" \
  --vm-size shared-cpu-1x \
  --volume-size 3 \
  --initial-cluster-size 1

echo "==> Attaching Postgres to app (sets DATABASE_URL secret automatically)"
fly postgres attach "${APP}-db" --app "$APP"

echo "==> Creating managed Redis"
fly redis create \
  --name "${APP}-redis" \
  --region "$REGION" \
  --plan free-6m

echo ""
echo "==> Copy the Redis URL above, then set all secrets:"
echo ""
echo "    fly secrets set \\"
echo "      ANTHROPIC_API_KEY=\$ANTHROPIC_API_KEY \\"
echo "      OPENAI_API_KEY=\$OPENAI_API_KEY \\"
echo "      REDIS_URL=<paste-redis-url-here> \\"
echo "      LANGFUSE_PUBLIC_KEY=\$LANGFUSE_PUBLIC_KEY \\"
echo "      LANGFUSE_SECRET_KEY=\$LANGFUSE_SECRET_KEY \\"
echo "      SLACK_WEBHOOK_URL=\$SLACK_WEBHOOK_URL"
echo ""
echo "==> Then deploy:"
echo "    make deploy"
