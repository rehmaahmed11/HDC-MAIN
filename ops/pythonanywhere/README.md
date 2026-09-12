# PythonAnywhere deployment

This directory contains the repeatable deployment path for the HDC ERP
application, plus the GitHub **webhook** that runs it automatically.

A deploy updates **all tracked code** and **no data**. The SQLite database,
`project_estimations.json`, `stage_drawings/` and backups are never pulled from
Git, never `git clean`ed, and never replaced -- and `scripts/check_db_safety.py`
fails the deploy if a commit ever tries to put a database *into* Git.

```
git push origin main  ->  GitHub "push" webhook
        ->  https://<you>.pythonanywhere.com/deploy/github   (deploy_receiver.py)
              verifies X-Hub-Signature-256, ref, repo; answers 202 at once
        ->  detached: bash ops/pythonanywhere/deploy.sh
              backup DB -> refuse unsafe commits -> checkout code -> pip (only if
              requirements.txt changed) -> checks -> migrations -> touch WSGI = reload
        ->  state in $HDC_INSTANCE_DIR/deploy/{deploy_state.json,deploy.log}
```

## Webhook setup (works on a free account)

Free accounts have **one web app** and no inbound SSH, so the receiver is
mounted beside the ERP by the WSGI file rather than run as its own site. No
open `/deploy` route is added to the ERP app itself.

1. **Bash console** -- clone, venv, instance dir, one manual deploy first:

   ```bash
   git clone https://github.com/rehmaahmed11/HDC-MAIN.git ~/HDC-MAIN
   python3.11 -m venv ~/.virtualenvs/hdc
   ~/.virtualenvs/hdc/bin/pip install -r ~/HDC-MAIN/requirements.txt
   mkdir -p ~/HDC_INSTANCE
   # move the existing live DB / instance files into ~/HDC_INSTANCE once
   ```

   HTTPS remotes only: free accounts may connect out to allowlisted hosts over
   HTTP(S) (`github.com` is on that list); `git@github.com:` SSH remotes will
   not work.

2. **Config file** -- copy `production.env.example` to
   `~/.config/hdc/production.env`, replace the placeholders, then:

   ```bash
   chmod 600 ~/.config/hdc/production.env
   python3 -c "import secrets; print(secrets.token_hex(32))"   # webhook secret
   python3 -c "import secrets; print(secrets.token_hex(32))"   # HDC_DEPLOY_TOKEN
   ```

   Set `HDC_DEPLOY_WEBHOOK_SECRET`, `HDC_DEPLOY_REPO=rehmaahmed11/HDC-MAIN`,
   `HDC_DEPLOY_BRANCH=main` there. Keep `HDC_DEPLOY_ALLOW_MANUAL=0` until you
   need manual triggers.

3. **Web tab** -- point the WSGI configuration at
   `ops/pythonanywhere/wsgi_deploy_with_receiver.example.py` (paste it, replace
   `yourname`) and Reload. Check it landed:

   ```bash
   curl -s https://<you>.pythonanywhere.com/deploy/health
   # {"status":"ok","deploy_enabled":true,...}  <- "disabled" means the env file
   #                                               was not picked up; read reason
   ```

4. **GitHub** -- Settings → Webhooks → **Add webhook**:

   | field | value |
   |---|---|
   | Payload URL | `https://<you>.pythonanywhere.com/deploy/github` |
   | Content type | `application/json` |
   | Secret | the `HDC_DEPLOY_WEBHOOK_SECRET` value |
   | Events | **Just the push event** (branch filter: `main`) |

   Or from a terminal, once you know the hook secret:

   ```bash
   gh api repos/rehmaahmed11/HDC-MAIN/hooks -X POST \
     -f name=web -F active=true -f 'events[]=push' \
     -f 'config[url]=https://<you>.pythonanywhere.com/deploy/github' \
     -f 'config[content_type]=json' \
     -f "config[secret]=$HOOK_SECRET" -f 'config[insecure_ssl]=0'
   ```

5. **Test it** -- push a harmless commit and watch:

   ```bash
   curl -s https://<you>.pythonanywhere.com/deploy/health | python3 -m json.tool
   tail -40 ~/HDC_INSTANCE/deploy/deploy.log
   ```

   Redeliveries of the same commit are cheap: the receiver ignores pushes for
   `HDC_DEPLOY_COOLDOWN_SECONDS` (45) and `deploy.sh` exits early when the
   checkout already sits on a successfully deployed commit.

## What a deploy does

`ops/pythonanywhere/deploy.sh` is the only thing that touches the server, and
it is safe to run by hand at any time:

1. Take a lock (`/tmp/hdc-erp-deploy.lock`) so two deploys cannot overlap.
2. `git fetch`, then compare `origin/<branch>` with `HEAD`.
3. **Refuse** if the incoming commit (or the current index) tracks a `.db`,
   `.sqlite`, `-wal`, archive or backup file, or if `HDC_DB_PATH` /
   `HDC_INSTANCE_DIR` is a tracked path -- `scripts/check_db_safety.py`.
4. Refuse if the checkout has modified tracked files (a hand hotfix on the
   server must be committed to Git first, never silently overwritten).
5. Snapshot the database with SQLite's backup API (consistent even while the
   web worker holds it open) and `PRAGMA integrity_check` it; copy
   `project_estimations.json` and tar `stage_drawings/`.
