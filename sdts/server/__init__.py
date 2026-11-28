"""Local web workbench for the diarization pipeline.

`sdts serve` starts a FastAPI app bound to 127.0.0.1 that wraps `Pipeline` in a
stateful job queue and serves the static pages in `sdts/web/`.

Scope, deliberately: this is a single-user, trusted-local-machine tool. No
authentication, no multi-tenancy, no TLS. Audio never leaves the disk it is
already on; voice embeddings stay in the registry and never reach the browser.
`app.py` refuses requests whose Origin is not the server itself, which blocks a
malicious page in the same browser from driving the API.

Nothing here is imported by the core pipeline, and FastAPI is an optional
dependency (`pip install sdts[web]`): `sdts FILE` and the `labels` / `registry`
subcommands keep working with no web stack installed.
"""
from __future__ import annotations


def create_app(*args, **kwargs):
    """Lazy re-export of `sdts.server.app.create_app`.

    Importing `sdts.server` must not require FastAPI at runtime, so the app
    factory is only resolved when someone actually asks for it.
    """
    from .app import create_app as _create_app
    return _create_app(*args, **kwargs)


__all__ = ["create_app"]
