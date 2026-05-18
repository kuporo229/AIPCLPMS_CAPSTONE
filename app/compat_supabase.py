import logging
import os
import re
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from types import SimpleNamespace

from argon2 import PasswordHasher
import bcrypt
from flask import current_app, has_app_context, request, session
import psycopg2
from psycopg2 import sql
from psycopg2.extras import Json, RealDictCursor

logger = logging.getLogger(__name__)

from app.local_schema import ensure_local_schema, ensure_storage_root


_PASSWORD_HASHER = PasswordHasher()
_ALLOWED_TABLES = {
    "ai_usage_log",
    "audit_logs",
    "background_tasks",
    "clp_comments",
    "clp_history",
    "clp_mapping_entries",
    "clp_versions",
    "copilot_reference_entries",
    "course_learning_plans",
    "course_outcomes",
    "departments",
    "generated_template_drafts",
    "institutional_outcomes",
    "knowledge_base",
    "notifications",
    "program_outcomes",
    "system_events",
    "system_settings",
    "teacher_subjects",
    "teacher_template_profiles",
    "template_profile_versions",
    "templates",
    "users",
}
_PRIMARY_KEYS = {
    "system_settings": ["key"],
    "users": ["id"],
}


class PostgrestAPIError(Exception):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.message = message
        self.code = code


@dataclass
class ClientOptions:
    postgrest_client_timeout: object = None
    storage_client_timeout: object = None


def _get_database_url():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url and has_app_context():
        database_url = current_app.config.get("DATABASE_URL")
    if not database_url:
        raise ValueError("DATABASE_URL is not configured")
    return database_url


def _get_storage_root():
    storage_root = os.environ.get("LPMS_STORAGE_ROOT")
    if not storage_root and has_app_context():
        storage_root = current_app.config.get("LPMS_STORAGE_ROOT")
    if not storage_root:
        storage_root = os.path.join(os.getcwd(), "local_storage")
    ensure_storage_root(storage_root)
    return storage_root


def _get_public_app_url():
    if has_app_context():
        public_url = current_app.config.get("PUBLIC_APP_URL")
        if public_url:
            return public_url.rstrip("/")
    public_url = os.environ.get("PUBLIC_APP_URL") or os.environ.get("ONLYOFFICE_CALLBACK_URL")
    if public_url:
        return public_url.rstrip("/")
    if has_app_context() and request:
        return request.host_url.rstrip("/")
    return "http://localhost:3000"


def build_public_storage_url(bucket, path):
    return f"{_get_public_app_url()}/storage/v1/object/public/{bucket}/{path}"


def get_storage_full_path(bucket, path):
    storage_root = _get_storage_root()
    bucket_root = os.path.abspath(os.path.join(storage_root, bucket))
    full_path = os.path.abspath(os.path.join(bucket_root, path))
    if full_path != bucket_root and not full_path.startswith(bucket_root + os.sep):
        raise PostgrestAPIError("Invalid storage path")
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    return full_path


def read_storage_bytes(bucket, path):
    with open(get_storage_full_path(bucket, path), "rb") as handle:
        return handle.read()


def write_storage_bytes(bucket, path, data):
    full_path = get_storage_full_path(bucket, path)
    temp_path = f"{full_path}.tmp"
    if isinstance(data, str):
        data = data.encode("utf-8")
    with open(temp_path, "wb") as handle:
        handle.write(data)
    os.replace(temp_path, full_path)
    return path


def delete_storage_paths(bucket, paths):
    for path in paths:
        full_path = get_storage_full_path(bucket, path)
        if os.path.exists(full_path):
            os.remove(full_path)
    return True


def storage_key_from_public_url(value, bucket):
    if not value:
        return None
    marker = f"/storage/v1/object/public/{bucket}/"
    if marker not in value:
        return None
    return value.split(marker, 1)[1]


def _normalize_value(value):
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, memoryview):
        return bytes(value)
    if isinstance(value, dict):
        return {k: _normalize_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize_value(v) for v in value]
    return value


def _normalize_row(row):
    return {key: _normalize_value(value) for key, value in row.items()}


def _adapt_param_value(value):
    if isinstance(value, (dict, list)):
        return Json(value)
    return value


