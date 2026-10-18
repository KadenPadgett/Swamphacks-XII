#!/usr/bin/env sh
set -eu
cd -- "$(dirname -- "$0")"
if ! command -v node >/dev/null 2>&1; then
  echo "Install Node.js 22 or newer, then run this file again."
  exit 1
fi
if [ ! -d node_modules ]; then
  npm ci
fi
echo "Open http://localhost:5173 in your browser."
echo "Keep this terminal open while using PlanIT Control."
npm run dev
