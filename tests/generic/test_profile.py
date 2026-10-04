from dataclasses import FrozenInstanceError, replace
import json

import pytest

from pdf_to_ofx.generic.profile import AmountMode, BalanceMode, DateMode, LayoutProfile


def profile():
    return LayoutProfile(DateMode.GROUPED, AmountMode.SIGNED, BalanceMode.RUNNING)


def test_profile_is_portable_structural_data_only():
    original = profile()
    payload = original.to_json()
    assert LayoutProfile.from_json(payload) == original
    assert payload == original.to_json()
    fields = json.loads(payload)
    assert set(fields) == {"version", "date_mode", "amount_mode", "balance_mode", "movement_column",
                           "balance_column", "carry_date_across_pages", "footer_rows", "tolerances",
                           "transaction_left", "continuation_left", "repeated_header_rows", "trailing_note_rows"}
    assert fields["date_mode"] == "grouped"
    with pytest.raises(FrozenInstanceError):
        original.footer_rows = 2


@pytest.mark.parametrize("changes", [
    {"movement_column": 1}, {"balance_column": None}, {"footer_rows": -1},
    {"footer_rows": True}, {"date_mode": "grouped"}, {"carry_date_across_pages": 1},
    {"balance_mode": BalanceMode.ABSENT},
])
def test_invalid_profiles_are_rejected(changes):
    with pytest.raises(ValueError):
        replace(profile(), **changes)


@pytest.mark.parametrize("payload", [
    "[]", "{}", '{"date_mode":"grouped","date_mode":"grouped"}',
    '{"date_mode":"grouped","amount_mode":"signed","balance_mode":"running_balance","account":"FICTITIOUS"}',
    '{"date_mode":"grouped","amount_mode":"signed","balance_mode":"running_balance","version":2}',
    '{"date_mode":"grouped","amount_mode":"signed","balance_mode":"running_balance","tolerances":{"row_y":NaN}}',
])
def test_unknown_fields_and_unsupported_json_are_rejected(payload):
    with pytest.raises(ValueError, match="structural"):
        LayoutProfile.from_json(payload)


def test_minimal_absent_balance_profile_json():
    result = LayoutProfile.from_json('{"date_mode":"per_transaction","amount_mode":"signed","balance_mode":"absent"}')
    assert result.balance_column is None


def test_m4_profile_json_remains_readable_without_new_optional_fields():
    payload = '{"date_mode":"grouped","amount_mode":"signed","balance_mode":"running_balance",' \
              '"tolerances":{"row_y":3.0,"token_gap":12.0},"version":1}'
    assert LayoutProfile.from_json(payload) == profile()
