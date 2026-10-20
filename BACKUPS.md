# Backups

Short version: **a backup you have not restored is a hypothesis, not a
backup.** Everything below is aimed at making the other half automatic.

---

## 1. What a backup actually contains

One `.zip` per point in time, written to `<instance>/backups/`
(`hdc_instance/backups/` by default):

| Member | What it is | Needed to restore |
| --- | --- | --- |
| `hdc_erp.db` | Consistent SQLite snapshot taken with the **online backup API**, so it is coherent even while people are entering data, and WAL content is folded in | **yes** |
| `project_estimations.json` | The legacy estimation store that lives outside the DB | if you use it |
| `hdc_data_export.xlsx` | Every table as a sheet. Human-readable, deliberately *not* a restore source | no |
| `files/tool-images/…` | Tool photos | no — but you will miss them |
| `files/stage_drawings/…` | Stage drawings | no — but you will miss them |
| `backup_manifest.json` | Schema version, kind, when it was taken, and a **SHA-256 of every member** | no — but it is what lets you *prove* the archive is intact |

Archives written before the manifest existed still restore; they are simply
reported as *legacy* and skip the checksum step.

## 2. Making one

**By hand:** Settings → Backup → *Create Backup Now*.

**From a scheduled task** (this is the one that matters):

```bash
cd ~/HDC-MAIN
python3 scripts/hdc_backup.py create --prune          # nightly
python3 scripts/hdc_backup.py create --no-xlsx --no-files --prune   # hourly, small
```

`create` writes the archive, then **verifies it immediately** and fails the
run (exit 1) if the archive does not check out. A backup job that cannot
notice its own failure is worse than no job.

## 3. Scheduling it

### PythonAnywhere scheduled task

Tasks → *Create a scheduled task*:

```
python3 /home/<you>/HDC-MAIN/scripts/hdc_backup.py create --prune
```

Daily at a quiet hour (02:00 PKT is good — the night shift is not entering
data). **Free accounts created on/after 2026-01-15 no longer include
scheduled tasks**; they are part of the paid Developer plan. Older free
accounts keep one daily task. If your account has none, either upgrade or
run the command from a cron job on any other machine (see below).

### A cron job somewhere else

```cron
0 2 * * * cd /srv/HDC-MAIN && python3 scripts/hdc_backup.py create --prune >> /var/log/hdc-backup.log 2>&1
```

The script never boots the Flask app, so it cannot run migrations, hold a
write transaction, or fail because a template changed. Overlap is handled: a
`.hdc-backup.lock` file makes a second concurrent run skip instead of
doubling up, and a lock older than two hours is reclaimed automatically.

### Environment knobs

| Variable | Default | Meaning |
| --- | --- | --- |
| `HDC_BACKUP_KEEP_RECENT` | `10` | always keep the newest N, calendar be damned |
| `HDC_BACKUP_KEEP_DAILY` | `7` | then one per day |
| `HDC_BACKUP_KEEP_WEEKLY` | `4` | then one per week |
| `HDC_BACKUP_KEEP_MONTHLY` | `6` | then one per month |
| `HDC_BACKUP_MAX_BYTES` | `0` (off) | hard cap on total backup bytes |
| `HDC_INSTANCE_DIR` / `HDC_DB_PATH` | `<repo>/hdc_instance` | where the data lives |

## 4. Retention: a ladder, not "the last ten clicks"

A flat "keep the newest 10" has a nasty failure mode: ten backups taken in
one afternoon while fixing a bug destroy *months* of history. Retention is
now grandfather-father-son — the newest N outright, then one surviving
archive per day, per week, per month. `HDC_BACKUP_MAX_BYTES` adds a hard
ceiling if disk is tight; **the single newest archive is never deleted**,
however small the cap.

The Settings → Cleanup button applies the same ladder.

> Free PythonAnywhere accounts have **512 MB of total disk**, so a byte cap
> is not paranoia there. Settings shows free space and warns when backups
> pass 40 % of the disk or free space drops below 128 MB.

## 5. Get one copy off this server

Nothing in this repository can do this for you, and it is the gap that
matters most: **backups live next to the database on the same host.** Host
loss, account loss, or a disk failure takes both.

Target 3-2-1: **3** copies, **2** kinds of media, **1** off-site.

