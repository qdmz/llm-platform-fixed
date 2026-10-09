#!/usr/bin/env bash
# 自动递增版本号 → 更新 VERSION / CHANGELOG / BUILD_TAG → 提交 → 打 tag → 推送
# 用法：bash scripts/release.sh [patch|minor|major]   （默认 patch）
set -euo pipefail
cd "$(dirname "$0")/.."
part="${1:-patch}"
cur="$(cat VERSION 2>/dev/null || echo 0.0.0)"
latest_tag="$(git tag -l 'v*' --sort=-v:refname | head -n1 | sed 's/^v//')"
# 以 VERSION 和最新 tag 中较大的为基准
base="$(printf '%s\n%s\n' "$cur" "${latest_tag:-0.0.0}" | sort -V | tail -n1)"
IFS=. read -r MA MI PA <<<"$base"
if git rev-parse -q --verify "refs/tags/v$base" >/dev/null || [ "$base" != "$cur" ]; then
  case "$part" in
    major) MA=$((MA+1)); MI=0; PA=0 ;;
    minor) MI=$((MI+1)); PA=0 ;;
    *)     PA=$((PA+1)) ;;
  esac
fi
new="$MA.$MI.$PA"
echo "$new" > VERSION
tag_ts="$(date +%Y-%m-%d.%H%M)"
sed -i "s/or '[0-9]\{4\}-[0-9]\{2\}-[0-9]\{2\}\.[0-9]\{4\}'/or '$tag_ts'/" gateway.py
if ! grep -q "^## v$new" CHANGELOG.md 2>/dev/null; then
  { echo "# 更新日志"; echo; echo "## v$new - $(date +%F)"; echo; echo "- 见提交记录"; echo; tail -n +2 CHANGELOG.md 2>/dev/null; } > CHANGELOG.md.tmp && mv CHANGELOG.md.tmp CHANGELOG.md
fi
git add -A
git commit -m "release: v$new" || true
git tag -a "v$new" -m "Release v$new"
git push origin HEAD
git push origin "v$new"
echo "已发布 v$new"
