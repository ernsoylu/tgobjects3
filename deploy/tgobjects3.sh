#!/usr/bin/env bash
#
# tgobjects3.sh — build, run, and stop the tgobjects3 system via Docker Compose.
#
# One entry point for operating the single-node deployment described in
# deploy/docker-compose.yml: install prerequisites (Docker Engine + the Compose
# plugin), bootstrap the .env file, build the images, bring the stack up/down,
# and tail logs.
#
# Usage:
#   deploy/tgobjects3.sh <command> [args...]
#
# Commands:
#   install     Install prerequisites (Docker Engine + Compose plugin).
#   env         Create deploy/.env from .env.example if it is missing.
#   build       Build (and pull) all service images.
#   up          Start the stack in the background (builds if needed).
#   run         Alias for `up`.
#   down        Stop and remove containers and the network (keeps volumes).
#   stop        Stop containers without removing them.
#   restart     Restart the running stack.
#   logs        Follow logs for all services (Ctrl-C to stop following).
#   ps|status   Show service/container status.
#   nuke        Stop and remove containers, network AND named volumes (data loss).
#   all         install -> env -> build -> up (full first-run bootstrap).
#   help        Show this help.
#
# Environment:
#   The stack reads deploy/.env (copied from deploy/.env.example). You MUST set
#   a real TGOS3_MASTER_KEY there before running in anger; the script warns if
#   it is still the placeholder.

set -Eeuo pipefail

# --- Resolve paths relative to this script ---------------------------------
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
COMPOSE_FILE="${SCRIPT_DIR}/docker-compose.yml"
ENV_FILE="${SCRIPT_DIR}/.env"
ENV_EXAMPLE="${SCRIPT_DIR}/.env.example"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." >/dev/null 2>&1 && pwd)"

# --- Pretty logging --------------------------------------------------------
if [[ -t 1 ]]; then
  C_INFO=$'\033[0;34m'; C_OK=$'\033[0;32m'; C_WARN=$'\033[0;33m'
  C_ERR=$'\033[0;31m'; C_RST=$'\033[0m'
else
  C_INFO=""; C_OK=""; C_WARN=""; C_ERR=""; C_RST=""
fi
info() { printf '%s==>%s %s\n' "$C_INFO" "$C_RST" "$*"; }
ok()   { printf '%s[ok]%s %s\n' "$C_OK" "$C_RST" "$*"; }
warn() { printf '%s[warn]%s %s\n' "$C_WARN" "$C_RST" "$*" >&2; }
die()  { printf '%s[error]%s %s\n' "$C_ERR" "$C_RST" "$*" >&2; exit 1; }

# --- Compose command detection ---------------------------------------------
# Prefer the modern `docker compose` plugin; fall back to the legacy
# `docker-compose` binary. Set once, reused everywhere.
COMPOSE=()
detect_compose() {
  if [[ ${#COMPOSE[@]} -gt 0 ]]; then
    return 0
  fi
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    COMPOSE=(docker compose)
  elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE=(docker-compose)
  else
    return 1
  fi
  return 0
}

require_compose() {
  detect_compose || die "Docker Compose not found. Run: $0 install"
  if ! docker info >/dev/null 2>&1; then
    die "Cannot talk to the Docker daemon. Is it running, and is your user in the 'docker' group? (log out/in after 'install', or run with sudo)"
  fi
}

# Build the compose invocation. The `console` service in docker-compose.yml
# builds from deploy/Dockerfile.console, which only exists once the Console
# deployment image (milestone M4) is added. If it is absent we scope every
# command to the core services so the stack still builds and runs.
compose() {
  local -a services=()
  if [[ ! -f "${SCRIPT_DIR}/Dockerfile.console" ]]; then
    services=(gateway postgres)
  fi

  local -a cmd=("${COMPOSE[@]}" -f "$COMPOSE_FILE")
  local sub="$1"; shift

  case "$sub" in
    # Subcommands that accept a service list get the scoped set appended.
    build|up|create|pull|start|restart|stop|rm)
      "${cmd[@]}" "$sub" "$@" "${services[@]}"
      ;;
    *)
      "${cmd[@]}" "$sub" "$@"
      ;;
  esac
}

console_note() {
  if [[ ! -f "${SCRIPT_DIR}/Dockerfile.console" ]]; then
    warn "Console service skipped: deploy/Dockerfile.console not present yet (M4). Running core services only (gateway + postgres)."
  fi
}

# --- .env bootstrap --------------------------------------------------------
ensure_env() {
  [[ -f "$ENV_EXAMPLE" ]] || die "Missing ${ENV_EXAMPLE}; cannot bootstrap .env."
  if [[ -f "$ENV_FILE" ]]; then
    ok ".env already exists at ${ENV_FILE}"
  else
    cp -- "$ENV_EXAMPLE" "$ENV_FILE"
    ok "Created ${ENV_FILE} from .env.example"
    warn "Edit ${ENV_FILE} and set a strong TGOS3_MASTER_KEY before using in production."
  fi
  # Nudge if the master key is still the shipped placeholder.
  if grep -qE '^TGOS3_MASTER_KEY=change-me' "$ENV_FILE" 2>/dev/null; then
    warn "TGOS3_MASTER_KEY is still the placeholder. Generate one, e.g.: openssl rand -base64 48"
  fi
}

# --- Prerequisite installation ---------------------------------------------
# Installs Docker Engine + the Compose plugin via Docker's official convenience
# script on Linux. On macOS it points you at Docker Desktop (no silent install).
install_prereqs() {
  if detect_compose; then
    ok "Docker + Compose already installed: $("${COMPOSE[@]}" version | head -n1)"
    return 0
  fi

  local os; os="$(uname -s)"
  case "$os" in
    Linux)
      info "Installing Docker Engine + Compose plugin (Docker's get.docker.com script)..."
      local sudo=""
      if [[ "$(id -u)" -ne 0 ]]; then
        command -v sudo >/dev/null 2>&1 || die "Need root or sudo to install Docker."
        sudo="sudo"
      fi
      local tmp; tmp="$(mktemp)"
      if command -v curl >/dev/null 2>&1; then
        curl -fsSL https://get.docker.com -o "$tmp"
      elif command -v wget >/dev/null 2>&1; then
        wget -qO "$tmp" https://get.docker.com
      else
        rm -f "$tmp"; die "Need curl or wget to download the Docker install script."
      fi
      $sudo sh "$tmp"
      rm -f "$tmp"
      # Enable + start the daemon where systemd is available.
      if command -v systemctl >/dev/null 2>&1; then
        $sudo systemctl enable --now docker || warn "Could not enable/start docker via systemctl; start it manually."
      fi
      # Add the invoking user to the docker group for non-sudo usage.
      if [[ -n "${SUDO_USER:-}" ]]; then
        $sudo usermod -aG docker "$SUDO_USER" || true
      elif [[ "$(id -u)" -ne 0 ]]; then
        $sudo usermod -aG docker "$USER" || true
      fi
      detect_compose || die "Docker installed but Compose plugin still not found."
      ok "Docker installed: $("${COMPOSE[@]}" version | head -n1)"
      warn "Log out and back in (or run 'newgrp docker') so group membership takes effect for non-root use."
      ;;
    Darwin)
      die "On macOS, install Docker Desktop (includes Compose): https://docs.docker.com/desktop/install/mac-install/ then re-run '$0 build'."
      ;;
    *)
      die "Unsupported OS '$os' for automatic install. Install Docker Engine + Compose manually: https://docs.docker.com/engine/install/"
      ;;
  esac
}

