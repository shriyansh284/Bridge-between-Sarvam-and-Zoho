#!/usr/bin/env bash
# Sets up the client's SFTP file drop on the server where the AMS calling app runs.
# Run setup_server.sh first: the app must be installed and up to date.
#
#   curl -fsSL https://raw.githubusercontent.com/shriyansh284/Bridge-between-Sarvam-and-Zoho/main/deploy/setup_sftp.sh -o setup_sftp.sh
#   sudo CLIENT_IPS="203.0.113.10" bash setup_sftp.sh                                    # password login
#   sudo CLIENT_IPS="203.0.113.10" CLIENT_KEY_FILE=/tmp/client_key.pub bash setup_sftp.sh  # key login (automated upload)
#
# Settings (environment variables):
#   CLIENT_IPS       The client's public IP addresses or ranges, comma-separated (203.0.113.10,198.51.100.0/24).
#                    Only these can log in. On a re-run, leave it out to keep the current list. CLIENT_IPS=any lets
#                    any address log in (only for a first test).
#   CLIENT_KEY_FILE  The client's SSH public key (.pub), for an automated upload. Without one, a password is created.
#   PASSWORD_LOGIN   yes | no. Default: yes, unless a key is installed.
#   NEW_PASSWORD=1   Create a new password. Otherwise an existing password is kept.
#   SFTP_USER        The client's login name (default amsclient).
#
# What it does:
#   1. Creates the login SFTP_USER. It can only use SFTP, locked into /srv/ams-sftp/SFTP_USER: no shell, no port
#      forwarding, and nothing else on the server is visible to it.
#   2. Creates inbox/ processed/ results/ errors/ there, shared with the app's account (group amsdrop).
#   3. Adds the SSH rules for that login at the end of /etc/ssh/sshd_config. They are checked (sshd -t and sshd -T)
#      on a copy BEFORE the real file is replaced, and SSH is then reloaded, not restarted: a problem leaves
#      everything as it was, so it can't lock anyone out, and sessions that are already open are never dropped.
#   4. Sets DROP_DIR in the app's .env (unless it's already set) and restarts the app.
#   5. Checks that the app can use the folder, and prints the login details to send to the client.
#
# Running it again is safe: folders and files are kept, and the password only changes with NEW_PASSWORD=1.
set -euo pipefail

APP_DIR=/opt/ams-calling
APP_USER=${APP_USER:-ams}
SFTP_USER=${SFTP_USER:-amsclient}
SFTP_GROUP=amsdrop
SFTP_BASE=/srv/ams-sftp
DROP="$SFTP_BASE/$SFTP_USER"
KEYS_DIR=/etc/ssh/ams-sftp-keys
KEY_FILE="$KEYS_DIR/$SFTP_USER"
SSHD_CONFIG=/etc/ssh/sshd_config
BEGIN_MARK="# BEGIN AMS client SFTP for $SFTP_USER (managed by setup_sftp.sh; keep this block at the end of the file)"
END_MARK="# END AMS client SFTP for $SFTP_USER"
ENV_FILE="$APP_DIR/.env"
DENY_PROBE=192.0.2.77   # a documentation-only address (RFC 5737): never a real client

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33mWARNING:\033[0m %s\n' "$*" >&2; }
die()  { printf '\n\033[31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Run it with sudo:  sudo bash $0"
SSHD=$(command -v sshd || echo /usr/sbin/sshd)
[ -x "$SSHD" ] && [ -f "$SSHD_CONFIG" ] || die "OpenSSH server (sshd) isn't installed here."
[ -f "$APP_DIR/ams/dropfolder.py" ] || die "The app in $APP_DIR is missing or too old. Install or update it first: sudo bash setup_server.sh"
[ -f "$ENV_FILE" ] || die "$ENV_FILE is missing. Finish setup_server.sh first."
id -u "$APP_USER" >/dev/null 2>&1 || die "The app's account '$APP_USER' doesn't exist. Run setup_server.sh first."
[[ "$SFTP_USER" =~ ^[a-z_][a-z0-9_-]{0,30}$ ]] || die "SFTP_USER '$SFTP_USER' isn't a valid login name."
[ "$SFTP_USER" != "$APP_USER" ] && [ "$SFTP_USER" != root ] || die "SFTP_USER must be a new, separate login."
[ ! -L "$ENV_FILE" ] || die "$ENV_FILE is a symbolic link; refusing to change it as root. Replace it with the real file."
MARKS=$(awk -v b="$BEGIN_MARK" -v e="$END_MARK" '$0 == b { nb++ } $0 == e { ne++ } END { print nb + 0, ne + 0 }' "$SSHD_CONFIG")
case "$MARKS" in
  "0 0"|"1 1") ;;
  *) die "$SSHD_CONFIG has an incomplete or repeated AMS SFTP block for $SFTP_USER (BEGIN/END count: $MARKS). Fix it by hand first." ;;
