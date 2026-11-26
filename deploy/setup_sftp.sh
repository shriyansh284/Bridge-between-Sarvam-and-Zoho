#!/usr/bin/env bash
# Sets up the client's SFTP folder on the server where the AMS calling app runs.
# Run setup_server.sh first: the app must be installed and up to date.
#
#   curl -fsSL https://raw.githubusercontent.com/shriyansh284/Bridge-between-Sarvam-and-Zoho/main/deploy/setup_sftp.sh -o setup_sftp.sh
#
# A. A new SFTP login for the client, locked into its folder (the default):
#   sudo DROP_DIR=/backend/fileupload CLIENT_IPS="203.0.113.10" bash setup_sftp.sh         # password login
#   sudo DROP_DIR=/backend/fileupload CLIENT_IPS="203.0.113.10" CLIENT_KEY_FILE=/tmp/client_key.pub bash setup_sftp.sh
#
# B. The client already has an SFTP login on this server that uploads into the folder (e.g. one the vendor set up):
#   sudo DROP_DIR=/backend/fileupload EXISTING_LOGIN=yes SFTP_USER=<that login> bash setup_sftp.sh
#   This only creates the folders and gives the app and that login access to them. SSH settings aren't touched.
#
# Settings (environment variables):
#   DROP_DIR         The client's folder; inbox/ send/ failed/ are created inside it. Default /srv/ams-sftp/<SFTP_USER>.
#                    For A, every folder in its path must be owned by root and writable by nobody else (SSH's rule for
#                    a login locked into a folder); folders that don't exist yet are created that way.
#   EXISTING_LOGIN   yes = B above. Default: no (A).
#   SFTP_USER        The client's login name (default amsclient).
#   CLIENT_IPS       (A) The client's public IP addresses or ranges, comma-separated (203.0.113.10,198.51.100.0/24).
#                    Only these can log in. On a re-run, leave it out to keep the current list. CLIENT_IPS=any lets
#                    any address log in (only for a first test).
#   CLIENT_KEY_FILE  (A) The client's SSH public key (.pub), for an automated upload. Without one, a password is created.
#   PASSWORD_LOGIN   (A) yes | no. Default: yes, unless a key is installed.
#   NEW_PASSWORD=1   (A) Create a new password. Otherwise an existing password is kept.
#
# What it does:
#   1. (A) Creates the login SFTP_USER. It can only use SFTP, locked into DROP_DIR: no shell, no port forwarding,
#      and nothing else on the server is visible to it.
#   2. Creates inbox/ send/ failed/ in DROP_DIR, shared by the client's login and the app's account (group amsdrop).
#   3. (A) Adds the SSH rules for that login at the end of /etc/ssh/sshd_config. They are checked (sshd -t and
#      sshd -T) on a copy BEFORE the real file is replaced, and SSH is then reloaded, not restarted: a problem leaves
#      everything as it was, so it can't lock anyone out, and sessions that are already open are never dropped.
#   4. Sets DROP_DIR in the app's .env and restarts the app.
#   5. Checks that the app (and the client's login) can use the folders, and prints what to send to the client.
#
# Running it again is safe: folders and files are kept, and the password only changes with NEW_PASSWORD=1.
set -euo pipefail

