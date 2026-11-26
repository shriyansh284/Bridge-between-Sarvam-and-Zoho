#!/usr/bin/env bash
# One-command server setup for the AMS calling automation.
# Supports Ubuntu 22.04 / 24.04 and Debian 12 (anything with apt, systemd and Python 3.10+).
#
#   curl -fsSL https://raw.githubusercontent.com/shriyansh284/Bridge-between-Sarvam-and-Zoho/main/deploy/setup_server.sh -o setup_server.sh
#   sudo bash setup_server.sh
#
# Options (environment variables; a later run keeps the values chosen the first time):
#   APP_DIR=/opt/ams-calling        where the app is installed
#   APP_USER=ams                    the system account it runs as (not a person's login)
#   BACKEND_PORT=8000               local port of the backend (127.0.0.1 only)
#   DASHBOARD_PORT=8501             local port of the dashboard (127.0.0.1 only)
#   BACKUP_DIR=/var/backups/ams-calling  BACKUP_DAYS=14   daily backup of data/ and how long to keep it
#   REPO_URL=... BRANCH=main        where the code comes from
#   e.g. on a server shared with other projects:
#   sudo APP_DIR=/backend/ams-calling BACKEND_PORT=8710 DASHBOARD_PORT=8711 bash setup_server.sh
#
# The step-by-step manual version of everything below is in the deployment guide (DEPLOYMENT.md).
#
# No domain and no HTTPS certificate are needed: the app only makes outgoing connections to Sarvam and
# fetches each call's result itself. The client's SFTP folder is set up next, with setup_sftp.sh.
#
# It installs Python, puts the app in /opt/ams-calling, runs it as two systemd services
# (backend + internal dashboard, both on 127.0.0.1 only), adds a daily backup of data/, and
# checks the Sarvam settings at the end.
#
# Updating later: download this script again and run it the same way (always run a fresh copy you
# downloaded, never the one inside /opt/ams-calling, which the app's account can modify).
# It never deletes data/ and never changes .env.
set -euo pipefail

