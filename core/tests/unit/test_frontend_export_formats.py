"""Slice S7 — export format options on the shortlist page.

``shortlist_list.html`` offers all three backend export formats — ``csv``,
``evidence-csv`` and ``json`` — each linking to
``GET /jobs/<id>/shortlist/export?format=<fmt>`` (the existing
``shortlist_export`` proxy + ``api_client.export_shortlist`` already support
``format``).

**Hard invariant:** exports stay anonymized. The page must NEVER add a
``reveal`` control / hidden field / query param of any kind — the proxy
defaults ``reveal=False`` and no browser input may override it.
"""

from __future__ import annotations

from pathlib import Path
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


def test_all_three_export_formats_are_linked(monkeypatch: Any, client: Any) -> None:
    job_id = uuid4()
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=[]))
    monkeypatch.setattr(
        api_client, "list_resumes", MagicMock(return_value=[{"status": "parsed"}])
    )
    body = client.get(f"/jobs/{job_id}/shortlist").get_data(as_text=True)
    base = f"/jobs/{job_id}/shortlist/export"
    assert f"{base}?format=csv" in body
    assert f"{base}?format=evidence-csv" in body
    assert f"{base}?format=json" in body


def test_export_links_never_carry_a_reveal_param(monkeypatch: Any, client: Any) -> None:
    job_id = uuid4()
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=[]))
    monkeypatch.setattr(
        api_client, "list_resumes", MagicMock(return_value=[{"status": "parsed"}])
    )
    body = client.get(f"/jobs/{job_id}/shortlist").get_data(as_text=True)
    assert "reveal" not in body.lower()


def test_shortlist_template_has_no_reveal_control() -> None:
    """Structural guard: the template source has no ``reveal`` field/param
    anywhere — exports cannot be de-anonymized from the browser."""
    template = (Path(app.root_path) / "templates" / "shortlist_list.html").read_text(
        encoding="utf-8"
    )
    lowered = template.lower()
    assert 'name="reveal"' not in lowered
    assert "reveal=true" not in lowered
    assert "reveal" not in lowered
