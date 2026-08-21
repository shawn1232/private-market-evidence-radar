"""Credential-free SQLite pool for imported and publicly discovered WeChat articles.

The module only persists normalized records supplied by callers.  It never logs
in, performs network I/O, or persists raw exporter/search payloads.  All database
writes are whitelisted and transactional so credential-like fields from an input
document cannot leak into the archive.
"""

from __future__ import annotations

from collections import defaultdict
from contextlib import closing
from datetime import date, datetime, time, timedelta, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
from html import unescape
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import csv
import io
import json
import re
import sqlite3


ROOT = Path(__file__).resolve().parent
DEFAULT_DB_PATH = ROOT / "data" / "wechat_pool" / "wechat_source_pool.sqlite3"
SHANGHAI_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")

ARTICLE_OUTPUT_FIELDS = (
    "account_name",
    "fakeid",
    "title",
    "url",
    "publish_time",
    "author",
    "digest",
    "cover_url",
    "body_markdown_path",
    "html_path",
    "image_dir",
    "read_count",
    "like_count",
    "share_count",
    "favorite_count",
    "comment_count",
    "comments_path",
    "comment_replies_path",
    "fetch_mode",
    "credential_status",
    "exported_at",
    "discovery_query",
    "discovery_provider",
    "discovered_at",
    "discovery_rank",
    "error",
)

FORBIDDEN_CREDENTIAL_KEYS = {
    "access_token",
    "auth_key",
    "authorization",
    "cookie",
    "cookies",
    "credential",
    "credentials",
    "key",
    "pass_ticket",
    "password",
    "refresh_token",
    "secret",
    "session",
    "sessionid",
    "skey",
    "token",
    "uin",
    "wxuin",
}

ALLOWED_CREDENTIAL_STATUSES = {
    "not_stored",
    "not_required",
    "not_provided",
    "missing",
    "expired",
    "fresh",
    "unavailable",
    "unknown",
}

ALLOWED_FETCH_MODES = {
    "history_import",
    "json_import",
    "csv_import",
    "enhanced_export",
    "body_export",
    "known_url",
    "known_url_public_exporter",
    "web_discovery",
    "metadata_only",
    "unknown",
}

IDENTITY_QUERY_KEYS = ("__biz", "mid", "idx", "sn", "appmsgid", "itemidx")
TEMPORARY_LINK_QUERY_KEYS = ("src", "timestamp", "ver", "signature", "new")
CONTAINER_KEYS = (
    "articles",
    "records",
    "rows",
    "history",
    "items",
    "item",
    "list",
    "appmsg",
    "appmsgs",
    "appmsg_list",
    "app_msg_list",
    "appmsg_info",
    "data",
    "result",
)
MULTI_ITEM_KEYS = (
    "multi_app_msg_item_list",
    "multi_app_msg_item",
    "multi_appmsg_item_list",
    "multi_appmsg",
    "sub_articles",
)


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    account_key TEXT NOT NULL UNIQUE,
    account_name TEXT NOT NULL DEFAULT '',
    fakeid TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS articles (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    canonical_url TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL,
    publish_time INTEGER,
    author TEXT NOT NULL DEFAULT '',
    digest TEXT NOT NULL DEFAULT '',
    cover_url TEXT NOT NULL DEFAULT '',
    body_markdown_path TEXT NOT NULL DEFAULT '',
    html_path TEXT NOT NULL DEFAULT '',
    image_dir TEXT NOT NULL DEFAULT '',
    read_count INTEGER,
    like_count INTEGER,
    share_count INTEGER,
    favorite_count INTEGER,
    comment_count INTEGER,
    comments_path TEXT NOT NULL DEFAULT '',
    comment_replies_path TEXT NOT NULL DEFAULT '',
    fetch_mode TEXT NOT NULL DEFAULT 'history_import',
    credential_status TEXT NOT NULL DEFAULT 'not_stored',
    exported_at TEXT NOT NULL DEFAULT '',
    discovery_query TEXT NOT NULL DEFAULT '',
    discovery_provider TEXT NOT NULL DEFAULT '',
    discovered_at TEXT NOT NULL DEFAULT '',
    discovery_rank INTEGER,
    error TEXT NOT NULL DEFAULT '',
    msgid TEXT NOT NULL DEFAULT '',
    appmsgid TEXT NOT NULL DEFAULT '',
    itemidx INTEGER,
    group_key TEXT NOT NULL,
    copyright_type INTEGER,
    copyright_stat INTEGER,
    is_deleted INTEGER NOT NULL DEFAULT 0,
    date_status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(account_id, canonical_url)
);

CREATE INDEX IF NOT EXISTS idx_articles_publish_time
    ON articles(publish_time DESC);
CREATE INDEX IF NOT EXISTS idx_articles_account_publish
    ON articles(account_id, publish_time DESC);

