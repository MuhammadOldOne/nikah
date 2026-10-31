"""Deployment files: the nginx template keeps login data out of its access log, and the compose
header does not suggest starting the bot before the legacy import."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_nginx_access_log_has_no_query_strings_and_skips_magic_links() -> None:
    conf = (ROOT / "deploy/nginx/nikah.conf").read_text(encoding="utf-8")
    [log_format] = re.findall(r"log_format nikah_noquery (.*?);", conf, flags=re.S)
    variables = set(re.findall(r"\$\w+", log_format))
    assert not variables & {"$request", "$request_uri", "$args", "$query_string", "$http_referer"}
    assert "$uri" in variables
    assert re.findall(r"^\s*access_log\s+(.*);", conf, flags=re.M) == [
        "/var/log/nginx/nikah.access.log nikah_noquery if=$nikah_loggable"
    ]
    assert re.search(r"~\^/auth/magic/\s+0;", conf)


def test_compose_header_never_suggests_starting_everything_before_the_import() -> None:
    header = "".join(
        line
        for line in (ROOT / "docker-compose.yml").read_text(encoding="utf-8").splitlines(True)
        if line.startswith("#")
    )
    assert "up -d --build" not in header and "docker compose up -d db web" in header
