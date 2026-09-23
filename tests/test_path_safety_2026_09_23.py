"""Tests for ``_safe_resolve_path`` (audit 2026-09-23).

Five export / snapshot endpoints accept a user-supplied absolute
path. The helper refuses any path that resolves to the live DB or
to a system / sensitive directory, so a caller cannot:

* overwrite the live SQLite store by pointing ``out_path`` at it,
* write to ``/`` / ``/etc`` / ``~/.ssh`` via ``out_dir``,
* read any path with a valid snapshot header via ``restore``.

These tests cover the three rules and every wired endpoint.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from loop_memory.serve.app import create_app
from loop_memory.serve.routes._shared import _safe_resolve_path
from loop_memory.storage.sqlite_store import MemoryStore


# ---------------------------------------------------------------------------
# Pure helper tests
# ---------------------------------------------------------------------------


class SafeResolvePathHelperTests(unittest.TestCase):
    """Audit 2026-09-23: path safety for export/snapshot endpoints."""

    def test_safe_resolve_accepts_home_relative(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            p = _safe_resolve_path(
                t + "/bundle", kind="write", live_db_path=t + "/db.sqlite",
            )
            self.assertEqual(str(p), str(Path(t + "/bundle").resolve()))

    def test_safe_resolve_rejects_live_db(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            db = t + "/loop_memory.db"
            Path(db).touch()
            with self.assertRaisesRegex(ValueError, "live SQLite store"):
                _safe_resolve_path(db, kind="write", live_db_path=db)

    def test_safe_resolve_rejects_live_db_wal_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            db = t + "/loop_memory.db"
            wal = t + "/loop_memory.db-wal"
            Path(db).touch()
            Path(wal).touch()
            with self.assertRaisesRegex(ValueError, "live SQLite store"):
                _safe_resolve_path(wal, kind="write", live_db_path=db)

    def test_safe_resolve_rejects_system_etc(self) -> None:
        with self.assertRaisesRegex(ValueError, "system directory"):
            _safe_resolve_path("/etc/passwd", kind="write", live_db_path="/tmp/x.db")

    def test_safe_resolve_rejects_system_usr(self) -> None:
        with self.assertRaisesRegex(ValueError, "system directory"):
            _safe_resolve_path("/usr/local/bin/anything", kind="write", live_db_path="/tmp/x.db")

    def test_safe_resolve_rejects_system_bin(self) -> None:
        with self.assertRaisesRegex(ValueError, "system directory"):
            _safe_resolve_path("/bin/ls", kind="write", live_db_path="/tmp/x.db")

    def test_safe_resolve_rejects_sensitive_ssh(self) -> None:
        with self.assertRaisesRegex(ValueError, "sensitive"):
            _safe_resolve_path(
                str(Path.home() / ".ssh" / "id_rsa"),
                kind="write", live_db_path="/tmp/x.db",
            )

    def test_safe_resolve_rejects_sensitive_aws(self) -> None:
        with self.assertRaisesRegex(ValueError, "sensitive"):
            _safe_resolve_path(
                str(Path.home() / ".aws" / "credentials"),
                kind="write", live_db_path="/tmp/x.db",
            )

    def test_safe_resolve_write_requires_existing_parent(self) -> None:
        with self.assertRaisesRegex(ValueError, "parent directory"):
            _safe_resolve_path(
                "/this/path/definitely/does/not/exist/bundle",
                kind="write", live_db_path="/tmp/x.db",
            )

    def test_safe_resolve_read_does_not_require_existing_parent(self) -> None:
        # Read endpoint should accept a path that does not exist
        # yet (the route will 404 on its own).
        p = _safe_resolve_path(
            "/tmp/this/path/does/not/exist.db",
            kind="read", live_db_path="/tmp/x.db",
        )
        self.assertTrue(str(p).endswith("/exist.db"))

    def test_safe_resolve_empty_raises(self) -> None:
        with self.assertRaisesRegex(ValueError, "required"):
            _safe_resolve_path("", kind="write", live_db_path="/tmp/x.db")

    def test_safe_resolve_expanduser(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            sub = t + "/sub"
            os.makedirs(sub)
            p = _safe_resolve_path("~/../../../../tmp", kind="write", live_db_path="/tmp/x.db")
            # Should not raise — ``~`` resolves to the user's home
            # and the ``..`` chain walks back to the filesystem root.
            # We accept any path that resolves successfully.
            self.assertTrue(str(p).startswith("/"))


# ---------------------------------------------------------------------------
# HTTP route tests — every wired endpoint must refuse bad paths
# ---------------------------------------------------------------------------


def _store() -> tuple[MemoryStore, Path]:
    tmp = Path(tempfile.mkdtemp(prefix="loop-safe-paths-"))
    return MemoryStore(str(tmp / "db.sqlite")), tmp


class ExportOkfSafePathRouteTests(unittest.TestCase):
    """Audit 2026-09-23: POST /api/export/okf refuses bad paths."""

    def setUp(self) -> None:
        self.store, self.tmp = _store()
        self.app = create_app(self.store)
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_refuses_live_db(self) -> None:
        r = self.client.post(
            "/api/export/okf",
            json={"out_dir": str(self.store.path)},
        )
        self.assertEqual(r.status_code, 400, msg=r.text)
        self.assertIn("live SQLite store", r.text)

    def test_refuses_etc(self) -> None:
        with tempfile.TemporaryDirectory():
            r = self.client.post(
                "/api/export/okf",
                json={"out_dir": "/etc"},
            )
            self.assertEqual(r.status_code, 400, msg=r.text)

    def test_refuses_ssh(self) -> None:
        r = self.client.post(
            "/api/export/okf",
            json={"out_dir": str(Path.home() / ".ssh" / "x")},
        )
        self.assertEqual(r.status_code, 400, msg=r.text)

    def test_accepts_valid_path(self) -> None:
        with tempfile.TemporaryDirectory() as out:
            r = self.client.post(
                "/api/export/okf",
                json={"out_dir": out},
            )
            self.assertEqual(r.status_code, 200, msg=r.text)


class SnapshotSafePathRouteTests(unittest.TestCase):
    """Audit 2026-09-23: POST /api/snapshot refuses live-DB overwrite."""

    def setUp(self) -> None:
        self.store, self.tmp = _store()
        self.app = create_app(self.store)
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_refuses_live_db(self) -> None:
        r = self.client.post(
            "/api/snapshot",
            json={"out_path": str(self.store.path)},
        )
        self.assertEqual(r.status_code, 400, msg=r.text)
        self.assertIn("live SQLite store", r.text)

    def test_refuses_etc(self) -> None:
        r = self.client.post(
            "/api/snapshot",
            json={"out_path": "/etc/passwd"},
        )
        self.assertEqual(r.status_code, 400, msg=r.text)

    def test_accepts_valid_path(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            r = self.client.post(
                "/api/snapshot",
                json={"out_path": t + "/snap.memory.sqlite"},
            )
            self.assertEqual(r.status_code, 200, msg=r.text)
            self.assertTrue(os.path.exists(t + "/snap.memory.sqlite"))


class SnapshotRestoreSafePathRouteTests(unittest.TestCase):
    """Audit 2026-09-23: POST /api/snapshot/restore refuses sensitive paths."""

    def setUp(self) -> None:
        self.store, self.tmp = _store()
        self.app = create_app(self.store)
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_refuses_ssh(self) -> None:
        r = self.client.post(
            "/api/snapshot/restore",
            json={"in_path": str(Path.home() / ".ssh" / "id_rsa")},
        )
        self.assertEqual(r.status_code, 400, msg=r.text)

    def test_refuses_etc(self) -> None:
        r = self.client.post(
            "/api/snapshot/restore",
            json={"in_path": "/etc/passwd"},
        )
        self.assertEqual(r.status_code, 400, msg=r.text)


if __name__ == "__main__":
    unittest.main()