CREATE TABLE IF NOT EXISTS import_runs (
    id INTEGER PRIMARY KEY,
    source_name TEXT NOT NULL,
    source_fingerprint TEXT NOT NULL,
    raw_records INTEGER NOT NULL,
    accepted_records INTEGER NOT NULL,
    publish_groups INTEGER NOT NULL,
    expanded_url_items INTEGER NOT NULL,
    original_articles INTEGER NOT NULL,
    added_articles INTEGER NOT NULL,
    existing_articles INTEGER NOT NULL,
    updated_articles INTEGER NOT NULL,
    rejected_records INTEGER NOT NULL,
    duplicate_records_removed INTEGER NOT NULL,
    credential_fields_ignored INTEGER NOT NULL,
    imported_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS import_account_scopes (
    run_id INTEGER NOT NULL REFERENCES import_runs(id) ON DELETE CASCADE,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    publish_groups INTEGER NOT NULL,
    expanded_url_items INTEGER NOT NULL,
    original_articles INTEGER NOT NULL,
    PRIMARY KEY(run_id, account_id)
);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _text(value: Any, limit: int = 20_000) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", unescape(str(value))).strip()[:limit]


def _first(mapping: Mapping[str, Any], keys: Sequence[str], default: Any = None) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value is not None and value != "":
            return value
    raw = mapping.get("raw")
    if isinstance(raw, Mapping):
        for key in keys:
            value = raw.get(key)
            if value is not None and value != "":
                return value
    return default


def _integer(value: Any) -> int | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return int(float(str(value).replace(",", "").strip()))
    except (TypeError, ValueError):
        return None


def _boolean(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "deleted"}


def _timestamp(value: Any) -> int | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=SHANGHAI_TZ)
        return int(dt.timestamp())
    if isinstance(value, date):
        return int(datetime.combine(value, time.min, SHANGHAI_TZ).timestamp())
    if isinstance(value, (int, float)) or re.fullmatch(r"\d{10,13}", str(value).strip()):
        try:
            stamp = float(value)
            if stamp > 10_000_000_000:
                stamp /= 1000
            return int(stamp) if stamp > 0 else None
        except (TypeError, ValueError, OverflowError):
            return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = parsedate_to_datetime(raw)
        except (TypeError, ValueError, OverflowError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=SHANGHAI_TZ)
    return int(dt.timestamp())


def _iso_datetime(value: Any, default: str = "") -> str:
    if value is None or value == "":
        return default
    stamp = _timestamp(value)
    if stamp is not None:
        return datetime.fromtimestamp(stamp, timezone.utc).isoformat(timespec="seconds")
    raw = _text(value, 100)
    return raw if re.match(r"^20\d{2}-\d{2}-\d{2}", raw) else default


def _safe_error(value: Any) -> str:
    text = _text(value, 2_000)
    if not text:
        return ""
    pattern = re.compile(
        r"(?i)\b(auth[-_ ]?key|access[-_ ]?token|refresh[-_ ]?token|token|cookie|"
        r"pass[-_ ]?ticket|uin|secret|password|authorization)\b\s*[:=]\s*[^\s,;]+"
    )
    return pattern.sub(lambda match: f"{match.group(1)}=[redacted]", text)


def _credential_status(value: Any) -> str:
    status = _text(value, 40).lower().replace(" ", "_")
    return status if status in ALLOWED_CREDENTIAL_STATUSES else "not_stored"


def _fetch_mode(value: Any, default: str) -> str:
    mode = _text(value, 40).lower().replace(" ", "_")
    return mode if mode in ALLOWED_FETCH_MODES else default


def canonicalize_wechat_url(value: Any) -> str:
    """Canonicalize a public-account article URL for account-scoped dedupe."""

    # Decode only explicit ampersand escapes.  A general HTML unescape turns
    # the ``&times`` prefix of ``&timestamp`` into a multiplication sign.
    raw = str(value or "").strip().strip("'\"").replace("\\/", "/")
    raw = re.sub(r"&amp;", "&", raw, flags=re.IGNORECASE)
    raw = re.sub(r"&#(?:38|x26);", "&", raw, flags=re.IGNORECASE)
    if "${" in raw or "{{" in raw:
        raise ValueError("placeholder WeChat URLs are not accepted")
    if raw.startswith("//"):
        raw = "https:" + raw
    try:
        parsed = urlsplit(raw)
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError as exc:
        raise ValueError("invalid WeChat article URL") from exc
    if parsed.scheme.lower() not in {"http", "https"} or host != "mp.weixin.qq.com":
        raise ValueError("only mp.weixin.qq.com article URLs are accepted")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URL user-info is not allowed")
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if not (path == "/s" or path.startswith("/s/") or path.startswith("/mp/")):
        raise ValueError("unsupported WeChat article path")
    query_pairs = parse_qsl(parsed.query, keep_blank_values=False)
    identity = {key: value for key, value in query_pairs if key in IDENTITY_QUERY_KEYS and value}
    if identity:
        query = urlencode([(key, identity[key]) for key in IDENTITY_QUERY_KEYS if key in identity])
    else:
        temporary = {key: value for key, value in query_pairs if key in TEMPORARY_LINK_QUERY_KEYS and value}
        query = urlencode([(key, temporary[key]) for key in TEMPORARY_LINK_QUERY_KEYS if key in temporary])
        if path == "/s" and not query:
            raise ValueError("article identity is missing from WeChat URL")
    if port not in {None, 80, 443}:
        raise ValueError("non-standard ports are not allowed")
    netloc = host
    return urlunsplit(("https", netloc, path, query, ""))


def _normalized_account_key(account_name: str, fakeid: str) -> str:
    if fakeid:
        return "fakeid:" + fakeid.casefold()
    normalized = re.sub(r"\s+", "", account_name).casefold()
    return "name:" + normalized


