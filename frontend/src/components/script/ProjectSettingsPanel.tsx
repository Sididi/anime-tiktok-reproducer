import { useState } from "react";
import type {
  ScriptPhaseSettingsRequest,
  ScriptPhaseSettingsResponse,
} from "../../types";

interface TemplateOption {
  key: string;
  label: string;
  overlay_enabled: boolean;
  overlay_title_enabled: boolean;
  overlay_category_enabled: boolean;
  overlay_title_text: string | null;
  overlay_category_text: string | null;
}

interface PresetOption {
  key: string;
  label: string;
}

interface PanelConfig {
  templates: TemplateOption[];
  llm_presets: PresetOption[];
  current: { llm_preset: string; template: string; min_playback_speed: number };
  defaults: { llm_preset: string; template: string; min_playback_speed: number };
}

interface Props {
  config: PanelConfig;
  onChange: (
    payload: ScriptPhaseSettingsRequest,
  ) => Promise<ScriptPhaseSettingsResponse>;
  disabled?: boolean;
}

export function ProjectSettingsPanel({ config, onChange, disabled }: Props) {
  const [llmPreset, setLlmPreset] = useState(config.current.llm_preset);
  const [template, setTemplate] = useState(config.current.template);
  const [speed, setSpeed] = useState(config.current.min_playback_speed);
  const [busy, setBusy] = useState(false);

  async function commit(payload: ScriptPhaseSettingsRequest) {
    setBusy(true);
    try {
      const result = await onChange(payload);
      setLlmPreset(result.llm_preset);
      setTemplate(result.template);
      setSpeed(result.min_playback_speed);
    } finally {
      setBusy(false);
    }
  }

  const isDefaultTemplate = template === config.defaults.template;
  const isDefaultLlm = llmPreset === config.defaults.llm_preset;
  const isDefaultSpeed =
    Math.abs(speed - config.defaults.min_playback_speed) < 1e-9;

  const inputDisabled = disabled || busy;

  return (
    <div className="project-settings-panel">
      <h3>Project settings</h3>

      <div className="project-settings-row">
        <label className="project-settings-field">
          <span>
            Template
            {isDefaultTemplate ? (
              <em className="project-settings-default-marker"> (default)</em>
            ) : null}
          </span>
          <select
            value={template}
            disabled={inputDisabled}
            onChange={(e) => commit({ template: e.target.value })}
          >
            {config.templates.map((t) => (
              <option key={t.key} value={t.key}>
                {t.label}
                {t.key === config.defaults.template ? " — default" : ""}
              </option>
            ))}
          </select>
        </label>

        <label className="project-settings-field">
          <span>
            LLM
            {isDefaultLlm ? (
              <em className="project-settings-default-marker"> (default)</em>
            ) : null}
          </span>
          <select
            value={llmPreset}
            disabled={inputDisabled}
            onChange={(e) => commit({ llm_preset: e.target.value })}
          >
            {config.llm_presets.map((p) => (
              <option key={p.key} value={p.key}>
                {p.label}
                {p.key === config.defaults.llm_preset ? " — default" : ""}
              </option>
            ))}
          </select>
        </label>

        <label className="project-settings-field">
          <span>
            Min playback speed: {speed.toFixed(2)}
            {isDefaultSpeed ? (
              <em className="project-settings-default-marker"> (default)</em>
            ) : null}
          </span>
          <div className="project-settings-slider-row">
            <input
              type="range"
              min={0.2}
              max={1.0}
              step={0.01}
              value={speed}
              disabled={inputDisabled}
              onChange={(e) => setSpeed(parseFloat(e.target.value))}
              onMouseUp={(e) =>
                commit({
                  min_playback_speed: parseFloat(
                    (e.target as HTMLInputElement).value,
                  ),
                })
              }
              onTouchEnd={(e) =>
                commit({
                  min_playback_speed: parseFloat(
                    (e.target as HTMLInputElement).value,
                  ),
                })
              }
            />
            <button
              type="button"
              className="project-settings-reset"
              disabled={inputDisabled || isDefaultSpeed}
              onClick={() =>
                commit({
                  min_playback_speed: config.defaults.min_playback_speed,
                })
              }
            >
              Reset
            </button>
          </div>
        </label>
      </div>
    </div>
  );
}