def _is_bcrypt_hash(value):
    return isinstance(value, str) and value.startswith(("$2a$", "$2b$", "$2y$"))


def verify_password_hash(password_hash, password):
    if not password_hash or password is None:
        return False
    try:
        if _is_bcrypt_hash(password_hash):
            return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
        _PASSWORD_HASHER.verify(password_hash, password)
        return True
    except Exception:
        return False


def _build_set_clause(values):
    assignments = []
    params = []
    for column, value in values.items():
        assignments.append(
            sql.SQL("{} = {}").format(sql.Identifier(column), sql.Placeholder())
        )
        params.append(_adapt_param_value(value))
    return sql.SQL(", ").join(assignments), params


class Client:
    _pool = None

    @classmethod
    def _get_pool(cls, database_url):
        """Lazy-init thread-safe connection pool (one per process)."""
        if cls._pool is None:
            from psycopg2.pool import ThreadedConnectionPool
            cls._pool = ThreadedConnectionPool(2, 10, database_url)
            cls._pool._timeout = 5  # wait up to 5s for a connection
        return cls._pool

    def __init__(self, database_url=None, storage_root=None):
        self.database_url = database_url or _get_database_url()
        self.storage_root = storage_root or _get_storage_root()
        ensure_local_schema(self.database_url)
        ensure_storage_root(self.storage_root)
        self.auth = LocalAuth(self)
        self.storage = LocalStorage(self)

    @contextmanager
    def get_connection(self):
        """Borrow a connection from the pool, return it on exit.

        Retries on transient failures (SSL drop, network blip, deadlock) with
        short back-off.  Autocommit prevents orphaned transactions.
        """
        pool = Client._get_pool(self.database_url)
        last_exc = None
        for attempt in range(1, 4):
            try:
                conn = pool.getconn()
                conn.autocommit = True
                last_exc = None
                with conn.cursor() as cur:
                    cur.execute("SET statement_timeout = '120s'")
            except (psycopg2.OperationalError, psycopg2.InterfaceError,
                    psycopg2.errors.DeadlockDetected) as exc:
                last_exc = exc
                if attempt < 3:
                    wait = 0.25 * (2 ** (attempt - 1))
                    logger.warning(
                        "Database connection attempt %d/3 failed (%s). Retrying in %.2fs...",
                        attempt, exc, wait,
                    )
                    time.sleep(wait)
                continue
            try:
                yield conn
                return
            except psycopg2.errors.DeadlockDetected as exc:
                last_exc = exc
                pool.putconn(conn)
                if attempt < 3:
                    wait = 0.25 * (2 ** (attempt - 1))
                    logger.warning(
                        "Deadlock detected (attempt %d/3). Retrying in %.2fs...",
                        attempt, wait,
                    )
                    time.sleep(wait)
                continue
            finally:
                if last_exc is None:
                    pool.putconn(conn)
        raise last_exc  # type: ignore[misc]

    def table(self, name):
        if name not in _ALLOWED_TABLES:
            raise PostgrestAPIError(f"Unknown table: {name}")
        return LocalTableQuery(self, name)

    def rpc(self, name, params):
        return LocalRpcQuery(self, name, params)

