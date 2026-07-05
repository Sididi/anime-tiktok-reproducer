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


@pytest.mark.parametrize(
    ("name", "allowed"),
    [
        ("output.mp4", True),
        ("OUTPUT.MP4", True),
        ("output_no_music.wav", True),
        ("ATR_final_v2.mp4", True),
        ("atr_final.mp4", True),           # ATR pattern is case-insensitive
        ("ATR_final__atr_proxy.mp4", False),
        ("output_instagram.mp4", False),
        ("evil/../output.mp4", False),
        ("..\\output.mp4", False),
        (".hidden.mp4", False),
        ("random.mp4", False),
        ("atr_evil.mp4\n", False),          # trailing newline must not slip past `$` anchor
        ("output.mp4\n", False),            # trailing newline on an exact-match name
        ("atr_x\t.mp4", False),             # embedded control character (tab)
    ],
)
def test_output_filename_whitelist(name, allowed):
    assert LanTransferService.is_allowed_output_filename(name) is allowed


@pytest.mark.asyncio
async def test_receive_output_stream_atomic(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.services.lan_transfer_service.ExportService.get_output_dir",
        classmethod(lambda cls, pid: tmp_path / pid / "output"),
    )

    async def _chunks():
        yield b"abc"
        yield b"def"

    dest = await LanTransferService.receive_output_stream("p1", "output.mp4", _chunks())
    assert dest == tmp_path / "p1" / "output" / "output.mp4"
    assert dest.read_bytes() == b"abcdef"
    assert not list(dest.parent.glob("*.lan_tmp"))


def test_sweep_stale_tmp_files(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.lan_transfer_service.settings.projects_dir", tmp_path)
    out = tmp_path / "p1" / "output"
    out.mkdir(parents=True)
    (out / "output.mp4.deadbeef.lan_tmp").write_bytes(b"partial")
    (out / "output.mp4").write_bytes(b"keep")
    assert LanTransferService.sweep_stale_tmp_files() == 1
    assert (out / "output.mp4").exists()
    assert not list(out.glob("*.lan_tmp"))
