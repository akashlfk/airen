"""Tests for the Redshift adapter's identifier-safety guard."""

from __future__ import annotations

import pytest

from airen.adapters.redshift_real import _assert_safe_identifier


def test_accepts_plain_table_name():
    _assert_safe_identifier("tl_eta_predictions")


def test_accepts_schema_qualified_name():
    _assert_safe_identifier("ml_predictions.tl_eta_datamart")


def test_rejects_empty():
    with pytest.raises(ValueError):
        _assert_safe_identifier("")


def test_rejects_sql_injection_attempts():
    with pytest.raises(ValueError):
        _assert_safe_identifier("foo; DROP TABLE users")
    with pytest.raises(ValueError):
        _assert_safe_identifier("foo' OR '1'='1")
    with pytest.raises(ValueError):
        _assert_safe_identifier("foo--bar")


def test_rejects_multiple_dots():
    """Schema.table allowed; deeper qualifiers like catalog.schema.table not."""
    with pytest.raises(ValueError):
        _assert_safe_identifier("a.b.c")
