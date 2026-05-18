# AIPCLPMS

AIPCLPMS is a Flask-based Course Learning Plan Management System for creating, reviewing, editing, storing, and monitoring CLP documents. It supports teacher, dean, and administrator workflows, AI-assisted CLP generation, document editing through ONLYOFFICE Docs, local PostgreSQL storage, and a local file-storage layer for uploaded/generated documents.

The system is designed for school CLP workflows: teachers prepare and submit course plans, deans review and approve them, and administrators manage users, templates, departments, subjects, settings, audit logs, and license status.

## Main Modules

- `Authentication and licensing`: login, signup, sessions, role-based access, and license activation.
- `Teacher module`: course/subject management, CLP creation, AI-assisted CLP generation, document editing, uploads, signatures, and profile settings.
- `Dean module`: faculty/course oversight, CLP review, document review, analytics, and approval workflows.
- `Admin module`: user management, department/subject setup, template management, system settings, analytics, audit logs, and knowledge-base data.
- `AI services`: Google Gemini integration, optional DeepSeek provider support, template profiling, validation, and background AI task processing.
- `Document services`: DOCX parsing/writing, template handling, TipTap conversion hooks, and ONLYOFFICE document editing callbacks.
- `Storage layer`: local Supabase-compatible database/file-storage adapter backed by PostgreSQL and `local_storage/`.

## Runtime Files

These are the important files and folders needed to run the system:

- `app/`
- `app.py`
- `wsgi.py`
- `uwsgi.ini`
- `run_prod.py`
- `requirements.txt`
- `db/migrations/`
- `db/lpms_current_full_database.dump`
- `license_manager.py`
- `license_state.json`
- `.env.example`
- `local_storage/` created on the target machine

Development-only folders such as `.pi/`, `.codex/`, `.lean-ctx/`, `tests/`, `scratch/`, `scripts/`, `conductor/`, and planning Markdown files are not required for normal runtime.

## Official Downloads

- Python for Windows: https://www.python.org/downloads/windows/
- PostgreSQL for Windows: https://www.postgresql.org/download/windows/
- pgvector extension: https://github.com/pgvector/pgvector
- pgvector PostgreSQL Docker image: https://hub.docker.com/r/pgvector/pgvector
- Docker Desktop for Windows: https://docs.docker.com/desktop/setup/install/windows-install/
- Docker Desktop product page: https://www.docker.com/products/docker-desktop/
- ONLYOFFICE Docs Docker install guide: https://helpcenter.onlyoffice.com/docs/installation/docs-community-install-docker.aspx
- ONLYOFFICE JWT guide: https://helpcenter.onlyoffice.com/docs/installation/docs-configure-jwt.aspx
- Google AI Studio / Gemini API key: https://ai.google.dev/aistudio
- Gemini API key guide: https://ai.google.dev/gemini-api/docs/api-key

## Windows Setup From a Fresh PC

These steps assume Windows 10/11 with administrator access.

1. Install Python from the official Python website.
2. During Python installation, check `Add python.exe to PATH`.
3. Install PostgreSQL from the official PostgreSQL Windows download page.
4. Remember the PostgreSQL password you set during installation.
5. Install Docker Desktop for Windows.
6. In Docker Desktop, use the WSL 2 backend when prompted.
7. Restart the PC if Docker or PostgreSQL asks for it.

Open PowerShell in the project folder and create a virtual environment:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

If PowerShell blocks activation scripts, run this once for your user:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

## Database Setup

AIPCLPMS uses PostgreSQL. The database must support the `vector` extension because the knowledge-base table stores AI embeddings with `vector(768)`.

Required database pieces:

- PostgreSQL server, recommended version 16 or newer.
- `vector` extension from pgvector.
- Database name: `lpms`.
- Database user: `lpms`.
- Database password: choose a strong password and reuse it in `.env`.
- Full current database dump: `db/lpms_current_full_database.dump`.

### Recommended Windows Setup: PostgreSQL in Docker

