"""Regression tests pinning ``loop_memory.__version__`` to the installed
distribution version.

Before this fix, ``__version__`` was hard-coded to ``"0.2.0"`` in
``loop_memory/__init__.py`` and had been stale since the 0.3.0 line —
every release from 0.4.0 → 0.4.4 shipped with ``pip show loop-memory``
reporting the real version but ``import loop_memory; loop_memory.__version__``
still saying ``0.2.0``.

The fix reads the version from installed distribution metadata, with a
fallback to ``"0.0.0+source"`` for source checkouts.
"""
from __future__ import annotations

import re
import unittest
from importlib import metadata as importlib_metadata


class PackageVersionTests(unittest.TestCase):
    def test_dunder_version_is_a_nonempty_string(self) -> None:
        import loop_memory
        self.assertIsInstance(loop_memory.__version__, str)
        self.assertTrue(loop_memory.__version__.strip(),
                        "loop_memory.__version__ must not be empty")

    def test_dunder_version_is_not_stale_0_2_0(self) -> None:
        """The bug that triggered this fix: ``__version__`` was
        hard-coded to "0.2.0" since 0.3.0 — every release since then
        had to ship with the wrong value. This test makes the regression
        loud if it ever comes back.
        """
        import loop_memory
        self.assertNotEqual(
            loop_memory.__version__,
            "0.2.0",
            "loop_memory.__version__ is hard-coded to '0.2.0' — "
            "should read from importlib.metadata instead",
        )

    def test_dunder_version_matches_installed_metadata(self) -> None:
        """When installed via pip, ``__version__`` MUST equal the
        distribution's own metadata version. This is the contract users
        rely on: ``pip show`` and ``import loop_memory; __version__``
        must agree.
        """
        import loop_memory
        try:
            installed = importlib_metadata.version("loop-memory")
        except importlib_metadata.PackageNotFoundError:
            self.skipTest("loop-memory is not installed (source checkout)")
        self.assertEqual(
            loop_memory.__version__,
            installed,
            f"loop_memory.__version__={loop_memory.__version__!r} "
            f"but pip metadata says {installed!r}",
        )

    def test_source_checkout_fallback_is_marker_not_silent_2_0(self) -> None:
        """When the package isn't installed (e.g. running tests from a
        fresh ``git clone``), ``__version__`` MUST surface that it came
        from source — never silently masquerade as a real release.
        """
        import loop_memory
        if loop_memory.__version__ == "0.0.0+source":
            return  # source-checkout fallback is active; good.
        # Otherwise it must match the installed distribution exactly,
        # which the test above already enforced.
        try:
            installed = importlib_metadata.version("loop-memory")
        except importlib_metadata.PackageNotFoundError:
            self.fail(
                "loop_memory.__version__ did not fall back to "
                "'0.0.0+source' even though no installed metadata exists; "
                f"got {loop_memory.__version__!r}"
            )
        self.assertEqual(loop_memory.__version__, installed)

    def test_version_string_looks_like_a_pep440_version(self) -> None:
        """Sanity: the version string must parse as something PEP 440-ish
        (X.Y.Z with optional pre/post suffixes), not be a hard-coded
        marker like 'unknown' or 'dev'.
        """
        import loop_memory
        v = loop_memory.__version__
        if v == "0.0.0+source":
            self.skipTest("source-checkout fallback")
        # Allow X.Y.Z plus optional a/b/rc/post/dev suffixes
        self.assertRegex(
            v,
            r"^\d+\.\d+\.\d+(?:[abc]|rc|\.post|\.dev|\.local|[\-+.][\w.]+)*$",
            f"version {v!r} is not PEP 440-shaped",
        )


if __name__ == "__main__":
    unittest.main()
