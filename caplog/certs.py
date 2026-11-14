"""TLS certificate files.

The private key is written only under the certificate directory, mode 0600.
It is not stored in the database, the log, or the repository.
"""

import ipaddress
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.x509.oid import NameOID

from caplog.errors import ValidationError


def _load_cert(cert_pem: bytes):
    try:
        return x509.load_pem_x509_certificate(cert_pem)
    except Exception as exc:
        raise ValidationError("the certificate is not a PEM certificate") from exc


def _load_key(key_pem: bytes):
    try:
        key = serialization.load_pem_private_key(key_pem, password=None)
    except TypeError as exc:
        raise ValidationError("the private key must be a PEM key with no passphrase") from exc
    except Exception as exc:
        raise ValidationError("the private key is not a PEM private key") from exc
    return key


def check_pair(cert_pem: bytes, key_pem: bytes) -> datetime:
    """Reject a pair whose key does not match the certificate. Return the expiry."""
    cert = _load_cert(cert_pem)
    key = _load_key(key_pem)
    cert_public = cert.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    pair_public = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    if cert_public != pair_public:
        raise ValidationError("the private key does not match the certificate")
    return cert.not_valid_after_utc


def generate_self_signed(hostname: str) -> tuple[bytes, bytes, datetime]:
    name = hostname.strip() or "caplog.local"
    if len(name) > 253 or any(ch.isspace() for ch in name):
        raise ValidationError("the site name is not a valid host name")
    key = ed25519.Ed25519PrivateKey.generate()
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name[:64])])
    now = datetime.now(timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=397))
    )
    try:
        ipaddress.ip_address(name)
        alt = x509.IPAddress(ipaddress.ip_address(name))
    except ValueError:
        alt = x509.DNSName(name)
    builder = builder.add_extension(x509.SubjectAlternativeName([alt]), critical=False)
    cert = builder.sign(key, algorithm=None if isinstance(key, ed25519.Ed25519PrivateKey) else hashes.SHA256())
    # Ed25519 sign() does not take a hash. cryptography accepts None for that.
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return cert_pem, key_pem, cert.not_valid_after_utc


def write_pair(directory: str, cert_pem: bytes, key_pem: bytes) -> datetime:
    expires = check_pair(cert_pem, key_pem)
    path = Path(directory)
    path.mkdir(parents=True, exist_ok=True)
    cert_path = path / "cert.pem"
    key_path = path / "key.pem"
    key_path.write_bytes(key_pem)
    key_path.chmod(0o600)
    cert_path.write_bytes(cert_pem)
    cert_path.chmod(0o644)
    return expires


def describe(directory: str) -> dict:
    cert_path = Path(directory) / "cert.pem"
    key_path = Path(directory) / "key.pem"
    if not cert_path.exists() or not key_path.exists():
        return {"present": False}
    try:
        cert = _load_cert(cert_path.read_bytes())
    except ValidationError:
        return {"present": False, "error": "the certificate file could not be read"}
    expires = cert.not_valid_after_utc
    names = []
    try:
        alt = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        names = [str(item) for item in alt.get_values_for_type(x509.DNSName)]
        names += [str(item) for item in alt.get_values_for_type(x509.IPAddress)]
    except x509.ExtensionNotFound:
        pass
    return {
        "present": True,
        "expires": expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "names": names,
        "seconds_left": int((expires - datetime.now(timezone.utc)).total_seconds()),
    }