```bash
# rclone (Drive / OneDrive / B2 / any S3-compatible store) — cheapest real answer
rclone sync ~/HDC-MAIN/hdc_instance/backups remote:hdc-backups --bwlimit 2M

# or, if you have nothing else: copy it to a laptop or a USB drive weekly
```

Encrypt off-site copies. A backup contains the entire financial ledger and
the password hashes — it is not a file to leave in a consumer Drive folder.

## 6. Prove it: verify and drill

```bash
python3 scripts/hdc_backup.py verify hdc_instance/backups/hdc_backup_*.zip
python3 scripts/hdc_backup.py status        # freshness + disk, exits 1 if stale
python3 scripts/hdc_backup.py list
```

`status` is also the monitoring hook: exit 1 and `NEEDS ATTENTION` when no
backup exists or the newest is older than `--max-age-hours` (26 h default).
Point any uptime checker at it.

**Quarterly drill** — restore into a throwaway instance, never into
production:

```bash
mkdir -p /tmp/hdc-drill
python3 scripts/hdc_backup.py --instance /tmp/hdc-drill --db /tmp/hdc-drill/drill.db \
    status
HDC_INSTANCE_DIR=/tmp/hdc-drill HDC_DB_PATH=/tmp/hdc-drill/drill.db \
    python3 -c "
from hdc.core.admin import _restore_from_backup_zip
from hdc.app import create_app
app = create_app()
with app.app_context():
    print(_restore_from_backup_zip('hdc_instance/backups/<newest>.zip'))
"
# then log in there and check a few screens
```

Fifteen minutes, and it is the difference between "we have backups" and "we
have restores".

## 7. Automatic safety snapshots

Before anything destructive, HDC now takes its own archive — named
`hdc_preflight_<reason>_<timestamp>_<hex>.zip`, fast path (no workbook):

| Trigger | Reason slug |
| --- | --- |
| Restore (saved archive or upload) | `restore` |
| Wipe data | `wipe` |
| Deploy (`/deploy` webhook) | `deploy` |

These never block the operation they protect. If a snapshot fails you get a
loud warning and can decide, rather than being locked out of a restore
because the backup machinery is the thing that is broken.

The deploy snapshot is armed by `HDC_PRE_DEPLOY_BACKUP=1`, which
`wsgi_dispatch_snippet.py` sets via `os.environ.setdefault` (and the
installer copies into your WSGI file). Set it to `0` to turn it off.

## 8. What is *not* covered

- **The off-site copy.** See §5. This is the one remaining manual step.
- **Point-in-time recovery.** Snapshots are discrete; if you need recovery to
  the second, look at Litestream (continuous SQLite replication to S3/B2).
- **An audit trail.** Snapshots give disaster recovery, not replay. For money
  data, an append-only change log is the complement worth considering.
- **Concurrency.** Restores still assume other workers are quiesced. On a
  single-worker PythonAnywhere app this is fine; the file-level safety
  (validate → snapshot → atomic swap → roll back on failure) is unchanged.

## 9. Quick reference

```bash
python3 scripts/hdc_backup.py create --prune                 # take one, then tidy
python3 scripts/hdc_backup.py create --no-xlsx --no-files    # small and fast
python3 scripts/hdc_backup.py list                           # what have we got
python3 scripts/hdc_backup.py verify <archive> [<archive>…]  # is it restorable
python3 scripts/hdc_backup.py status [--json] [--exit-zero]  # are we protected
python3 scripts/hdc_backup.py prune --max-bytes 209715200    # cap at 200 MB
python3 scripts/hdc_backup.py --instance /path --db /path/db.db status
```

Exit status is `0` on success and `1` on failure, so a scheduled task that
breaks shows up as a failed task instead of a silent gap in your history.

## 10. Tests

```bash
python -m unittest discover -s tests -p 'test_backup_hardening.py'   # this work
python -m unittest discover -s tests -p 'test_recovery.py'           # safety gates
```

`test_backup_hardening.py` covers archive contents, manifest checksums and
tamper detection, legacy archives, the retention ladder and byte cap,
pre-flight snapshots, the settings screen, and the CLI.
`test_recovery.py` still pins the original guarantees: corrupt, empty,
partial, orphan and path-traversal archives are rejected **before** live data
is touched.