6. `git checkout` + `git merge --ff-only` the fetched commit. No `git clean`,
   no worktree-wide reset -- untracked data under the checkout survives.
7. `pip install -r requirements.txt`, skipped automatically when
   `requirements.txt` is identical to the deployed version (fast, and it keeps
   code-only pushes off the network).
8. `compileall`, `scripts/check_layers.py`, frontend layout check, JS syntax.
9. Boot `create_app()` once against the live DB to apply schema heals and
   migrations, then `GET /hdc/login` as a health check.
10. Import-check `deploy_receiver.py` and save a last-known-good copy to
    `~/hdc_deploy_receiver.py`, so `/deploy` survives a future bad commit.
11. Write `last-successful-deploy`, then `touch` the WSGI file to reload.

Any failing step stops the script **before** the reload, so the site keeps
serving the previous code; the outcome lands in `deploy_state.json`.

## Manual trigger, rollback, kill switch

Set `HDC_DEPLOY_ALLOW_MANUAL=1` and `HDC_DEPLOY_TOKEN=...` to enable:

```bash
# status with log tail
curl -s -H "Authorization: Bearer $HDC_DEPLOY_TOKEN" \
  https://<you>.pythonanywhere.com/deploy/status

# redeploy branch tip / force a retry
curl -s -X POST -H "Authorization: Bearer $HDC_DEPLOY_TOKEN" \
  -d '{"force":true}' https://<you>.pythonanywhere.com/deploy/trigger

# roll code back to a known commit (databases are never rolled back)
curl -s -X POST -H "Authorization: Bearer $HDC_DEPLOY_TOKEN" \
  -d '{"revision":"<previous-commit>"}' \
  https://<you>.pythonanywhere.com/deploy/trigger
```

After a rollback the next push fast-forwards back onto `main` on its own.
Emergency stop: set `HDC_DEPLOY_DISABLED=1` in `production.env` and Reload --
`/deploy/*` then answers `503` and pushes are ignored while the ERP keeps
running. To roll back or fix things without the webhook, use a Bash console:

```bash
cd ~/HDC-MAIN && HDC_DEPLOY_REVISION=<commit> bash ops/pythonanywhere/deploy.sh
```

## Free-tier limits worth knowing

* **One web worker** serves the ERP *and* `/deploy`, one request at a time. The
  receiver therefore returns `202` immediately and runs the deploy as a
  detached process in its own session -- otherwise the request timeout would
  kill a deploy mid-flight.
* **CPU seconds per day** are capped, so `HDC_PIP_AUTOSKIP`, the cooldown and
  the already-deployed marker are on by default.
* Free accounts get **no inbound SSH**, so GitHub Actions cannot log in; the
  webhook receiver is the supported path. On a paid plan you can either give
  the receiver its own web app (`from deploy_receiver import application`) or
  let Actions SSH in and run the same `deploy.sh`.
* Migrations are forward-only: a failed deploy reloads no code, but if a
  migration already ran, restore the timestamped DB backup in
  `$HDC_BACKUP_DIR` only when the old code truly requires the old schema.

## Branch policy

Deploy `main`, not an unreviewed feature branch. `HDC_DEPLOY_REPO` and the
`refs/heads/main` check mean a hook copied to a fork, or a push to any other
branch, is acknowledged with `200 ignored` and deploys nothing.

## Security notes

* Every `POST /deploy/github` request must carry a valid
  `HMAC-SHA256(secret, body)` in `X-Hub-Signature-256`; comparison is
  constant-time and failures return `401` without echoing the payload.
* The receiver imports nothing from `hdc`: no ORM, no login, no session, no
  CSRF bypass, no database path. `GET /deploy/status` and `POST /deploy/trigger`
  require `Authorization: Bearer $HDC_DEPLOY_TOKEN`.
* `production.env` stays outside the repo with mode `600`; the repo, the logs
  and the state file never contain its contents.
* Set `HDC_DEPLOY_ALLOW_MANUAL=0` (default) unless the manual endpoint is
  needed, and rotate the webhook secret by updating both sides at once.

## Troubleshooting

| symptom | cause and fix |
|---|---|
| `503 {"status":"disabled"}` from `/deploy/health` | env not loaded: check `HDC_DEPLOY_WEBHOOK_SECRET`/`HDC_APP_DIR` in `production.env`, then Reload |
| GitHub delivery `401` | webhook secret differs from `HDC_DEPLOY_WEBHOOK_SECRET` |
| GitHub delivery `404` | wrong payload URL, or the WSGI dispatcher is not mounted at `/deploy` |
| `200 ignored` | branch is not `HDC_DEPLOY_BRANCH`, repo is not `HDC_DEPLOY_REPO`, or cooldown absorbed a redelivery |
| `deploy_state.json` says `lost`/`stale` | the deploy process died without reporting (worker recycled, memory limit); read `deploy.log` and re-run from a Bash console |
| `DB safety FAILED` | a commit tracked a database/archive; `git rm --cached <file>`, keep it in `HDC_INSTANCE_DIR`, push again |
| `git fetch` fails | remote is `git@github.com:` (SSH) -- switch to `https://github.com/rehmaahmed11/HDC-MAIN.git`; private repos need `https://<user>:<token>@github.com/...` |
| `Tracked local changes found` | a hotfix was edited on the server; commit or discard it, then redeploy |
| deploy succeeded but site looks old | `HDC_WSGI_FILE` unset/wrong, so no reload happened; click Reload in the Web tab |
