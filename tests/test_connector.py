import copy
import json
import tempfile
import unittest
from pathlib import Path

from connector import (
    Connector,
    SourceResponseError,
    SyncError,
    UnsafeLink,
    WindowLoop,
    apply_entry,
)
from lab import serving
from source import Source


def page(value=None, next_link=None, delta_link=None):
    result = {"value": value or []}
    if next_link is not None:
        result["nextLink"] = next_link
    if delta_link is not None:
        result["deltaLink"] = delta_link
    return result


def fixture(pages, expired="/delta?token=expired"):
    return {"pages": pages, "expired": expired}


def first_window():
    return fixture(
        {
            "/full": page(
                [
                    {"id": "shop-a", "version": 1, "name": "青禾店", "phone": "100"}
                ],
                next_link="/page?token=opaque-x",
            ),
            "/page?token=opaque-x": page(
                [{"id": "shop-b", "version": 1, "name": "河湾店"}],
                delta_link="/delta?token=delta-1",
            ),
            "/delta?token=delta-1": page(delta_link="/delta?token=delta-2"),
            "/delta?token=delta-2": page(delta_link="/delta?token=delta-2"),
        }
    )


class ConnectorTestBase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state = Path(self.temporary.name) / "state" / "sync.json"

    def make(self, base, state=None):
        return Connector(base, state or self.state)

    def full(self, data):
        with serving(Source(data)) as base:
            result = self.make(base).sync()
        self.assertEqual(result["待确认窗口"], None)
        return result


class FullWindowTests(ConnectorTestBase):
    def test_full_window_follows_next_links_and_stops_at_delta_link(self):
        data = first_window()
        result = self.full(data)
        self.assertEqual(set(result["视图"]), {"shop-a", "shop-b"})
        self.assertEqual(result["视图"]["shop-a"]["phone"], "100")
        self.assertEqual(result["游标"], "/delta?token=delta-1")
        self.assertEqual(result["冲突"], [])
        self.assertEqual(result["已完成窗口"], 1)

    def test_first_page_alone_does_not_publish(self):
        data = first_window()
        source = Source(data)
        source.fail_path = "/page?token=opaque-x"
        with serving(source) as base:
            with self.assertRaises(SyncError):
                self.make(base).sync()
            pending = self.make(base).status()
        self.assertEqual(pending["视图"], {})
        self.assertEqual(pending["待确认窗口"]["模式"], "全量")
        self.assertEqual(pending["待确认窗口"]["下一条"], "/page?token=opaque-x")
        self.assertEqual(set(pending["待确认窗口"]["暂存门店"]), {"shop-a"})
        self.assertIsNone(json.loads(self.state.read_text(encoding="utf-8"))["cursor"])

    def test_restart_resumes_pending_window_from_failed_link(self):
        data = first_window()
        source = Source(data)
        source.fail_path = "/page?token=opaque-x"
        with serving(source) as base:
            with self.assertRaises(SyncError):
                self.make(base).sync()
        source.fail_path = None
        with serving(source) as base:
            result = self.make(base).sync()
        self.assertEqual(set(result["视图"]), {"shop-a", "shop-b"})
        self.assertEqual(result["游标"], "/delta?token=delta-1")
        self.assertEqual(source.requests.count("/full"), 1)
        self.assertEqual(
            source.requests,
            ["/full", "/page?token=opaque-x", "/page?token=opaque-x"],
        )

    def test_missing_page_keeps_previous_formal_view(self):
        data = first_window()
        with serving(Source(data)) as base:
            self.make(base).sync()
        data["pages"]["/delta?token=delta-1"] = page(next_link="/missing")
        with serving(Source(data)) as base:
            with self.assertRaises(SyncError):
                self.make(base).sync()
            status = self.make(base).status()
        self.assertEqual(set(status["视图"]), {"shop-a", "shop-b"})
        self.assertEqual(status["待确认窗口"]["下一条"], "/missing")

    def test_same_origin_absolute_links_are_followed(self):
        data = first_window()
        with serving(Source(data)) as base:
            data["pages"]["/full"]["nextLink"] = base + "/page?token=opaque-x"
            data["pages"]["/page?token=opaque-x"]["deltaLink"] = (
                base + "/delta?token=delta-1"
            )
            result = self.make(base).sync()
        self.assertEqual(set(result["视图"]), {"shop-a", "shop-b"})
        self.assertEqual(result["游标"], base + "/delta?token=delta-1")

    def test_cross_origin_link_is_rejected_without_request(self):
        data = first_window()
        data["pages"]["/full"]["nextLink"] = "https://other.example/collect"
        source = Source(data)
        with serving(source) as base:
            with self.assertRaises(UnsafeLink):
                self.make(base).sync()
        self.assertEqual(source.requests, ["/full"])

    def test_next_link_loop_in_one_window_is_rejected(self):
        data = fixture(
            {
                "/full": page([], next_link="/page?token=x"),
                "/page?token=x": page([], next_link="/page?token=y"),
                "/page?token=y": page([], next_link="/page?token=x"),
            }
        )
        source = Source(data)
        with serving(source) as base:
            with self.assertRaises(WindowLoop):
                self.make(base).sync()
            status = self.make(base).status()
        self.assertEqual(status["视图"], {})
        self.assertEqual(status["待确认窗口"]["下一条"], "/page?token=y")

    def test_next_link_loop_survives_a_restart_against_a_new_local_port(self):
        data = fixture(
            {
                "/full": page([], next_link="/page?token=x"),
                "/page?token=x": page(
                    [{"id": "shop-a", "version": 1}], next_link="/page?token=y"
                ),
                "/page?token=y": page([], next_link="/page?token=x"),
            }
        )
        source = Source(data)
        source.fail_path = "/page?token=y"
        with serving(source) as base:
            with self.assertRaises(SyncError):
                self.make(base).sync()
        source.fail_path = None
        with serving(source) as base:
            with self.assertRaises(WindowLoop):
                self.make(base).sync()

    def test_window_must_end_in_a_link(self):
        data = fixture(
            {"/full": page([{"id": "shop-a", "version": 1}])}
        )
        with serving(Source(data)) as base:
            with self.assertRaises(SourceResponseError):
                self.make(base).sync()

    def test_both_links_in_one_response_is_a_protocol_error(self):
        data = fixture(
            {
                "/full": page(
                    [{"id": "shop-a", "version": 1}],
                    next_link="/page",
                    delta_link="/delta?token=x",
                )
            }
        )
        with serving(Source(data)) as base:
            with self.assertRaises(SourceResponseError):
                self.make(base).sync()