class LocalAuthAdmin:
    def __init__(self, client):
        self.client = client

    def create_user(self, payload):
        email = payload.get("email")
        password = payload.get("password")
        user_id = str(uuid.uuid4())
        password_hash = _PASSWORD_HASHER.hash(password)

        with self.client.get_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    INSERT INTO users (id, email, password_hash, approved, password_reset_required, created_at, updated_at)
                    VALUES (%s, %s, %s, TRUE, TRUE, now(), now())
                    ON CONFLICT (email) DO UPDATE
                    SET password_hash = EXCLUDED.password_hash,
                        approved = TRUE,
                        password_reset_required = TRUE,
                        updated_at = now()
                    RETURNING id
                    """,
                    (user_id, email, password_hash),
                )
                created = cur.fetchone()
            conn.commit()
        return SimpleNamespace(user=SimpleNamespace(id=_normalize_value(created["id"])))


class LocalAuth:
    def __init__(self, client):
        self.client = client
        self.admin = LocalAuthAdmin(client)

    def sign_in_with_password(self, payload):
        email = payload.get("email")
        password = payload.get("password")
        with self.client.get_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT * FROM users WHERE email = %s", (email,))
                user = cur.fetchone()
        if not user or not user.get("password_hash"):
            raise PostgrestAPIError("Invalid credentials")
        if user.get("active") is False:
            raise PostgrestAPIError("Account inactive")
        password_hash = user["password_hash"]
        replacement_hash = None
        try:
            if _is_bcrypt_hash(password_hash):
                if not bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8")):
                    raise PostgrestAPIError("Invalid credentials")
                replacement_hash = _PASSWORD_HASHER.hash(password)
            else:
                _PASSWORD_HASHER.verify(password_hash, password)
                if _PASSWORD_HASHER.check_needs_rehash(password_hash):
                    replacement_hash = _PASSWORD_HASHER.hash(password)
        except Exception as exc:
            if isinstance(exc, PostgrestAPIError):
                raise
            raise PostgrestAPIError("Invalid credentials") from exc
        with self.client.get_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                if replacement_hash:
                    cur.execute(
                        """
                        UPDATE users
                        SET password_hash = %s,
                            password_reset_required = FALSE,
                            last_login_at = now(),
                            updated_at = now()
                        WHERE id = %s
                        """,
                        (replacement_hash, user["id"]),
                    )
                else:
                    cur.execute(
                        "UPDATE users SET last_login_at = now(), updated_at = now() WHERE id = %s",
                        (user["id"],),
                    )
            conn.commit()
        return SimpleNamespace(user=SimpleNamespace(id=_normalize_value(user["id"])))

    def sign_up(self, payload):
        email = payload.get("email")
        password = payload.get("password")
        user_id = str(uuid.uuid4())
        password_hash = _PASSWORD_HASHER.hash(password)
        with self.client.get_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    INSERT INTO users (id, email, password_hash, role, approved, password_reset_required, created_at, updated_at)
                    VALUES (%s, %s, %s, 'teacher', FALSE, FALSE, now(), now())
                    ON CONFLICT (email) DO NOTHING
                    RETURNING id
                    """,
                    (user_id, email, password_hash),
                )
                created = cur.fetchone()
            conn.commit()
        if not created:
            raise PostgrestAPIError("User already exists")
        return SimpleNamespace(user=SimpleNamespace(id=_normalize_value(created["id"])))

    def update_user(self, payload):
        new_password = payload.get("password")
        user_id = session.get("user_id") if has_app_context() else None
        if not user_id:
            raise PostgrestAPIError("No authenticated user")
        password_hash = _PASSWORD_HASHER.hash(new_password)
        with self.client.get_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    UPDATE users
                    SET password_hash = %s,
                        password_reset_required = FALSE,
                        updated_at = now()
                    WHERE id = %s
                    """,
                    (password_hash, user_id),
                )
            conn.commit()
        return True

    def sign_out(self):
        return True


class LocalBucket:
    def __init__(self, client, bucket):
        self.client = client
        self.bucket = bucket
        self.bucket_root = os.path.join(self.client.storage_root, bucket)
        os.makedirs(self.bucket_root, exist_ok=True)

    def _full_path(self, path):
        return get_storage_full_path(self.bucket, path)

    def upload(self, path, file, file_options=None):
        data = file.read() if hasattr(file, "read") else file
        if isinstance(data, str):
            data = data.encode("utf-8")
        with open(self._full_path(path), "wb") as handle:
            handle.write(data)
        return {"path": path}

    def download(self, path):
        return read_storage_bytes(self.bucket, path)

    def remove(self, paths):
        return delete_storage_paths(self.bucket, paths)


class LocalStorage:
    def __init__(self, client):
        self.client = client

    def from_(self, bucket):
        return LocalBucket(self.client, bucket)


class LocalRpcQuery:
    def __init__(self, client, name, params):
        self.client = client
        self.name = name
        self.params = params

    def execute(self):
        if self.name != "match_documents":
            raise PostgrestAPIError(f"Unsupported RPC: {self.name}")
        with self.client.get_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT * FROM match_documents(%s::vector, %s, %s, %s)
                    """,
                    (
                        self.params.get("query_embedding"),
                        self.params.get("match_threshold"),
                        self.params.get("match_count"),
                        self.params.get("filter_department_id"),
                    ),
                )
                rows = [_normalize_row(row) for row in cur.fetchall()]
        return SimpleNamespace(data=rows, count=len(rows))