esac

# ---------------------------------------------------------------- 1. who may log in, and how
CURRENT_IPS=$(awk -v b="$BEGIN_MARK" -v e="$END_MARK" -v u="$SFTP_USER" '
  $0 == b { inb = 1; next } $0 == e { inb = 0 }
  inb && $1 == "Match" && $2 == "User" && $3 == u && $4 == "Address" { print $5 }' "$SSHD_CONFIG")
if [ -z "${CLIENT_IPS+set}" ]; then
  IPS=$CURRENT_IPS
  [ -z "$IPS" ] || echo "Keeping the allowed client addresses: $IPS"
else
  IPS=${CLIENT_IPS// /}
  [ -n "$IPS" ] || die "CLIENT_IPS is empty. Give the client's IP addresses, or CLIENT_IPS=any to allow every address."
  [ "$IPS" != any ] || IPS=""
fi
if [ -n "$IPS" ]; then
  [[ "$IPS" =~ ^[0-9A-Fa-f:.,/]+$ ]] || die "CLIENT_IPS must be IP addresses or ranges separated by commas (got '$IPS')."
  case ",$IPS," in *,0.0.0.0/0,*|*,::/0,*) die "To allow every address, use CLIENT_IPS=any." ;; esac
fi

if [ -n "${CLIENT_KEY_FILE:-}" ]; then
  [ -f "$CLIENT_KEY_FILE" ] || die "CLIENT_KEY_FILE $CLIENT_KEY_FILE doesn't exist."
  if grep -q "PRIVATE KEY" "$CLIENT_KEY_FILE"; then die "That's a PRIVATE key. Ask the client for their PUBLIC key (the .pub file) only."; fi
  ssh-keygen -l -f "$CLIENT_KEY_FILE" >/dev/null 2>&1 || die "$CLIENT_KEY_FILE isn't an SSH public key."
  install -d -o root -g root -m 755 "$KEYS_DIR"
  install -o root -g root -m 644 "$CLIENT_KEY_FILE" "$KEY_FILE"   # root-owned: the client can't change it
  echo "Installed the client's key: $(ssh-keygen -l -f "$KEY_FILE" | tail -n 1)"
fi
HAVE_KEY=no
[ -s "$KEY_FILE" ] && HAVE_KEY=yes
if [ -z "${PASSWORD_LOGIN:-}" ]; then
  PASSWORD_LOGIN=yes
  [ "$HAVE_KEY" = no ] || PASSWORD_LOGIN=no
fi
case "$PASSWORD_LOGIN" in yes|no) ;; *) die "PASSWORD_LOGIN must be yes or no." ;; esac
[ "$PASSWORD_LOGIN" = yes ] || [ "$HAVE_KEY" = yes ] || die "With PASSWORD_LOGIN=no the client needs a key: add CLIENT_KEY_FILE=/path/to/their_key.pub"