def _account_context(mapping: Mapping[str, Any], inherited: Mapping[str, Any]) -> dict[str, Any]:
    context = dict(inherited)
    account_name = _first(
        mapping,
        ("account_name", "nickname", "nick_name", "wx_name", "mp_name", "account"),
    )
    fakeid = _first(mapping, ("fakeid", "fake_id"))
    if account_name:
        context["account_name"] = account_name
    if fakeid:
        context["fakeid"] = fakeid
    for target, aliases in {
        "msgid": ("msgid", "msg_id"),
        "appmsgid": ("appmsgid", "appmsg_id", "aid"),
        "publish_time": ("publish_time", "publishTime", "create_time", "datetime", "timestamp"),
        "exported_at": ("exported_at", "downloaded_at", "fetched_at"),
        "fetch_mode": ("fetch_mode",),
        "credential_status": ("credential_status",),
    }.items():
        value = _first(mapping, aliases)
        if value is not None and value != "":
            context[target] = value
    return context


def _decode_nested_cells(mapping: Mapping[str, Any]) -> dict[str, Any]:
    decoded = dict(mapping)
    for key in set(CONTAINER_KEYS + MULTI_ITEM_KEYS + ("app_msg_ext_info", "comm_msg_info")):
        value = decoded.get(key)
        if isinstance(value, str) and value.lstrip().startswith(("{", "[")):
            try:
                decoded[key] = json.loads(value)
            except json.JSONDecodeError:
                pass
    return decoded


def _article_url(mapping: Mapping[str, Any]) -> Any:
    return _first(mapping, ("url", "content_url", "article_url", "source_url", "link"))


def _expand_article(
    article: Mapping[str, Any],
    inherited: Mapping[str, Any],
    default_itemidx: int | None = None,
) -> Iterable[dict[str, Any]]:
    node = _decode_nested_cells(article)
    context = _account_context(node, inherited)
    row = {**context, **node}
    if default_itemidx is not None and _first(row, ("itemidx", "item_idx", "idx")) in {None, ""}:
        row["itemidx"] = default_itemidx
    raw_url = _article_url(row)
    group_key = _text(context.get("_group_key"), 300)
    if not group_key:
        msgid = _text(_first(row, ("msgid", "msg_id")), 200)
        appmsgid = _text(_first(row, ("appmsgid", "appmsg_id")), 200)
        if msgid:
            group_key = "msgid:" + msgid
        elif appmsgid:
            group_key = "appmsgid:" + appmsgid
        else:
            seed = f"{raw_url}|{_first(row, ('publish_time', 'create_time', 'datetime'))}|{_first(row, ('title',))}"
            group_key = "fallback:" + sha256(seed.encode("utf-8", "ignore")).hexdigest()[:24]
    row["_group_key"] = group_key
    if raw_url:
        yield row

    child_context = dict(context)
    child_context["_group_key"] = group_key
    for key in MULTI_ITEM_KEYS:
        children = node.get(key)
        if isinstance(children, Mapping):
            children = [children]
        if isinstance(children, list):
            for index, child in enumerate(children, start=2):
                if isinstance(child, Mapping):
                    yield from _expand_article(child, child_context, index)


def _flatten_history(payload: Any, inherited: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    inherited = dict(inherited or {})
    rows: list[dict[str, Any]] = []
    if isinstance(payload, list):
        for item in payload:
            rows.extend(_flatten_history(item, inherited))
        return rows
    if not isinstance(payload, Mapping):
        return rows

    node = _decode_nested_cells(payload)
    context = _account_context(node, inherited)
    comm = node.get("comm_msg_info")
    ext = node.get("app_msg_ext_info")
    if isinstance(ext, Mapping):
        if isinstance(comm, Mapping):
            context = _account_context(comm, context)
            msgid = _first(comm, ("id", "msgid", "msg_id"))
            if msgid:
                context["msgid"] = msgid
                context["_group_key"] = "msgid:" + _text(msgid, 200)
            published = _first(comm, ("datetime", "create_time", "publish_time"))
            if published:
                context["publish_time"] = published
        rows.extend(_expand_article(ext, context, 1))
        return rows

    if _article_url(node):
        rows.extend(_expand_article(node, context))
        return rows

    found_container = False
    for key in CONTAINER_KEYS:
        child = node.get(key)
        if isinstance(child, (list, Mapping)):
            found_container = True
            rows.extend(_flatten_history(child, context))
    if found_container:
        return rows

    for key in MULTI_ITEM_KEYS:
        child = node.get(key)
        if isinstance(child, (list, Mapping)):
            rows.extend(_flatten_history(child, context))
    return rows


def _count_credential_keys(value: Any) -> int:
    if isinstance(value, Mapping):
        count = 0
        for key, child in value.items():
            normalized = re.sub(r"[^a-z0-9]+", "_", str(key).lower()).strip("_")
            if normalized in FORBIDDEN_CREDENTIAL_KEYS:
                count += 1
                continue
            count += _count_credential_keys(child)
        return count
    if isinstance(value, list):
        return sum(_count_credential_keys(item) for item in value)
    return 0


def _decode_source(data: bytes, name: str) -> tuple[Any, str]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("gb18030")
    suffix = Path(name or "history.json").suffix.lower()
    if suffix in {".csv", ".tsv"}:
        delimiter = "\t" if suffix == ".tsv" else ","
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=",\t;")
            delimiter = dialect.delimiter
        except csv.Error:
            pass
        rows = [_decode_nested_cells(row) for row in csv.DictReader(io.StringIO(text), delimiter=delimiter)]
        return rows, "csv_import"
    try:
        return json.loads(text), "json_import"
    except json.JSONDecodeError:
        rows = []
        for line in text.splitlines():
            if line.strip():
                rows.append(json.loads(line))
        return rows, "json_import"


