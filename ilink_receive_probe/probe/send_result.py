"""Fail-closed classification for iLink send responses.

The iLink API can accept a request without proving that the message was
enqueued for display. Callers must not equate ret=0 with delivery.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

API_ACCEPTED_WITH_RECEIPT = "API_ACCEPTED_WITH_RECEIPT"
DELIVERY_UNCONFIRMED = "DELIVERY_UNCONFIRMED"
DEFINITE_FAILED = "DEFINITE_FAILED"
INVALID_RESPONSE = "INVALID_RESPONSE"
AMBIGUOUS = "AMBIGUOUS"


def _receipt_id(response: Mapping[str, Any]) -> str | None:
    for key in ("message_id", "msg_id", "svr_id"):
        value = response.get(key)
        if value is not None and str(value) != "":
            return str(value)
    nested = response.get("msg")
    if isinstance(nested, Mapping):
        for key in ("message_id", "msg_id", "svr_id"):
            value = nested.get(key)
            if value is not None and str(value) != "":
                return str(value)
    return None


@dataclass(frozen=True)
class ClassifiedSendResult:
    state: str
    confirmed: bool
    retry_allowed: bool
    ret: Any = None
    errcode: Any = None
    receipt_id: str | None = None
    response_keys: tuple[str, ...] = ()


def classify_send_response(response: Any) -> ClassifiedSendResult:
    """Classify a decoded iLink response without guessing delivery.

    Automatic retries are never authorized by this classifier. A caller may
    expose a separate, explicit retry action for a definite API rejection.
    """
    if not isinstance(response, Mapping):
        return ClassifiedSendResult(INVALID_RESPONSE, False, False)

    ret = response.get("ret")
    errcode = response.get("errcode")
    keys = tuple(sorted(str(key) for key in response.keys()))
    if ret is None:
        return ClassifiedSendResult(INVALID_RESPONSE, False, False, ret, errcode, None, keys)
    if ret != 0 or (errcode not in (None, 0, "0")):
        return ClassifiedSendResult(DEFINITE_FAILED, False, True, ret, errcode, None, keys)

    receipt = _receipt_id(response)
    if receipt is None:
        return ClassifiedSendResult(DELIVERY_UNCONFIRMED, False, False, ret, errcode, None, keys)
    return ClassifiedSendResult(API_ACCEPTED_WITH_RECEIPT, True, False, ret, errcode, receipt, keys)
