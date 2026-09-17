"""Compile the stereo audio preset into the generated Premiere JSX.

Audio presets contain alternate mono/stereo/5.1 component records, not six
effects to stack. We process a stereo nest, so only ChannelType=1 is relevant.
The XML stores normalized parameter values; these must not be passed as dB.
"""
from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


RAW_AUDIO_PRESET = "Raw Scene Audio.prfpset"
DYNAMICS_ID = "f76dad9c-77da-477a-9939-eefefc5c23bd"
LIMITER_ID = "e0b23f05-f1a7-4ef7-9b50-7ec3e3002058"

# Piecewise input/output dB curve. Leave the noise floor alone, raise quiet
# dialogue and compress louder passages. The limiter handles attack overshoot.
# Premiere's Location/Value graph coordinates map -100..0 dB to 0..1.
LEVELING_CURVE_DB = ((-100, -100), (-60, -60), (-40, -18), (-20, -12), (0, -10))


def load_raw_audio_effects(path: Path) -> list[dict[str, Any]]:
    """Read scalar controls and configure Dynamics before the saved limiter.

    Keep the user-exported limiter settings (including its normalized ceiling).
    Reject unknown layouts instead of silently applying an incomplete preset.
    """
    root = ET.parse(path).getroot()
    objects = {node.get("ObjectID"): node for node in root}
    effects = {}
    for preset in root.findall("FilterPreset"):
        ref = preset.find("Component")
        component = objects.get(ref.get("ObjectRef")) if ref is not None else None
        if component is None or component.tag != "AudioFilterComponent":
            continue
        if component.findtext("AudioComponent/ChannelType") != "1":
            continue
        match_name = preset.findtext("FilterMatchName")
        if match_name not in (DYNAMICS_ID, LIMITER_ID):
            raise ValueError(f"Unsupported stereo raw audio effect: {match_name}")
        if match_name in effects:
            raise ValueError(f"Duplicate stereo raw audio effect: {match_name}")
        params = []
        for ref in component.findall("AudioComponent/Component/Params/Param"):
            param = objects[ref.get("ObjectRef")]
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
            params.append({"index": int(ref.get("Index")), "name": param.findtext("Name"), "value": value})
        params.sort(key=lambda p: p["index"])
        if [p["index"] for p in params] != list(range(len(params))):
            raise ValueError("Non-contiguous audio parameter indices")
        effects[match_name] = {
            "matchName": match_name,
            "names": [component.findtext("AudioComponent/Component/DisplayName")],
            "params": params,
        }

    if set(effects) != {DYNAMICS_ID, LIMITER_ID}:
        raise ValueError("Raw Scene Audio preset needs stereo Dynamics Processing and Hard Limiter")
    dynamics, limiter = effects[DYNAMICS_ID], effects[LIMITER_ID]
    dynamics["names"] += ["Dynamics Processing", "Traitement de la dynamique"]
    limiter["names"] += ["Hard Limiter", "Limitateur fixe"]
    if len(dynamics["params"]) != 33 or len(limiter["params"]) != 8:
        raise ValueError("Raw audio preset parameter layout has changed; inspect it before importing")

    # Match the exported curve controls before interpreting their indices.
    for point in range(7):
        for offset, name in enumerate((f"Location{point + 1}", f"Value{point + 1}")):
            if dynamics["params"][18 + point * 2 + offset]["name"] != name:
                raise ValueError("Unrecognized Dynamics Processing curve layout")
    for effect in (dynamics, limiter):
        effect["params"][0]["value"] = False  # Bypass
    dynamics["params"][11]["value"] = len(LEVELING_CURVE_DB) / 7.0
    dynamics["params"][12]["value"] = 1.0  # Link stereo channels
    dynamics["params"][14]["value"] = 0.0  # Do not output envelope only
    dynamics["params"][15]["value"] = 0.0  # Piecewise linear, no spline overshoot
    for point, (input_db, output_db) in enumerate(LEVELING_CURVE_DB):
        dynamics["params"][18 + point * 2]["value"] = (input_db + 100) / 100.0
        dynamics["params"][19 + point * 2]["value"] = (output_db + 100) / 100.0
    limiter["params"][5]["value"] = 1.0  # Link stereo channels
    return [dynamics, limiter]