def _source_bytes(source: bytes | bytearray | memoryview | str | Path, name: str | None) -> tuple[bytes, str]:
    if isinstance(source, (bytes, bytearray, memoryview)):
        return bytes(source), Path(name or "history.json").name
    path = Path(source).expanduser()
    return path.read_bytes(), Path(name or path.name).name


def _normalize_article(
    row: Mapping[str, Any],
    *,
    account_name: str = "",
    fakeid: str = "",
    fallback_account: str,
    default_fetch_mode: str,
    default_exported_at: str,
) -> dict[str, Any]:
    url = canonicalize_wechat_url(_article_url(row))
    resolved_account_name = _text(account_name or _first(row, ("account_name", "nickname", "nick_name", "wx_name", "mp_name", "account")), 300)
    resolved_fakeid = _text(fakeid or _first(row, ("fakeid", "fake_id")), 300)
    if not resolved_account_name and not resolved_fakeid:
        resolved_account_name = fallback_account
    account_key = _normalized_account_key(resolved_account_name, resolved_fakeid)
    published = _timestamp(_first(row, ("publish_time", "publishTime", "publish_time_iso", "create_time", "datetime", "timestamp")))
    msgid = _text(_first(row, ("msgid", "msg_id")), 200)
    appmsgid = _text(_first(row, ("appmsgid", "appmsg_id", "aid")), 200)
    itemidx = _integer(_first(row, ("itemidx", "item_idx", "idx")))
    group_key = _text(row.get("_group_key"), 300)
    if not group_key:
        if msgid:
            group_key = "msgid:" + msgid
        elif appmsgid:
            group_key = "appmsgid:" + appmsgid
        else:
            group_key = "fallback:" + sha256(f"{account_key}|{url}|{published}".encode()).hexdigest()[:24]
    copyright_type = _integer(_first(row, ("copyright_type",)))
    copyright_stat = _integer(_first(row, ("copyright_stat",)))
    is_deleted = _boolean(_first(row, ("is_deleted", "deleted"), False))
    fetch_mode = _fetch_mode(_first(row, ("fetch_mode",)), default_fetch_mode)
    exported_at = _iso_datetime(_first(row, ("exported_at", "downloaded_at", "fetched_at")), default_exported_at)
    return {
        "account_key": account_key,
        "account_name": resolved_account_name,
        "fakeid": resolved_fakeid,
        "canonical_url": url,
        "title": _text(_first(row, ("title", "name")), 1_000),
        "url": url,
        "publish_time": published,
        "author": _text(_first(row, ("author", "author_name")), 500),
        "digest": _text(_first(row, ("digest", "summary", "description")), 20_000),
        "cover_url": _text(_first(row, ("cover_url", "cover", "cover_img", "cover_img_url", "cdn_url")), 4_000),
        "body_markdown_path": _text(_first(row, ("body_markdown_path", "markdown_path", "md_path")), 4_000),
        "html_path": _text(_first(row, ("html_path",)), 4_000),
        "image_dir": _text(_first(row, ("image_dir", "images_dir")), 4_000),
        "read_count": _integer(_first(row, ("read_count", "read_num", "readNum"))),
        "like_count": _integer(_first(row, ("like_count", "like_num", "old_like_num"))),
        "share_count": _integer(_first(row, ("share_count", "share_num"))),
        "favorite_count": _integer(_first(row, ("favorite_count", "favorite_num", "fav_count", "collect_count"))),
        "comment_count": _integer(_first(row, ("comment_count", "comment_num"))),
        "comments_path": _text(_first(row, ("comments_path",)), 4_000),
        "comment_replies_path": _text(_first(row, ("comment_replies_path", "replies_path")), 4_000),
        "fetch_mode": fetch_mode,
        "credential_status": _credential_status(_first(row, ("credential_status",))),
        "exported_at": exported_at,
        "discovery_query": _text(_first(row, ("discovery_query", "query_name")), 2_000),
        "discovery_provider": _text(_first(row, ("discovery_provider", "search_provider")), 120),
        "discovered_at": _iso_datetime(_first(row, ("discovered_at",))),
        "discovery_rank": _integer(_first(row, ("discovery_rank", "search_rank"))),
        "error": _safe_error(_first(row, ("error",))),
        "msgid": msgid,
        "appmsgid": appmsgid,
        "itemidx": itemidx,
        "group_key": group_key,
        "copyright_type": copyright_type,
        "copyright_stat": copyright_stat,
        "is_deleted": int(is_deleted),
        "is_original": copyright_type == 1 and copyright_stat == 1 and not is_deleted,
        "date_status": "known" if published is not None else "pending",
    }