UNIT=/etc/systemd/system/ams-backend.service
# The app's folder and account, as setup_server.sh (or a manual install with the same service name) left them
APP_DIR=${APP_DIR:-$(sed -n 's/^WorkingDirectory=//p' "$UNIT" 2>/dev/null || true)}
APP_DIR=${APP_DIR:-/opt/ams-calling}
APP_USER=${APP_USER:-$(sed -n 's/^User=//p' "$UNIT" 2>/dev/null || true)}
APP_USER=${APP_USER:-ams}
SFTP_USER=${SFTP_USER:-amsclient}
SFTP_GROUP=amsdrop
DROP=${DROP_DIR:-/srv/ams-sftp/$SFTP_USER}
DROP=${DROP%/}
EXISTING_LOGIN=${EXISTING_LOGIN:-no}
KEYS_DIR=/etc/ssh/ams-sftp-keys
KEY_FILE="$KEYS_DIR/$SFTP_USER"
SSHD_CONFIG=/etc/ssh/sshd_config
BEGIN_MARK="# BEGIN AMS client SFTP for $SFTP_USER (managed by setup_sftp.sh; keep this block at the end of the file)"
END_MARK="# END AMS client SFTP for $SFTP_USER"
ENV_FILE="$APP_DIR/.env"
DENY_PROBE=192.0.2.77   # a documentation-only address (RFC 5737): never a real client
SUBFOLDERS="inbox send failed"

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33mWARNING:\033[0m %s\n' "$*" >&2; }
die()  { printf '\n\033[31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Run it with sudo:  sudo bash $0"
SSHD=$(command -v sshd || echo /usr/sbin/sshd)
[ -x "$SSHD" ] && [ -f "$SSHD_CONFIG" ] || die "OpenSSH server (sshd) isn't installed here."
[ -f "$APP_DIR/ams/outbox.py" ] || die "The app in $APP_DIR is missing or too old. Install or update it first: sudo bash setup_server.sh"
[ -f "$ENV_FILE" ] || die "$ENV_FILE is missing. Finish setup_server.sh first."
[ ! -L "$ENV_FILE" ] || die "$ENV_FILE is a symbolic link; refusing to change it as root. Replace it with the real file."
id -u "$APP_USER" >/dev/null 2>&1 || die "The app's account '$APP_USER' doesn't exist. Run setup_server.sh first."
case "$EXISTING_LOGIN" in yes|no) ;; *) die "EXISTING_LOGIN must be yes or no." ;; esac
[[ "$SFTP_USER" =~ ^[a-z_][a-z0-9_.-]{0,31}$ ]] || die "SFTP_USER '$SFTP_USER' isn't a valid login name."
[ "$SFTP_USER" != "$APP_USER" ] && [ "$SFTP_USER" != root ] || die "SFTP_USER must be the client's own login, not '$SFTP_USER'."
[[ "$DROP" =~ ^(/[A-Za-z0-9._-]+)+$ ]] || die "DROP_DIR must be a full path such as /backend/fileupload (letters, digits, . _ - only; got '$DROP')."
case "$DROP/" in */../*|*/./*) die "DROP_DIR can't contain . or .. parts." ;; esac
case "$DROP/" in
  /etc/*|/usr/*|/bin/*|/sbin/*|/lib/*|/lib64/*|/proc/*|/sys/*|/dev/*|/boot/*|/root/*|/run/*|/var/lib/*|"$APP_DIR"/*)
    die "DROP_DIR can't be inside a system folder or the app's own folder ($DROP)." ;;
esac

# ---------------------------------------------------------------- 1. who may log in, and how
if [ "$EXISTING_LOGIN" = yes ]; then
  id -u "$SFTP_USER" >/dev/null 2>&1 || die "EXISTING_LOGIN=yes needs the client's existing login: SFTP_USER=<login> (there's no user '$SFTP_USER')."
  for v in CLIENT_IPS CLIENT_KEY_FILE PASSWORD_LOGIN NEW_PASSWORD; do
    [ -z "${!v:-}" ] || warn "$v is ignored with EXISTING_LOGIN=yes: the existing login's settings stay as they are."
  done
else
  MARKS=$(awk -v b="$BEGIN_MARK" -v e="$END_MARK" '$0 == b { nb++ } $0 == e { ne++ } END { print nb + 0, ne + 0 }' "$SSHD_CONFIG")
  case "$MARKS" in
    "0 0"|"1 1") ;;
    *) die "$SSHD_CONFIG has an incomplete or repeated AMS SFTP block for $SFTP_USER (BEGIN/END count: $MARKS). Fix it by hand first." ;;
  esac
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
fi

# ---------------------------------------------------------------- 2. account and folders
say "Setting up $DROP for the login '$SFTP_USER'"
getent group "$SFTP_GROUP" >/dev/null || groupadd --system "$SFTP_GROUP"
if [ "$EXISTING_LOGIN" = yes ]; then
  usermod -aG "$SFTP_GROUP" "$SFTP_USER"   # the existing login can now use the shared folders
  if [ ! -e "$DROP" ]; then
    mkdir -p "$DROP"
    chmod 755 "$DROP"
  fi
  [ -d "$DROP" ] && [ ! -L "$DROP" ] || die "$DROP isn't a folder."
else
  if id -u "$SFTP_USER" >/dev/null 2>&1; then
    case "$(getent passwd "$SFTP_USER" | cut -d: -f7)" in
      */nologin|*/false) usermod -g "$SFTP_GROUP" "$SFTP_USER" ;;
      *) die "A normal login called '$SFTP_USER' already exists. If it's the client's existing SFTP login, run with EXISTING_LOGIN=yes; otherwise pick another name: SFTP_USER=amsupload" ;;
    esac
  else
    useradd --system --no-create-home --home-dir / --shell /usr/sbin/nologin --gid "$SFTP_GROUP" "$SFTP_USER"
  fi
  # SSH locks the login into DROP_DIR only if every folder on its path is owned by root and writable by nobody
  # else. Missing folders are created that way; existing ones are checked, never changed (they may be in use).
  path=""
  IFS='/' read -r -a parts <<< "${DROP#/}"
  for part in "${parts[@]}"; do
    path="$path/$part"
    [ ! -L "$path" ] || die "$path is a symbolic link; SSH can't lock a login into a folder through it."
    if [ ! -e "$path" ]; then
      mkdir "$path"
      chown root:root "$path"
      chmod 755 "$path"
    fi
    [ -d "$path" ] || die "$path isn't a folder."
    owner=$(stat -c '%U' "$path")
    mode=$(stat -c '%a' "$path")
    if [ "$owner" != root ] || [ $(( 0$mode & 022 )) -ne 0 ]; then
      die "$path is owned by '$owner' with permissions $mode. SSH can only lock the client's login into $DROP if every
