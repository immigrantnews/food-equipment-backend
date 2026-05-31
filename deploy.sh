#!/usr/bin/env bash
set -euo pipefail

: "${ANTHROPIC_API_KEY:?ANTHROPIC_API_KEY is not set — export it before running deploy.sh}"

sed "s|__ANTHROPIC_API_KEY__|$ANTHROPIC_API_KEY|" index.html > dist.html

scp dist.html clubtrade@92.53.96.2:/home/c/clubtrade/indmart.ru/index.html

echo "Задеплоено на indmart.ru ✅"