# ---------------------------------------------------------------- 2. account and folders
say "Creating the client's SFTP login '$SFTP_USER' and its folders"
getent group "$SFTP_GROUP" >/dev/null || groupadd --system "$SFTP_GROUP"
if id -u "$SFTP_USER" >/dev/null 2>&1; then
  case "$(getent passwd "$SFTP_USER" | cut -d: -f7)" in
    */nologin|*/false) usermod -g "$SFTP_GROUP" "$SFTP_USER" ;;
    *) die "A normal login called '$SFTP_USER' already exists. Run again with another name: SFTP_USER=amsupload" ;;
  esac
else
  useradd --system --no-create-home --home-dir / --shell /usr/sbin/nologin --gid "$SFTP_GROUP" "$SFTP_USER"
fi
usermod -aG "$SFTP_GROUP" "$APP_USER"   # the app reads, moves and writes files in the client's folders

# SSH's chroot needs every folder above the client's own to be owned by root and writable by nobody else.
mkdir -p /srv
[ "$(stat -c '%U' /srv)" = root ] && [ "$(( 0$(stat -c '%a' /srv) & 022 ))" -eq 0 ] \
  || die "/srv must be owned by root and not writable by group/others for SFTP's chroot."
for d in "$SFTP_BASE" "$DROP"; do
  [ ! -L "$d" ] || die "$d is a symbolic link; refusing to use it."
  mkdir -p "$d"
  chown root:root "$d"
  chmod 755 "$d"
done
for sub in inbox processed results errors; do
  p="$DROP/$sub"
  [ ! -L "$p" ] || die "$p is a symbolic link; refusing to use it."
  mkdir -p "$p"
  chown "root:$SFTP_GROUP" "$p"   # owned by root, so the client can't change the folder's permissions
  chmod 2770 "$p"                 # client + app (both in the group) only; new files inherit the group
done

# ---------------------------------------------------------------- 3. SSH rules
say "Adding the SSH rules for '$SFTP_USER'"
PUBKEY=$HAVE_KEY
if [ -n "$IPS" ]; then
  ALLOWED="Match User $SFTP_USER Address $IPS
    PasswordAuthentication $PASSWORD_LOGIN
    PubkeyAuthentication $PUBKEY"
  OTHER_PW=no; OTHER_PK=no   # from any other address this login can't sign in at all
else
  ALLOWED="# (any address may log in: run again with CLIENT_IPS=... to restrict it)"
  OTHER_PW=$PASSWORD_LOGIN; OTHER_PK=$PUBKEY
fi
BLOCK="$BEGIN_MARK
# The client's file drop: SFTP only, locked into $DROP. For each setting the first matching block wins.
$ALLOWED
Match User $SFTP_USER
    ChrootDirectory $DROP
    ForceCommand internal-sftp -u 0007 -d /
    AuthorizedKeysFile $KEYS_DIR/%u
    AuthenticationMethods any
    PasswordAuthentication $OTHER_PW
    PubkeyAuthentication $OTHER_PK
    KbdInteractiveAuthentication no
    HostbasedAuthentication no
    GSSAPIAuthentication no
    PermitEmptyPasswords no
    DisableForwarding yes
    AllowTcpForwarding no
    AllowAgentForwarding no
    AllowStreamLocalForwarding no
    X11Forwarding no
    PermitTunnel no
    PermitTTY no
    PermitUserRC no
$END_MARK"