This is the easiest setup for a stock Windows PC because Docker is already needed for ONLYOFFICE and the `pgvector/pgvector` image already includes the `vector` extension.

Start PostgreSQL with pgvector:

```powershell
docker run -d --name lpms-postgres `
  -e POSTGRES_DB=lpms `
  -e POSTGRES_USER=lpms `
  -e POSTGRES_PASSWORD=replace_with_strong_password `
  -p 5432:5432 `
  -v lpms_pgdata:/var/lib/postgresql/data `
  pgvector/pgvector:pg16
```

Wait a few seconds, then confirm it is running:

```powershell
docker ps
```

Enable the vector extension:

```powershell
docker exec -e PGPASSWORD=replace_with_strong_password lpms-postgres psql -U lpms -d lpms -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

Set this in `.env`:

```env
DATABASE_URL=postgresql://lpms:replace_with_strong_password@127.0.0.1:5432/lpms
```

### Restore the Included Database Dump

The repository includes a current full database backup:

```text
db/lpms_current_full_database.dump
```

This is a PostgreSQL custom-format dump made with `pg_dump --format=custom`. Restore it into the Docker PostgreSQL database:

```powershell
docker cp db\lpms_current_full_database.dump lpms-postgres:/tmp/lpms_current_full_database.dump
docker exec -e PGPASSWORD=replace_with_strong_password lpms-postgres pg_restore `
  --clean `
  --if-exists `
  --no-owner `
  --no-privileges `
  -U lpms `
  -d lpms `
  /tmp/lpms_current_full_database.dump
```

If the restore prints warnings about dropping objects that do not exist, that is usually fine on a fresh database. The important part is that the restore finishes without a final fatal error.

### Native Windows PostgreSQL Option

You can also install PostgreSQL directly on Windows from the official PostgreSQL download page. If you choose this path, you must also install the pgvector extension for the same PostgreSQL version. This is more work than the Docker option.

After installing PostgreSQL, create the user and database in pgAdmin or `psql`:

```sql
CREATE USER lpms WITH PASSWORD 'replace_with_strong_password';
CREATE DATABASE lpms OWNER lpms;
\c lpms
CREATE EXTENSION IF NOT EXISTS vector;
```

Then set:

```env
DATABASE_URL=postgresql://lpms:replace_with_strong_password@127.0.0.1:5432/lpms
```

Restore the dump with `pg_restore`:

```powershell
pg_restore --clean --if-exists --no-owner --no-privileges `
  --dbname "postgresql://lpms:replace_with_strong_password@127.0.0.1:5432/lpms" `
  db\lpms_current_full_database.dump
```

The app also calls its local schema setup on startup, but restoring the dump is the fastest way to reproduce the current system data.

## Environment Setup

Create your real `.env` file from `.env.example`:

```powershell
copy .env.example .env
```

Required values:

- `FLASK_SECRET_KEY`: generate with `python -c "import secrets; print(secrets.token_urlsafe(48))"`.
- `GEMINI_API_KEY`: create this in Google AI Studio.
- `DATABASE_URL`: your PostgreSQL connection string.
- `PUBLIC_APP_URL`: the URL used to open the app.
- `LPMS_STORAGE_ROOT`: full path to `local_storage`.
- `ONLYOFFICE_API_JS_URL`: ONLYOFFICE Docs browser API script URL.
- `ONLYOFFICE_JWT_SECRET`: must match the JWT secret used by ONLYOFFICE Docs.

For local Windows testing, use your computer LAN IP instead of `localhost` when connecting LPMS and ONLYOFFICE together. Example:

```env
PUBLIC_APP_URL=http://192.168.1.50:3000
ONLYOFFICE_CALLBACK_URL=http://192.168.1.50:3000
ONLYOFFICE_INTERNAL_APP_URL=http://192.168.1.50:3000
ONLYOFFICE_API_JS_URL=http://localhost:8080/web-apps/apps/api/documents/api.js
LPMS_STORAGE_ROOT=C:\path\to\lpms\local_storage
```

Replace `192.168.1.50` with the PC's actual local IP address. You can find it in PowerShell:

