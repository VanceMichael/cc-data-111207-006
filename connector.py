"""Persistent store-collection connector for the partner's cursor API.

The API exposes opaque links.  This module never parses or rebuilds a link;
it follows ``nextLink`` until one window reaches ``deltaLink`` and only then
publishes the result to the formal view.

State is written after every accepted page, so an interrupted window can be
resumed from the exact link that failed rather than from the start of the
window or from a cursor belonging to another window.
"""

import copy
import json
import os
import tempfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class _NoRedirect(HTTPRedirectHandler):
    """Refuse HTTP redirects; the connector only follows links in payloads."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = build_opener(_NoRedirect)

SCHEMA = 1
RESERVED_KEYS = {"id", "version", "removed"}
FULL_LINK = "/full"


class SyncError(Exception):
    """A window could not be completed; persisted progress is unchanged."""


class UnsafeLink(SyncError):
    """A link points to another origin or is not a usable link."""


class WindowLoop(SyncError):
    """A nextLink traversed back into the current window."""


class SourceResponseError(SyncError):
    """The source returned an error or a response that breaks the protocol."""


def _is_version(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _conflict(kind, store_id, incoming_version, current_version, link, entry):
    return {
        "kind": kind,
        "id": store_id,
        "version": incoming_version,
        "current_version": current_version,
        "link": link,
        "entry": copy.deepcopy(entry),
    }


def apply_entry(entry, staged, known, link):
    """Apply one ordered operation to a window's working copy.

    ``staged`` is the live projection being built, while ``known`` also keeps
    tombstones (deleted stores) so a later reopen can be checked against the
    version it last had.  A conflicting operation is returned to the caller
    and is not applied.
    """
    if not isinstance(entry, dict):
        raise SourceResponseError("value 中的操作必须是对象")
    store_id = entry.get("id")
    if not isinstance(store_id, str) or not store_id:
        raise SourceResponseError("操作缺少有效的 id")
    version = entry.get("version")
    if not _is_version(version):
        raise SourceResponseError(f"门店 {store_id} 缺少有效的 version")
    if "removed" in entry and not isinstance(entry["removed"], bool):
        raise SourceResponseError(f"门店 {store_id} 的 removed 不是布尔值")

    removed = entry.get("removed") is True
    current = known.get(store_id)
    if current is None:
        current_version = -1
        current_live = False
        current_fields = {}
    else:
        current_version = current.get("version")
        current_live = not current.get("deleted", False)
        current_fields = copy.deepcopy(current.get("fields", {})) if current_live else {}

    if removed:
        candidate_live = False
        candidate_fields = {}
    else:
        # Absent fields retain the old value; an explicit null is a value.
        candidate_live = True
        candidate_fields = copy.deepcopy(current_fields)
        for key, value in entry.items():
            if key in RESERVED_KEYS:
                continue
            candidate_fields[key] = copy.deepcopy(value)

    if current is not None:
        if version < current_version:
            return _conflict(
                "version_regression", store_id, version, current_version, link, entry
            )
        if version == current_version:
            same_live = candidate_live == current_live
            same_fields = candidate_fields == current_fields
            if same_live and (not candidate_live or same_fields):
                # An identical replay at the same version is idempotent.
                return None
            return _conflict(
                "same_version_conflict", store_id, version, current_version, link, entry
            )

    if removed:
        tombstone = {
            "id": store_id,
            "version": version,
            "deleted": True,
            "fields": {},
        }
        known[store_id] = tombstone
        staged.pop(store_id, None)
    else:
        store = {
            "id": store_id,
            "version": version,
            "deleted": False,
            "fields": candidate_fields,
        }
        known[store_id] = copy.deepcopy(store)
        staged[store_id] = copy.deepcopy(store)
    return None


class Connector:
    """One-source, one-state-file collector.

    Call :meth:`sync` once per polling run.  A single call completes one
    window (full or delta).  If the window cannot be completed, the formal
    view is left untouched and the exact next link is persisted for the next
    call.
    """

    def __init__(self, base, state_path, full_link=FULL_LINK, max_pages=1000):
        self.base = base.rstrip("/")
        self.state_path = Path(state_path)
        self.full_link = full_link
        self.max_pages = max_pages
        self.state = self._load()

    # ------------------------------------------------------------------
    # persistence

    def _empty_state(self):
        return {
            "schema": SCHEMA,
            "view": {},
            "known": {},
            "cursor": None,
            "pending": None,
            "conflicts": [],
            "completed_windows": 0,
            "next_window_id": 1,
        }

    def _load(self):
        if not self.state_path.exists():
            return self._empty_state()
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SyncError(f"无法读取同步状态: {error}") from error
        if not isinstance(state, dict):
            raise SyncError("同步状态不是 JSON 对象")
        if state.get("schema", SCHEMA) != SCHEMA:
            raise SyncError("同步状态版本不受支持")
        defaults = self._empty_state()
        for key, value in defaults.items():
            state.setdefault(key, copy.deepcopy(value))
        state["schema"] = SCHEMA
        if not isinstance(state["view"], dict) or not isinstance(state["known"], dict):
            raise SyncError("同步状态中的视图或版本记录无效")
        if not isinstance(state["conflicts"], list):
            raise SyncError("同步状态中的冲突记录无效")
        return state

    def _save(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(self.state, ensure_ascii=False, indent=2, sort_keys=False)
        descriptor, temporary = tempfile.mkstemp(
            prefix=self.state_path.name + ".", dir=str(self.state_path.parent)
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.state_path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    # ------------------------------------------------------------------
    # links and transport

    def _resolve(self, link):
        """Resolve an opaque link against the configured origin.

        Relative links and absolute links on the configured origin are fine.
        A link that changes origin is rejected before any request is made.
        """
        if not isinstance(link, str) or not link.strip():
            raise UnsafeLink(f"无效的链接: {link!r}")
        url = urljoin(self.base + "/", link)
        target = urlsplit(url)
        origin = urlsplit(self.base)
        if target.scheme.lower() not in ("http", "https"):
            raise UnsafeLink(f"链接使用了不支持的协议: {link!r}")
        if (
            target.scheme.lower() != origin.scheme.lower()
            or target.netloc.lower() != origin.netloc.lower()
        ):
            raise UnsafeLink(f"链接跳转到其他源站: {link!r}")
        return url

    def _identity(self, link):
        """Stable identity of a link for loop detection within one window.

        The URL is only split, never rebuilt into a request.  Using path and
        query keeps identities valid if the process restarts against the same
        source on a different local port.
        """
        url = self._resolve(link)
        target = urlsplit(url)
        return target.path + ("?" + target.query if target.query else "")

    def _request(self, link):
        url = self._resolve(link)
        request = Request(url, headers={"Accept": "application/json"}, method="GET")
        try:
            response = _OPENER.open(request, timeout=5)
        except HTTPError as error:
            response = error
        except (URLError, OSError, TimeoutError) as error:
            raise SyncError(f"读取 {link} 失败: {error}") from error
        try:
            return response.code, dict(response.headers.items()), response.read()
        except (URLError, OSError, TimeoutError) as error:
            raise SyncError(f"读取 {link} 失败: {error}") from error
        finally:
            response.close()

    @staticmethod
    def _parse(body, link):
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SourceResponseError(f"{link} 的响应不是有效 JSON") from error
        if not isinstance(payload, dict):
            raise SourceResponseError(f"{link} 的响应不是 JSON 对象")
        if not isinstance(payload.get("value", []), list):
            raise SourceResponseError(f"{link} 的 value 不是数组")
        return payload

    @staticmethod
    def _restart_link(body):
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if isinstance(payload, dict) and isinstance(payload.get("restart"), str):
            return payload["restart"]
        return None

    # ------------------------------------------------------------------
    # windows

    def _new_pending(self, mode, start, staged, known):
        window_id = int(self.state.get("next_window_id", 1))
        self.state["next_window_id"] = window_id + 1
        return {
            "id": window_id,
            "mode": mode,
            "start": start,
            "next": start,
            "seen": [],
            "seen_urls": [],
            "staged": staged,
            "known": known,
            "entries": 0,
        }

    def _begin_next_window(self):
        if self.state.get("cursor") is None:
            pending = self._new_pending("full", self.full_link, {}, {})
        else:
            pending = self._new_pending(
                "delta",
                self.state["cursor"],
                copy.deepcopy(self.state["view"]),
                copy.deepcopy(self.state["known"]),
            )
        self._resolve(pending["next"])
        self.state["pending"] = pending
        return pending

    def _begin_full_after_expiry(self, body):
        restart = self._restart_link(body)
        start = restart if restart is not None else self.full_link
        self._resolve(start)
        # The expired incremental cursor and its partial delta window are
        # discarded.  The old formal view stays readable until the new full
        # snapshot completes.
        self.state["cursor"] = None
        self.state["pending"] = self._new_pending("full", start, {}, {})

    def sync(self):
        """Advance one window and return its status.

        Starts a full snapshot when there is no cursor, resumes a persisted
        pending window when there is one, and otherwise opens a delta window
        from the saved cursor.  A 410 on a delta window immediately starts a
        fresh full snapshot.
        """
        pages = 0
        while True:
            pending = self.state.get("pending")
            if pending is None:
                self._begin_next_window()
                self._save()
                pending = self.state["pending"]

            link = pending["next"]
            status, headers, body = self._request(link)

            if status == 410:
                # A 410 is only a cursor expiry for a delta window.  Trying to
                # open a fresh full window would hide a broken full endpoint.
                if pending["mode"] != "delta":
                    raise SourceResponseError(f"全量窗口收到 410: {link}")
                self._begin_full_after_expiry(body)
                self._save()
                continue
            if status != 200:
                raise SourceResponseError(f"来源返回状态 {status}: {link}")

            payload = self._parse(body, link)
            next_link = payload.get("nextLink")
            delta_link = payload.get("deltaLink")
            if next_link is not None and delta_link is not None:
                raise SourceResponseError(
                    f"{link} 同时返回了 nextLink 和 deltaLink"
                )
            if next_link is not None:
                self._resolve(next_link)
            if delta_link is not None:
                self._resolve(delta_link)

            staged = copy.deepcopy(pending["staged"])
            known = copy.deepcopy(pending["known"])
            # A conflict from an earlier attempt at this same page of this
            # window is already recorded once; do not append it again on a
            # resumed run.
            conflicts = [
                item
                for item in self.state["conflicts"]
                if item.get("window") != pending.get("id")
                or item.get("link") != link
            ]
            entries = payload.get("value", [])
            for entry in entries:
                conflict = apply_entry(entry, staged, known, link)
                if conflict is not None:
                    conflict["window"] = pending.get("id")
                    conflicts.append(conflict)

            if delta_link is not None:
                # Only reaching a deltaLink confirms the whole window.
                self.state["view"] = staged
                self.state["known"] = known
                self.state["cursor"] = delta_link
                self.state["conflicts"] = conflicts
                self.state["pending"] = None
                self.state["completed_windows"] = (
                    int(self.state.get("completed_windows", 0)) + 1
                )
                self._save()
                return self.status()

            if next_link is not None:
                current_identity = self._identity(link)
                visited = list(pending.get("seen_urls", []))
                if current_identity in visited:
                    raise WindowLoop(f"nextLink 回到已访问链接: {link}")
                visited.append(current_identity)
                if self._identity(next_link) in visited:
                    # An empty delta that returns its own deltaLink is handled
                    # by the branch above; only nextLink traversal loops here.
                    raise WindowLoop(f"nextLink 回环: {next_link}")
                pending["staged"] = staged
                pending["known"] = known
                pending["seen"] = list(pending.get("seen", [])) + [link]
                pending["seen_urls"] = visited
                pending["next"] = next_link
                pending["entries"] = int(pending.get("entries", 0)) + len(entries)
                self.state["conflicts"] = conflicts
                self._save()
                pages += 1
                if pages > self.max_pages:
                    raise WindowLoop(f"窗口超过 {self.max_pages} 页")
                continue

            raise SourceResponseError(f"{link} 既没有 nextLink 也没有 deltaLink")

    # ------------------------------------------------------------------
    # display

    @staticmethod
    def _flatten(stores):
        result = {}
        for store_id in sorted(stores):
            store = stores[store_id]
            item = {"id": store_id, "version": store.get("version")}
            item.update(copy.deepcopy(store.get("fields", {})))
            result[store_id] = item
        return result

    def status(self):
        """Return a JSON-ready view of the formal set, pending window and conflicts."""
        pending = self.state.get("pending")
        result = {
            "视图": self._flatten(self.state.get("view", {})),
            "游标": self.state.get("cursor"),
            "待确认窗口": None,
            "冲突": copy.deepcopy(self.state.get("conflicts", [])),
            "已完成窗口": int(self.state.get("completed_windows", 0)),
        }
        if pending is not None:
            result["待确认窗口"] = {
                "窗口编号": pending.get("id"),
                "模式": "全量" if pending["mode"] == "full" else "增量",
                "起点": pending["start"],
                "下一条": pending["next"],
                "已遍历链接": list(pending.get("seen", [])),
                "已处理操作": int(pending.get("entries", 0)),
                "暂存门店": self._flatten(pending.get("staged", {})),
            }
        return result