class DeltaTests(ConnectorTestBase):
    def test_delta_retains_absent_fields_and_clears_explicit_null(self):
        data = first_window()
        data["pages"]["/delta?token=delta-1"] = page(
            [
                {"id": "shop-a", "version": 2, "phone": None},
                {"id": "shop-b", "version": 2, "removed": True},
            ],
            delta_link="/delta?token=delta-2",
        )
        with serving(Source(data)) as base:
            self.make(base).sync()
            result = self.make(base).sync()
        self.assertEqual(set(result["视图"]), {"shop-a"})
        self.assertEqual(result["视图"]["shop-a"]["name"], "青禾店")
        self.assertIsNone(result["视图"]["shop-a"]["phone"])
        self.assertEqual(result["游标"], "/delta?token=delta-2")
        self.assertEqual(result["冲突"], [])

    def test_update_then_remove_then_reopen_inside_one_window(self):
        data = first_window()
        data["pages"]["/delta?token=delta-1"] = page(
            [
                {"id": "shop-a", "version": 2, "name": "改名店"},
                {"id": "shop-a", "version": 3, "removed": True},
                {"id": "shop-a", "version": 4, "name": "重开店"},
            ],
            delta_link="/delta?token=delta-2",
        )
        with serving(Source(data)) as base:
            self.make(base).sync()
            result = self.make(base).sync()
        self.assertEqual(result["视图"]["shop-a"]["version"], 4)
        self.assertEqual(result["视图"]["shop-a"]["name"], "重开店")

    def test_removed_store_can_be_reopened_in_a_later_window(self):
        data = first_window()
        data["pages"]["/delta?token=delta-1"] = page(
            [{"id": "shop-b", "version": 2, "removed": True}],
            delta_link="/delta?token=delta-2",
        )
        data["pages"]["/delta?token=delta-2"] = page(
            [{"id": "shop-b", "version": 3, "name": "重开河湾店"}],
            delta_link="/delta?token=delta-3",
        )
        data["pages"]["/delta?token=delta-3"] = page(
            delta_link="/delta?token=delta-3"
        )
        with serving(Source(data)) as base:
            self.make(base).sync()
            self.make(base).sync()
            result = self.make(base).sync()
        self.assertEqual(result["视图"]["shop-b"]["version"], 3)
        self.assertEqual(result["视图"]["shop-b"]["name"], "重开河湾店")
        self.assertEqual(result["游标"], "/delta?token=delta-3")

    def test_empty_delta_returning_its_own_link_is_normal_completion(self):
        data = first_window()
        with serving(Source(data)) as base:
            self.make(base).sync()
            result = self.make(base).sync()
        self.assertEqual(result["待确认窗口"], None)
        self.assertEqual(result["游标"], "/delta?token=delta-2")
        self.assertEqual(result["已完成窗口"], 2)

    def test_next_link_that_repeats_the_start_of_the_previous_window_is_not_a_loop(self):
        data = fixture(
            {
                "/full": page(
                    [{"id": "shop-a", "version": 1}],
                    delta_link="/delta?token=only",
                ),
                "/delta?token=only": page(
                    [{"id": "shop-b", "version": 1}],
                    next_link="/delta?token=only-2",
                ),
                "/delta?token=only-2": page(delta_link="/delta?token=only-3"),
                "/delta?token=only-3": page(delta_link="/delta?token=only-3"),
            }
        )
        with serving(Source(data)) as base:
            self.make(base).sync()
            result = self.make(base).sync()
        self.assertEqual(set(result["视图"]), {"shop-a", "shop-b"})
        self.assertEqual(result["游标"], "/delta?token=only-3")