UNIT_DIR=/etc/systemd/system
BACKUP_SCRIPT=/usr/local/sbin/ams-calling-backup
# Choices made on the first run are read back from the installed files, so a later run keeps them.
unit_port() { [ -f "$UNIT_DIR/$1.service" ] && sed -n "s/.*$2 \([0-9][0-9]*\).*/\1/p" "$UNIT_DIR/$1.service" || true; }
unit_get()  { [ -f "$UNIT_DIR/$1.service" ] && sed -n "s/^$2=//p" "$UNIT_DIR/$1.service" || true; }
backup_get() { [ -f "$BACKUP_SCRIPT" ] && sed -n "s/^$1=//p" "$BACKUP_SCRIPT" || true; }
APP_DIR=${APP_DIR:-$(unit_get ams-backend WorkingDirectory)}
APP_DIR=${APP_DIR:-/opt/ams-calling}
APP_DIR=${APP_DIR%/}
APP_USER=${APP_USER:-$(unit_get ams-backend User)}
APP_USER=${APP_USER:-ams}
APP_HOME=/var/lib/$APP_USER
BACKEND_PORT=${BACKEND_PORT:-$(unit_port ams-backend ' --port')}
BACKEND_PORT=${BACKEND_PORT:-8000}
DASHBOARD_PORT=${DASHBOARD_PORT:-$(unit_port ams-dashboard '--server.port')}
DASHBOARD_PORT=${DASHBOARD_PORT:-8501}
BACKUP_DIR=${BACKUP_DIR:-$(backup_get BACKUP_DIR)}
BACKUP_DIR=${BACKUP_DIR:-/var/backups/ams-calling}
BACKUP_DIR=${BACKUP_DIR%/}
BACKUP_DAYS=${BACKUP_DAYS:-$(backup_get KEEP_DAYS)}
BACKUP_DAYS=${BACKUP_DAYS:-14}
REPO_URL=${REPO_URL:-https://github.com/shriyansh284/Bridge-between-Sarvam-and-Zoho.git}
BRANCH=${BRANCH:-main}
ENV_FILE="$APP_DIR/.env"
USERS_FILE="$APP_DIR/config/users.json"
ONE_COPY="Only ONE copy of the backend may ever run with this .env. Stop it on any other machine (e.g. a developer PC) first: two copies working on the same campaigns call candidates twice."

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33mWARNING:\033[0m %s\n' "$*" >&2; }
die()  { printf '\n\033[31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }
as_app() { runuser -u "$APP_USER" -- env HOME="$APP_HOME" "$@"; }

[ "$(id -u)" -eq 0 ] || die "Run it with sudo:  sudo bash $0"
for p in "$BACKEND_PORT" "$DASHBOARD_PORT"; do
  [[ "$p" =~ ^[0-9]+$ ]] && [ "$p" -ge 1024 ] && [ "$p" -le 65535 ] || die "Ports must be numbers from 1024 to 65535 (got '$p')."
done
[ "$BACKEND_PORT" != "$DASHBOARD_PORT" ] || die "BACKEND_PORT and DASHBOARD_PORT must be different."
for d in "$APP_DIR" "$BACKUP_DIR"; do
  [[ "$d" =~ ^(/[A-Za-z0-9._-]+)+$ ]] || die "Folders must be full paths with letters, digits, . _ - only (got '$d')."
  case "$d/" in */../*|*/./*|/etc/*|/usr/*|/bin/*|/sbin/*|/lib/*|/proc/*|/sys/*|/dev/*|/boot/*|/root/*|/run/*)
    die "'$d' can't be used: choose a folder outside the system folders." ;; esac
done
[[ "$BACKUP_DAYS" =~ ^[0-9]+$ ]] && [ "$BACKUP_DAYS" -ge 1 ] || die "BACKUP_DAYS must be a number of days (got '$BACKUP_DAYS')."
[[ "$APP_USER" =~ ^[a-z_][a-z0-9_-]{0,30}$ ]] || die "APP_USER '$APP_USER' isn't a valid account name."
command -v apt-get >/dev/null && command -v systemctl >/dev/null \
  || die "This script needs Ubuntu/Debian with systemd. Follow the manual steps in the deployment guide (DEPLOYMENT.md) instead."
FIRST_INSTALL=1
[ -f /etc/systemd/system/ams-backend.service ] && FIRST_INSTALL=0

# ---------------------------------------------------------------- 1. packages
say "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
# A new cloud VM runs cloud-init and automatic updates right after boot: wait for them instead of failing on the apt lock.
if command -v cloud-init >/dev/null; then cloud-init status --wait >/dev/null 2>&1 || true; fi
for i in $(seq 1 20); do
  if apt-get update -q; then break; fi
  [ "$i" -lt 20 ] || die "apt-get update kept failing (see the messages above)."
  warn "apt is busy (automatic updates?); retrying in 15 s"; sleep 15
done
apt-get -o DPkg::Lock::Timeout=600 install -y -q python3 python3-venv python3-pip git curl ca-certificates iproute2
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || die "Python 3.10 or newer is needed; this server has $(python3 --version 2>&1)."

# ---------------------------------------------------------------- 2. app account
if id -u "$APP_USER" >/dev/null 2>&1; then
  case "$(getent passwd "$APP_USER" | cut -d: -f7)" in
    */nologin|*/false) ;;
    *) die "A login user called '$APP_USER' already exists; the app must not run as a person's account. Run again with APP_USER=amscalling sudo -E bash $0" ;;
  esac
else
  useradd --system --create-home --home-dir "$APP_HOME" --shell /usr/sbin/nologin "$APP_USER"
fi
mkdir -p "$APP_HOME" && chown "$APP_USER:" "$APP_HOME"

# ---------------------------------------------------------------- 3. code
if [ -d "$APP_DIR/.git" ]; then
  say "Updating the code in $APP_DIR"
  chown -R "$APP_USER:" "$APP_DIR"
  as_app git -C "$APP_DIR" pull -q --ff-only origin "$BRANCH" \
    || die "git pull failed (local changes on the server?). Check with: sudo -u $APP_USER git -C $APP_DIR status"
else
  say "Downloading the code to $APP_DIR"
  mkdir -p "$APP_DIR"
  extra=$(ls -A "$APP_DIR" | grep -vxE '\.env|config' || true)
  [ -z "$extra" ] || die "$APP_DIR already contains files other than .env and config/ ($(echo $extra)). Move them away, then run again. Don't copy the project folder from a PC: this script downloads the code itself."
  git -C "$APP_DIR" init -q
  git -C "$APP_DIR" remote add origin "$REPO_URL" 2>/dev/null || git -C "$APP_DIR" remote set-url origin "$REPO_URL"
  git -C "$APP_DIR" fetch -q origin "$BRANCH" \
    || die "Couldn't download the code from $REPO_URL. If the repository is private, ask the project owner for access."
  git -C "$APP_DIR" checkout -q -B "$BRANCH" --track "origin/$BRANCH"
fi
[ -f "$APP_DIR/ams/outbox.py" ] \
  || die "The code on GitHub ($REPO_URL, branch $BRANCH) is an old version. Ask the project owner to push the latest code, then run again."

# A new server must start with an EMPTY data/: data copied from another machine carries that machine's
# scheduled calls, so the same candidates would be called again from here.
if [ "$FIRST_INSTALL" = 1 ] && [ -d "$APP_DIR/data" ] && [ -n "$(find "$APP_DIR/data" -type f -print -quit)" ] \
   && [ -z "${AMS_KEEP_DATA:-}" ]; then
  die "$APP_DIR/data already contains files (copied from another machine?). Move it away: sudo mv $APP_DIR/data $APP_DIR/data.copied
(To restore a real backup on purpose, run again with AMS_KEEP_DATA=1.)"
fi
mkdir -p "$APP_DIR/data" "$APP_DIR/config"
chown -R "$APP_USER:" "$APP_DIR"
chmod 750 "$APP_DIR"                  # candidate data is personal: not readable by other accounts
chmod -R g-w,o-rwx "$APP_DIR/data"

say "Installing Python packages"
[ -x "$APP_DIR/venv/bin/python" ] || as_app python3 -m venv "$APP_DIR/venv"
as_app "$APP_DIR/venv/bin/pip" install -q --upgrade pip
as_app "$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

# ---------------------------------------------------------------- 4. secrets
if [ ! -f "$ENV_FILE" ]; then
  cat >&2 <<EOF

The secrets aren't on the server yet. From the PC that has the project, copy both files to your home
folder here:

  scp .env               <user>@<this-server>:ams.env
  scp config/users.json  <user>@<this-server>:ams-users.json

then on this server:

  sudo install -m 600 ~/ams.env $ENV_FILE && rm -f ~/ams.env
  sudo install -m 600 ~/ams-users.json $USERS_FILE && rm -f ~/ams-users.json

and run this script again. (Packages and code are already installed, so the next run is quick.)

$ONE_COPY
EOF
  exit 1
fi
for f in "$ENV_FILE" "$USERS_FILE"; do
  [ ! -L "$f" ] || die "$f is a symbolic link; refusing to change it as root. Replace it with the real file."
done
chown -h "$APP_USER:" "$ENV_FILE" && chmod 600 "$ENV_FILE"
if [ -f "$USERS_FILE" ]; then
  chown -h "$APP_USER:" "$USERS_FILE" && chmod 600 "$USERS_FILE"
else
  warn "$USERS_FILE is missing: the dashboard will run but nobody can sign in until it's copied over."
fi

# ---------------------------------------------------------------- 5. services
say "Installing the services"
systemctl stop ams-backend ams-dashboard 2>/dev/null || true
BUSY=$(ss -ltnH "( sport = :$BACKEND_PORT or sport = :$DASHBOARD_PORT )" 2>/dev/null || true)
[ -z "$BUSY" ] || die "Port $BACKEND_PORT or $DASHBOARD_PORT is already used by another program on this server (the app needs both, on 127.0.0.1).
Pick two free ports and run again, e.g.:  sudo BACKEND_PORT=8710 DASHBOARD_PORT=8711 bash $0
$BUSY"
cat > /etc/systemd/system/ams-backend.service <<EOF
[Unit]
Description=AMS calling backend (SFTP folder watcher, call scheduler, result fetcher, result files)
After=network-online.target
Wants=network-online.target

[Service]
User=$APP_USER
WorkingDirectory=$APP_DIR
Environment=HOME=$APP_HOME PYTHONUNBUFFERED=1
# ONE worker only: the app keeps its state in files; two copies would call candidates twice.
ExecStart=$APP_DIR/venv/bin/uvicorn ams.server:app --host 127.0.0.1 --port $BACKEND_PORT --workers 1 --no-access-log
Restart=always
RestartSec=5
UMask=0027
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
cat > /etc/systemd/system/ams-dashboard.service <<EOF
[Unit]
Description=AMS calling dashboard (internal; reach it through an SSH tunnel)
After=network-online.target
Wants=network-online.target

[Service]
User=$APP_USER
WorkingDirectory=$APP_DIR
Environment=HOME=$APP_HOME PYTHONUNBUFFERED=1
ExecStart=$APP_DIR/venv/bin/streamlit run dashboard.py --server.address 127.0.0.1 --server.port $DASHBOARD_PORT --server.headless true --server.fileWatcherType none --browser.gatherUsageStats false
Restart=always
RestartSec=5
UMask=0027
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

# ---------------------------------------------------------------- 6. daily backup of data/ (systemd timer: no cron needed)
cat > "$BACKUP_SCRIPT" <<EOF
#!/bin/sh
# Daily copy of the AMS calling data (campaigns, call results, result files).
set -e
APP_DIR=$APP_DIR
BACKUP_DIR=$BACKUP_DIR
KEEP_DAYS=$BACKUP_DAYS
umask 077
mkdir -p "\$BACKUP_DIR"
tar -czf "\$BACKUP_DIR/data-\$(date +%F).tar.gz" -C "\$APP_DIR" data
find "\$BACKUP_DIR" -name 'data-*.tar.gz' -mtime +"\$KEEP_DAYS" -delete
EOF
chmod 755 "$BACKUP_SCRIPT"
cat > /etc/systemd/system/ams-calling-backup.service <<EOF
[Unit]
Description=Back up the AMS calling data folder

[Service]
Type=oneshot
ExecStart=$BACKUP_SCRIPT
EOF
cat > /etc/systemd/system/ams-calling-backup.timer <<EOF
[Unit]
Description=Daily backup of the AMS calling data folder

[Timer]
OnCalendar=daily
Persistent=true
RandomizedDelaySec=30min

[Install]
WantedBy=timers.target
EOF

warn "$ONE_COPY"
systemctl daemon-reload
systemctl enable -q ams-backend ams-dashboard ams-calling-backup.timer
systemctl start ams-calling-backup.timer
systemctl restart ams-backend ams-dashboard

# ---------------------------------------------------------------- 7. checks
say "Checking the app"
FAILED=0
ok=0
for _ in $(seq 1 30); do   # the answer must be THIS app's health check, not another program on the port
  health=$(curl -fsS --max-time 5 "http://127.0.0.1:$BACKEND_PORT/health" 2>/dev/null || true)
  case "$health" in *scheduler_heartbeat*) ok=1; break ;; esac
  sleep 1
