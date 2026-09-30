"""Hermes service paths required before the pilot (review of 1.7, §3).

Each module is the ONLY code path for its concern; tests/test_service_boundaries.py fails if a second
path appears (another HTTP client, a template renderer without escaping, a log handler without redaction).
Database effects go through ports whose production implementation calls the functions in migration 0010;
the ports are faked in unit tests and exercised for real only in CI/staging (db/tests/*).
"""
