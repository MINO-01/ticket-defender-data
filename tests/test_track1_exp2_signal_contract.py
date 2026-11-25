import pytest
from pydantic import ValidationError

from schemas.dto import Track1LimitTicketInput


def base():
    return {
        "accountId": "account", "reservationNo": "reservation", "eventId": "event",
        "performanceId": "00000000-0000-4000-8000-000000000001",
        "purchasedAt": "2026-01-01T00:00:00Z", "effectiveTicketQuantity": 1,
    }


def test_exp2_optional_pseudonymous_signals_preserve_existing_input_shape():
    payload = base() | {"refundAccountHash": "a" * 64, "phoneHash": "b" * 64}
    parsed = Track1LimitTicketInput.model_validate(payload)
    assert parsed.refund_account_hash == "a" * 64
    assert parsed.phone_hash == "b" * 64


def test_ip_network_type_requires_existing_ip_hash():
    with pytest.raises(ValidationError):
        Track1LimitTicketInput.model_validate(base() | {"ipNetworkType": "RESIDENTIAL"})
