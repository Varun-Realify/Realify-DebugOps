"""Amazon SNS message verification (for CloudWatch alarm notifications).

Implements AWS's documented verification: the signing certificate must come from an https://sns.<region>.amazonaws.com
URL, the signature is RSA over a canonical string of specific fields (SHA1 for SignatureVersion 1, SHA256 for 2).
"""

import base64
import re
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from urllib.parse import urlsplit

import httpx
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

_SNS_HOST = re.compile(r"^sns\.[a-z0-9-]+\.amazonaws\.com(\.cn)?$")
MAX_AGE = timedelta(hours=1)
NOTIFICATION_KEYS = ["Message", "MessageId", "Subject", "Timestamp", "TopicArn", "Type"]
SUBSCRIPTION_KEYS = ["Message", "MessageId", "SubscribeURL", "Timestamp", "Token", "TopicArn", "Type"]


class SNSVerificationError(Exception):
    pass


def is_sns_url(url: str, path_suffix: str | None = None) -> bool:
    p = urlsplit(url or "")
    return p.scheme == "https" and bool(_SNS_HOST.match(p.hostname or "")) and (path_suffix is None or p.path.endswith(path_suffix))


@lru_cache(maxsize=32)
def _fetch_cert(url: str) -> bytes:
    r = httpx.get(url, timeout=10, follow_redirects=False)
    r.raise_for_status()
    return r.content


def canonical_string(msg: dict) -> bytes:
    keys = NOTIFICATION_KEYS if msg.get("Type") == "Notification" else SUBSCRIPTION_KEYS
    return "".join(f"{k}\n{msg[k]}\n" for k in keys if k in msg and msg[k] is not None).encode()


def verify(msg: dict, now: datetime | None = None, fetch_cert=None) -> None:
    """Raise SNSVerificationError unless the message is authentic and fresh."""
    cert_url = msg.get("SigningCertURL") or msg.get("SigningCertUrl") or ""
    if not is_sns_url(cert_url, ".pem"):
        raise SNSVerificationError("signing certificate is not from an SNS endpoint")
    version = str(msg.get("SignatureVersion"))
    if version not in ("1", "2"):
        raise SNSVerificationError("unsupported SignatureVersion")
    try:
        ts = datetime.fromisoformat(str(msg.get("Timestamp", "")).replace("Z", "+00:00"))
    except ValueError as e:
        raise SNSVerificationError("bad Timestamp") from e
    if abs((now or datetime.now(UTC)) - ts) > MAX_AGE:
        raise SNSVerificationError("message too old (possible replay)")
    try:
        cert = x509.load_pem_x509_certificate((fetch_cert or _fetch_cert)(cert_url))
        cert.public_key().verify(base64.b64decode(msg.get("Signature", "")), canonical_string(msg), padding.PKCS1v15(),
                                 hashes.SHA1() if version == "1" else hashes.SHA256())  # noqa: S303 (SNS v1 is SHA1 by spec)
    except InvalidSignature as e:
        raise SNSVerificationError("invalid signature") from e
    except (ValueError, httpx.HTTPError) as e:
        raise SNSVerificationError(f"cannot verify signature: {type(e).__name__}") from e
