from app.security.dlp_filter import SensitiveDataFilter


def test_mask_sensitive_data_masks_phone_and_id_card() -> None:
    text = "手机号 13812345678，身份证 110101199001011234"

    masked = SensitiveDataFilter.mask_sensitive_data(text)

    assert "138****5678" in masked
    assert "1101**********1234" in masked
    assert "13812345678" not in masked
    assert "110101199001011234" not in masked


def test_mask_sensitive_data_masks_email_and_bank_card() -> None:
    text = "邮箱 user@example.com，银行卡 6222 0000 0000 1234"

    masked = SensitiveDataFilter.mask_sensitive_data(text)

    assert "u***@example.com" in masked
    assert "6222********1234" in masked


def test_mask_sensitive_data_does_not_match_long_number_as_phone() -> None:
    text = "流水号 991381234567899"

    assert SensitiveDataFilter.mask_sensitive_data(text) == text
