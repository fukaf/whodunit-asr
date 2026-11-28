"""A cancelled job: raised inside the pipeline's progress callback.

Cancellation rides on the existing `on_stage` hook rather than a separate
signal check. The hook fires before a chunk's decode work starts, so raising
here stops the job between chunks - the only place where stopping is both safe
and cheap. The pipeline never sees this class; it only knows the callback
raised something, and `_emit` does not swallow this one.

Python cannot kill a thread that is inside a CTranslate2 or pyannote call, so
a cancel requested mid-chunk takes effect when that chunk finishes. The UI
says exactly that instead of pretending the work stopped instantly.
"""
from __future__ import annotations

from ..pipeline import register_cancel_exception


class JobCancelled(RuntimeError):
    """Raised by the progress callback when the user pressed Cancel."""


# Pipeline._emit only distinguishes "this is a deliberate stop" from "a
# listener crashed" by the exception type, so the type is registered once here
# rather than imported the other way round (core must not import the server).
register_cancel_exception(JobCancelled)


def raise_if_cancelled(flag) -> None:
    """`flag` is any object with a `.is_set()` method (threading.Event)."""
    if flag is not None and flag.is_set():
        raise JobCancelled("cancelled by user")
