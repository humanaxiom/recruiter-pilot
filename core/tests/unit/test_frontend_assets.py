"""Slice S0 — offline assets + base layout.

Locks the offline/no-CDN posture: all JS/CSS is served locally from
``frontend/static/`` and no ``http(s)``/``cdn`` reference may leak into a
rendered page. ``htmx.min.js`` is vendored (htmx 2.0.4); ``app.css`` is a
hand-authored utility stylesheet (there is no Node toolchain in the
container, so this is deliberately NOT a Tailwind build).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from frontend import api_client
from frontend.app import app


@pytest.fixture
def client() -> Any:
    app.config.update(TESTING=True)
    return app.test_client()


def test_static_app_css_is_served(client: Any) -> None:
    resp = client.get("/static/app.css")
    assert resp.status_code == 200
    assert "text/css" in resp.headers.get("Content-Type", "")


def test_static_htmx_is_served(client: Any) -> None:
    resp = client.get("/static/vendor/htmx.min.js")
    assert resp.status_code == 200


def test_rendered_page_links_the_local_stylesheet(
    monkeypatch: Any, client: Any
) -> None:
    monkeypatch.setattr(api_client, "list_jobs", MagicMock(return_value=[]))
    resp = client.get("/")
    body = resp.get_data(as_text=True)
    assert '<link rel="stylesheet" href="/static/app.css">' in body


def test_rendered_page_loads_local_vendored_htmx(monkeypatch: Any, client: Any) -> None:
    monkeypatch.setattr(api_client, "list_jobs", MagicMock(return_value=[]))
    resp = client.get("/")
    body = resp.get_data(as_text=True)
    assert 'src="/static/vendor/htmx.min.js"' in body


@pytest.mark.parametrize(
    "path_fn",
    [
        lambda: "/",
        lambda: f"/jobs/{uuid4()}",
    ],
)
def test_no_cdn_or_remote_references_in_rendered_pages(
    monkeypatch: Any, client: Any, path_fn: Any
) -> None:
    monkeypatch.setattr(api_client, "list_jobs", MagicMock(return_value=[]))
    monkeypatch.setattr(
        api_client,
        "get_job",
        MagicMock(
            return_value={
                "id": str(uuid4()),
                "title": "T",
                "status": "draft",
                "blind_review": True,
                "parsed_at": None,
                "min_years": None,
                "description_parsed": None,
            }
        ),
    )
    monkeypatch.setattr(api_client, "list_resumes", MagicMock(return_value=[]))
    resp = client.get(path_fn())
    body = resp.get_data(as_text=True)
    assert "http" not in body
    assert "cdn" not in body.lower()


def test_static_css_file_has_no_remote_references() -> None:
    from pathlib import Path

    css = Path(app.static_folder) / "app.css"  # type: ignore[arg-type]
    text = css.read_text(encoding="utf-8")
    assert "http" not in text
    assert "cdn" not in text.lower()
