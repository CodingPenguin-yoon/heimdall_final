from heimdall.project_database.provisioner import _scram_verifier


def test_scram_verifier_does_not_contain_raw_password() -> None:
    password = "raw-password-canary"

    verifier = _scram_verifier(password)

    assert verifier.startswith("SCRAM-SHA-256$4096:")
    assert password not in verifier
