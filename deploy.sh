#!/usr/bin/env bash
#
# deploy.sh [dir]
#
#   ./deploy.sh [dir]                   overlay, validate, commit, deploy
#   ./deploy.sh                         deploy what is already committed
#   ./deploy.sh -n [dir]                dry run, change nothing
#   ./deploy.sh -c                      validate only; touch nothing, deploy nothing
#   ./deploy.sh -s                      what is ACTUALLY running out there
#   ./deploy.sh -b VERSION [dir]        build+push eg4poll:VERSION, update .env, then deploy
#
# Overlays a folder of freshly-downloaded files onto the repo, validates,
# commits, and pushes everything to the right host.
#
# WHY OVERLAY RATHER THAN WIPE
# A download is usually a handful of changed files, not the whole project.
# Clearing the directory first and copying three files in would delete the
# other thirty. Git then shows exactly what the overlay changed, which is the
# review step -- so a stray file is visible before it is committed, not after.
#
# A file DELETED upstream will not disappear by overlaying. That is deliberate;
# remove it with `git rm` so the deletion is an explicit act.

set -euo pipefail

# Everything site-specific comes from .env, which is gitignored. No hostname
# or path belongs in a tracked file -- tools/check_secrets.py enforces that
# before every commit.
[[ -f .env ]] && set -a && . ./.env && set +a

REPO="${REPO:-$PWD}"
POLLER_HOST="${POLLER_HOST:?set POLLER_HOST in .env}"
POLLER_DIR="${POLLER_DIR:?set POLLER_DIR in .env}"   # expanded on the remote

DRY=0 CHECK_ONLY=0 STATUS_ONLY=0 BUILD_VERSION=""
while [[ "${1:-}" == -* ]]; do
    case "$1" in
        -n) DRY=1 ;;
        -c) CHECK_ONLY=1 ;;
        -s) STATUS_ONLY=1 ;;
        -b) shift; BUILD_VERSION="${1:?-b needs a version, e.g. -b 0.10.0}" ;;
        -h|--help) sed -n '2,21p' "$0" | sed 's/^#//'; exit 0 ;;
        *)  echo "unknown flag $1" >&2; exit 2 ;;
    esac
    shift
done
SRC="${1:-}"

[[ "$BUILD_VERSION" == -* ]] && { echo "-b needs a version, got flag-like '$BUILD_VERSION'" >&2; exit 2; }
[[ -n "$BUILD_VERSION" ]] && (( CHECK_ONLY || STATUS_ONLY )) \
    && { echo "-b can't be combined with -c or -s" >&2; exit 2; }

c()  { printf '\033[1;36m==\033[0m %s\n' "$*"; }
ok() { printf '   \033[32m%s\033[0m\n' "$*"; }
warn(){ printf '   \033[33m%s\033[0m\n' "$*"; }
die(){ printf '\033[1;31m!!\033[0m %s\n' "$*" >&2; exit 1; }
run(){ if (( DRY )); then printf '   [dry] %s\n' "$*"; else eval "$@"; fi; }

[[ -d "$REPO/.git" ]] || die "$REPO is not a git repo -- see DEPLOY.md for setup"
cd "$REPO"

# ------------------------------------------------------------- 0. build image
# Normally deploy.sh never touches the eg4poll image itself -- code reaches
# the Pi via the ./app bind mount + rsync (see docker-compose.yml), and the
# image only needs to change when requirements.txt does. -b is that manual
# step, automated: builds and pushes a new tag, then updates IMAGE in both
# copies of .env (this one, and the Pi's -- .env is gitignored and excluded
# from the rsync below, so it is never otherwise kept in sync between them).
if [[ -n "$BUILD_VERSION" ]]; then
    c "building eg4poll:$BUILD_VERSION"
    NEW_IMAGE="${IMAGE%:*}:$BUILD_VERSION"
    # buildx, not build: the Pi is arm64, this is very likely an x86 dev
    # machine -- same cross-arch command docker-compose.yml's own comment
    # already documented for doing this by hand.
    run "docker buildx build --platform linux/arm64 -t '$NEW_IMAGE' --push ."
    ok "pushed $NEW_IMAGE"

    run "sed -i 's|^IMAGE=.*|IMAGE=$NEW_IMAGE|' .env"
    run "ssh '$POLLER_HOST' \"sed -i 's|^IMAGE=.*|IMAGE=$NEW_IMAGE|' '$POLLER_DIR/.env'\""
    IMAGE="$NEW_IMAGE"
    ok ".env updated (local and remote)"
fi

# ---------------------------------------------------------------- 1. overlay
if [[ -n "$SRC" ]]; then
    [[ -d "$SRC" ]] || die "no such directory: $SRC"
    c "overlaying $SRC"
    # Downloads sometimes arrive as a flat folder and sometimes nested one
    # level; find the real root by looking for a file we know exists.
    if   [[ -f "$SRC/app/poller.py" ]];         then ROOT="$SRC"
    elif [[ -f "$SRC/eg4poll/app/poller.py" ]]; then ROOT="$SRC/eg4poll"
    else ROOT="$SRC"; warn "no app/poller.py found -- treating $SRC as the root"
    fi
    # --exclude .env: it is gitignored, holds credentials, and is never in a
    # download. Copying over it would blank the MQTT password.
    run "rsync -a --exclude='.env' --exclude='.git' '$ROOT/' './'"
    ok "copied"
fi

c "what changed"
if git diff --quiet && git diff --cached --quiet && [[ -z "$(git status --porcelain)" ]]; then
    ok "nothing -- deploying the current commit"
    CHANGED=""
else
    git status --short | sed 's/^/   /'
    CHANGED="$(git status --porcelain | awk '{print $2}')"
