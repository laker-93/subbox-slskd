#!/usr/bin/env bash
#
# Install (if needed) and run slskd on macOS (Apple Silicon or Intel).
#
# slskd is the Soulseek client that download_wishlist.py talks to. On first run
# this downloads the binary; on later runs it just launches it. Either way it
# prompts for your Soulseek username/password, starts slskd with them, and
# verifies the login works before handing off.
#
# The CPU architecture (Apple Silicon arm64 vs Intel x64) is detected from the
# machine via `uname -m`, so the same script works on both — no need to pick a
# variant. Override it with SLSKD_ARCH_TAG if the detection is ever wrong.
#
# The same username/password are used for BOTH the Soulseek network login and
# the slskd web login — so pass them to the wishlist script as
# --slskd-username / --slskd-password.
#
# Usage:
#   ./run-slskd-macos.sh [version]
#
# Environment overrides:
#   SLSKD_VERSION       release tag to (re)install (default: latest)
#   SLSKD_INSTALL_DIR   where the binary lives (default: $HOME/slskd)
#   SLSKD_PORT          slskd web port (default: 5030)
#   SLSKD_ARCH_TAG      slskd release arch tag (default: derived from `uname -m`)
set -euo pipefail

REPO="slskd/slskd"

# Derive the slskd release architecture tag from the running machine. Apple Silicon
# reports "arm64"; Intel reports "x86_64". SLSKD_ARCH_TAG overrides if ever needed.
detect_arch_tag() {
    if [ -n "${SLSKD_ARCH_TAG:-}" ]; then
        printf '%s' "$SLSKD_ARCH_TAG"
        return
    fi
    case "$(uname -m)" in
        arm64|aarch64) printf 'osx-arm64' ;;
        x86_64)        printf 'osx-x64' ;;
        *)
            echo "error: unsupported macOS architecture '$(uname -m)' — set SLSKD_ARCH_TAG (e.g. osx-arm64 or osx-x64)" >&2
            exit 1
            ;;
    esac
}
ARCH_TAG="$(detect_arch_tag)"

INSTALL_DIR="${SLSKD_INSTALL_DIR:-$HOME/slskd}"
BIN="$INSTALL_DIR/slskd"
PORT="${SLSKD_PORT:-5030}"
# IPv4 loopback, not "localhost": slskd binds IPv6 dual-stack, and connecting to it over
# the IPv6 loopback (::1, which "localhost" can resolve to on macOS) gets reset. Using
# 127.0.0.1 here keeps both the login check below and the printed --slskd-url reliable.
BASE_URL="http://127.0.0.1:${PORT}"

# Credentials are cached here (alongside the script) so later runs skip the prompt.
# Plaintext, so it's created mode 600 and git-ignored; removed if a login is rejected.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CRED_FILE="${SLSKD_CRED_FILE:-$SCRIPT_DIR/slskd-credentials.env}"

# Where slskd writes finished downloads. We point slskd at this dir so the user
# configures ONE directory (their Subbox watch dir) and finished tracks land straight in
# it — the wishlist script never needs it. Resolved highest-priority-first:
# the SLSKD_DOWNLOADS_DIR env override, the cached value, or an interactive prompt that
# defaults to <install-dir>/downloads. Set by get_credentials before slskd starts.
DEFAULT_DOWNLOADS_DIR="$INSTALL_DIR/downloads"
DOWNLOADS_DIR=""

# Which directory slskd SHARES back to the Soulseek network. Soulseek peers commonly
# refuse to queue downloads for users who share nothing, so we prompt for a folder to
# share. Resolved like DOWNLOADS_DIR: the SLSKD_SHARE_DIR env override, the cached value,
# or an interactive prompt that defaults to the downloads dir (so finished tracks are
# shared straight back). Set by get_credentials before slskd starts.
SHARE_DIR=""

