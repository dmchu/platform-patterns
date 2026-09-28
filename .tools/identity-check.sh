#!/usr/bin/env bash
# Fails if any commit or tag in the repository at $1 carries an identity other than the public
# noreply address. File content is checked by redact-check.py; this checks the metadata, which
# no content scanner reads. GitHub's own web-flow identity is allowed, so a merge made in the
# browser does not fail the check. Lightweight tags carry no identity and are skipped.
set -euo pipefail
repo="${1:-.}"
allowed="20864385+dmchu@users.noreply.github.com"
web="noreply@github.com"
bad="$(git -C "$repo" log --all --format='%h %ae %ce' | awk -v a="$allowed" -v w="$web" '($2!=a && $2!=w) || ($3!=a && $3!=w)')"
badtags="$(git -C "$repo" for-each-ref refs/tags --format='%(refname:short) %(taggeremail)' | awk -v a="<$allowed>" 'NF==2 && $2!=a')"
if [ -n "$bad$badtags" ]; then
  echo "::error::identities other than $allowed:" >&2
  [ -z "$bad" ] || echo "$bad" >&2
  [ -z "$badtags" ] || echo "$badtags" >&2
  exit 1
fi
echo "$(git -C "$repo" rev-list --all --count) commits and $(git -C "$repo" tag | wc -l | tr -d ' ') tags: every identity is $allowed"
