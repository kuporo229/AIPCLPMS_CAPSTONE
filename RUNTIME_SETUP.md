# LPMS Runtime Setup

This repo only needs the runtime application files, dependency list, database migration files, and environment settings to run. Keep real secrets and uploaded/generated documents out of git.

## Files and folders needed to run

- `app/` - Flask blueprints, services, templates, static files, and the bundled CLP template.
- `app.py` - local development entrypoint.
- `wsgi.py` - production/uWSGI entrypoint.
- `uwsgi.ini` - uWSGI service configuration for the current production app server.
- `run_prod.py` - alternate production runner.
- `requirements.txt` - Python dependencies.
- `db/migrations/` - database migration SQL files.
- `db/lpms_current_full_database.dump` - current full PostgreSQL dump for restoring the system database.
- `license_manager.py` - license helper used by auth/license flows.
- `license_state.json` - current local license state if this deployment expects it.
- `local_storage/` - runtime storage folder for uploaded and generated files. Create it on the server, but do not commit its contents.
- `.env.example` - template for creating the real `.env` file.

## Files and folders not needed for runtime

- `.pi/`, `.codex/`, `.lean-ctx/`, `.cursor/`, `.github/agents/` - local agent/editor tooling.
- `tests/`, `scratch/`, `revert/`, `scripts/`, `conductor/` - development, QA, planning, or migration helpers.
- `*.md` planning/report files - documentation only, except this setup guide.
- `.DS_Store`, `__pycache__/`, `*.pyc`, generated dumps, extracted JSON, backup SQL, screenshots, and temporary samples.

## Create `.env`

1. Copy `.env.example` to `.env`.
2. Replace every `change-me...` placeholder with a real value.
3. Keep `.env` private. It is intentionally ignored by git.

## Where to get each env value

- `FLASK_SECRET_KEY`: generate locally with `python -c "import secrets; print(secrets.token_urlsafe(48))"`.
- `GEMINI_API_KEY`: create an API key in Google AI Studio, then paste it here.
- `DATABASE_URL`: use the PostgreSQL connection string for the LPMS database. Format: `postgresql://USER:PASSWORD@HOST:PORT/DB_NAME`.
- `FLASK_DEBUG`: use `False` on production and `True` only for local development.
- `SESSION_COOKIE_SECURE`: use `True` when the site is served over HTTPS; use `False` only for plain local HTTP.
- `PUBLIC_APP_URL`: the public URL users open in the browser, for example `https://aipclpms.example.com`.
- `LPMS_STORAGE_ROOT`: absolute path to the server folder used for uploads/generated DOCX files, normally `/home/<user>/lpms/local_storage`.
- `STORAGE_BUCKET_NAME`: keep `clp_files` unless the code and existing storage paths are changed together.
- `ONLYOFFICE_API_JS_URL`: the ONLYOFFICE Document Server API script URL. Usually `https://<onlyoffice-host>/web-apps/apps/api/documents/api.js`.
- `ONLYOFFICE_CALLBACK_URL`: the public LPMS base URL reachable by ONLYOFFICE callbacks. Usually the same value as `PUBLIC_APP_URL`.
- `ONLYOFFICE_INTERNAL_APP_URL`: internal URL ONLYOFFICE can use to reach Flask/uWSGI, for example `http://127.0.0.1:3000` or the service URL inside Docker/networking.
- `ONLYOFFICE_JWT_SECRET`: copy the JWT secret configured in ONLYOFFICE Document Server. It must match exactly.
- `ONLYOFFICE_FORCE_AI_PLUGIN_SETTINGS`: set `true` only when forcing LPMS AI plugin settings into ONLYOFFICE.
- `ONLYOFFICE_FORCE_AI_PLUGIN_MODEL_OVERRIDES`: set `true` only when forcing model overrides into the ONLYOFFICE AI plugin.
- `DEEPSEEK_API_KEY`: optional. Add only if admin settings select DeepSeek as the AI provider.
- `TIPTAP_*`: optional. Fill these from the Tiptap Cloud dashboard only if document conversion/collaboration features are enabled.
- `LOUIS_LICENSE_KEY` and `LOUIS_LICENSE_API`: obtain from the license provider/admin who issued this LPMS deployment.
- `LOUIS_LICENSE_ENFORCEMENT_ENABLED`: use `true` only when production license enforcement should block access without a valid license.
- `LOCAL_MIGRATION_DEFAULT_PASSWORD`: optional helper password for local migration scripts; leave blank if not running those scripts.
- `SUPABASE_URL`, `SUPABASE_KEY`, `SUPABASE_SERVICE_KEY`: legacy compatibility names. This deployment uses the local Supabase-compatible layer, so `SUPABASE_KEY=local` and `SUPABASE_SERVICE_KEY=local-service` are enough unless the code is changed back to real Supabase.

## Server folder setup

Create the storage folder before first use:

```bash
mkdir -p local_storage/clp_files
```

For production, make sure the OS user running `aipclpms` can read and write that folder.

## Minimal start check

After `.env`, database, and `local_storage/` are ready:

```bash
pip install -r requirements.txt
python app.py
```

For the current systemd deployment, restart after changes with:

```bash
systemctl restart aipclpms
```