class LocalTableQuery:
    def __init__(self, client, table_name):
        self.client = client
        self.table_name = table_name
        self._select = "*"
        self._count_requested = False
        self._filters = []
        self._or_filters = []
        self._order_by = None
        self._limit = None
        self._single = False
        self._insert_values = None
        self._update_values = None
        self._delete_mode = False
        self._upsert_values = None

    def select(self, columns="*", count=None):
        self._select = columns
        self._count_requested = count == "exact"
        return self

    def eq(self, field, value):
        self._filters.append(("eq", field, value))
        return self

    def neq(self, field, value):
        self._filters.append(("neq", field, value))
        return self

    def in_(self, field, values):
        self._filters.append(("in", field, list(values)))
        return self

    def gte(self, field, value):
        self._filters.append(("gte", field, value))
        return self

    def or_(self, expression):
        or_parts = []
        for clause in expression.split(","):
            parts = clause.split(".")
            if len(parts) < 3:
                continue
            field = parts[0]
            operator = parts[1]
            value = ".".join(parts[2:])
            or_parts.append((operator, field, value))
        self._or_filters.extend(or_parts)
        return self

    def order(self, field, desc=False):
        self._order_by = (field, desc)
        return self

    def limit(self, value):
        self._limit = value
        return self

    def single(self):
        self._single = True
        return self

    def insert(self, values):
        self._insert_values = values
        return self

    def update(self, values):
        self._update_values = values
        return self

    def delete(self):
        self._delete_mode = True
        return self

    def upsert(self, values):
        self._upsert_values = values
        return self

    def _build_where(self):
        clauses = []
        params = []
        for operator, field, value in self._filters:
            identifier = sql.Identifier(field)
            if operator == "eq":
                clauses.append(sql.SQL("{} = {}").format(identifier, sql.Placeholder()))
                params.append(value)
            elif operator == "neq":
                clauses.append(sql.SQL("{} != {}").format(identifier, sql.Placeholder()))
                params.append(value)
            elif operator == "in":
                # Detect UUID lists and add explicit cast to avoid "uuid = text" errors
                if isinstance(value, list) and value and isinstance(value[0], str) and len(value[0]) > 30 and '-' in value[0]:
                    clauses.append(sql.SQL("{} = ANY({}::uuid[])").format(identifier, sql.Placeholder()))
                else:
                    clauses.append(sql.SQL("{} = ANY({})").format(identifier, sql.Placeholder()))
                params.append(value)
            elif operator == "gte":
                clauses.append(sql.SQL("{} >= {}").format(identifier, sql.Placeholder()))
                params.append(value)

        if self._or_filters:
            or_clauses = []
            for operator, field, value in self._or_filters:
                identifier = sql.Identifier(field)
                if operator == "eq":
                    or_clauses.append(sql.SQL("{} = {}").format(identifier, sql.Placeholder()))
                    params.append(value)
            if or_clauses:
                clauses.append(sql.SQL("(") + sql.SQL(" OR ").join(or_clauses) + sql.SQL(")"))

        if not clauses:
            return sql.SQL(""), params
        return sql.SQL(" WHERE ") + sql.SQL(" AND ").join(clauses), params

    def _split_select_terms(self):
        if not isinstance(self._select, str):
            return ["*"]
        terms = []
        current = []
        depth = 0
        for char in self._select:
            if char == "(":
                depth += 1
            elif char == ")" and depth:
                depth -= 1
            if char == "," and depth == 0:
                term = "".join(current).strip()
                if term:
                    terms.append(term)
                current = []
                continue
            current.append(char)
        term = "".join(current).strip()
        if term:
            terms.append(term)
        return terms or ["*"]

    def _select_columns_sql(self):
        """Build a safe local SELECT list for Supabase-style simple column selects."""
        selected = []
        saw_star = False
        for term in self._split_select_terms():
            if term == "*":
                saw_star = True
                break
            if re.match(r"^\w+:\w+\([^)]*\)$", term) or re.match(r"^\w+\([^)]*\)$", term):
                continue
            if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", term):
                selected.append(term)
                continue
            return sql.SQL("*")

        if saw_star or not selected:
            return sql.SQL("*")

        # Preserve caller order but avoid selecting the same column twice.
        unique = list(dict.fromkeys(selected))
        return sql.SQL(", ").join(sql.Identifier(column) for column in unique)

    def _execute_select(self):
        where_sql, params = self._build_where()
        query = sql.SQL("SELECT {} FROM {}").format(
            self._select_columns_sql(),
            sql.Identifier(self.table_name),
        ) + where_sql
        if self._order_by:
            query += sql.SQL(" ORDER BY {} {}").format(
                sql.Identifier(self._order_by[0]),
                sql.SQL("DESC" if self._order_by[1] else "ASC"),
            )
        if self._limit is not None:
            query += sql.SQL(" LIMIT {}").format(sql.Literal(self._limit))

        with self.client.get_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(query, params)
                rows = [_normalize_row(row) for row in cur.fetchall()]
                count = None
                if self._count_requested:
                    count_query = sql.SQL("SELECT COUNT(*) AS count FROM {}").format(sql.Identifier(self.table_name)) + where_sql
                    cur.execute(count_query, params)
                    count = cur.fetchone()["count"]

        rows = self._hydrate_relationships(rows)
        if self._single:
            if not rows:
                raise PostgrestAPIError("PGRST116: No rows found")
            return SimpleNamespace(data=rows[0], count=1)
        return SimpleNamespace(data=rows, count=count)

    def _execute_insert(self):
        values_list = self._insert_values if isinstance(self._insert_values, list) else [self._insert_values]
        inserted = []
        with self.client.get_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                for values in values_list:
                    columns = list(values.keys())
                    placeholders = [sql.Placeholder()] * len(columns)
                    query = sql.SQL("INSERT INTO {} ({}) VALUES ({}) RETURNING *").format(
                        sql.Identifier(self.table_name),
                        sql.SQL(", ").join(sql.Identifier(col) for col in columns),
                        sql.SQL(", ").join(placeholders),
                    )
                    try:
                        cur.execute(query, [_adapt_param_value(values[col]) for col in columns])
                    except psycopg2.errors.UniqueViolation:
                        if self.table_name == "users":
                            conn.rollback()
                            set_clause, set_params = _build_set_clause(values)
                            cur.execute(
                                sql.SQL("UPDATE users SET {} WHERE id = {} RETURNING *").format(
                                    set_clause,
                                    sql.Placeholder(),
                                ),
                                set_params + [values["id"]],
                            )
                        else:
                            raise
                    inserted.append(_normalize_row(cur.fetchone()))
            conn.commit()
        return SimpleNamespace(data=inserted, count=len(inserted))

    def _execute_update(self):
        set_clause, set_params = _build_set_clause(self._update_values)
        where_sql, where_params = self._build_where()
        query = sql.SQL("UPDATE {} SET {}").format(sql.Identifier(self.table_name), set_clause) + where_sql + sql.SQL(" RETURNING *")
        with self.client.get_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(query, set_params + where_params)
                rows = [_normalize_row(row) for row in cur.fetchall()]
            conn.commit()
        rows = self._hydrate_relationships(rows)
        return SimpleNamespace(data=rows, count=len(rows))

    def _execute_delete(self):
        where_sql, params = self._build_where()
        query = sql.SQL("DELETE FROM {}").format(sql.Identifier(self.table_name)) + where_sql + sql.SQL(" RETURNING *")
        with self.client.get_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(query, params)
                rows = [_normalize_row(row) for row in cur.fetchall()]
            conn.commit()
        return SimpleNamespace(data=rows, count=len(rows))

    def _execute_upsert(self):
        values_list = self._upsert_values if isinstance(self._upsert_values, list) else [self._upsert_values]
        conflict_columns = _PRIMARY_KEYS.get(self.table_name) or ["id"]
        rows = []
        with self.client.get_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                for values in values_list:
                    columns = list(values.keys())
                    update_columns = [col for col in columns if col not in conflict_columns]
                    query = sql.SQL(
                        "INSERT INTO {} ({}) VALUES ({}) ON CONFLICT ({}) DO UPDATE SET {} RETURNING *"
                    ).format(
                        sql.Identifier(self.table_name),
                        sql.SQL(", ").join(sql.Identifier(col) for col in columns),
                        sql.SQL(", ").join(sql.Placeholder() for _ in columns),
                        sql.SQL(", ").join(sql.Identifier(col) for col in conflict_columns),
                        sql.SQL(", ").join(
                            sql.SQL("{} = EXCLUDED.{}").format(sql.Identifier(col), sql.Identifier(col))
                            for col in update_columns
                        ),
                    )
                    cur.execute(query, [_adapt_param_value(values[col]) for col in columns])
                    rows.append(_normalize_row(cur.fetchone()))
            conn.commit()
        return SimpleNamespace(data=rows, count=len(rows))

    def _hydrate_relationships(self, rows):
        if not rows or not isinstance(self._select, str):
            return rows

        patterns = []
        patterns.extend(re.findall(r"(\w+):(\w+)\(([^)]*)\)", self._select))
        patterns.extend((name, name, fields) for name, fields in re.findall(r"(?<!:)(\w+)\(([^)]*)\)", self._select))
        if not patterns:
            return rows

        hydrated = [dict(row) for row in rows]
        for alias, relation_table, fields in patterns:
            requested_fields = None if fields.strip() == "*" else [field.strip() for field in fields.split(",")]
            if self.table_name == "course_learning_plans" and relation_table == "users" and alias == "author":
                self._attach_related_rows(hydrated, alias, relation_table, "user_id", "id", requested_fields)
            elif self.table_name == "clp_history" and relation_table == "users" and alias == "actor":
                self._attach_related_rows(hydrated, alias, relation_table, "actor_id", "id", requested_fields)
            elif self.table_name == "clp_versions" and relation_table == "users" and alias == "actor":
                self._attach_related_rows(hydrated, alias, relation_table, "actor_id", "id", requested_fields)
            elif self.table_name == "audit_logs" and relation_table == "users" and alias == "actor":
                self._attach_related_rows(hydrated, alias, relation_table, "user_id", "id", requested_fields)
            elif self.table_name == "templates" and relation_table == "departments" and alias == "departments":
                self._attach_related_rows(hydrated, alias, relation_table, "department_id", "id", requested_fields)
        return hydrated

    def _attach_related_rows(self, rows, alias, relation_table, local_key, remote_key, requested_fields):
        ids = [str(row.get(local_key)) for row in rows if row.get(local_key) is not None]
        if not ids:
            for row in rows:
                row[alias] = None
            return
        with self.client.get_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    sql.SQL("SELECT * FROM {} WHERE {}::text = ANY(%s)").format(
                        sql.Identifier(relation_table),
                        sql.Identifier(remote_key),
                    ),
                    (ids,),
                )
                related = {str(_normalize_value(row[remote_key])): _normalize_row(row) for row in cur.fetchall()}
        for row in rows:
            related_row = related.get(str(row.get(local_key))) if row.get(local_key) is not None else None
            if related_row and requested_fields:
                related_row = {field: related_row.get(field) for field in requested_fields}
            row[alias] = related_row

    def execute(self):
        """Execute the query with retry on transient connection failures."""
        last_exc = None
        for attempt in range(1, 4):
            try:
                if self._insert_values is not None:
                    return self._execute_insert()
                if self._update_values is not None:
                    return self._execute_update()
                if self._delete_mode:
                    return self._execute_delete()
                if self._upsert_values is not None:
                    return self._execute_upsert()
                return self._execute_select()
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as exc:
                last_exc = exc
                if attempt < 3:
                    wait = 0.5 * (2 ** (attempt - 1))
                    logger.warning(
                        "Query execution attempt %d/3 failed (%s). Retrying in %.2fs...",
                        attempt, exc, wait,
                    )
                    time.sleep(wait)
        raise last_exc


def create_client(_url=None, _key=None, options=None):
    return Client(database_url=_url)
