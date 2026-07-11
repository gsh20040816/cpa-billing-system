from cpa_billing.security import cpamp_key_hash, login_fingerprint, mask_api_key


def test_cpamp_hash_trims_whitespace() -> None:
    assert cpamp_key_hash(" key \n") == cpamp_key_hash("key")


def test_login_fingerprint_depends_on_pepper() -> None:
    assert login_fingerprint("key", "a") != login_fingerprint("key", "b")


def test_mask_does_not_contain_complete_key() -> None:
    raw = "sk-cpa-abcdefghijklmnopqrstuvwxyz"
    masked = mask_api_key(raw)
    assert raw not in masked
    assert masked.startswith("sk-cpa-a")