class ConflictTests(ConnectorTestBase):
    def setUp(self):
        super().setUp()
        self.staged = {}
        self.known = {}

    def apply(self, entries):
        conflicts = []
        for entry in entries:
            conflict = apply_entry(entry, self.staged, self.known, "/delta")
            if conflict is not None:
                conflicts.append(conflict)
        return conflicts

    def test_version_regression_is_recorded_and_not_applied(self):
        self.apply([{"id": "shop-a", "version": 5, "name": "新名"}])
        conflicts = self.apply([{"id": "shop-a", "version": 4, "name": "旧名"}])
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["kind"], "version_regression")
        self.assertEqual(conflicts[0]["version"], 4)
        self.assertEqual(self.staged["shop-a"]["fields"]["name"], "新名")

    def test_same_version_with_different_content_is_recorded(self):
        self.apply([{"id": "shop-a", "version": 2, "name": "甲", "phone": "1"}])
        conflicts = self.apply([{"id": "shop-a", "version": 2, "name": "乙"}])
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["kind"], "same_version_conflict")
        self.assertEqual(self.staged["shop-a"]["fields"]["name"], "甲")

    def test_same_version_identical_replay_is_idempotent(self):
        self.apply([{"id": "shop-a", "version": 2, "name": "甲"}])
        self.assertEqual(
            self.apply([{"id": "shop-a", "version": 2, "name": "甲"}]), []
        )

    def test_lower_version_reopen_after_removal_is_a_conflict(self):
        self.apply(
            [
                {"id": "shop-a", "version": 1, "name": "旧店"},
                {"id": "shop-a", "version": 2, "removed": True},
            ]
        )
        conflicts = self.apply([{"id": "shop-a", "version": 1, "name": "旧店"}])
        self.assertEqual(conflicts[0]["kind"], "version_regression")
        self.assertNotIn("shop-a", self.staged)

    def test_conflicts_survive_persistence_and_are_shown_by_status(self):
        data = first_window()
        data["pages"]["/delta?token=delta-1"] = page(
            [
                {"id": "shop-a", "version": 1, "name": "同版本改名"},
                {"id": "shop-a", "version": 0, "name": "更早版本"},
            ],
            delta_link="/delta?token=delta-2",
        )
        with serving(Source(data)) as base:
            self.make(base).sync()
            result = self.make(base).sync()
        self.assertEqual(len(result["冲突"]), 2)
        self.assertEqual(
            [item["kind"] for item in result["冲突"]],
            ["same_version_conflict", "version_regression"],
        )
        self.assertEqual(result["视图"]["shop-a"]["name"], "青禾店")
        # The conflict log survives a fresh Connector instance.
        self.assertEqual(len(self.make("http://127.0.0.1:1").status()["冲突"]), 2)


