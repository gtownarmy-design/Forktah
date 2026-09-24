# Running Control Center on Windows + WSL

Control Center's dashboard and board live in WSL (`dashboard/server.py`, SQLite at
`~/.agentmux/cc.db`). Two files keep them up across reboots and back the board up.

| File | What |
|---|---|
| `ccc-keepalive.vbs` | Hidden launcher. A shortcut to it in the Startup folder (`shell:startup`) starts the keepalive at logon, with no window. |
| `ccc-keepalive.sh` | The keepalive, in WSL. Keeps WSL up (its `wsl.exe` stays running); restarts the dashboard in `tmux -L ccc` (session `dashboard`) when it stops answering; takes an online, integrity-checked backup of `cc.db` every hour; mirrors the evidence folder. |

## Install

1. Create a shortcut to `ccc-keepalive.vbs` in `shell:startup`.
2. Run it once (double-click) or sign out and in.

Defaults, derived by the `.vbs` from where the checkout is:

| | Default | Override (environment) |
|---|---|---|
| Backups | `%USERPROFILE%\ccc-backups` (`hourly\` 48 kept, `daily\` 14 kept, `latest.json`) | `CCC_BACKUP_DIR`, `CCC_KEEP_HOURLY`, `CCC_KEEP_DAILY` |
| Evidence mirrored from | `%USERPROFILE%\ccc-evidence` | `CCC_EVIDENCE_DIR` |
| WSL distribution | `Ubuntu` | `CCC_WSL_DISTRO` |
| Check / backup interval | 60 s / 3600 s | `CCC_KEEPALIVE_INTERVAL`, `CCC_BACKUP_EVERY` |

## By hand (from WSL, repo root)

```sh
export CCC_REPO=$PWD CCC_BACKUP_DIR=/mnt/c/Users/<you>/ccc-backups
bash <(tr -d '\r' < deploy/windows/ccc-keepalive.sh) --status      # what it sees
bash <(tr -d '\r' < deploy/windows/ccc-keepalive.sh) --backup-now  # one backup now
bash <(tr -d '\r' < deploy/windows/ccc-keepalive.sh) --once        # one repair pass
```

Log: `~/.local/state/ccc-keepalive/keepalive.log`.

## Restoring a backup

Stop the dashboard (`tmux -L ccc kill-session -t dashboard`), delete `cc.db-wal` and
`cc.db-shm` next to `cc.db`, copy the backup over `cc.db` (a real copy - the server
refuses a symlinked or hard-linked database), then start the dashboard again (the
keepalive does it within a minute).