done
if [ "$ok" = 1 ]; then echo "Backend: running"; else FAILED=1; warn "Backend isn't answering. Logs: journalctl -u ams-backend -n 50"; fi
if systemctl is-active -q ams-dashboard; then echo "Dashboard: running"; else FAILED=1; warn "Dashboard isn't running. Logs: journalctl -u ams-dashboard -n 50"; fi
if systemctl is-active -q ams-calling-backup.timer; then echo "Daily backup: scheduled"; else FAILED=1; warn "The backup timer isn't active: systemctl status ams-calling-backup.timer"; fi
if [ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null || echo unknown)" = "no" ]; then
  warn "The server clock isn't NTP-synchronised; calls are placed at scheduled times, so please enable NTP."
fi

cd "$APP_DIR"
for tool in sarvam_setup; do   # read-only check of the Sarvam key, IDs and agent
  rc=0
  out=$(as_app "$APP_DIR/venv/bin/python" -m "ams.$tool" check 2>&1) || rc=$?
  echo; echo "${tool%_setup}:"
  if [ "$rc" = 0 ]; then
    printf '%s\n' "$out" | sed -n '/could not parse statement/d; 1,4p'
  else
    FAILED=1
    printf '%s\n' "$out" | tail -n 6
    warn "ams.$tool check failed (full output: cd $APP_DIR && sudo -u $APP_USER venv/bin/python -m ams.$tool check)"
  fi
done

say "Summary"
cat <<EOF
The app is running. It needs no public address: it places the calls and fetches each
result from Sarvam by itself. Next, set up the client's SFTP folder (below).

Dashboard : from your PC run   ssh -L $DASHBOARD_PORT:127.0.0.1:$DASHBOARD_PORT <user>@<this-server>
            then open          http://localhost:$DASHBOARD_PORT
Status    : curl -s http://127.0.0.1:$BACKEND_PORT/health
Logs      : journalctl -u ams-backend -f        (dashboard: -u ams-dashboard)
Update    : download this script again and run it:  sudo bash setup_server.sh
Installed : $APP_DIR  (runs as $APP_USER; backend 127.0.0.1:$BACKEND_PORT, dashboard 127.0.0.1:$DASHBOARD_PORT)
Backups   : $BACKUP_DIR (daily, kept $BACKUP_DAYS days)
SFTP folder: sudo DROP_DIR=<folder> CLIENT_IPS=<client IP> bash setup_sftp.sh  (see the SFTP setup guide, SFTP_SETUP.md)

$ONE_COPY
EOF
[ "$FAILED" = 0 ] || { warn "Some checks failed; see the warnings above."; exit 2; }
