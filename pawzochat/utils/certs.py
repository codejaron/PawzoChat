# PawzoChat - Multi-platform LLM-powered chatbot
# Copyright (C) 2026  iwyxdxl
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""Self-signed TLS certificate generation and persistence."""

from __future__ import annotations

import datetime
import ipaddress
import logging
import os
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

logger = logging.getLogger(__name__)


def _existing_pair_is_usable(cert_path: Path, key_path: Path) -> bool:
    """Return whether the persisted pair is valid for the loopback listener."""
    if not cert_path.is_file() or not key_path.is_file():
        return False
    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        key = serialization.load_pem_private_key(
            key_path.read_bytes(), password=None,
        )
        cert_public = cert.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        key_public = key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        san = cert.extensions.get_extension_for_class(
            x509.SubjectAlternativeName,
        ).value
        now = datetime.datetime.now(datetime.timezone.utc)
        return (
            cert_public == key_public
            and cert.not_valid_before_utc <= now < cert.not_valid_after_utc
            and ipaddress.IPv4Address("127.0.0.1") in san.get_values_for_type(
                x509.IPAddress,
            )
            and "localhost" in san.get_values_for_type(x509.DNSName)
        )
    except (OSError, ValueError, TypeError, x509.ExtensionNotFound):
        # Old PawzoChat certificates contained IPv4Network("0.0.0.0/0") in
        # an iPAddress SAN. That encodes eight bytes where X.509 permits only
        # four or sixteen, so standards-compliant clients reject the entire
        # certificate before TLS verification can even be disabled.
        return False


def _write_pair_atomic(
    cert_path: Path,
    key_path: Path,
    cert_bytes: bytes,
    key_bytes: bytes,
) -> None:
    cert_tmp = cert_path.with_suffix(cert_path.suffix + ".tmp")
    key_tmp = key_path.with_suffix(key_path.suffix + ".tmp")
    try:
        key_tmp.write_bytes(key_bytes)
        os.chmod(key_tmp, 0o600)
        cert_tmp.write_bytes(cert_bytes)
        os.chmod(cert_tmp, 0o644)
        os.replace(key_tmp, key_path)
        os.replace(cert_tmp, cert_path)
    finally:
        for path in (cert_tmp, key_tmp):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.warning("清理 TLS 临时文件失败: %s", path, exc_info=True)


def ensure_self_signed_cert(cert_dir: Path) -> tuple[str, str]:
    """Return a valid self-signed pair, replacing malformed legacy pairs."""
    cert_dir.mkdir(parents=True, exist_ok=True)
    cert_path = cert_dir / "server.crt"
    key_path = cert_dir / "server.key"

    if _existing_pair_is_usable(cert_path, key_path):
        try:
            os.chmod(key_path, 0o600)
        except OSError:
            logger.warning("收紧 TLS 私钥权限失败: %s", key_path, exc_info=True)
        logger.debug("已有 TLS 证书: %s", cert_path)
        return str(cert_path), str(key_path)

    if cert_path.exists() or key_path.exists():
        logger.warning("TLS 证书无效或与私钥不匹配，正在重新生成")
    else:
        logger.info("生成自签名 TLS 证书…")

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "PawzoChat"),
        x509.NameAttribute(NameOID.COMMON_NAME, "PawzoChat Self-Signed"),
    ])

    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(
            x509.SubjectAlternativeName([
                x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
                x509.DNSName("localhost"),
            ]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=None,
                decipher_only=None,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    key_bytes = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    cert_bytes = cert.public_bytes(serialization.Encoding.PEM)
    _write_pair_atomic(cert_path, key_path, cert_bytes, key_bytes)

    logger.info("TLS 证书已生成: %s", cert_path)
    return str(cert_path), str(key_path)
