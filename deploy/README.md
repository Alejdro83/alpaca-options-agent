# Deploy notes

## spread_monitor.py — systemd, not cron

`spread_monitor.py` runs forever by design (a persistent WebSocket
connection, `while self._running`) — it never exits on its own. A
daily-trigger cron job is the wrong execution model for that (real bug
found 2026-08-28: a cron job for this had the raw script content pasted
into the wrong field and failed every run since creation, so the monitor
had never actually run in production despite being reviewed and fixed the
day before).

Deployed instead as a `systemd --user` service, per the module's own
docstring ("designed to run as a systemd service or background process"):

```bash
cp run_spread_monitor.sh /home/lab-master/alpaca-options-agent/
chmod +x /home/lab-master/alpaca-options-agent/run_spread_monitor.sh
mkdir -p ~/.config/systemd/user
cp deploy/alpaca-spread-monitor.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now alpaca-spread-monitor.service
```

Requires `loginctl show-user <user>` to report `Linger=yes` so the user
service keeps running without an active login session
(`loginctl enable-linger <user>` if not).

Logs: `state/spread_monitor.log` (the module's own structured log) and
`state/spread_monitor.stdout.log` (raw stdout/stderr, mostly redundant
with the above — kept for whatever the structured logger doesn't catch).

`bot.py` itself stays on the existing 15-minute Hermes cron
(`run_options_cron.sh`) — it's a finite script that's supposed to run,
finish, and report status each time, which is exactly what cron is for.