install_slskd() {
    local version asset url tmpzip
    version="${1:-${SLSKD_VERSION:-}}"
    if [ -z "$version" ]; then
        echo "Resolving latest slskd release…"
        version="$(curl -fsSL "https://api.github.com/repos/${REPO}/releases/latest" \
            | grep -m1 '"tag_name"' | sed -E 's/.*"tag_name": *"([^"]+)".*/\1/')"
    fi
    if [ -z "$version" ]; then
        echo "error: could not determine slskd version (pass one explicitly, e.g. '$0 0.25.1')" >&2
        exit 1
    fi

    asset="slskd-${version}-${ARCH_TAG}.zip"
    url="https://github.com/${REPO}/releases/download/${version}/${asset}"

    echo "Installing slskd ${version} (${ARCH_TAG}) into ${INSTALL_DIR}"
    mkdir -p "$INSTALL_DIR"
    tmpzip="$(mktemp -t slskd-XXXXXX).zip"
    trap 'rm -f "$tmpzip"' RETURN

    echo "Downloading ${url}"
    curl -fL -o "$tmpzip" "$url"
    echo "Unzipping…"
    unzip -o "$tmpzip" -d "$INSTALL_DIR" >/dev/null

    if [ ! -f "$BIN" ]; then
        echo "error: slskd binary not found after unzip in $INSTALL_DIR" >&2
        exit 1
    fi
    # Clear the macOS quarantine flag so Gatekeeper doesn't block the unsigned binary.
    xattr -d com.apple.quarantine "$BIN" 2>/dev/null || true
    chmod +x "$BIN"
    echo "✓ Installed slskd ${version} at ${BIN}"
}

prompt_credentials() {
    echo
    echo "Enter your Soulseek credentials (a free account — if it doesn't exist yet,"
    echo "slskd registers it on first connect). These are reused as the slskd web login."
    while [ -z "${RUN_USERNAME:-}" ]; do
        printf "  Username: "
        read -r RUN_USERNAME || true
        if [ -z "${RUN_USERNAME:-}" ]; then echo "  (username can't be empty)"; fi
    done
    while [ -z "${RUN_PASSWORD:-}" ]; do
        printf "  Password: "
        read -rs RUN_PASSWORD || true
        echo
        if [ -z "${RUN_PASSWORD:-}" ]; then echo "  (password can't be empty)"; fi
    done
}

# Prompt for the directory finished downloads go into — your Subbox watch dir, so
# tracks import automatically. Defaults to the cached dir, else <install-dir>/downloads.
prompt_downloads_dir() {
    local default_dir="${RUN_DOWNLOADS_DIR:-$DEFAULT_DOWNLOADS_DIR}"
    echo
    echo "Where should finished downloads go? Use your Subbox watch directory so tracks"
    echo "import automatically — slskd will download straight into it."
    printf "  Download dir [%s]: " "$default_dir"
    read -r RUN_DOWNLOADS_DIR || true
    [ -n "${RUN_DOWNLOADS_DIR:-}" ] || RUN_DOWNLOADS_DIR="$default_dir"
    # Expand a leading ~ (read doesn't do tilde expansion).
    case "$RUN_DOWNLOADS_DIR" in
        "~")   RUN_DOWNLOADS_DIR="$HOME" ;;
        "~/"*) RUN_DOWNLOADS_DIR="$HOME/${RUN_DOWNLOADS_DIR#\~/}" ;;
    esac
}

# Prompt for a directory to SHARE on Soulseek. Sharing is effectively required: many
# peers won't queue downloads for users who share nothing. Defaults to the downloads dir
# (set just before this) so finished tracks are shared back automatically.
prompt_share_dir() {
    local default_dir="${RUN_SHARE_DIR:-${RUN_DOWNLOADS_DIR:-$DEFAULT_DOWNLOADS_DIR}}"
    echo
    echo "Which directory should you SHARE on Soulseek? Soulseek often blocks or throttles"
    echo "downloads for users who don't share anything back, so pick a folder of music to"
    echo "share — your downloads dir is a fine default."
    printf "  Share dir [%s]: " "$default_dir"
    read -r RUN_SHARE_DIR || true
    [ -n "${RUN_SHARE_DIR:-}" ] || RUN_SHARE_DIR="$default_dir"
    # Expand a leading ~ (read doesn't do tilde expansion).
    case "$RUN_SHARE_DIR" in
        "~")   RUN_SHARE_DIR="$HOME" ;;
        "~/"*) RUN_SHARE_DIR="$HOME/${RUN_SHARE_DIR#\~/}" ;;
    esac
}

