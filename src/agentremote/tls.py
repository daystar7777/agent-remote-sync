from __future__ import annotations

import datetime as dt
import hashlib
import ipaddress
import socket
import ssl
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import HTTPSHandler, Request, build_opener, urlopen

import http.client

from .common import AgentRemoteError
from .connections import config_home


@dataclass
class TLSFiles:
    cert_file: Path
    key_file: Path
    fingerprint: str


def ensure_self_signed_cert(root: Path, *, store_dir: Path | None = None) -> TLSFiles:
    directory = store_dir or default_cert_dir(root)
    cert_file = directory / "agentremote-slave-cert.pem"
    key_file = directory / "agentremote-slave-key.pem"
    if cert_file.exists() and key_file.exists():
        return TLSFiles(cert_file, key_file, certificate_fingerprint(cert_file))
    directory.mkdir(parents=True, exist_ok=True)
    generate_self_signed_cert(cert_file, key_file)
    return TLSFiles(cert_file, key_file, certificate_fingerprint(cert_file))


def default_cert_dir(root: Path) -> Path:
    digest = hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()[:16]
    return config_home() / "tls" / digest


def generate_self_signed_cert(cert_file: Path, key_file: Path) -> None:
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError as exc:
        raise AgentRemoteError(
            500,
            "missing_tls_dependency",
            "cryptography is required to generate self-signed TLS certificates",
        ) from exc

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, "agent-remote-sync self-signed slave"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "agent-remote-sync"),
        ]
    )
    alt_names = [
        x509.DNSName("localhost"),
        x509.DNSName(socket.gethostname()),
        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
        x509.IPAddress(ipaddress.ip_address("::1")),
    ]
    try:
        for _, _, _, _, address in socket.getaddrinfo(socket.gethostname(), None):
            host = address[0]
            try:
                alt_names.append(x509.IPAddress(ipaddress.ip_address(host)))
            except ValueError:
                pass
    except OSError:
        pass
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=365))
        .add_extension(x509.SubjectAlternativeName(alt_names), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def certificate_fingerprint(cert_file: Path) -> str:
    pem = cert_file.read_text(encoding="ascii")
    der = ssl.PEM_cert_to_DER_cert(pem)
    return hashlib.sha256(der).hexdigest()


def normalize_fingerprint(value: str) -> str:
    cleaned = "".join(ch for ch in value.lower() if ch in "0123456789abcdef")
    if len(cleaned) != 64:
        raise AgentRemoteError(400, "bad_tls_fingerprint", "TLS fingerprint must be a SHA-256 hex value")
    return cleaned


def format_fingerprint(value: str) -> str:
    cleaned = normalize_fingerprint(value)
    return ":".join(cleaned[index : index + 2] for index in range(0, len(cleaned), 2)).upper()


def is_https_endpoint(host: str) -> bool:
    return host.lower().startswith("https://")


def fetch_remote_fingerprint(host: str, port: int, timeout: float = 10) -> str:
    parsed = urlparse(host if "://" in host else f"https://{host}:{port}")
    if parsed.scheme != "https":
        raise AgentRemoteError(400, "not_https", "TLS fingerprint can only be fetched for HTTPS endpoints")
    hostname = parsed.hostname
    if not hostname:
        raise AgentRemoteError(400, "bad_https_host", "HTTPS host is missing")
    resolved_port = parsed.port or port or 443
    context = ssl._create_unverified_context()
    with socket.create_connection((hostname, resolved_port), timeout=timeout) as sock:
        with context.wrap_socket(sock, server_hostname=hostname) as wrapped:
            cert = wrapped.getpeercert(binary_form=True)
    return hashlib.sha256(cert).hexdigest()


def server_context(cert_file: Path, key_file: Path) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(str(cert_file), str(key_file))
    return context


def wrap_server_socket(server: object, cert_file: Path, key_file: Path) -> None:
    context = server_context(cert_file, key_file)
    server.socket = context.wrap_socket(server.socket, server_side=True)


def open_url(
    request: Request,
    *,
    timeout: float,
    tls_fingerprint: str = "",
    tls_insecure: bool = False,
    ca_file: str | Path | None = None,
):
    if not request.full_url.lower().startswith("https://"):
        return urlopen(request, timeout=timeout)
    if tls_fingerprint:
        opener = build_opener(PinnedHTTPSHandler(tls_fingerprint))
        return opener.open(request, timeout=timeout)
    if tls_insecure:
        return urlopen(request, timeout=timeout, context=ssl._create_unverified_context())
    if ca_file:
        return urlopen(request, timeout=timeout, context=ssl.create_default_context(cafile=str(ca_file)))
    return urlopen(request, timeout=timeout)


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int | None = None, *, fingerprint: str, **kwargs):
        kwargs["context"] = ssl._create_unverified_context()
        super().__init__(host, port=port, **kwargs)
        self.expected_fingerprint = normalize_fingerprint(fingerprint)

    def connect(self) -> None:
        super().connect()
        cert = self.sock.getpeercert(binary_form=True) if self.sock else b""
        actual = hashlib.sha256(cert).hexdigest()
        if actual != self.expected_fingerprint:
            self.close()
            raise ssl.SSLError(
                "TLS certificate fingerprint mismatch: "
                f"expected {format_fingerprint(self.expected_fingerprint)}, "
                f"got {format_fingerprint(actual)}"
            )


class PinnedHTTPSHandler(HTTPSHandler):
    def __init__(self, fingerprint: str):
        super().__init__(context=ssl._create_unverified_context())
        self.fingerprint = normalize_fingerprint(fingerprint)

    def https_open(self, request):
        return self.do_open(self._connection, request, context=self._context)

    def _connection(self, host: str, **kwargs):
        return PinnedHTTPSConnection(host, fingerprint=self.fingerprint, **kwargs)