class ExpiryRebuildTests(ConnectorTestBase):
    def new_snapshot(self):
        return fixture(
            {
                "/full": page(
                    [{"id": "shop-c", "version": 1, "name": "新全量店"}],
                    next_link="/page?token=new-x",
                ),
                "/page?token=new-x": page(
                    [{"id": "shop-d", "version": 1, "name": "新全量二店"}],
                    delta_link="/delta?token=new-1",
                ),
                "/delta?token=new-1": page(
                    delta_link="/delta?token=new-1"
                ),
            },
            expired="/delta?token=delta-1",
        )

    def test_expiry_starts_a_new_full_window_and_switches_after_completion(self):
        data = first_window()
        with serving(Source(data)) as base:
            self.make(base).sync()
        old = self.new_snapshot()
        with serving(Source(old)) as base:
            result = self.make(base).sync()
        self.assertEqual(set(result["视图"]), {"shop-c", "shop-d"})
        self.assertEqual(result["游标"], "/delta?token=new-1")
        self.assertEqual(result["待确认窗口"], None)
        self.assertEqual(result["已完成窗口"], 2)

        with serving(Source(old)) as base:
            result = self.make(base).sync()
        self.assertEqual(result["游标"], "/delta?token=new-1")
        self.assertEqual(set(result["视图"]), {"shop-c", "shop-d"})

    def test_expiry_with_old_view_readable_during_failed_rebuild(self):
        data = first_window()
        with serving(Source(data)) as base:
            self.make(base).sync()
        old = self.new_snapshot()
        source = Source(old)
        source.fail_path = "/full"
        with serving(source) as base:
            with self.assertRaises(SyncError):
                self.make(base).sync()
            status = self.make(base).status()
        self.assertEqual(set(status["视图"]), {"shop-a", "shop-b"})
        self.assertEqual(status["待确认窗口"]["模式"], "全量")
        self.assertEqual(status["待确认窗口"]["暂存门店"], {})
        self.assertIsNone(status["游标"])
        persisted = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertIsNone(persisted["cursor"])
        self.assertEqual(persisted["pending"]["mode"], "full")

        source.fail_path = None
        with serving(source) as base:
            result = self.make(base).sync()
        self.assertEqual(set(result["视图"]), {"shop-c", "shop-d"})
        self.assertEqual(result["游标"], "/delta?token=new-1")

    def test_old_cursor_is_not_mixed_with_the_new_full_window(self):
        data = first_window()
        with serving(Source(data)) as base:
            self.make(base).sync()
        old = self.new_snapshot()
        source = Source(old)
        source.fail_path = "/page?token=new-x"
        with serving(source) as base:
            with self.assertRaises(SyncError):
                self.make(base).sync()
        persisted = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(persisted["pending"]["mode"], "full")
        self.assertEqual(persisted["pending"]["next"], "/page?token=new-x")
        self.assertIsNone(persisted["cursor"])

        source.fail_path = None
        with serving(source) as base:
            result = self.make(base).sync()
        self.assertEqual(result["游标"], "/delta?token=new-1")
        self.assertEqual(set(result["视图"]), {"shop-c", "shop-d"})

    def test_410_during_a_resumed_delta_window_restarts_full(self):
        data = first_window()
        with serving(Source(data)) as base:
            self.make(base).sync()
        data["pages"]["/delta?token=delta-1"] = page(next_link="/delta?token=delta-1b")
        data["pages"]["/delta?token=delta-1b"] = page(next_link="/delta?token=delta-2")
        source = Source(data)
        source.fail_path = "/delta?token=delta-1b"
        with serving(source) as base:
            with self.assertRaises(SyncError):
                self.make(base).sync()
        fresh = self.new_snapshot()
        source.fixture["expired"] = "/delta?token=delta-1b"
        source.fixture["pages"].update(fresh["pages"])
        source.fail_path = None
        with serving(source) as base:
            result = self.make(base).sync()
        self.assertEqual(set(result["视图"]), {"shop-c", "shop-d"})
        self.assertEqual(result["游标"], "/delta?token=new-1")

    def test_410_on_a_full_window_is_an_error(self):
        data = fixture({"/full": page(delta_link="/delta?token=x")}, expired="/full")
        with serving(Source(data)) as base:
            with self.assertRaises(SourceResponseError):
                self.make(base).sync()

    def test_missing_restart_link_falls_back_to_full(self):
        data = first_window()
        with serving(Source(data)) as base:
            self.make(base).sync()
        fresh = self.new_snapshot()
        fresh["pages"]["/delta?token=delta-1"] = page(
            [{"id": "shop-a", "version": 2}], delta_link="/delta?token=delta-2"
        )
        fresh["expired"] = "/delta?token=delta-1"
        with serving(Source(fresh)) as base:
            # The fixture's 410 body points at /full, which exists.
            result = self.make(base).sync()
        self.assertEqual(set(result["视图"]), {"shop-c", "shop-d"})