folder on that path is owned by root and not writable by group or others. Either choose another DROP_DIR, or, if the
client already has an SFTP login that uploads there, run with EXISTING_LOGIN=yes SFTP_USER=<that login>."
    fi
  done
fi
usermod -aG "$SFTP_GROUP" "$APP_USER"   # the app reads, renames and writes files in the client's folders
for sub in $SUBFOLDERS; do
  p="$DROP/$sub"
  [ ! -L "$p" ] || die "$p is a symbolic link; refusing to use it."
  mkdir -p "$p"
  chown "root:$SFTP_GROUP" "$p"   # owned by root, so the client can't change the folder's permissions
  chmod 2770 "$p"                 # client + app (both in the group) only; new files inherit the group
done

# ---------------------------------------------------------------- 3. SSH rules
EFF=$("$SSHD" -T -C "user=$SFTP_USER,host=client.check,addr=${DENY_PROBE}" 2>&1 || true)
if [ "$EXISTING_LOGIN" = no ]; then
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
fi
PORT=$(printf '%s\n' "$EFF" | awk '$1 == "port" { print $2 }' | paste -sd, -)
case "$EFF" in
  *"allowusers "*|*"allowgroups "*) warn "This server's SSH only admits listed users (AllowUsers/AllowGroups). Make sure '$SFTP_USER' is allowed, or the client can't log in." ;;
esac

