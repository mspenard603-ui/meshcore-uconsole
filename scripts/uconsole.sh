#!/usr/bin/env bash
# Sync this checkout to a uConsole over SSH and run it there for live testing.
#
# Usage:
#   scripts/uconsole.sh sync              # push the working tree
#   scripts/uconsole.sh bootstrap         # sync + create/refresh the Pi's .venv (needs network on the Pi)
#   scripts/uconsole.sh run [--mock] ...  # sync + (re)start the app on the uConsole's screen, logs stream here
#   scripts/uconsole.sh dev [--mock] ...  # run, then re-sync and restart whenever a file changes
#   scripts/uconsole.sh deploy [--mock]   # sync + restart detached (survives this shell), report radio errors
#   scripts/uconsole.sh stop              # stop the app on the uConsole
#   scripts/uconsole.sh shell             # open an SSH shell in the remote checkout
#
# Configuration (environment, or a gitignored .uconsole.env in the repo root):
#   UCONSOLE_HOST  ssh destination, e.g. "pi@192.168.1.50" or an ~/.ssh/config alias (default: uconsole)
#   UCONSOLE_DIR   remote checkout path, relative to the remote home (default: meshcore-uconsole)
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ -f .uconsole.env ]]; then
  # shellcheck disable=SC1091
  source .uconsole.env
fi
HOST="${UCONSOLE_HOST:-uconsole}"
DIR="${UCONSOLE_DIR:-meshcore-uconsole}"

# Reuse one SSH connection for every sync/restart so the loop stays fast.
CTL_DIR="${XDG_RUNTIME_DIR:-/tmp}/uconsole-ssh"
mkdir -p "$CTL_DIR" && chmod 700 "$CTL_DIR"
SSH_OPTS=(-o ControlMaster=auto -o "ControlPath=$CTL_DIR/%C" -o ControlPersist=10m -o ConnectTimeout=8)

# Non-interactive SSH shells skip ~/.profile, so put ~/.local/bin (uv) on PATH.
REMOTE_ENV='export PATH="$HOME/.local/bin:$PATH";'
remote() {
  local tty=()
  if [[ "${1:-}" == "-t" ]]; then tty=(-t); shift; fi
  ssh "${SSH_OPTS[@]}" "${tty[@]}" "$HOST" "$REMOTE_ENV $*"
}

sync_tree() {
  remote "mkdir -p '$DIR'"
  # The Pi keeps its own .venv (built against Pi system packages), so never touch it.
  rsync -az --delete \
    -e "ssh ${SSH_OPTS[*]}" \
    --exclude '.git/' \
    --exclude '.venv/' \
    --exclude '__pycache__/' \
    --exclude '*.py[cod]' \
    --exclude '.pytest_cache/' \
    --exclude '.ruff_cache/' \
    --exclude 'dist/' --exclude 'build/' --exclude '*.egg-info/' \
    --exclude 'result' \
    --exclude 'debian/' \
    --exclude '.uconsole.env' \
    --exclude '.claude/' \
    --exclude '.direnv/' --exclude '.envrc' \
    ./ "$HOST:$DIR/"
  echo "[uconsole] synced -> $HOST:$DIR ($(date +%H:%M:%S))"
}

# Stops the dev copy *and* the installed .deb app: they share one GTK
# application ID (a second launch just hands off to the first and exits) and
# only one process can own the radio. The [x] brackets keep pkill from
# matching the shell that runs it.
stop_app() {
  remote "pkill -f 'meshcore_console[.]main|/usr/bin/meshcor[e]-console' >/dev/null 2>&1
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      pgrep -f 'meshcore_console[.]main|/usr/bin/meshcor[e]-console' >/dev/null || break
      sleep 0.3
    done; true"
}

# Start the app on the uConsole's own Wayland session. It finds the compositor
# socket rather than assuming wayland-0 vs wayland-1 (labwc vs wayfire).
start_cmd() {
  local args=""
  local a
  for a in "$@"; do args+=" $(printf '%q' "$a")"; done
  cat <<EOF
cd '$DIR' || exit 1
export XDG_RUNTIME_DIR=\${XDG_RUNTIME_DIR:-/run/user/\$(id -u)}
if [ -z "\${WAYLAND_DISPLAY:-}" ]; then
  sock=\$(ls "\$XDG_RUNTIME_DIR" 2>/dev/null | grep -E '^wayland-[0-9]+\$' | head -n1)
  if [ -z "\$sock" ]; then
    echo "[uconsole] no Wayland session found in \$XDG_RUNTIME_DIR; is the desktop logged in?" >&2
    exit 1
  fi
  export WAYLAND_DISPLAY=\$sock
fi
export PYTHONUNBUFFERED=1
exec ./scripts/run-gtk-pi.sh$args
EOF
}

cmd="${1:-sync}"
shift || true

case "$cmd" in
  sync)
    sync_tree
    ;;
  bootstrap)
    sync_tree
    remote "cd '$DIR' && ./scripts/run-gtk-pi.sh --bootstrap"
    ;;
  run)
    sync_tree
    stop_app
    # -t so Ctrl-C here stops the remote app too.
    remote -t "$(start_cmd "$@")"
    ;;
  deploy)
    sync_tree
    stop_app
    log='~/.cache/meshcore-console-run.log'
    # setsid -f + every fd redirected: the app outlives the SSH call, which returns at once.
    remote "mkdir -p ~/.cache; setsid -f sh -c $(printf '%q' "$(start_cmd "$@")") \
      > $log 2>&1 < /dev/null"
    sleep 12
    remote "pgrep -f 'meshcore_console[.]main' >/dev/null \
      && echo '[uconsole] app running' || echo '[uconsole] app NOT running'
      grep -E 'Connection error|Traceback|FATAL|radio initialized|Connected' $log | tail -5; true"
    ;;
  dev)
    stamp="$(mktemp)"
    cleanup() {
      trap - EXIT INT TERM
      rm -f "$stamp"
      stop_app
      kill 0 2>/dev/null || true
    }
    trap cleanup EXIT INT TERM
    restart() {
      sync_tree
      stop_app
      remote "$(start_cmd "$@")" &
    }
    touch "$stamp"
    restart "$@"
    echo "[uconsole] watching for changes (Ctrl-C to stop)"
    while sleep 1; do
      changed="$(find src scripts pyproject.toml -newer "$stamp" -type f \
        ! -path '*/__pycache__/*' ! -name '*.pyc' -print -quit 2>/dev/null || true)"
      if [[ -n "$changed" ]]; then
        sleep 0.3  # let a multi-file save settle
        touch "$stamp"
        echo "[uconsole] change: $changed -> restarting"
        restart "$@"
      fi
    done
    ;;
  stop)
    stop_app
    echo "[uconsole] stopped"
    ;;
  shell)
    remote -t "cd '$DIR' && exec \$SHELL -l"
    ;;
  *)
    sed -n '2,15p' "$0"
    exit 2
    ;;
esac