class PersistenceBoundaryTests(ConnectorTestBase):
    def test_conflict_on_a_resumed_page_is_not_recorded_twice(self):
        data = fixture(
            {
                "/full": page(
                    [
                        {"id": "shop-a", "version": 2, "name": "甲"},
                        {"id": "shop-a", "version": 2, "name": "乙"},
                    ],
                    next_link="/page?token=x",
                ),
                "/page?token=x": page(
                    [{"id": "shop-b", "version": 1}],
                    delta_link="/delta?token=1",
                ),
                "/delta?token=1": page(delta_link="/delta?token=1"),
            }
        )
        source = Source(data)
        source.fail_path = "/page?token=x"
        with serving(source) as base:
            with self.assertRaises(SyncError):
                self.make(base).sync()
        source.fail_path = None
        with serving(source) as base:
            result = self.make(base).sync()
        self.assertEqual(len(result["冲突"]), 1)
        self.assertEqual(result["冲突"][0]["kind"], "same_version_conflict")

    def test_cross_origin_restart_hint_keeps_old_view_and_cursor(self):
        data = first_window()
        with serving(Source(data)) as base:
            self.make(base).sync()
        fresh = fixture(
            {
                "/full": page(
                    [{"id": "shop-c", "version": 1}],
                    delta_link="/delta?token=new",
                ),
                "/delta?token=new": page(delta_link="/delta?token=new"),
            },
            expired="/delta?token=delta-1",
        )
        # 410 body points at a different origin.
        source = Source(fresh)
        original = source.handle

        def handle(path, headers):
            if path == fresh["expired"]:
                return 410, {"Content-Type": "application/json"}, (
                    b'{"restart":"https://other.example/full"}'
                )
            return original(path, headers)

        source.handle = handle
        with serving(source) as base:
            with self.assertRaises(UnsafeLink):
                self.make(base).sync()
            status = self.make(base).status()
        self.assertEqual(set(status["视图"]), {"shop-a", "shop-b"})
        self.assertEqual(status["游标"], "/delta?token=delta-1")
        self.assertEqual(status["待确认窗口"]["模式"], "增量")

    def test_page_cap_stops_an_endless_window(self):
        data = fixture(
            {
                "/full": page([], next_link="/page?token=1"),
                **{
                    f"/page?token={n}": page([], next_link=f"/page?token={n + 1}")
                    for n in range(1, 10)
                },
            }
        )
        with serving(Source(data)) as base:
            connector = Connector(base, self.state, max_pages=3)
            with self.assertRaises(WindowLoop):
                connector.sync()
        self.assertEqual(
            json.loads(self.state.read_text(encoding="utf-8"))["pending"]["next"],
            "/page?token=4",
        )

    def test_invalid_entry_is_a_source_error_not_a_state_change(self):
        data = fixture(
            {
                "/full": page(
                    [
                        {"id": "shop-a", "version": 1},
                        {"id": "shop-b"},
                    ],
                    delta_link="/delta?token=x",
                )
            }
        )
        with serving(Source(data)) as base:
            with self.assertRaises(SourceResponseError):
                self.make(base).sync()
            status = self.make(base).status()
        self.assertEqual(status["视图"], {})
        self.assertEqual(status["待确认窗口"]["暂存门店"], {})

    def test_status_only_works_without_a_source(self):
        data = first_window()
        with serving(Source(data)) as base:
            self.make(base).sync()
        from app import main
        import contextlib
        import io
        import sys

        argv = sys.argv
        sys.argv = ["app.py", "--state", str(self.state), "--status-only"]
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                main()
        finally:
            sys.argv = argv


class EntryPointTests(ConnectorTestBase):
    def test_app_collect_returns_status_and_persists(self):
        data = first_window()
        with serving(Source(data)) as base:
            from app import collect

            result = collect(data, self.state)
        self.assertEqual(set(result["视图"]), {"shop-a", "shop-b"})
        self.assertTrue(self.state.exists())

    def test_corrupt_state_is_not_silently_reset(self):
        self.state.parent.mkdir(parents=True)
        self.state.write_text("{not json", encoding="utf-8")
        with self.assertRaises(SyncError):
            self.make("http://127.0.0.1:1")
        self.assertEqual(self.state.read_text(encoding="utf-8"), "{not json")


if __name__ == "__main__":
    unittest.main()
