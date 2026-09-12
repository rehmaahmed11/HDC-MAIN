# PythonAnywhere deployment

This directory contains the repeatable deployment path for the HDC ERP
application.

## One-time setup

1. Clone the repository on PythonAnywhere, preferably at
   `/home/yourname/HDC-MAIN`.
2. Create/use a virtualenv at `/home/yourname/.virtualenvs/hdc` and install
   `requirements.txt`.
3. Keep production data outside Git, for example:

   ```text
   /home/yourname/HDC_INSTANCE/hdc_erp.db
   /home/yourname/HDC_INSTANCE/project_estimations.json
   /home/yourname/HDC_INSTANCE/stage_drawings/
   ```

   If the old database is already on PythonAnywhere, point `HDC_DB_PATH` at
   it. Otherwise copy the old database and instance files there once.
4. Copy `production.env.example` to
   `/home/yourname/.config/hdc/production.env`, replace placeholders, and run:

   ```bash
   chmod 600 /home/yourname/.config/hdc/production.env
   ```
5. Configure the PythonAnywhere WSGI file using `wsgi_config.example.py`.
6. Copy the deployment script outside the repository, or call it directly
   from this checkout:

   ```bash
   cp ops/pythonanywhere/deploy.sh /home/yourname/deploy_hdc.sh
   chmod +x /home/yourname/deploy_hdc.sh
   ```
7. Run the first deployment from a Bash console:

   ```bash
   bash /home/yourname/deploy_hdc.sh
   ```

   The script creates a database backup, pulls the selected branch, installs
   packages, runs syntax/layer checks, starts the app to apply all schema
   heals and migrations, and performs a login health check. If
   `HDC_WSGI_FILE` is set, touching that file requests a PythonAnywhere reload;
   otherwise click **Reload** on the Web tab.

## Every later deployment

After merging approved changes into `main`:

```bash
bash /home/yourname/deploy_hdc.sh
```

The production database is not pulled from Git and is not replaced by this
script. It stays at `HDC_DB_PATH`. A timestamped SQLite backup is created
before every deployment. If any check or migration fails, the script exits
without requesting a reload.

## Branch policy

Deploy `main`, not an unreviewed feature branch. Set
`HDC_DEPLOY_BRANCH` only when intentionally testing another branch.

## Free versus paid PythonAnywhere

On a free account, run the script from the PythonAnywhere Bash console. The
safe fallback is to click **Reload** manually after it succeeds.

On a paid account with SSH, GitHub Actions can connect to PythonAnywhere and
run this same script after CI passes. Do not expose a public unauthenticated
`/deploy` route inside the ERP application.

## Rollback

The script keeps the database backup, but database migrations may be
forward-only. For a failed deployment:

1. Do not run another migration against the live database.
2. Restore the previous code commit.
3. Restore the timestamped database backup only if the migration changed the
   database and the old code requires the old schema.
4. Reload the Web app and inspect the PythonAnywhere error log.
