"""Device selection with actionable diagnostics.

The single most common install problem is a CPU-only PyTorch wheel in an
environment that does have a GPU. The default `torch.cuda.is_available()`
warning cannot distinguish that from "no GPU present", and the two need
completely different fixes, so we probe for the cause explicitly.

Also relevant: Blackwell cards (RTX 50xx, sm_120) require a CUDA 12.8+ build.
An older cu121 wheel installs fine and then reports no devices, which looks
exactly like a driver failure. `_blackwell_hint` catches that case.
"""
from __future__ import annotations

import logging
import shutil
import subprocess

log = logging.getLogger(__name__)

# Blackwell consumer parts are sm_120; workstation/datacenter are sm_100.
_BLACKWELL_SMS = {100, 120}


def _nvidia_smi_names() -> list[str]:
    """GPU names from the driver, independent of the torch build."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return []
    try:
        out = subprocess.run(
            [exe, "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode != 0:
            return []
        return [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]
    except (OSError, subprocess.SubprocessError):
        return []


def _looks_blackwell(names: list[str]) -> bool:
    for n in names:
        low = n.lower()
        # RTX 50xx is Blackwell; B100/B200 are datacenter Blackwell.
        if "rtx 50" in low or low.startswith(("nvidia b100", "nvidia b200")):
            return True
    return False


def resolve_device(requested: str) -> str:
    """Return a usable torch device string, logging why if we fall back."""
    import torch

    requested = (requested or "cpu").lower()

    if requested.startswith("cuda"):
        if torch.cuda.is_available():
            # Confirm the arch is actually supported, not just visible. A wheel
            # built without sm_120 loads but fails at the first kernel launch.
            try:
                major, minor = torch.cuda.get_device_capability(0)
                if (major * 10 + minor) in _BLACKWELL_SMS:
                    log.info("CUDA device %s (sm_%d%d) via torch %s / CUDA %s",
                             torch.cuda.get_device_name(0), major, minor,
                             torch.__version__, torch.version.cuda)
            except Exception:      # capability query is best-effort
                pass
            return requested

        # CUDA unavailable: work out which of the three causes it is.
        names = _nvidia_smi_names()
        cuda_build = torch.version.cuda

        if cuda_build is None:
            log.warning(
                "PyTorch is a CPU-only build (%s), so CUDA cannot be used even "
                "though %s. Install a CUDA wheel, e.g.:\n"
                "  pip install --index-url https://download.pytorch.org/whl/cu128 "
                "torch torchaudio\n"
                "Falling back to CPU for now.",
                torch.__version__,
                f"a GPU is present: {', '.join(names)}" if names else "no GPU was detected",
            )
        elif names and _looks_blackwell(names) and cuda_build.startswith("12."):
            minor = int(cuda_build.split(".")[1])
            if minor < 8:
                log.warning(
                    "GPU %s is Blackwell (sm_120) but PyTorch was built for "
                    "CUDA %s, which has no sm_120 kernels. Install the cu128 build:\n"
                    "  pip install --index-url https://download.pytorch.org/whl/cu128 "
                    "torch torchaudio",
                    ", ".join(names), cuda_build,
                )
            else:
                log.warning(
                    "CUDA reported unavailable despite CUDA %s and %s; check the "
                    "GPU driver and that no other process holds the device.",
                    cuda_build, ", ".join(names),
                )
        else:
            log.warning(
                "CUDA is unavailable (torch %s, built for CUDA %s, driver-side GPUs: %s). "
                "Usual causes: outdated GPU driver, or a torch version older than "
                "the driver supports. Falling back to CPU.",
                torch.__version__, cuda_build, ", ".join(names) or "none detected",
            )
        return "cpu"

    return requested


def resolve_float(requested: str) -> str:
    """float16 is unsafe on CPU; map it down so the run does not just crawl."""
    requested = (requested or "float32").lower()
    if requested in ("float16", "half") and not _cuda_ok():
        log.info("float16 requested without CUDA; using float32 on CPU")
        return "float32"
    return requested


def _cuda_ok() -> bool:
    try:
        import torch
        return torch.cuda.is_available()
    except Exception:
        return False