fi

# Stage before validating, not after: check_secrets.py reads `git ls-files`,
# which only sees tracked files. A file staged for the first time is
# untracked until `git add` runs -- validate it after that, not before, or
# its first commit is also the one commit that skipped the scan.
[[ -n "$CHANGED" ]] && run "git add -A"

validate() {
    # Before the commit, not after: a broken state should never enter history.
    c "validating"
    python3 -m py_compile app/*.py       || die "python does not compile"
    python3 tools/validate.py            || die "register/config validation failed"
    python3 tools/check_secrets.py       || die "site-specific data would be committed"
    for f in dashboard/solar_dash.html dashboard/solar_settings.html dashboard/config.html; do
        node -e "const h=require('fs').readFileSync('$f','utf8');new Function([...h.matchAll(/<script>([\s\S]*?)<\/script>/g)].pop()[1])" \
            || die "$f does not parse"
    done
    ok "all checks passed"
}

remote_banner() {
    ssh "$POLLER_HOST" "cd $POLLER_DIR && docker compose logs --tail 300 eg4poll" 2>/dev/null \
      | grep -E 'starting|code /app|config .* sha256' | tail -4 || true
}

if (( STATUS_ONLY )); then
    c "expected"
    printf '   version   %s\n' "$(git describe --tags --always --dirty 2>/dev/null || echo dev)"
    printf '   code      %s\n' "$(cat app/*.py | sha256sum | cut -c1-12)"
    c "running on $POLLER_HOST"
    B="$(remote_banner)"
    [[ -n "$B" ]] && echo "$B" | sed 's/^/   /' || warn "no startup banner found"
    exit 0
fi

# --------------------------------------------------------------- 2. validate
validate

if (( CHECK_ONLY )); then
    # -c promises to touch nothing: undo the staging done above so a check
    # run doesn't leave the index changed.
    [[ -n "$CHANGED" ]] && run "git reset --quiet"
    c "check only -- nothing deployed"
    exit 0
fi

# ----------------------------------------------------------------- 3. commit
if [[ -n "$CHANGED" ]]; then
    c "committing"
    MSG="deploy: $(echo "$CHANGED" | tr '\n' ' ' | cut -c1-120)"
    run "git commit -q -m \"\$(printf '%s' \"$MSG\")\""
    ok "$(git log -1 --oneline 2>/dev/null || echo committed)"
    if git remote get-url origin >/dev/null 2>&1; then
        run "git push -q" && ok "pushed" \
            || warn "push to origin failed (see error above) -- commit is local only"
    else
        warn "no origin remote -- commit is local only"
    fi
fi

VERSION="$(git describe --tags --always --dirty 2>/dev/null || echo dev)"
SHA="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
LOCAL_CODE="$(cat app/*.py | sha256sum | cut -c1-12)"

# ------------------------------------------------------------ 4. poller host
c "deploying to $POLLER_HOST"
# --delete keeps the remote a mirror, so a file removed here is removed there.
# .env is excluded: it lives only on the Pi. mosquitto/influx/nginx/grafana
# are part of the same compose stack (nginx serves the dashboard locally),
# so they sync alongside app/config. The device/site config itself
# (app/devconfig.py) is NOT synced -- it lives on a Docker volume on the
# remote host, managed through the Config page, not through this repo.
# Nothing dashboard-side is site-specific anymore either (see
# app/webapp.py's get_site()), so there is nothing under dashboard/ left
# to exclude.
run "rsync -a --delete --exclude='.env' --exclude='.git' --exclude='__pycache__' \
     app config tools dashboard mosquitto influx nginx grafana \
     docker-compose.yml Dockerfile requirements.txt \
     '$POLLER_HOST:$POLLER_DIR/'"
ok "files synced"

# `up -d` rather than restart: it recreates containers when compose or an
# image changed, and is a no-op when neither did. `restart` would silently
# keep old container definitions. EG4POLL_VERSION/EG4POLL_SHA are exported
# into the remote shell (not written to .env -- see docker-compose.yml's
# eg4poll environment block) so app/poller.py's startup banner says exactly
# what deploy.sh just verified was pushed, not "dev (unknown)".
run "ssh '$POLLER_HOST' 'cd $POLLER_DIR && EG4POLL_VERSION=\"$VERSION\" EG4POLL_SHA=\"$SHA\" docker compose up -d --build'"
ok "containers up"

# ----------------------------------------------------------------- 5. verify
(( DRY )) && { c "dry run -- nothing changed"; exit 0; }

c "verifying"
sleep 5
BANNER="$(remote_banner)"
if [[ -z "$BANNER" ]]; then
    warn "no startup banner -- the container may not have restarted"
    warn "  ssh $POLLER_HOST 'cd $POLLER_DIR && docker compose logs --tail 40 eg4poll'"
    exit 1
fi
echo "$BANNER" | sed 's/^/   /'

# The whole point: prove the Pi is running what the repo has, rather than
# inferring it from behaviour later. Code is the only thing left to compare
# -- the device/site config is app-managed runtime state now (see
# app/devconfig.py), not something this repo has a local copy of to diff
# against; its hash in $BANNER is informational only, logged so a support
# session can see whether it changed between two runs.
REMOTE_CODE="$(sed -n 's/.*code \/app  sha256 \([a-f0-9]*\).*/\1/p' <<<"$BANNER" | tail -1)"
echo
[[ "$REMOTE_CODE" == "$LOCAL_CODE" ]] \
    && ok   "code   $LOCAL_CODE  matches" \
    || die  "code   local $LOCAL_CODE != remote $REMOTE_CODE -- the container is stale"
ok "version $VERSION deployed"
