"""Read saved audio preset variants without changing their order or values."""
from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


RAW_AUDIO_PRESET = "Raw Scene Audio.prfpset"
DYNAMICS_ID = "f76dad9c-77da-477a-9939-eefefc5c23bd"
LIMITER_ID = "e0b23f05-f1a7-4ef7-9b50-7ec3e3002058"
_EFFECT_NAMES = {
    DYNAMICS_ID: ["Dynamics Processing", "Traitement de la dynamique"],
    LIMITER_ID: ["Hard Limiter", "Limitateur fixe"],
}


def load_raw_audio_effects(path: Path) -> list[dict[str, Any]]:
    """Compile static mono/stereo/5.1 records in the preset's saved order.

    Channel variants are alternatives, not effects to stack on one clip.
    Parameters are serialized host values, not dB. Unsupported files raise
    here; the JSX generator catches that and emits a non-fatal import warning.
    """
    root = ET.parse(path).getroot()
    objects = {node.get("ObjectID"): node for node in root}
    items = root.findall("FilterPresetItem")
    if len(items) != 1:
        raise ValueError("Expected one Raw Scene Audio preset")
    refs = sorted(items[0].findall("FilterPresets/FilterPreset"), key=lambda r: int(r.get("Index")))
    effects = []
    seen = set()
    for ref in refs:
        preset = objects[ref.get("ObjectRef")]
        component = objects[preset.find("Component").get("ObjectRef")]
        if component.tag != "AudioFilterComponent":
            raise ValueError("Raw Scene Audio preset contains a non-audio effect")
        channel_type = int(component.findtext("AudioComponent/ChannelType"))
        match_name = preset.findtext("FilterMatchName")
        if channel_type not in (0, 1, 2) or match_name not in _EFFECT_NAMES:
            raise ValueError("Unsupported Raw Scene Audio effect or channel format")
        key = (channel_type, match_name)
        if key in seen:
            raise ValueError("Duplicate Raw Scene Audio effect variant")
        seen.add(key)
        params = []
        for param_ref in component.findall("AudioComponent/Component/Params/Param"):
            param = objects[param_ref.get("ObjectRef")]
            if param.findtext("IsTimeVarying") != "false":
                raise ValueError("Raw audio preset must use static parameters")
            raw = param.findtext("CurrentValue")
            if raw is None:
                raw = (param.findtext("StartKeyframe") or "").split(",")[1]
            value = {"false": False, "true": True}.get(raw)
            if value is None:
                value = float(raw)
                if not math.isfinite(value) or not 0 <= value <= 1:
                    raise ValueError(f"Invalid audio parameter: {param.findtext('Name')}")
            params.append({"index": int(param_ref.get("Index")), "name": param.findtext("Name"), "value": value})
        params.sort(key=lambda p: p["index"])
        if not params or [p["index"] for p in params] != list(range(len(params))):
            raise ValueError("Non-contiguous audio parameter indices")
        # Both supported effects expose Bypass at index 0. It lets failed
        # applications restore the original sound without touching clip media.
        if not isinstance(params[0]["value"], bool):
            raise ValueError("Raw audio effect has no recognizable bypass control")
        effects.append({
            "matchName": match_name,
            "channelType": channel_type,
            "names": list(dict.fromkeys([
                component.findtext("AudioComponent/Component/DisplayName"),
                *_EFFECT_NAMES[match_name],
            ])),
            "params": params,
        })
    if not effects:
        raise ValueError("Raw Scene Audio preset contains no effects")
    for channel_type in {effect["channelType"] for effect in effects}:
        if {e["matchName"] for e in effects if e["channelType"] == channel_type} != set(_EFFECT_NAMES):
            raise ValueError("Incomplete Raw Scene Audio channel variant")
    return effects