# What the client sees after logging in: a locked-in login sees its folder as "/".
CHROOT=$(printf '%s\n' "$EFF" | awk '$1 == "chrootdirectory" { print $2 }')
CHROOT=${CHROOT//%u/$SFTP_USER}
if [ -z "$CHROOT" ] || [ "$CHROOT" = none ]; then
  VISIBLE=$DROP
elif [ "$DROP" = "$CHROOT" ]; then
  VISIBLE=""
elif [ "${DROP#"$CHROOT"/}" != "$DROP" ]; then
  VISIBLE="/${DROP#"$CHROOT"/}"
else
  VISIBLE=$DROP
  warn "The login '$SFTP_USER' is locked into $CHROOT, which doesn't contain $DROP, so it can't reach the folders. Ask the vendor, or use another DROP_DIR."
fi

# ---------------------------------------------------------------- 4. the app
say "Connecting the app to the folder"
if grep -q '^DROP_DIR=' "$ENV_FILE"; then
  CURRENT=$(sed -n 's/^DROP_DIR=//p' "$ENV_FILE" | tail -n 1)
  CURRENT=${CURRENT%%#*}; CURRENT=${CURRENT//\"/}; CURRENT=${CURRENT//\'/}; CURRENT=${CURRENT%"${CURRENT##*[![:space:]]}"}
  if [ "$CURRENT" != "$DROP" ]; then
    sed -i "s|^DROP_DIR=.*|DROP_DIR=$DROP|" "$ENV_FILE"
    echo "Set DROP_DIR=$DROP in $ENV_FILE${CURRENT:+ (it was $CURRENT)}"
  fi
else
  printf '\n# The client'"'"'s SFTP folder (set by setup_sftp.sh)\nDROP_DIR=%s\n' "$DROP" >> "$ENV_FILE"
  echo "Added DROP_DIR=$DROP to $ENV_FILE"
fi
chown "$APP_USER:" "$ENV_FILE" && chmod 600 "$ENV_FILE"   # as setup_server.sh leaves it
for who in "$APP_USER" "$SFTP_USER"; do
  for sub in $SUBFOLDERS; do
    runuser -u "$who" -- test -r "$DROP/$sub" -a -w "$DROP/$sub" -a -x "$DROP/$sub" \
      || die "The account '$who' can't use $DROP/$sub. Check every folder on the path is readable: ls -ld $DROP $DROP/$sub; id $who"
  done
done
systemctl restart ams-backend ams-dashboard   # picks up DROP_DIR and the new group membership

BACKEND_PORT=$(sed -n 's/.* --port \([0-9][0-9]*\).*/\1/p' "$UNIT" 2>/dev/null || true)
BACKEND_PORT=${BACKEND_PORT:-8000}   # the port setup_server.sh chose
OK=0
for _ in $(seq 1 45); do
  HEALTH=$(curl -fsS --max-time 5 "http://127.0.0.1:$BACKEND_PORT/health" 2>/dev/null || true)
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

# ---------------------------------------------------------------- 5. what to send the client
FOLDERS="  Folders  : ${VISIBLE}/inbox    upload candidate files here (.csv or .xlsx, with the header row)
             ${VISIBLE}/send     the output sheet: <file>_<campaign>_RESULTS.csv while calls run, then _FINAL.csv
             ${VISIBLE}/failed   <file>_<campaign>_MISSED_CALLS.csv (candidates not reached), and files that
                         can't be used, each with a _ERROR.txt note saying what to fix"
if [ "$EXISTING_LOGIN" = yes ]; then
  say "Done"
  cat <<EOF
The client keeps using their existing SFTP login '$SFTP_USER' (its password, key and allowed addresses are unchanged).
It needs to log in again once, so the new folder access applies. Send them:

$FOLDERS

Folder here : $DROP  (DROP_DIR in $ENV_FILE)
SSH port    : $PORT
EOF
  exit 0
fi

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
UFW=$(ufw status 2>/dev/null || true)
case "$UFW" in "Status: active"*) warn "ufw is active here: allow port $PORT from the client, e.g. sudo ufw allow from 203.0.113.10 to any port $PORT proto tcp" ;; esac

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
  Server   : <the address the client reaches this server at>
  Port     : $PORT
  Username : $SFTP_USER
  $LOGIN
$FOLDERS

Allowed from : ${IPS:-ANY address (run again with CLIENT_IPS=... to restrict it)}
Key login    : $HAVE_KEY      Password login: $PASSWORD_LOGIN
Folder here  : $DROP  (DROP_DIR in $ENV_FILE; the client sees it as /)
SSH backup   : $BACKUP

The firewall (and the vendor's network, if it forwards the port) must allow TCP port $PORT from the client's addresses.
Test from an allowed address:  sftp -P $PORT $SFTP_USER@<server>   then  ls  and  put test.csv inbox/
Login attempts are logged:     journalctl -u ssh --since today | grep $SFTP_USER
Recommended with password login: sudo apt install fail2ban  (blocks repeated wrong passwords)
EOF
[ -n "$IPS" ] || warn "Any address on the internet can try to log in as $SFTP_USER. Restrict it once you have the client's IP: sudo CLIENT_IPS=... bash setup_sftp.sh"
