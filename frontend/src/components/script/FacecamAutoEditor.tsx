import { RotateCcw, Scissors } from 'lucide-react';
import { Button } from '@/components/ui';
import { sameAutoEditorSettings, type FacecamAutoEditorSettings } from '@/api/facecam';

const controls: {key: keyof FacecamAutoEditorSettings; label: string; help: string; min: number; max: number; step: number; unit: string}[] = [
  {key: 'noise_threshold_db', label: 'Seuil de silence', help: 'Plus proche de 0 : coupe davantage de respirations et de bruit de fond.', min: -60, max: -15, step: 1, unit: 'dB'},
  {key: 'min_silence_ms', label: 'Pause minimale à couper', help: 'Plus court : supprime aussi les petites hésitations entre les mots.', min: 80, max: 1000, step: 10, unit: 'ms'},
  {key: 'margin_before_ms', label: 'Marge avant la parole', help: 'Temps conservé avant la reprise de la voix.', min: 0, max: 300, step: 10, unit: 'ms'},
  {key: 'margin_after_ms', label: 'Marge après la parole', help: 'Temps conservé après la fin de la voix.', min: 0, max: 500, step: 10, unit: 'ms'},
];

export function FacecamAutoEditor({value, defaults, disabled, pending, onChange}: {
  value: FacecamAutoEditorSettings; defaults: FacecamAutoEditorSettings;
  disabled: boolean; pending: boolean; onChange: (value: FacecamAutoEditorSettings) => void;
}) {
  return <div className="facecam-auto-editor">
    <div className="facecam-auto-editor-heading">
      <h3><Scissors size={17}/> Réglages AutoEditor</h3>
      <Button variant="ghost" size="sm" disabled={disabled || sameAutoEditorSettings(value, defaults)} onClick={() => onChange({...defaults})}>
        <RotateCcw size={14} className="mr-2"/>Rétablir le rythme rapide
      </Button>
    </div>
    <p>Resserrez les pauses pour un montage plus dynamique. Les mots détectés et leurs attaques restent protégés, même avec des marges à zéro.</p>
    <div className="facecam-auto-editor-controls">{controls.map(control => <label key={control.key}>
      <span>{control.label}<output>{value[control.key]} {control.unit}</output></span>
      <input type="range" aria-label={control.label} aria-describedby={`auto-editor-${control.key}`} min={control.min} max={control.max} step={control.step}
        disabled={disabled} value={value[control.key]} onChange={event => onChange({...value, [control.key]: Number(event.target.value)})}/>
      <small id={`auto-editor-${control.key}`}>{control.help}</small>
    </label>)}</div>
    <p role="status">{pending ? 'Réglages modifiés : préparez l’aperçu audio pour les enregistrer et recalculer les coupes.' : 'Ces réglages sont enregistrés avec le projet lors de la préparation de l’aperçu audio.'}</p>
  </div>;
}