# --- Readiness / endpoint summary ------------------------------------------
print_endpoints() {
  # Read published host ports from .env (fall back to compose defaults).
  local s3_port admin_port console_port
  s3_port="$\(get_env S3_PORT 9000)"
  admin_port="$(get_env ADMIN_PORT 8080)"
  console_port="$(get_env CONSOLE_PORT 8081)"
  info "Endpoints:"
  printf '    S3 API    : http://localhost:%s\n' "$s3_port"
  printf '    Admin API : http://localhost:%s  (health: /health/ready)\n' "$admin_port"
  if [[ -f "${SCRIPT_DIR}/Dockerfile.console" ]]; then
    printf '    Console   : http://localhost:%s\n' "$console_port"
  fi
}

get_env() {
  local key="$1" default="$2" val=""
  if [[ -f "$ENV_FILE" ]]; then
    val="$(grep -E "^${key}=" "$ENV_FILE" | tail -n1 | cut -d= -f2- || true)"
  fi
  printf '%s' "${val:-$default}"
}

# --- Commands --------------------------------------------------------------
cmd_build() {
  require_compose; ensure_env; console_note
  info "Building images (context: ${REPO_ROOT})..."
  compose build "$@"
  ok "Build complete."
}

cmd_up() {
  require_compose; ensure_env; console_note
  info "Starting stack..."
  compose up -d "$@"
  ok "Stack is up."
  compose ps
  print_endpoints
}

cmd_down() {
  require_compose
  info "Stopping and removing containers + network (volumes kept)..."
  compose down "$@"
  ok "Stack is down. Named volumes (data + postgres) were preserved."
}

cmd_stop() {
  require_compose
  info "Stopping containers (not removing them)..."
  compose stop "$@"
  ok "Containers stopped."
}

cmd_restart() {
  require_compose
  info "Restarting stack..."
  compose restart "$@"
  ok "Restarted."
  compose ps
}

cmd_logs()  { require_compose; compose logs -f --tail=100 "$@"; }
cmd_ps()    { require_compose; compose ps "$@"; }

cmd_nuke() {
  require_compose
  warn "This removes containers, the network, AND the named volumes (gateway /data + postgres)."
  warn "The PostgreSQL index is rebuildable from Telegram, but any un-uploaded staged data is lost."
  read -r -p "Type 'yes' to continue: " reply
  [[ "$reply" == "yes" ]] || die "Aborted."
  compose down -v --remove-orphans
  ok "Stack and volumes removed."
}

cmd_all() {
  install_prereqs
  ensure_env
  cmd_build
  cmd_up
}

usage() { sed -n '2,40p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

main() {
  local cmd="${1:-help}"; shift || true
  case "$cmd" in
    install)        install_prereqs ;;
    env)            ensure_env ;;
    build)          cmd_build "$@" ;;
    up|run|start)   cmd_up "$@" ;;
    down)           cmd_down "$@" ;;
    stop)           cmd_stop "$@" ;;
    restart)        cmd_restart "$@" ;;
    logs)           cmd_logs "$@" ;;
    ps|status)      cmd_ps "$@" ;;
    nuke)           cmd_nuke ;;
    all|bootstrap)  cmd_all ;;
    help|-h|--help) usage ;;
    *)              warn "Unknown command: ${cmd}"; usage; exit 2 ;;
  esac
}

main "$@"