# The new configuration is written to a separate file and fully checked BEFORE it replaces the real one,
# so an error (or an interrupted run) never leaves SSH with an unchecked configuration.
NEW="$SSHD_CONFIG.ams-new"
{
  awk -v b="$BEGIN_MARK" -v e="$END_MARK" '
    $0 == b { skip = 1 }
    !skip { line[++n] = $0; if ($0 ~ /[^[:space:]]/) last = n }
    $0 == e { skip = 0 }
    END { for (i = 1; i <= last; i++) print line[i] }' "$SSHD_CONFIG"
  printf '\n%s\n' "$BLOCK"
} > "$NEW"
chown --reference="$SSHD_CONFIG" "$NEW"
chmod --reference="$SSHD_CONFIG" "$NEW"
discard() { rm -f "$NEW"; die "$1
$SSHD_CONFIG was not changed and SSH was not reloaded, so nothing changed for anyone."; }

install -d -m 0755 /run/sshd   # sshd -t needs it; missing on servers where SSH starts on demand
ERR=$("$SSHD" -t -f "$NEW" 2>&1) || discard "SSH rejected the new configuration: $ERR"
PROBE=${IPS%%,*}; PROBE=${PROBE%%/*}; PROBE=${PROBE:-203.0.113.7}
EFF=$("$SSHD" -T -f "$NEW" -C "user=$SFTP_USER,host=client.check,addr=$PROBE" 2>&1) || discard "sshd -T failed: $EFF"
for want in "chrootdirectory $DROP" "forcecommand internal-sftp -u 0007 -d /" "permittty no" \
            "allowtcpforwarding no" "passwordauthentication $PASSWORD_LOGIN" "pubkeyauthentication $PUBKEY"; do
  case "$EFF" in *"$want"*) ;; *) discard "Check failed for the client's login from $PROBE: expected '$want'." ;; esac
done
if [ -n "$IPS" ]; then
  DENIED=$("$SSHD" -T -f "$NEW" -C "user=$SFTP_USER,host=other.check,addr=$DENY_PROBE" 2>&1) || discard "sshd -T failed: $DENIED"
  for want in "passwordauthentication no" "pubkeyauthentication no" "kbdinteractiveauthentication no" \
              "hostbasedauthentication no"; do
    case "$DENIED" in *"$want"*) ;; *) discard "Check failed: from other addresses the login should be refused ('$want')." ;; esac
  done
fi
ADMIN=$("$SSHD" -T -f "$NEW" -C "user=${SUDO_USER:-root},host=admin.check,addr=$PROBE" 2>&1) || discard "sshd -T failed: $ADMIN"
case "$ADMIN" in *"chrootdirectory none"*) ;; *) discard "Check failed: other logins must not be affected." ;; esac

BACKUP="$SSHD_CONFIG.before-ams-sftp.$(date +%Y%m%d%H%M%S)"
cp -p "$SSHD_CONFIG" "$BACKUP"
mv "$NEW" "$SSHD_CONFIG"
if systemctl is-active -q ssh 2>/dev/null; then systemctl reload ssh
elif systemctl is-active -q sshd 2>/dev/null; then systemctl reload sshd
else echo "SSH starts on demand here; new logins use the new rules."
fi
PORT=$(printf '%s\n' "$EFF" | awk '$1 == "port" { print $2 }' | paste -sd, -)
case "$EFF" in
  *"allowusers "*|*"allowgroups "*) warn "This server's SSH only admits listed users (AllowUsers/AllowGroups). Add '$SFTP_USER' there too, or the client can't log in." ;;
esac

# ---------------------------------------------------------------- 4. the app
say "Connecting the app to the folder"
if grep -q '^DROP_DIR=' "$ENV_FILE"; then
  CURRENT=$(sed -n 's/^DROP_DIR=//p' "$ENV_FILE" | tail -n 1)
  CURRENT=${CURRENT%%#*}; CURRENT=${CURRENT//\"/}; CURRENT=${CURRENT//\'/}; CURRENT=${CURRENT%"${CURRENT##*[![:space:]]}"}
  if [ -z "$CURRENT" ]; then   # the empty line from .env.example
    sed -i "s|^DROP_DIR=.*|DROP_DIR=$DROP|" "$ENV_FILE"
    echo "Set DROP_DIR=$DROP in $ENV_FILE"
  elif [ "$CURRENT" != "$DROP" ]; then
    warn "$ENV_FILE already has DROP_DIR=$CURRENT, but the client's folder is $DROP. Fix it by hand if that's wrong, then: sudo systemctl restart ams-backend ams-dashboard"
  fi
else
  printf '\n# Client SFTP file drop (added by setup_sftp.sh)\nDROP_DIR=%s\n' "$DROP" >> "$ENV_FILE"
  echo "Added DROP_DIR=$DROP to $ENV_FILE"
fi
chown "$APP_USER:" "$ENV_FILE" && chmod 600 "$ENV_FILE"   # as setup_server.sh leaves it
for sub in inbox processed results errors; do
  runuser -u "$APP_USER" -- test -r "$DROP/$sub" -a -w "$DROP/$sub" -a -x "$DROP/$sub" \
    || die "The app's account '$APP_USER' can't use $DROP/$sub. Check: ls -ld $DROP/$sub; id $APP_USER"
done
systemctl restart ams-backend ams-dashboard   # picks up DROP_DIR and the new group membership

OK=0
for _ in $(seq 1 45); do
  HEALTH=$(curl -fsS --max-time 5 http://127.0.0.1:8000/health 2>/dev/null || true)
  if printf '%s' "$HEALTH" | python3 -c 'import json, sys; d = json.load(sys.stdin).get("drop_last_check") or {}; sys.exit(0 if d.get("ok") else 1)' 2>/dev/null; then
    OK=1; break
  fi
  sleep 2
done
if [ "$OK" = 1 ]; then
  echo "The app is watching $DROP/inbox."
else
  warn "The app hasn't reported a successful check of the folder yet. Health: ${HEALTH:-no answer}. Logs: journalctl -u ams-backend -n 50"
fi
UFW=$(ufw status 2>/dev/null || true)
case "$UFW" in "Status: active"*) warn "ufw is active here: allow port $PORT from the client, e.g. sudo ufw allow from 203.0.113.10 to any port $PORT proto tcp" ;; esac

# ---------------------------------------------------------------- 5. what to send the client
# Set last, once everything else worked, so a new password is never set and then lost to an error.
PASSWORD=""
if [ "$PASSWORD_LOGIN" = yes ]; then
  if [ "$(passwd -S "$SFTP_USER" | awk '{print $2}')" != P ] || [ "${NEW_PASSWORD:-}" = 1 ]; then
    PASSWORD=$(python3 -c 'import secrets; print(secrets.token_urlsafe(15))')
    printf '%s:%s\n' "$SFTP_USER" "$PASSWORD" | chpasswd
  fi
else
  usermod -p '*' "$SFTP_USER"   # no password can be used; key login still works
fi

say "Done"
if [ -n "$PASSWORD" ]; then
  LOGIN="Password : $PASSWORD   (shown only now; send it separately from the rest, e.g. by phone)"
elif [ "$PASSWORD_LOGIN" = yes ]; then
  LOGIN="Password : unchanged (run again with NEW_PASSWORD=1 for a new one)"
else
  LOGIN="Login    : with the client's SSH key (password login is off)"
fi
cat <<EOF
Send these to the client:

  Protocol : SFTP  (in FileZilla or WinSCP choose SFTP, not FTP or FTPS)
  Server   : <this server's public IP address or name>
  Port     : $PORT
  Username : $SFTP_USER
  $LOGIN
  Folders  : inbox/      upload candidate files here (.csv or .xlsx, with the header row)
             processed/  a file moves here once it's picked up (within about a minute)
             results/    <file>_<campaign>_RESULTS.csv while calls run, then <file>_<campaign>_FINAL.csv
             errors/     files that can't be used, each with a _ERROR.txt note saying what to fix

Allowed from : ${IPS:-ANY address (run again with CLIENT_IPS=... to restrict it)}
Key login    : $HAVE_KEY      Password login: $PASSWORD_LOGIN
Folder here  : $DROP  (DROP_DIR in $ENV_FILE)
SSH backup   : $BACKUP

The firewall / cloud security group must allow TCP port $PORT from the client's addresses.
Test from an allowed address:  sftp -P $PORT $SFTP_USER@<server>   then  ls  and  put test.csv inbox/
Login attempts are logged:     journalctl -u ssh --since today | grep $SFTP_USER
Recommended with password login: sudo apt install fail2ban  (blocks repeated wrong passwords)
EOF
[ -n "$IPS" ] || warn "Any address on the internet can try to log in as $SFTP_USER. Restrict it once you have the client's IP: sudo CLIENT_IPS=... bash setup_sftp.sh"
