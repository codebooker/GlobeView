#!/bin/bash
set -euo pipefail
umask 027

readonly REPO_URL="https://github.com/codebooker/GlobeView.git"
readonly ROOT="/opt/globeview"
readonly RELEASES="$ROOT/releases"
readonly CURRENT="$ROOT/current"

die() { echo "deploy: $*" >&2; exit 1; }

[[ "$#" -eq 1 ]] || die "expected: globeview-release <40-character commit SHA>"
sha="$1"
[[ "$sha" =~ ^[0-9a-f]{40}$ ]] || die "expected: deploy <40-character commit SHA>"

mkdir -p "$RELEASES"
repo="$ROOT/repository"
if [[ ! -f "$repo/HEAD" || ! -d "$repo/objects" ]]; then
    git init --bare "$repo" >/dev/null
fi
if git --git-dir="$repo" remote get-url origin >/dev/null 2>&1; then
    [[ "$(git --git-dir="$repo" remote get-url origin)" == "$REPO_URL" ]] || die "repository origin does not match GlobeView"
else
    git --git-dir="$repo" remote add origin "$REPO_URL"
fi

git --git-dir="$repo" fetch --quiet --depth=1 origin refs/heads/main
main_sha="$(git --git-dir="$repo" rev-parse FETCH_HEAD)"
[[ "$sha" == "$main_sha" ]] || die "requested SHA is not the current main commit"

release="$RELEASES/$sha"
if [[ ! -d "$release/.venv" ]]; then
    rm -rf -- "$release"
    build="$(mktemp -d "$RELEASES/.build-$sha.XXXXXX")"
    trap 'rm -rf -- "$build"' EXIT
    git --git-dir="$repo" archive "$sha" | tar -x -C "$build"
    python3 -m venv "$build/.venv"
    "$build/.venv/bin/pip" install --disable-pip-version-check --no-cache-dir --quiet --requirement "$build/requirements.txt"
    chmod 0750 "$build"
    mv "$build" "$release"
    trap - EXIT
fi

previous=""
if [[ -d "$CURRENT" ]]; then previous="$(readlink -f "$CURRENT")"; fi
if [[ "$previous" == "$release" ]]; then
    echo "already deployed $sha"
    exit 0
fi
next="$ROOT/.current-$sha"
ln -sfn "$release" "$next"
mv -Tf "$next" "$CURRENT"

if ! sudo -n /usr/bin/systemctl restart globeview.service; then
    if [[ -z "$previous" ]]; then rm -f "$CURRENT"; else ln -sfn "$previous" "$next"; mv -Tf "$next" "$CURRENT"; fi
    sudo -n /usr/bin/systemctl restart globeview.service || true
    die "service restart failed; previous release restored"
fi

healthy=false
for _ in {1..20}; do
    if curl --fail --silent --max-time 3 http://127.0.0.1:8765/healthz >/dev/null; then
        healthy=true
        break
    fi
    sleep 1
done
if [[ "$healthy" != true ]]; then
    if [[ -z "$previous" ]]; then rm -f "$CURRENT"; else ln -sfn "$previous" "$next"; mv -Tf "$next" "$CURRENT"; fi
    sudo -n /usr/bin/systemctl restart globeview.service || true
    die "health check failed; previous release restored"
fi

echo "deployed $sha"

# Retain the current and previous releases; remove older checked-out copies.
find "$RELEASES" -mindepth 1 -maxdepth 1 -type d ! -name "$sha" -printf '%T@ %p\n' \
    | sort -rn | tail -n +2 | cut -d' ' -f2- | xargs -r rm -rf --