# Load cached creds into RUN_USERNAME/RUN_PASSWORD; succeeds only if both are present.
# Parsed line-by-line (not sourced) so the file can't execute arbitrary shell.
load_credentials() {
    [ -f "$CRED_FILE" ] || return 1
    local k v
    while IFS='=' read -r k v; do
        case "$k" in
            SLSKD_RUN_USERNAME) RUN_USERNAME="$v" ;;
            SLSKD_RUN_PASSWORD) RUN_PASSWORD="$v" ;;
            SLSKD_RUN_DOWNLOADS_DIR) RUN_DOWNLOADS_DIR="$v" ;;
            SLSKD_RUN_SHARE_DIR) RUN_SHARE_DIR="$v" ;;
        esac
    done < "$CRED_FILE"
    [ -n "${RUN_USERNAME:-}" ] && [ -n "${RUN_PASSWORD:-}" ]
}

save_credentials() {
    ( umask 077; printf 'SLSKD_RUN_USERNAME=%s\nSLSKD_RUN_PASSWORD=%s\nSLSKD_RUN_DOWNLOADS_DIR=%s\nSLSKD_RUN_SHARE_DIR=%s\n' \
        "$RUN_USERNAME" "$RUN_PASSWORD" "${RUN_DOWNLOADS_DIR:-}" "${RUN_SHARE_DIR:-}" > "$CRED_FILE" )
    chmod 600 "$CRED_FILE" 2>/dev/null || true
}

get_credentials() {
    if load_credentials; then
        echo "Using saved credentials for '${RUN_USERNAME}' from ${CRED_FILE}"
        echo "  (delete that file to re-enter; it's auto-removed if the login is rejected)"
        # Older caches predate the download-dir / share-dir prompts; ask for whichever is
        # missing and re-save (unless the matching env override is in play, which wins).
        local need_save=""
        if [ -z "${SLSKD_DOWNLOADS_DIR:-}" ] && [ -z "${RUN_DOWNLOADS_DIR:-}" ]; then
            prompt_downloads_dir
            need_save=1
        fi
        if [ -z "${SLSKD_SHARE_DIR:-}" ] && [ -z "${RUN_SHARE_DIR:-}" ]; then
            prompt_share_dir
            need_save=1
        fi
        [ -n "$need_save" ] && save_credentials
    else
        prompt_credentials
        prompt_downloads_dir
        prompt_share_dir
        save_credentials
        echo "Saved credentials to ${CRED_FILE} (mode 600) — future runs won't prompt."
    fi
    # Env override always wins; otherwise the cached/prompted dir, then the default.
    DOWNLOADS_DIR="${SLSKD_DOWNLOADS_DIR:-${RUN_DOWNLOADS_DIR:-$DEFAULT_DOWNLOADS_DIR}}"
    # Share dir falls back to the downloads dir so the user always shares something.
    SHARE_DIR="${SLSKD_SHARE_DIR:-${RUN_SHARE_DIR:-$DOWNLOADS_DIR}}"
}

