from __future__ import annotations

import base64
import gzip
import json
import re
import struct
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


@dataclass(frozen=True)
class SubtitleEntry:
    index: int
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class SubtitleBakeResult:
    entries_count: int
    generated_count: int
    output_files: list[Path]


class PremiereSubtitleBakerService:
    """Bake one Premiere .mogrt per subtitle line from an SRT file."""

    _SUBTITLE_FILENAME_RE = re.compile(r"^subtitle_(\d+)\.mogrt$", re.IGNORECASE)
    _START_KEYFRAME_RE = re.compile(
        r"(<StartKeyframeValue[^>]*>)([\s\S]*?)(</StartKeyframeValue>)",
        re.IGNORECASE,
    )

    @classmethod
    def parse_srt_entries(cls, srt_path: Path) -> list[SubtitleEntry]:
        if not srt_path.exists():
            raise FileNotFoundError(f"SRT file not found: {srt_path}")

        content = srt_path.read_text(encoding="utf-8")
        content = content.replace("\ufeff", "")
        content = content.replace("\r\n", "\n").replace("\r", "\n")

        entries: list[SubtitleEntry] = []
        blocks = re.split(r"\n{2,}", content)
        fallback_index = 1

        for block in blocks:
            lines = [line for line in block.split("\n") if line.strip() != ""]
            if len(lines) < 2:
                continue

            cursor = 0
            maybe_index = lines[0].strip()
            if maybe_index.isdigit():
                cursor = 1

            if cursor >= len(lines):
                continue

            timing_line = lines[cursor]
            if "-->" not in timing_line:
                continue
            start_txt, end_txt = [part.strip() for part in timing_line.split("-->", 1)]
            start_sec = cls._parse_srt_timestamp(start_txt)
            end_sec = cls._parse_srt_timestamp(end_txt)
            if start_sec is None or end_sec is None or end_sec <= start_sec:
                continue

            text_lines = [line.rstrip() for line in lines[cursor + 1 :] if line.strip()]
            if not text_lines:
                continue

            idx = int(maybe_index) if maybe_index.isdigit() and int(maybe_index) > 0 else fallback_index
            fallback_index = max(fallback_index + 1, idx + 1)
            entries.append(
                SubtitleEntry(
                    index=idx,
                    start=start_sec,
                    end=end_sec,
                    text="\r".join(text_lines),
                )
            )

        return entries

    @classmethod
    def bake_from_srt(
        cls,
        *,
        template_mogrt_path: Path,
        srt_path: Path,
        output_dir: Path,
    ) -> SubtitleBakeResult:
        if not template_mogrt_path.exists():
            raise FileNotFoundError(f"Subtitle MOGRT template not found: {template_mogrt_path}")

        entries = cls.parse_srt_entries(srt_path)
        if not entries:
            raise ValueError(f"No subtitle entries parsed from {srt_path}")

        output_dir.mkdir(parents=True, exist_ok=True)
        cls._clear_previous_outputs(output_dir)

        template_entries = cls._read_template_entries(template_mogrt_path)
        definition_raw = template_entries.get("definition.json")
        project_prgraphic_raw = template_entries.get("project.prgraphic")
        if definition_raw is None or project_prgraphic_raw is None:
            raise ValueError(
                "Template MOGRT is missing required files: definition.json and/or project.prgraphic"
            )

        template_definition_text = definition_raw.decode("utf-8")

        generated: list[Path] = []
        for entry in entries:
            patched_definition, old_text = cls._patch_definition_json(
                template_definition_text,
                entry.text,
            )
            patched_project_prgraphic = cls._patch_project_prgraphic(
                project_prgraphic_raw,
                old_text,
                entry.text,
            )

            filename = f"subtitle_{entry.index:04d}.mogrt"
            output_path = output_dir / filename
            cls._write_baked_mogrt(
                output_path=output_path,
                template_entries=template_entries,
                patched_definition_json=patched_definition.encode("utf-8"),
                patched_project_prgraphic=patched_project_prgraphic,
            )
            generated.append(output_path)

        return SubtitleBakeResult(
            entries_count=len(entries),
            generated_count=len(generated),
            output_files=generated,
        )

    @classmethod
    def _parse_srt_timestamp(cls, raw: str) -> float | None:
        m = re.match(r"^(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})$", raw.strip())
        if not m:
            return None
        h = int(m.group(1))
        mn = int(m.group(2))
        sec = int(m.group(3))
        ms = int((m.group(4) + "00")[:3])
        return h * 3600 + mn * 60 + sec + ms / 1000.0

    @classmethod
    def _read_template_entries(cls, template_mogrt_path: Path) -> dict[str, bytes]:
        entries: dict[str, bytes] = {}
        with ZipFile(template_mogrt_path, "r") as zf:
            for info in zf.infolist():
                entries[info.filename] = zf.read(info.filename)
        return entries

    @classmethod
    def _clear_previous_outputs(cls, output_dir: Path) -> None:
        for path in output_dir.iterdir():
            if not path.is_file():
                continue
            match = cls._SUBTITLE_FILENAME_RE.match(path.name)
            if match:
                path.unlink(missing_ok=True)
                continue
            if path.name.lower() == "subtitle_entries.json":
                path.unlink(missing_ok=True)

    @classmethod
    def _patch_definition_json(cls, definition_json: str, new_text: str) -> tuple[str, str]:
        data = json.loads(definition_json)
        controls = data.get("clientControls") or []
        old_text = ""
        patched_any = False

        for control in controls:
            value = (control or {}).get("value") or {}
            str_db = value.get("strDB") or []
            if not str_db:
                continue
            first_val = str_db[0].get("str")
            if isinstance(first_val, str) and not old_text:
                old_text = first_val
            for localized in str_db:
                localized["str"] = new_text
            patched_any = True

        if not patched_any:
            raise ValueError("Template definition.json has no clientControls[].value.strDB entries")
        if not old_text:
            raise ValueError("Template definition.json did not provide an original subtitle text value")

        return json.dumps(data, ensure_ascii=False, separators=(",", ":")), old_text

    @classmethod
    def _patch_project_prgraphic(
        cls,
        project_prgraphic_bytes: bytes,
        old_text: str,
        new_text: str,
    ) -> bytes:
        with ZipFile(BytesIO(project_prgraphic_bytes), "r") as inner_zip:
            names = inner_zip.namelist()
            if not names:
                raise ValueError("project.prgraphic inner archive is empty")
            inner_project_name = names[0]
            gzip_payload = inner_zip.read(inner_project_name)

        xml_text = gzip.decompress(gzip_payload).decode("utf-8")
        match = cls._START_KEYFRAME_RE.search(xml_text)
        if not match:
            raise ValueError("StartKeyframeValue not found in project.prgraphic payload")

        b64_payload = re.sub(r"\s+", "", match.group(2))
        blob = bytearray(base64.b64decode(b64_payload))
        patched_blob = cls._patch_text_blob(blob, old_text, new_text)
        patched_b64 = base64.b64encode(patched_blob).decode("ascii")

        patched_xml = (
            xml_text[: match.start()]
            + match.group(1)
            + patched_b64
            + match.group(3)
            + xml_text[match.end() :]
        )
        patched_gzip_payload = gzip.compress(patched_xml.encode("utf-8"))

        out = BytesIO()
        with ZipFile(out, "w", ZIP_DEFLATED) as rebuilt:
            rebuilt.writestr(inner_project_name, patched_gzip_payload)
        return out.getvalue()

    @classmethod
    def _patch_text_blob(cls, blob: bytearray, old_text: str, new_text: str) -> bytes:
        if not old_text:
            raise ValueError("Cannot patch subtitle blob: template old text is empty")

        old_bytes = old_text.encode("utf-8")
        old_idx = blob.find(old_bytes)
        if old_idx < 0:
            raise ValueError(f"Cannot patch subtitle blob: '{old_text}' not found")
        old_string_start = old_idx - 4
        if old_string_start < 0:
            raise ValueError("Cannot patch subtitle blob: invalid old string offset")

        pointer_positions: list[int] = []
        for pos in range(0, len(blob) - 3):
            offset = struct.unpack_from("<I", blob, pos)[0]
            if pos + offset == old_string_start:
                pointer_positions.append(pos)

        if len(pointer_positions) != 1:
            raise ValueError(
                f"Cannot patch subtitle blob: expected 1 pointer to old string, found {len(pointer_positions)}"
            )

        pointer_pos = pointer_positions[0]
        while len(blob) % 4 != 0:
            blob.append(0)

        new_string_start = len(blob)
        new_bytes = new_text.encode("utf-8")
        blob.extend(struct.pack("<I", len(new_bytes)))
        blob.extend(new_bytes)
        blob.append(0)
        while len(blob) % 4 != 0:
            blob.append(0)

        new_offset = new_string_start - pointer_pos
        struct.pack_into("<I", blob, pointer_pos, new_offset)
        return bytes(blob)

    @classmethod
    def _write_baked_mogrt(
        cls,
        *,
        output_path: Path,
        template_entries: dict[str, bytes],
        patched_definition_json: bytes,
        patched_project_prgraphic: bytes,
    ) -> None:
        with ZipFile(output_path, "w", ZIP_DEFLATED) as out_zip:
            for name, raw in template_entries.items():
                if name == "definition.json":
                    out_zip.writestr(name, patched_definition_json)
                elif name == "project.prgraphic":
                    out_zip.writestr(name, patched_project_prgraphic)
                else:
                    out_zip.writestr(name, raw)
