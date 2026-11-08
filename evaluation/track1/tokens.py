"""Non-production HMAC tokens for synthetic and local IEEE evaluation inputs."""

from __future__ import annotations

import hmac
from hashlib import sha256
from typing import Iterable

# This is a public reproducibility domain separator, not an operational HMAC
# secret. Inputs here are synthetic identifiers or user-local IEEE proxy values
# and are never emitted in results or logs.
_EVALUATION_DOMAIN_KEY = b"ticket-defender-evaluation-nonproduction-v1"


def hmac_token(tenant_id: str, signal_type: str, parts: Iterable[str]) -> str:
    """Return a deterministic 64-hex token scoped to tenant and signal type."""

    canonical = "\x1f".join((tenant_id, signal_type, *parts)).encode("utf-8")
    return hmac.new(_EVALUATION_DOMAIN_KEY, canonical, sha256).hexdigest()


def optional_hmac_token(
    tenant_id: str, signal_type: str, parts: Iterable[str | None]
) -> str | None:
    """Do not manufacture a connection token when every proxy value is absent."""

    present = [str(part).strip() for part in parts if part is not None and str(part).strip()]
    return hmac_token(tenant_id, signal_type, present) if present else None