```powershell
ipconfig
```

## Local Storage Setup

Create the storage folder:

```powershell
mkdir local_storage
mkdir local_storage\clp_files
```

Do not commit `local_storage/`. It contains uploaded files, generated DOCX files, signatures, template copies, and runtime artifacts.

## ONLYOFFICE Docker Setup

ONLYOFFICE Docs is required for browser-based DOCX editing and review. On Windows, the easiest setup is Docker Desktop plus the official `onlyoffice/documentserver` image.

Choose one strong JWT secret and use it in both Docker and `.env`.

Start ONLYOFFICE Docs on port `8080`:

```powershell
docker run -i -t -d --name onlyoffice-documentserver `
  -p 8080:80 `
  --restart=always `
  -e JWT_SECRET=replace_with_same_onlyoffice_jwt_secret `
  onlyoffice/documentserver
```

Check that ONLYOFFICE is running:

```powershell
docker ps
```

Open this in a browser:

```text
http://localhost:8080
```

Then set these LPMS `.env` values:

```env
ONLYOFFICE_API_JS_URL=http://localhost:8080/web-apps/apps/api/documents/api.js
ONLYOFFICE_JWT_SECRET=replace_with_same_onlyoffice_jwt_secret
```

For document callbacks and file downloads, ONLYOFFICE must be able to reach the LPMS app. If LPMS runs on your Windows host, use the PC LAN IP in `PUBLIC_APP_URL`, `ONLYOFFICE_CALLBACK_URL`, and `ONLYOFFICE_INTERNAL_APP_URL`.

If you need to recreate the ONLYOFFICE container:

```powershell
docker stop onlyoffice-documentserver
docker rm onlyoffice-documentserver
```

Then run the `docker run` command again with the same JWT secret.

## License Setup

The system has a license activation flow.

1. Put the issued license API value in `.env` as `LOUIS_LICENSE_API`.
2. Put the issued license key in `.env` as `LOUIS_LICENSE_KEY`, if one was already assigned.
3. Start the app.
4. Open `/license` in the browser.
5. Enter the license key and submit.
6. After successful activation, the app writes/updates `license_state.json`.

For production enforcement:

```env
LOUIS_LICENSE_ENFORCEMENT_ENABLED=true
```

For classroom demos or development where license blocking is not desired:

```env
LOUIS_LICENSE_ENFORCEMENT_ENABLED=false
```

Keep `license_state.json` private if it contains deployment-specific license data.

## Run The App Locally

Activate the virtual environment:

```powershell
.\.venv\Scripts\Activate.ps1
```

Run the Flask app:

```powershell
python app.py
```

Open:

```text
http://localhost:3000
```

If you configured ONLYOFFICE using the LAN IP, open the app using that same LAN URL:

```text
http://192.168.1.50:3000
```

## Production Notes

The current deployment stack uses nginx plus uWSGI through `uwsgi.ini`. On Linux production, install dependencies in `.venv`, configure `.env`, create `local_storage/clp_files`, configure nginx to proxy to `/run/aipclpms.sock`, then restart the service:

```bash
systemctl restart aipclpms
```

After every code edit on the production server, restart:

```bash
systemctl restart aipclpms
```

## Quick Troubleshooting

- `GEMINI_API_KEY is missing`: create a key in Google AI Studio and put it in `.env`.
- `DATABASE_URL is not configured`: check the PostgreSQL connection string in `.env`.
- Login or license page redirects unexpectedly: check `license_state.json`, `LOUIS_LICENSE_API`, and `LOUIS_LICENSE_KEY`.
- ONLYOFFICE editor is blank: check `ONLYOFFICE_API_JS_URL` and confirm `http://localhost:8080` opens.
- ONLYOFFICE cannot save or load documents: use a LAN IP for `PUBLIC_APP_URL`/callbacks so the Docker container can reach LPMS.
- Uploaded/generated files are missing: check `LPMS_STORAGE_ROOT` and make sure `local_storage/clp_files` exists.
