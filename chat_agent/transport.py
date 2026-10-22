"""Chat-specific iLink send boundary.

Daily Plan keeps its existing compatibility behavior. Chat Agent opts into
receipt-aware sending and returns a classified result to the draft store.
"""

from ilink_receive_probe.probe.reply_transport import (
    AmbiguousSendError,
    DefiniteSendError,
    RenderedReply,
    ReplyOnlyTransport,
    UnconfirmedSendError,
)
from ilink_receive_probe.probe.send_result import (
    AMBIGUOUS,
    DEFINITE_FAILED,
    DELIVERY_UNCONFIRMED,
    classify_send_response,
    ClassifiedSendResult,
)


class ChatReplyTransport:
    def __init__(self, token=None, *, transport=None, base_url=None, timeout=15_000):
        self.transport = transport or ReplyOnlyTransport(
            token,
            base_url=base_url,
            timeout=timeout,
            require_receipt=True,
        )

    def send(self, context, idempotency_key, text):
        try:
            response = self.transport.reply_bound_rendered_context(
                context, idempotency_key, RenderedReply(text),
            )
        except UnconfirmedSendError:
            return ClassifiedSendResult(DELIVERY_UNCONFIRMED, False, False)
        except AmbiguousSendError:
            return ClassifiedSendResult(AMBIGUOUS, False, False)
        except DefiniteSendError:
            return ClassifiedSendResult(DEFINITE_FAILED, False, True)
        return classify_send_response(response)
