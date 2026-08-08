from cryptography import x509

from layout_server.certs import ensure_self_signed_cert


def test_ensure_self_signed_cert_creates_files(tmp_path):
    cert_path = tmp_path / "certs" / "cert.pem"
    key_path = tmp_path / "certs" / "key.pem"

    ensure_self_signed_cert(cert_path, key_path)

    assert cert_path.exists()
    assert key_path.exists()

    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    common_name = cert.subject.get_attributes_for_oid(x509.oid.NameOID.COMMON_NAME)[0].value
    assert common_name == "localhost"


def test_ensure_self_signed_cert_is_idempotent(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"

    ensure_self_signed_cert(cert_path, key_path)
    first_cert_bytes = cert_path.read_bytes()

    ensure_self_signed_cert(cert_path, key_path)
    second_cert_bytes = cert_path.read_bytes()

    assert first_cert_bytes == second_cert_bytes
