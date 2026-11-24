# AMS Calling Automation: server setup for DevOps

The app reads candidate lists from a Zoho Mail folder, places AI phone calls through Sarvam, fetches each call's
result from Sarvam, and emails the result file back. It is a small Python service: a backend and an internal
dashboard. It has no database: all state is kept in files in `data/`.

**It makes outgoing connections only.** No domain, no HTTPS certificate and no open inbound ports are needed.

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

## Please send back

1. The summary the script prints at the end of the second run (it contains no secrets).
2. Who has SSH access for the dashboard tunnel.
3. Where the backups are kept, if you move them off the server.
