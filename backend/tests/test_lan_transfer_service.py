# backend/tests/test_lan_transfer_service.py
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from app.services.export_service import ManifestEntry
from app.services.lan_transfer_service import LanTransferService


class _FakeProject:
    id = "p1"
    drive_folder_id = "drv-folder-1"


@pytest.fixture
def fake_manifest(tmp_path: Path, monkeypatch):
    jsx = tmp_path / "import_project.jsx"
    jsx.write_bytes(b"// jsx" * 10)
    entries = [
        ManifestEntry(relative_path="SPM_demo_p1/import_project.jsx", source_path=jsx),
        ManifestEntry(
            relative_path="SPM_demo_p1/README.txt",
            inline_content=b"hello readme",
            mime_type="text/plain",
        ),
    ]
    monkeypatch.setattr(
        "app.services.lan_transfer_service.LanTransferService._build_entries",
        classmethod(lambda cls, project: ("SPM_demo_p1", entries)),
    )
    return entries


def test_manifest_payload_strips_folder_prefix(fake_manifest):
    payload = LanTransferService.build_manifest_payload(_FakeProject())
    assert payload["api_version"] == 1
    assert payload["folder_name"] == "SPM_demo_p1"
    assert payload["drive_folder_id"] == "drv-folder-1"
    paths = [f["relative_path"] for f in payload["files"]]
    assert paths == ["import_project.jsx", "README.txt"]
    assert payload["files"][0]["size"] == 60
    assert payload["files"][1]["size"] == len(b"hello readme")


def test_resolve_entry_by_stripped_path(fake_manifest):
    entry = LanTransferService.resolve_entry(_FakeProject(), "README.txt")
    assert entry is not None and entry.inline_content == b"hello readme"
    assert LanTransferService.resolve_entry(_FakeProject(), "../../etc/passwd") is None
    assert LanTransferService.resolve_entry(_FakeProject(), "nope.bin") is None