def _merge_records(existing: dict[str, Any], incoming: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    for key, value in incoming.items():
        if value is None or value == "":
            continue
        if key == "is_deleted":
            # A deletion marker is conservative and prevents a duplicate stale
            # row from inflating the original-article count.
            merged[key] = max(int(merged.get(key, 0)), int(value))
        else:
            merged[key] = value
    merged["is_original"] = (
        merged.get("copyright_type") == 1
        and merged.get("copyright_stat") == 1
        and not bool(merged.get("is_deleted"))
    )
    return merged


def _scope_counts(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    unique_urls = {str(row["canonical_url"]) for row in records}
    groups = {(str(row["account_key"]), str(row["group_key"])) for row in records}
    original = sum(1 for row in records if row.get("is_original"))
    return {
        "publish_groups": len(groups),
        "expanded_url_items": len(unique_urls),
        "original_articles": original,
    }


class WeChatSourcePool:
    """Offline article pool with preview-confirmed, atomic imports."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path or DEFAULT_DB_PATH).expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.executescript(SCHEMA_SQL)
            existing_columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(articles)").fetchall()
            }
            migrations = {
                "discovery_query": "TEXT NOT NULL DEFAULT ''",
                "discovery_provider": "TEXT NOT NULL DEFAULT ''",
                "discovered_at": "TEXT NOT NULL DEFAULT ''",
                "discovery_rank": "INTEGER",
            }
            for column, definition in migrations.items():
                if column not in existing_columns:
                    connection.execute(f"ALTER TABLE articles ADD COLUMN {column} {definition}")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_articles_discovered_at ON articles(discovered_at DESC)"
            )
            connection.commit()

    def _prepare_import(
        self,
        data: bytes,
        name: str,
        *,
        account_name: str = "",
        fakeid: str = "",
    ) -> dict[str, Any]:
        payload, inferred_mode = _decode_source(data, name)
        flattened = _flatten_history(payload)
        credential_fields_ignored = _count_credential_keys(payload)
        normalized: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        fallback_account = f"待识别:{Path(name).stem or 'history'}"
        exported_at = _now_iso()
        for index, row in enumerate(flattened, start=1):
            try:
                normalized.append(
                    _normalize_article(
                        row,
                        account_name=account_name,
                        fakeid=fakeid,
                        fallback_account=fallback_account,
                        default_fetch_mode=inferred_mode,
                        default_exported_at=exported_at,
                    )
                )
            except Exception as exc:
                rejected.append({"row": index, "error": _safe_error(exc)})

        deduped_map: dict[tuple[str, str], dict[str, Any]] = {}
        for row in normalized:
            key = (row["account_key"], row["canonical_url"])
            if key in deduped_map:
                deduped_map[key] = _merge_records(deduped_map[key], row)
            else:
                deduped_map[key] = row
        deduped = list(deduped_map.values())
        deduped.sort(key=lambda row: (row.get("publish_time") or 0, row.get("itemidx") or 0), reverse=True)

        accounts: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in deduped:
            accounts[row["account_key"]].append(row)
        account_summaries = []
        for account_key, rows in sorted(accounts.items()):
            counts = _scope_counts(rows)
            account_summaries.append(
                {
                    "account_key": account_key,
                    "account_name": rows[0]["account_name"],
                    "fakeid": rows[0]["fakeid"],
                    **counts,
                }
            )

        counts = _scope_counts(deduped)
        valid_count = len(normalized)
        return {
            "fingerprint": sha256(data).hexdigest(),
            "source_name": _safe_error(Path(name).name) or "history.json",
            "raw_records": len(flattened),
            "accepted_records": len(deduped),
            "rejected_records": len(rejected),
            "duplicate_records_removed": valid_count - len(deduped),
            "credential_fields_ignored": credential_fields_ignored,
            **counts,
            "accounts": account_summaries,
            "rejections": rejected[:50],
            "records": deduped,
        }

    def preview_import(
        self,
        source: bytes | bytearray | memoryview | str | Path,
        name: str | None = None,
        *,
        account_name: str = "",
        fakeid: str = "",
        preview_limit: int = 20,
    ) -> dict[str, Any]:
        data, source_name = _source_bytes(source, name)
        prepared = self._prepare_import(data, source_name, account_name=account_name, fakeid=fakeid)
        preview = {key: value for key, value in prepared.items() if key != "records"}
        with closing(self._connect()) as connection:
            existing_keys = {
                (str(row["account_key"]), str(row["canonical_url"]))
                for row in connection.execute(
                    """
                    SELECT accounts.account_key, articles.canonical_url
                    FROM articles JOIN accounts ON accounts.id = articles.account_id
                    """
                ).fetchall()
            }
        incoming_keys = {
            (str(row["account_key"]), str(row["canonical_url"]))
            for row in prepared["records"]
        }
        known_dates = [int(row["publish_time"]) for row in prepared["records"] if row.get("publish_time")]
        preview.update(
            rows_total=prepared["raw_records"],
            row_count=prepared["raw_records"],
            added_count=sum(1 for key in incoming_keys if key not in existing_keys),
            existing_count=sum(1 for key in incoming_keys if key in existing_keys),
            invalid_count=prepared["rejected_records"],
            date_start=(
                datetime.fromtimestamp(min(known_dates), SHANGHAI_TZ).date().isoformat()
                if known_dates else ""
            ),
            date_end=(
                datetime.fromtimestamp(max(known_dates), SHANGHAI_TZ).date().isoformat()
                if known_dates else ""
            ),
        )
        preview["articles"] = [self._public_record(row, include_id=False) for row in prepared["records"][: max(0, preview_limit)]]
        preview["confirmation_required"] = True
        return preview

    def import_file(
        self,
        source: bytes | bytearray | memoryview | str | Path,
        name: str | None = None,
        fingerprint: str | None = None,
        *,
        account_name: str = "",
        fakeid: str = "",
    ) -> dict[str, Any]:
        data, source_name = _source_bytes(source, name)
        actual_fingerprint = sha256(data).hexdigest()
        if not fingerprint:
            raise ValueError("preview fingerprint is required before import")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", fingerprint) or fingerprint.lower() != actual_fingerprint:
            raise ValueError("preview fingerprint does not match import bytes")
        prepared = self._prepare_import(data, source_name, account_name=account_name, fakeid=fakeid)
        if not prepared["records"]:
            raise ValueError("import contains no valid WeChat article URLs")

        connection = self._connect()
        added = 0
        existing = 0
        updated = 0
        account_ids: dict[str, int] = {}
        imported_at = _now_iso()
        try:
            connection.execute("BEGIN IMMEDIATE")
            for record in prepared["records"]:
                account_id = account_ids.get(record["account_key"])
                if account_id is None:
                    account_id = self._upsert_account(connection, record, imported_at)
                    account_ids[record["account_key"]] = account_id
                existed, changed = self._upsert_article(connection, account_id, record, imported_at)
                if existed:
                    existing += 1
                    updated += int(changed)
                else:
                    added += 1

            cursor = connection.execute(
                """
                INSERT INTO import_runs (
                    source_name, source_fingerprint, raw_records, accepted_records,
                    publish_groups, expanded_url_items, original_articles,
                    added_articles, existing_articles, updated_articles,
                    rejected_records, duplicate_records_removed,
                    credential_fields_ignored, imported_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    prepared["source_name"],
                    actual_fingerprint,
                    prepared["raw_records"],
                    prepared["accepted_records"],
                    prepared["publish_groups"],
                    prepared["expanded_url_items"],
                    prepared["original_articles"],
                    added,
                    existing,
                    updated,
                    prepared["rejected_records"],
                    prepared["duplicate_records_removed"],
                    prepared["credential_fields_ignored"],
                    imported_at,
                ),
            )
            run_id = int(cursor.lastrowid)
            for scope in prepared["accounts"]:
                account_id = account_ids[scope["account_key"]]
                connection.execute(
                    """
                    INSERT INTO import_account_scopes (
                        run_id, account_id, publish_groups,
                        expanded_url_items, original_articles
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        account_id,
                        scope["publish_groups"],
                        scope["expanded_url_items"],
                        scope["original_articles"],
                    ),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

        return {
            key: value
            for key, value in {
                **prepared,
                "records": None,
                "run_id": run_id,
                "added": added,
                "exists": existing,
                "updated": updated,
                "imported_at": imported_at,
            }.items()
            if key != "records"
        }

    def add_urls(
        self,
        urls: Iterable[str | Mapping[str, Any]],
        *,
        account_name: str = "待归属公众号",
        fakeid: str = "",
        dedupe_globally: bool = False,
    ) -> dict[str, Any]:
        connection = self._connect()
        results: list[dict[str, Any]] = []
        added = 0
        existing = 0
        now = _now_iso()
        try:
            connection.execute("BEGIN IMMEDIATE")
            for index, item in enumerate(urls, start=1):
                raw = dict(item) if isinstance(item, Mapping) else {"url": item}
                try:
                    record = _normalize_article(
                        raw,
                        account_name=account_name or _text(raw.get("account_name"), 300),
                        fakeid=fakeid or _text(raw.get("fakeid"), 300),
                        fallback_account="待归属公众号",
                        default_fetch_mode="known_url",
                        default_exported_at=now,
                    )
                    global_row = None
                    if dedupe_globally:
                        global_row = connection.execute(
                            """
                            SELECT id, account_id
                            FROM articles
                            WHERE canonical_url = ?
                            ORDER BY (body_markdown_path <> '') DESC,
                                     (publish_time IS NOT NULL) DESC,
                                     id ASC
                            LIMIT 1
                            """,
                            (record["canonical_url"],),
                        ).fetchone()
                    account_id = (
                        int(global_row["account_id"])
                        if global_row is not None
                        else self._upsert_account(connection, record, now)
                    )
                    existed, _changed = self._upsert_article(connection, account_id, record, now)
                    status = "exists" if existed else "added"
                    existing += int(existed)
                    added += int(not existed)
                    article_id = connection.execute(
                        "SELECT id FROM articles WHERE account_id = ? AND canonical_url = ?",
                        (account_id, record["canonical_url"]),
                    ).fetchone()["id"]
                    results.append(
                        {
                            "row": index,
                            "article_id": int(article_id),
                            "url": record["canonical_url"],
                            "status": status,
                            "date_status": record["date_status"],
                            "error": "",
                        }
                    )
                except Exception as exc:
                    results.append({"row": index, "article_id": None, "url": "", "status": "error", "date_status": "pending", "error": _safe_error(exc)})
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return {
            "added": added,
            "exists": existing,
            "errors": sum(1 for item in results if item["status"] == "error"),
            "results": results,
        }

    def get_stats(
        self,
        start: Any = None,
        end: Any = None,
        *,
        scope: str | Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        where, params = self._where(scope=scope, start=start, end=end)
        with closing(self._connect()) as connection:
            row = connection.execute(
                f"""
                SELECT
                    COUNT(*) AS stored_article_rows,
                    COUNT(DISTINCT articles.canonical_url) AS expanded_url_items,
                    COUNT(DISTINCT CASE WHEN articles.group_key <> '' THEN accounts.account_key || '|' || articles.group_key END) AS publish_groups,
                    SUM(CASE WHEN articles.copyright_type = 1 AND articles.copyright_stat = 1 AND articles.is_deleted = 0 THEN 1 ELSE 0 END) AS original_articles,
                    SUM(CASE WHEN articles.publish_time IS NULL THEN 1 ELSE 0 END) AS pending_date_items,
                    SUM(CASE WHEN articles.error = '' AND articles.publish_time IS NOT NULL AND articles.body_markdown_path <> '' THEN 1 ELSE 0 END) AS ready_items,
                    SUM(CASE WHEN articles.error = '' AND (articles.publish_time IS NULL OR articles.body_markdown_path = '') THEN 1 ELSE 0 END) AS pending_items,
                    SUM(CASE WHEN articles.error <> '' THEN 1 ELSE 0 END) AS failed_items,
                    SUM(CASE WHEN articles.discovered_at <> '' THEN 1 ELSE 0 END) AS discovered_items,
                    COUNT(DISTINCT CASE WHEN articles.discovered_at <> '' THEN accounts.id END) AS discovered_accounts,
                    MAX(NULLIF(articles.discovered_at, '')) AS last_discovery_at,
                    MIN(articles.publish_time) AS first_publish_time,
                    MAX(articles.publish_time) AS last_publish_time
                FROM articles
                JOIN accounts ON accounts.id = articles.account_id
                {where}
                """,
                params,
            ).fetchone()
            account_count = connection.execute(
                f"SELECT COUNT(DISTINCT accounts.id) FROM articles JOIN accounts ON accounts.id = articles.account_id {where}",
                params,
            ).fetchone()[0]
        latest_import = self.latest_import()
        total = int(row["stored_article_rows"] or 0)
        ready = int(row["ready_items"] or 0)
        pending = int(row["pending_items"] or 0)
        failed = int(row["failed_items"] or 0)
        discovered = int(row["discovered_items"] or 0)
        return {
            "total": total,
            "accounts": int(account_count or 0),
            "account_count": int(account_count or 0),
            "stored_article_rows": total,
            "publish_groups": int(row["publish_groups"] or 0),
            "expanded_url_items": int(row["expanded_url_items"] or 0),
            "original_articles": int(row["original_articles"] or 0),
            "pending_date_items": int(row["pending_date_items"] or 0),
            "ready": ready,
            "pending": pending,
            "failed": failed,
            "discovered_total": discovered,
            "discovered_accounts": int(row["discovered_accounts"] or 0),
            "last_discovery_at": str(row["last_discovery_at"] or ""),
            "first_publish_time": row["first_publish_time"],
            "last_publish_time": row["last_publish_time"],
            "last_import_at": str((latest_import or {}).get("imported_at") or ""),
        }

    def list_articles(
        self,
        scope: str | Mapping[str, Any] | None = None,
        start: Any = None,
        end: Any = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        where, params = self._where(scope=scope, start=start, end=end)
        limit = max(1, min(int(limit), 10_000))
        offset = max(0, int(offset))
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"""
                SELECT articles.*, accounts.account_name, accounts.fakeid
                FROM articles
                JOIN accounts ON accounts.id = articles.account_id
                {where}
                ORDER BY articles.publish_time IS NULL, articles.publish_time DESC, articles.id DESC
                LIMIT ? OFFSET ?
                """,
                (*params, limit, offset),
            ).fetchall()
        return [self._public_record(dict(row), include_id=True) for row in rows]

    def records_for_window(
        self,
        start: Any,
        end: Any,
        *,
        scope: str | Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        return self.list_articles(scope=scope, start=start, end=end, limit=10_000, offset=0)

    def query_recent(
        self,
        days: int = 7,
        *,
        scope: str | Mapping[str, Any] | None = None,
        limit: int = 100,
        offset: int = 0,
        as_of: Any = None,
    ) -> list[dict[str, Any]]:
        end_stamp = _range_timestamp(as_of, end=True) if as_of is not None else int(datetime.now(timezone.utc).timestamp())
        start_stamp = end_stamp - max(1, int(days)) * 86_400 + 1
        return self.list_articles(scope=scope, start=start_stamp, end=end_stamp, limit=limit, offset=offset)

    def query_recent_with_summary(
        self,
        days: int = 7,
        *,
        scope: str | Mapping[str, Any] | None = None,
        limit: int = 100,
        as_of: Any = None,
    ) -> dict[str, Any]:
        end_stamp = _range_timestamp(as_of, end=True) if as_of is not None else int(datetime.now(timezone.utc).timestamp())
        start_stamp = end_stamp - max(1, int(days)) * 86_400 + 1
        articles = self.list_articles(scope=scope, start=start_stamp, end=end_stamp, limit=limit)
        return {
            "window_days": max(1, int(days)),
            "start": start_stamp,
            "end": end_stamp,
            "summary": self.get_stats(start_stamp, end_stamp, scope=scope),
            "articles": articles,
        }

    def remove(self, article_id: int) -> bool:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute("DELETE FROM articles WHERE id = ?", (int(article_id),))
            removed = cursor.rowcount > 0
            connection.commit()
            return removed
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def latest_import(self) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM import_runs ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def _where(
        self,
        *,
        scope: str | Mapping[str, Any] | None,
        start: Any,
        end: Any,
    ) -> tuple[str, tuple[Any, ...]]:
        conditions: list[str] = []
        params: list[Any] = []
        if scope and scope != "all":
            if isinstance(scope, Mapping):
                fakeid = _text(scope.get("fakeid"), 300)
                account_name = _text(scope.get("account_name"), 300)
                if fakeid:
                    conditions.append("accounts.fakeid = ?")
                    params.append(fakeid)
                elif account_name:
                    conditions.append("accounts.account_name = ?")
                    params.append(account_name)
            else:
                scope_text = _text(scope, 300)
                if scope_text == "pending":
                    conditions.append("articles.error = '' AND (articles.publish_time IS NULL OR articles.body_markdown_path = '')")
                elif scope_text == "failed":
                    conditions.append("articles.error <> ''")
                elif scope_text == "ready":
                    conditions.append("articles.error = '' AND articles.publish_time IS NOT NULL AND articles.body_markdown_path <> ''")
                elif scope_text == "discovered":
                    conditions.append("articles.discovered_at <> ''")
                elif scope_text.startswith("fakeid:"):
                    conditions.append("accounts.account_key = ?")
                    params.append(scope_text.casefold())
                else:
                    conditions.append("(accounts.account_name = ? OR accounts.fakeid = ?)")
                    params.extend([scope_text, scope_text])
        if start is not None:
            conditions.append("articles.publish_time >= ?")
            params.append(_range_timestamp(start, end=False))
        if end is not None:
            conditions.append("articles.publish_time <= ?")
            params.append(_range_timestamp(end, end=True))
        return ("WHERE " + " AND ".join(conditions) if conditions else "", tuple(params))

    @staticmethod
    def _public_record(row: Mapping[str, Any], *, include_id: bool) -> dict[str, Any]:
        result = {field: row.get(field) for field in ARTICLE_OUTPUT_FIELDS}
        if include_id:
            result = {
                "article_id": int(row["id"]),
                "date_status": row.get("date_status") or ("known" if row.get("publish_time") is not None else "pending"),
                **result,
            }
        return result

    @staticmethod
    def _upsert_account(connection: sqlite3.Connection, record: Mapping[str, Any], now: str) -> int:
        row = connection.execute(
            "SELECT id, account_name, fakeid FROM accounts WHERE account_key = ?",
            (record["account_key"],),
        ).fetchone()
        if row:
            connection.execute(
                "UPDATE accounts SET account_name = ?, fakeid = ?, updated_at = ? WHERE id = ?",
                (
                    record.get("account_name") or row["account_name"],
                    record.get("fakeid") or row["fakeid"],
                    now,
                    row["id"],
                ),
            )
            return int(row["id"])
        cursor = connection.execute(
            "INSERT INTO accounts (account_key, account_name, fakeid, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (record["account_key"], record["account_name"], record["fakeid"], now, now),
        )
        return int(cursor.lastrowid)

    @staticmethod
    def _upsert_article(
        connection: sqlite3.Connection,
        account_id: int,
        record: Mapping[str, Any],
        now: str,
    ) -> tuple[bool, bool]:
        existing = connection.execute(
            "SELECT * FROM articles WHERE account_id = ? AND canonical_url = ?",
            (account_id, record["canonical_url"]),
        ).fetchone()
        storage_fields = (
            "canonical_url", "title", "url", "publish_time", "author", "digest", "cover_url",
            "body_markdown_path", "html_path", "image_dir", "read_count", "like_count",
            "share_count", "favorite_count", "comment_count", "comments_path",
            "comment_replies_path", "fetch_mode", "credential_status", "exported_at", "error",
            "discovery_query", "discovery_provider", "discovered_at", "discovery_rank",
            "msgid", "appmsgid", "itemidx", "group_key", "copyright_type", "copyright_stat",
            "is_deleted", "date_status",
        )
        values = {field: record.get(field) for field in storage_fields}
        changed = False
        created_at = now
        if existing:
            created_at = existing["created_at"]
            for field in storage_fields:
                incoming = values[field]
                if incoming is None or (isinstance(incoming, str) and not incoming):
                    if field != "error":
                        values[field] = existing[field]
                if values[field] != existing[field]:
                    changed = True
        placeholders = ", ".join("?" for _ in storage_fields)
        updates = ", ".join(f"{field} = excluded.{field}" for field in storage_fields)
        connection.execute(
            f"""
            INSERT INTO articles (account_id, {', '.join(storage_fields)}, created_at, updated_at)
            VALUES (?, {placeholders}, ?, ?)
            ON CONFLICT(account_id, canonical_url) DO UPDATE SET {updates}, updated_at = excluded.updated_at
            """,
            (account_id, *(values[field] for field in storage_fields), created_at, now),
        )
        return existing is not None, changed


def _range_timestamp(value: Any, *, end: bool) -> int:
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
        parsed = date.fromisoformat(value.strip())
        boundary = time.max if end else time.min
        return int(datetime.combine(parsed, boundary, SHANGHAI_TZ).timestamp())
    if isinstance(value, date) and not isinstance(value, datetime):
        boundary = time.max if end else time.min
        return int(datetime.combine(value, boundary, SHANGHAI_TZ).timestamp())
    stamp = _timestamp(value)
    if stamp is None:
        raise ValueError("invalid date/time boundary")
    return stamp


Pool = WeChatSourcePool


def open_pool(db_path: str | Path | None = None) -> WeChatSourcePool:
    return WeChatSourcePool(db_path)


__all__ = [
    "ARTICLE_OUTPUT_FIELDS",
    "DEFAULT_DB_PATH",
    "Pool",
    "WeChatSourcePool",
    "canonicalize_wechat_url",
    "open_pool",
]
