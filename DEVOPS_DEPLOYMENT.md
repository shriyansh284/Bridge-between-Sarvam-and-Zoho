# AMS Calling Automation: server setup for DevOps

The app reads candidate lists from a Zoho Mail folder, places AI phone calls through Sarvam, fetches each call's
result from Sarvam, and emails the result file back. It is a small Python service: a backend and an internal
dashboard. It has no database: all state is kept in files in `data/`.

**The app makes outgoing connections only.** No domain, no HTTPS certificate and no open inbound ports are needed.
The one exception is optional: the client's SFTP folder (below) needs this server's SSH port reachable from the
client's IP addresses only.

## What we need from you

A Linux server (Ubuntu 22.04/24.04 or Debian 12) with:

- 1–2 vCPU, 2 GB RAM, 20 GB **persistent** disk. `data/` must survive restarts and redeploys.
- Outbound HTTPS (port 443) to `apps.sarvam.ai`, `accounts.zoho.in` and `mail.zoho.in`.
- The clock synced with NTP: calls are placed at scheduled times. The app uses India time internally, whatever the server's time zone.
- SSH access for our team. The dashboard is reached through an SSH tunnel, so it is never public.

## Setup: one script

On the server, as a user with sudo:

```bash
curl -fsSL https://raw.githubusercontent.com/shriyansh284/Bridge-between-Sarvam-and-Zoho/main/deploy/setup_server.sh -o setup_server.sh
sudo bash setup_server.sh
```

1. **First run:** it installs Python, downloads the code to `/opt/ams-calling`, then stops and asks for the two secret
   files. We'll send `.env` and `config/users.json` through a secure channel (they are not in git; don't commit or
   email them). Copy them as the script shows (`scp` to your home folder, then `sudo install -m 600 …`).
   Don't copy the project folder itself from anyone's PC: the script downloads the code, and a copied `data/` folder
   would make old emails start real calls (the script refuses it).
2. **Second run** (`sudo bash setup_server.sh` again): it starts both services (bound to 127.0.0.1 only), schedules a
   daily backup of `data/` to `/var/backups/ams-calling` (a systemd timer, kept 14 days), checks the Sarvam key and the
   Zoho mailbox, and prints a summary.

If the server was created minutes ago, the script first waits for the automatic updates that new cloud servers run,
so the first run can take a few extra minutes.

Every step is commented in [deploy/setup_server.sh](deploy/setup_server.sh) if you'd like to review it or do it by hand.

## Client SFTP folder

The client wants to upload candidate files over SFTP, and collect the results the same way, instead of by email.
Once the app is running (above), run on the same server:

```bash
curl -fsSL https://raw.githubusercontent.com/shriyansh284/Bridge-between-Sarvam-and-Zoho/main/deploy/setup_sftp.sh -o setup_sftp.sh
sudo CLIENT_IPS="<client's public IP>" bash setup_sftp.sh
```

- **What it sets up:** an SFTP-only login, `amsclient`, locked into `/srv/ams-sftp/amsclient` with the folders
  `inbox/ processed/ results/ errors/`. It can't open a shell or forward ports, and it sees nothing else on the server.
  The app reads and writes those folders through a shared group (`amsdrop`). Nothing new is installed: it uses the
  server's own OpenSSH.
- **Your SSH access is safe.** The rules are added at the end of `/etc/ssh/sshd_config`, only for that login. They are
  checked with `sshd -t` and `sshd -T` on a copy before the real file is replaced; any problem leaves the file
  unchanged. SSH is then reloaded, not restarted, so open sessions are never dropped. A backup of the old file is kept
  next to it.
- **Login:** by default it creates a password and prints it once. For an automated upload, the client sends an SSH
  public key: `sudo CLIENT_IPS=... CLIENT_KEY_FILE=/tmp/client_key.pub bash setup_sftp.sh` (password login is then
  off unless `PASSWORD_LOGIN=yes`).
- **Only the client's addresses** (`CLIENT_IPS`, comma-separated, ranges allowed) can log in as `amsclient`. Also allow
  TCP port 22 (or your SSH port) from those addresses in the firewall or cloud security group. `CLIENT_IPS=any` allows
  every address, for a first test only.
- **Re-running is safe:** it keeps the folders, files, password and allowed addresses unless you pass new ones
  (`NEW_PASSWORD=1`, `CLIENT_IPS=...`).
- It adds `DROP_DIR=/srv/ams-sftp/amsclient` to `/opt/ams-calling/.env` and restarts the app. (The `.env` we send
  already has the client's date order, `DROP_DATE_ORDER=mdy`.)

Send the summary it prints (server, port, username, folders) to us. Send the password through a separate channel.

## Dashboard access (internal only)

```bash
ssh -L 8501:127.0.0.1:8501 <user>@<server>     # then open http://localhost:8501 in a browser
```

## Important rules

- **Run exactly one instance:** one server, one backend process (`--workers 1`, as the script sets it). No autoscaling,
  replicas or a second container. The app keeps its state in files, so two copies would call the same candidates twice.
- **Never run a second copy with the same `.env`** (for example a staging server, or a developer's PC). It would read
  the same mailbox and call real people. A test copy must use `MAIL_ENABLED=false` and its own Sarvam settings.
- **Keep the backups.** `data/` holds every campaign, call result and sent email. To restore, stop the services,
  unpack a backup into `/opt/ams-calling/data`, and start them again.
- **Restarts are safe:** scheduled calls and pending emails resume after a restart.

## Day-to-day

| Task | Command |
|---|---|
| Logs | `journalctl -u ams-backend -f` and `journalctl -u ams-dashboard -f` |
| Status | `curl -s http://127.0.0.1:8000/health` (on the server) |
| Backups | `ls /var/backups/ams-calling` (timer: `systemctl list-timers ams-calling-backup.timer`) |
| Restart | `sudo systemctl restart ams-backend ams-dashboard` |
| Update the code | Download the script again (same `curl` as above) and `sudo bash setup_server.sh`. It pulls, reinstalls and restarts, and never deletes `data/` or changes `.env`. Always run a freshly downloaded copy, not the one inside `/opt/ams-calling`. |
| Monitoring | Alert if the health check fails, or if `scheduler_heartbeat` in it is older than 2 minutes |
| SFTP logins | `journalctl -u ssh --since today \| grep amsclient`; files in `/srv/ams-sftp/amsclient` |

## Please send back

1. The summary the script prints at the end of the second run (it contains no secrets).
2. Who has SSH access for the dashboard tunnel.
3. Where the backups are kept, if you move them off the server.
4. If you set up the SFTP folder: the summary `setup_sftp.sh` prints, and the server's public IP or name for the client.
