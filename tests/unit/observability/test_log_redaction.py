from autotrader.observability.logging import redact_sensitive_values


def test_redacts_nested_sensitive_values_without_hiding_internal_identifiers():
    payload = {
        "request_id": "018fcb2a-0000-7000-8000-000000000000",
        "authorization": "Bearer secret-token",
        "nested": {
            "account_number": "1234567890",
            "items": [{"token": "abc"}, {"internal_id": "visible"}],
        },
    }

    redacted = redact_sensitive_values(payload)

    assert redacted == {
        "request_id": "018fcb2a-0000-7000-8000-000000000000",
        "authorization": "[REDACTED]",
        "nested": {
            "account_number": "[REDACTED]",
            "items": [{"token": "[REDACTED]"}, {"internal_id": "visible"}],
        },
    }