# Returns the HTTP status of a login attempt: 200 ok, 401 rejected, 000 not up yet.
login_status() {
    python3 - "$BASE_URL" "$RUN_USERNAME" "$RUN_PASSWORD" <<'PY'
import sys, json, urllib.request, urllib.error
base, user, pw = sys.argv[1:4]
req = urllib.request.Request(
    base + "/api/v0/session",
    data=json.dumps({"username": user, "password": pw}).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
try:
    with urllib.request.urlopen(req, timeout=5) as r:
        print(r.status)
except urllib.error.HTTPError as e:
    print(e.code)
except Exception:
    print("000")
PY
}

run_slskd() {
    local log="$INSTALL_DIR/slskd-run.log"
    export SLSKD_SLSK_USERNAME="$RUN_USERNAME" SLSKD_SLSK_PASSWORD="$RUN_PASSWORD"
    export SLSKD_USERNAME="$RUN_USERNAME" SLSKD_PASSWORD="$RUN_PASSWORD"

    mkdir -p "$DOWNLOADS_DIR" "$SHARE_DIR"

    # slskd opens many sockets and cache files at once. macOS shells often default to a
    # low open-file limit (256), which slskd exhausts under load ("Too many open files"),
    # after which it resets/refuses its own API connections — surfacing in the wishlist
    # script as "Connection reset by peer" / "Broken pipe". Raise the soft limit (bounded
    # by the hard limit) before launching so slskd inherits a generous one.
    _hard_nofile="$(ulimit -Hn 2>/dev/null || echo unlimited)"
    if [ "$_hard_nofile" = "unlimited" ] || { [ "${_hard_nofile:-0}" -gt 10240 ] 2>/dev/null; }; then
        ulimit -n 10240 2>/dev/null || true
    else
        ulimit -n "$_hard_nofile" 2>/dev/null || true
    fi
    echo "open-file limit (ulimit -n): $(ulimit -n)"

    echo
    echo "Starting slskd…"
    "$BIN" --downloads "$DOWNLOADS_DIR" --shared "$SHARE_DIR" >"$log" 2>&1 &
    local slskd_pid=$!
    local tail_pid=""
    cleanup() {
        [ -n "$tail_pid" ] && kill "$tail_pid" 2>/dev/null || true
        kill "$slskd_pid" 2>/dev/null || true
    }
    trap cleanup INT TERM EXIT

    local ok="" code
    for _ in $(seq 1 30); do
        if ! kill -0 "$slskd_pid" 2>/dev/null; then
            echo "error: slskd exited unexpectedly. Last log lines:" >&2
            tail -n 20 "$log" >&2
            exit 1
        fi
        code="$(login_status)"
        if [ "$code" = "200" ]; then ok=1; break; fi
        if [ "$code" = "401" ]; then
            echo "error: slskd rejected that username/password (HTTP 401)." >&2
            rm -f "$CRED_FILE"
            echo "Removed saved credentials (${CRED_FILE}). Re-run to enter new ones." >&2
            exit 1
        fi
        sleep 1
    done
    if [ -z "$ok" ]; then
        echo "error: slskd didn't become ready in time. Last log lines:" >&2
        tail -n 20 "$log" >&2
        exit 1
    fi

    echo "✓ slskd is running and your login works."
    echo "    Web UI:   ${BASE_URL}"
    echo "    Logs:     ${log}"
    echo "    Downloads: ${DOWNLOADS_DIR}"
    echo "    Sharing:   ${SHARE_DIR}"
    echo "    Wishlist: download_wishlist.py --slskd-url ${BASE_URL} \\"
    echo "                  --slskd-username '${RUN_USERNAME}' --slskd-password <same password>"
    echo "              (finished tracks land in ${DOWNLOADS_DIR} — slskd writes them; no dir flag needed)"
    echo
    echo "Watch the log below for 'logged in' to confirm the Soulseek connection."
    echo "Leave this terminal open. Press Ctrl-C to stop slskd."
    echo "------------------------------------------------------------------------"
    tail -n +1 -f "$log" &
    tail_pid=$!
    wait "$slskd_pid"
}

main() {
    echo "Detected macOS architecture: $(uname -m) → slskd asset '${ARCH_TAG}'"
    if [ -x "$BIN" ]; then
        echo "slskd already installed at ${BIN} (set SLSKD_VERSION + delete it to upgrade)."
    else
        install_slskd "${1:-}"
    fi
    get_credentials
    run_slskd
}

main "$@"
