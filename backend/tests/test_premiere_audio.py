"""Audio config, exported preset interpretation, and executable JSX contracts."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import PROJECT_ROOT
from app.services.premiere_audio import DYNAMICS_ID, LIMITER_ID, RAW_AUDIO_PRESET, load_raw_audio_effects
from app.services.processing import ProcessingService
from app.services.voice_config_service import VoiceConfigService
from test_processing_overlay_jsx import _template


PRESET = PROJECT_ROOT / "assets" / RAW_AUDIO_PRESET


def _voice_config(**extra):
    return {"default_voice_key": "test", "voices": {"test": {
        "display_name": "Test", "elevenlabs_voice_id": "id", **extra,
    }}}


@pytest.mark.parametrize("gain", [-96, -24, 0, 7.5, 15])
def test_voice_gain_accepts_attenuation_and_positive_boost(gain):
    assert VoiceConfigService._parse(_voice_config(volume_db=gain)).voices["test"].volume_db == gain


def test_existing_voice_config_defaults_to_unity():
    assert VoiceConfigService._parse(_voice_config()).voices["test"].volume_db == 0


@pytest.mark.parametrize("gain", [True, False, "7", None, float("nan"), float("inf"), -96.1, 15.1])
def test_invalid_voice_gain_is_rejected(gain):
    with pytest.raises(ValueError, match="volume_db"):
        VoiceConfigService._parse(_voice_config(volume_db=gain))


def test_preset_preserves_saved_order_curve_and_channel_variants():
    effects = load_raw_audio_effects(PRESET)
    assert [e["matchName"] for e in effects] == [LIMITER_ID] * 3 + [DYNAMICS_ID] * 3
    assert [e["channelType"] for e in effects] == [1, 0, 2, 1, 0, 2]
    assert effects[0]["params"][1]["value"] == 0.89901471138
    assert effects[1]["params"][1]["value"] == effects[2]["params"][1]["value"] == 0.999000012875
    for effect in effects[3:]:
        params = effect["params"]
        count = round(params[11]["value"] * 7)
        points = [(params[18 + i * 2]["value"] * 100 - 100,
                   params[19 + i * 2]["value"] * 100 - 100) for i in range(count)]
        for actual, expected in zip(points, [(-100, -100), (-20, -20), (0, 0)], strict=True):
            assert actual == pytest.approx(expected)
        assert params[12]["value"] == 0  # Preserve the file's unlinked channels.


def test_preset_uses_reference_order_instead_of_xml_object_order(tmp_path):
    tree = ET.parse(PRESET)
    refs = tree.getroot().find("FilterPresetItem/FilterPresets")
    for ref in refs:
        ref.set("Index", str(5 - int(ref.get("Index"))))
    path = tmp_path / "reordered.prfpset"
    tree.write(path)
    effects = load_raw_audio_effects(path)
    assert [e["matchName"] for e in effects] == [DYNAMICS_ID] * 3 + [LIMITER_ID] * 3
    assert [e["channelType"] for e in effects] == [2, 0, 1, 2, 0, 1]


@pytest.mark.parametrize("mutation", ["duplicate_variant", "animated", "bad_indices", "extra_effect"])
def test_preset_rejects_ambiguous_or_unsupported_serialization(tmp_path, mutation):
    tree = ET.parse(PRESET)
    root = tree.getroot()
    if mutation == "duplicate_variant":
        for c in root.findall("AudioFilterComponent"):
            c.find("AudioComponent/ChannelType").text = "0"
    elif mutation == "animated":
        root.find("AudioComponentParam/IsTimeVarying").text = "true"
    elif mutation == "bad_indices":
        root.find("AudioFilterComponent/AudioComponent/Component/Params/Param").set("Index", "42")
    else:
        root.find("FilterPreset/FilterMatchName").text = "unknown-effect"
    path = tmp_path / "broken.prfpset"
    tree.write(path)
    with pytest.raises(ValueError):
        load_raw_audio_effects(path)


def _render(scenes, gain=0):
    return ProcessingService._render_jsx_from_template(
        project_id="audio_test", scenes=scenes, source_audio_policies={},
        source_fps_num=24000, source_fps_den=1001,
        subtitle_timing_relative_path="subtitles/subtitle_timings.srt",
        raw_scene_subtitle_timing_relative_path="raw_scene_subtitles/text_subtitles.srt",
        raw_scene_subtitle_mogrt_relative_dir="raw_scene_subtitles/text_mogrts",
        music_filename="music.wav", music_gain_db=-24, voice_gain_db=gain,
        template=_template(), overlay_title_enabled=False, overlay_category_enabled=False,
    )


def test_render_embeds_saved_audio_variants_and_voice_gain():
    jsx = _render([{"is_raw": True, "start": 2, "end": 4}], 15)
    effects = json.loads(re.search(r"var RAW_AUDIO_EFFECTS = (.*);", jsx)[1])
    assert effects == load_raw_audio_effects(PRESET)
    assert "var VOICE_GAIN_DB = 15.0;" in jsx
    assert "var MUSIC_GAIN_DB = -24;" in jsx


def test_raw_preset_runs_after_timeline_construction_without_nesting():
    jsx = _render([{"is_raw": True, "start": 2, "end": 4}])
    main = jsx[jsx.index("    var seqName = BATCH_SEQUENCE_NAME;"):jsx.index('    perfEnd("total");')]
    assert main.index("applyRawSceneAudioPreset(sequence, scenes);") > main.index("    importUnifiedSubtitles(")
    assert "catch (eRawPreset)" in main
    assert "createRawAudioNest" not in jsx
    assert "levelRawSceneAudio" not in jsx
    assert 'deleteSequenceByName("ATR_RAW_AUDIO__"' not in jsx


def test_no_raw_scenes_does_not_require_a_preset(monkeypatch):
    monkeypatch.setattr("app.services.premiere_audio.load_raw_audio_effects", lambda _: pytest.fail("No preset needed"))
    assert "var RAW_AUDIO_EFFECTS = [];" in _render([])


@pytest.mark.parametrize("error", [FileNotFoundError("preset missing"), ValueError("unsupported preset")])
def test_unavailable_preset_generates_importable_jsx_with_a_warning(monkeypatch, error):
    def fail(_):
        raise error
    monkeypatch.setattr("app.services.premiere_audio.load_raw_audio_effects", fail)
    jsx = _render([{"is_raw": True, "start": 2, "end": 4}])
    assert "var RAW_AUDIO_EFFECTS = [];" in jsx
    warning = json.loads(re.search(r"var RAW_AUDIO_PRESET_WARNING = (.*);", jsx)[1])
    assert "original audio retained" in warning
    assert str(error) in warning


def test_generation_resolves_project_voice_gain(monkeypatch):
    from app.models.project import Project
    from app.models.transcription import Transcription
    from app.services.template_service import TemplateService

    monkeypatch.setattr(TemplateService, "get", lambda _: _template())
    monkeypatch.setattr(VoiceConfigService, "get_config", lambda **_: VoiceConfigService._parse(_voice_config(volume_db=7)))
    jsx = ProcessingService.generate_jsx_script(
        Project(id="test", template="classic", voice_key="test"), Transcription(language="fr", scenes=[]), [],
    )
    assert "var VOICE_GAIN_DB = 7.0;" in jsx


def test_export_includes_the_user_audio_preset(monkeypatch):
    from app.models.project import Project
    from app.services.export_service import ExportService
    from app.services.template_service import TemplateService

    monkeypatch.setattr(TemplateService, "get", lambda _: _template())
    assert RAW_AUDIO_PRESET in ExportService.get_required_import_assets(Project(id="test"))


def test_missing_optional_preset_does_not_become_a_required_export_asset(monkeypatch, tmp_path):
    from app.models.project import Project
    from app.services.export_service import ExportService
    from app.services.template_service import TemplateService

    monkeypatch.setattr(TemplateService, "get", lambda _: _template())
    monkeypatch.setattr(ExportService, "get_assets_dir", lambda: tmp_path)
    assert RAW_AUDIO_PRESET not in ExportService.get_required_import_assets(Project(id="test"))


def _run_js(script):
    node = shutil.which("node")
    if not node: pytest.skip("Node required for Premiere scripting contract tests")
    result = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def _jsx_function_block(start, end):
    source = ProcessingService.PREMIERE_JSX_TEMPLATE_PATH.read_text()
    return source[source.index("  function " + start):source.index("  function " + end)]


def test_clip_gain_is_absolute_supports_positive_and_rejects_clamping():
    code = _jsx_function_block("scoreAudioGainProperty", "buildLoopedMusicBed")
    _run_js("""
        const assert = require('node:assert/strict');
        var AUDIO_GAIN_RAW_FALLBACK_AT_0DB = 0.1778279410038923;
        function log() {}
        function setPropertyValueFast(p, v) { p.setValue(v); return true; }
    """ + code + """
        var value = 0.1778279410038923;
        var prop = { displayName: 'Niveau', getValue: () => value, setValue: v => value = v };
        var props = [prop]; props.numItems = 1;
        var components = [{ displayName: 'Volume', properties: props }]; components.numItems = 1;
        var clip = { name: 'Narration', components };
        for (var gain of [-96, -24, 0, 7.5, 15, 7.5, 0]) {
            assert.equal(applyTrackItemGainDb(clip, gain), true);
            assert.ok(Math.abs(20 * Math.log10(value / 0.1778279410038923) - gain) < 0.00001);
        }
        // Simulate a host silently clamping at unity instead of accepting boost.
        prop.setValue = v => value = Math.min(v, 0.1778279410038923);
        assert.equal(applyTrackItemGainDb(clip, 15), false);
        prop.getValue = () => { throw Error('stale property'); };
        assert.equal(applyTrackItemGainDb(clip, 0), false);
    """)


@pytest.mark.parametrize("channel_mode", ["stereo", "mono", "51", "split51", "unknown"])
def test_direct_raw_preset_preserves_clips_and_uses_saved_channel_variant(channel_mode):
    _run_raw_preset_host(channel_mode=channel_mode)


@pytest.mark.parametrize("failure", [
    "lookup", "lookup_second", "qe_sequence", "insert", "second_insert",
    "insert_after_mutation", "noop_insert", "layout", "write", "clamp", "enable", "bypass",
    "rollback", "host_property", "outer_host", "logging", "existing_effect",
    "missing_preset", "missing_clip", "no_raw",
])
def test_raw_preset_failures_warn_and_allow_remaining_import_steps(failure):
    _run_raw_preset_host(failure=failure)


@pytest.mark.parametrize("host_mode", ["truthy_noop", "runtime_identity", "stale_dom", "string_counts", "catalog", "late_insert"])
def test_raw_effect_insertion_verifies_mutation_and_handles_host_variations(host_mode):
    _run_raw_preset_host(host_mode=host_mode)


def _run_raw_preset_host(*, channel_mode="stereo", failure=None, host_mode="normal"):
    code = _jsx_function_block("warnRawAudioPreset", "scoreAudioGainProperty")
    fixture = (Path(__file__).parent / "fixtures" / "raw_audio_preset_host.js").read_text()
    _run_js(
        "var RAW_AUDIO_EFFECTS=" + json.dumps(load_raw_audio_effects(PRESET)) + ";\n"
        + "var channelMode=" + json.dumps(channel_mode) + ";\n"
        + "var hostMode=" + json.dumps(host_mode) + ";\n"
        + "var failure=" + json.dumps(failure) + ";\n" + code + fixture
    )
