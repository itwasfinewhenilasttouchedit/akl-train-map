"""NCSA combined-format access log, for self-hosted traffic analytics.

WHY THIS EXISTS RATHER THAN uvicorn's access_log
    uvicorn's access log cannot be given a custom format, and the line it
    emits carries the *proxy's* address. Behind Cloudflare Tunnel every
    request appears to come from 127.0.0.1, so its lines are useless for
    analytics. This middleware writes real combined-format lines that
    GoAccess parses natively, taking the client address from the header
    Cloudflare sets.

TRUSTING THE CLIENT-IP HEADER
    CF-Connecting-IP is only trustworthy because the single inbound path to
    this origin is the tunnel: the container's port is published on an
    isolated DMZ VLAN whose gateway denies inbound from the WAN and from
    every internal VLAN bar one admin host. Nothing else can reach the app
    to forge it. If this app is ever exposed directly, this header becomes
    attacker-controlled and the value must be dropped -- hence
    ACCESS_LOG_TRUST_PROXY, which defaults to on but can be turned off.

DISABLED BY DEFAULT
    ACCESS_LOG unset (or "off") writes nothing at all, which is the correct
    posture for the Fly instance and for local development.

WHAT IS AND IS NOT RECORDED
    Recorded: timestamp, client IP, method, path, protocol, status, bytes,
    referrer, user agent. That is the combined format and nothing more.
    Not recorded: request bodies, cookies, query strings on feedback
    submissions, or any reporter identity. The public pages never attribute
    a report to a person -- the feedback table has no reporter column -- and
    this log does not reintroduce that by the back door.
    The file rotates by size: flash storage has finite writes and an
    unbounded log on a phone is a slow way to kill the eMMC.
"""

from __future__ import annotations

import logging
import os
import time
from logging.handlers import RotatingFileHandler

LOG = logging.getLogger("akl.access")

_ENABLED = False
_TRUST_PROXY = True
_writer: logging.Logger | None = None

# Combined-format month abbreviations: the format demands English regardless
# of the host locale, so they are hardcoded rather than taken from strftime.
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def init() -> bool:
    """Configure the access log. Returns True when enabled.

    Safe to call more than once; later calls are ignored.
    """
    global _ENABLED, _TRUST_PROXY, _writer

    if _writer is not None:
        return _ENABLED

    path = (os.environ.get("ACCESS_LOG", "") or "").strip()
    if not path or path.lower() == "off":
        _ENABLED = False
        LOG.info("access log: disabled")
        return False

    _TRUST_PROXY = (os.environ.get("ACCESS_LOG_TRUST_PROXY", "1").strip().lower()
                    not in ("0", "false", "no", "off"))

    # 20 MB x 5 caps the log at 100 MB. At the measured request rates this is
    # months of history, and it can never grow without bound on flash.
    max_bytes = _env_int("ACCESS_LOG_MAX_BYTES", 20 * 1024 * 1024)
    backups = _env_int("ACCESS_LOG_BACKUPS", 5)

    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        handler = RotatingFileHandler(path, maxBytes=max_bytes,
                                      backupCount=backups, encoding="utf-8")
        # The line is already fully formatted; the handler must add nothing.
        handler.setFormatter(logging.Formatter("%(message)s"))
        writer = logging.getLogger("akl.access.file")
        writer.setLevel(logging.INFO)
        writer.propagate = False       # never duplicate into the app log
        for old in list(writer.handlers):
            writer.removeHandler(old)
        writer.addHandler(handler)
    except OSError as exc:
        # A log that cannot be opened must not stop the site from serving.
        LOG.warning("access log: cannot open %s (%s); disabled", path, exc)
        _ENABLED = False
        _writer = logging.getLogger("akl.access.noop")
        return False

    _writer = writer
    _ENABLED = True
    LOG.info("access log: %s (rotate %d x %d bytes, trust_proxy=%s)",
             path, backups, max_bytes, _TRUST_PROXY)
    return True


def enabled() -> bool:
    return _ENABLED


def _client_ip(request) -> str:
    """Best available client address.

    Cloudflare sets CF-Connecting-IP to the real client. X-Forwarded-For is
    the fallback and only its FIRST entry is the client -- later entries are
    proxies, and taking the last would record Cloudflare's own edge.
    """
    if _TRUST_PROXY:
        cf = request.headers.get("cf-connecting-ip")
        if cf:
            return cf.strip()
        xff = request.headers.get("x-forwarded-for")
        if xff:
            first = xff.split(",")[0].strip()
            if first:
                return first
    client = getattr(request, "client", None)
    return getattr(client, "host", None) or "-"


def _stamp(when: float) -> str:
    """strftime("%d/%b/%Y:%H:%M:%S %z") without locale dependence."""
    lt = time.localtime(when)
    offset = -(time.altzone if lt.tm_isdst and time.daylight else time.timezone)
    sign = "+" if offset >= 0 else "-"
    offset = abs(offset)
    return (f"{lt.tm_mday:02d}/{_MONTHS[lt.tm_mon - 1]}/{lt.tm_year:04d}:"
            f"{lt.tm_hour:02d}:{lt.tm_min:02d}:{lt.tm_sec:02d} "
            f"{sign}{offset // 3600:02d}{(offset % 3600) // 60:02d}")


def _quote(value: str | None) -> str:
    """Escape for a quoted combined-format field.

    A user agent or referrer containing a double quote would otherwise end
    the field early and desynchronise every parser downstream.
    """
    if not value:
        return "-"
    return value.replace("\\", "\\\\").replace('"', '\\"')


def record(request, status: int, length: int | str = "-",
           when: float | None = None) -> None:
    """Append one combined-format line. Never raises."""
    if not _ENABLED or _writer is None:
        return
    try:
        path = request.url.path
        if request.url.query:
            path = f"{path}?{request.url.query}"
        proto = request.scope.get("http_version", "1.1")
        line = (f'{_client_ip(request)} - - [{_stamp(when or time.time())}] '
                f'"{_quote(request.method)} {_quote(path)} HTTP/{proto}" '
                f'{int(status)} {length} '
                f'"{_quote(request.headers.get("referer"))}" '
                f'"{_quote(request.headers.get("user-agent"))}"')
        _writer.info(line)
    except Exception as exc:                      # pragma: no cover - defensive
        # Analytics are never worth a 500. Log once at debug and move on.
        LOG.debug("access log: failed to record (%s)", exc)
