"""
Unit tests for api/aws_errors.is_expired_cred_error.

This helper is the single source of truth for "is this
boto3 error a stale-creds error?" — any drift here makes
every Athena/Bedrock route 500 instead of 503 with the
useful message. Plain unit tests, no DB, no boto3 calls.
"""
from __future__ import annotations
import pytest
from botocore.exceptions import ClientError

from api.aws_errors import (
    EXPIRED_CRED_CODES,
    is_expired_cred_error,
)


@pytest.mark.parametrize("code", sorted(EXPIRED_CRED_CODES))
def test_each_expired_code_matches(code):
    err = ClientError(
        {"Error": {"Code": code, "Message": "x"}},
        "AnyOp",
    )
    assert is_expired_cred_error(err) is True


def test_unrelated_code_does_not_match():
    err = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "x"}},
        "AnyOp",
    )
    assert is_expired_cred_error(err) is False


def test_none_input_returns_false():
    assert is_expired_cred_error(None) is False


def test_non_clienterror_input_returns_false():
    """A plain Exception with no .response must not crash
    the helper and must not be misclassified as an
    expired-cred error."""
    assert is_expired_cred_error(ValueError("nope")) is False


def test_malformed_response_dict_returns_false():
    """A ClientError-shaped object missing the inner "Code"
    key must be handled without a KeyError leak."""
    class Fake:
        response = {"Error": {}}

    assert is_expired_cred_error(Fake()) is False
