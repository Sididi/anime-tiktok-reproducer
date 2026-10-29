import { AlertTriangle, Check, Clock3, Loader2, Sparkles } from 'lucide-react';
import { Button } from '@/components/ui';
import { FULL_STORY_ID, ORIGINAL_STORY_ID, type FacecamState } from '@/api/facecam';

function durationLabel(seconds: number) {
  const rounded = Math.ceil(seconds);
  return rounded < 60 ? `${rounded} s` : `${Math.floor(rounded / 60)} min ${String(rounded % 60).padStart(2, '0')} s`;
}

export function FacecamStories({state, busy, onAnalyze, onSelect}: {
  state: FacecamState; busy: boolean; onAnalyze: () => void; onSelect: (id: string) => void;
}) {
  const stories = state.stories ?? [];
  const fullSelected = state.selected_story_id === FULL_STORY_ID;
  const originalSelected = state.generation_mode === 'original';
  const fullDuration = state.full_story?.estimated_duration_seconds;
  const tooLong = fullDuration != null && fullDuration > 70;
  return <section className="facecam-card" aria-labelledby="facecam-stories-title">
    <h2 id="facecam-stories-title">1. Choisir l’histoire à raconter</h2>
    <p>{originalSelected ? 'Vous partez directement de l’anime. Choisissez maintenant votre accroche et votre point de vue.' : !stories.length
      ? 'Présentez l’anime de zéro, analysez la vidéo source pour choisir une histoire, ou gardez son récit intégral.'
      : stories.length === 1
        ? state.selected_story_id === stories[0].id ? 'Une seule histoire détectée : elle est retenue automatiquement.' : 'Une seule histoire détectée. Vous pouvez choisir ce récit ou garder toute l’histoire.'
        : `${stories.length} histoires détectées. Choisissez celle que vous souhaitez développer.`}</p>
    {!stories.length && <Button variant="outline" disabled={busy} onClick={onAnalyze}>
      {busy && <Loader2 size={16} className="animate-spin mr-2"/>}
      {busy ? 'Analyse en cours…' : state.job?.status === 'error' ? 'Réessayer l’analyse' : 'Analyser les histoires'}
    </Button>}
    {!!stories.length && state.job?.status === 'error' && <Button variant="outline" disabled={busy} onClick={onAnalyze}>Actualiser l’analyse</Button>}
    <div className="facecam-stories">
      <article className={originalSelected ? 'selected' : ''} aria-label="Présentation originale">
        <span className="facecam-story-label">Depuis l’anime · sans histoire source</span>
        <h3><Sparkles size={16} aria-hidden="true"/> Présentation originale</h3>
        <p className="facecam-story-duration"><Clock3 size={16} aria-hidden="true"/>Cible : 60–75 s</p>
        <p>Créez directement un récit depuis la prémisse de l’anime, sans gros spoilers ni dialogues originaux imposés.</p>
        <p>Aucune analyse des histoires ni génération préalable de script nécessaire. Vous choisissez ensuite votre accroche et votre point de vue.</p>
        <Button variant={originalSelected ? 'outline' : 'default'} disabled={busy || originalSelected} onClick={() => onSelect(ORIGINAL_STORY_ID)}>
          {originalSelected ? 'Présentation originale retenue' : 'Choisir la présentation originale'}
        </Button>
      </article>
      {stories.map(story => {
      const selected = state.selected_story_id === story.id;
      return <article key={story.id} className={selected ? 'selected' : ''} aria-label={story.title}>
        <h3>{selected && <Check size={16} aria-label="Histoire retenue"/>}{story.title}</h3>
        <p className="facecam-story-duration"><Clock3 size={16} aria-hidden="true"/>Cible : 65–70 s</p>
        <p>{story.premise}</p>
        <ol>{story.steps.map((step, index) => <li key={index}>{step}</li>)}</ol>
        <p><strong>Point d’arrivée : </strong>{story.ending}</p>
        {!!story.omitted.length && <details><summary>Ce récit laisse de côté</summary><ul>{story.omitted.map((item, index) => <li key={index}>{item}</li>)}</ul></details>}
        <Button variant={selected ? 'outline' : 'default'} disabled={busy || selected} onClick={() => onSelect(story.id)}>
          {selected ? 'Histoire retenue' : state.blocks.length ? 'Repartir de cette histoire' : 'Choisir cette histoire'}
        </Button>
      </article>;
    })}
      <article className={`facecam-full-story ${fullSelected ? 'selected' : ''}`} aria-label="Garder toute l’histoire">
        <span className="facecam-story-label">Option intégrale · facultative</span>
        <h3>{fullSelected && <Check size={16} aria-label="Histoire retenue"/>}Garder toute l’histoire</h3>
        <p>Conservez toutes les étapes du récit et les extraits audio originaux, sans limiter le script à une minute.</p>
        <p className={`facecam-story-duration ${tooLong ? 'over-target' : ''}`}>
          <Clock3 size={18} aria-hidden="true"/>
          <strong>{fullDuration != null ? `Durée estimée : ${durationLabel(fullDuration)}` : 'Durée estimée indisponible'}</strong>
        </p>
        <p className="facecam-story-estimate">Estimation à partir du texte source, des extraits originaux et de la carte finale. La durée finale dépendra de la rédaction, de l’accroche, du point de vue et de votre débit.</p>
        {tooLong && <div className="facecam-story-warning" role="note">
          <AlertTriangle size={18} aria-hidden="true"/>
          <p><strong>Ce récit dépasse la cible de 65–70 s.</strong> La vidéo risque d’être trop longue pour garder l’attention. Privilégiez une histoire plus courte parmi les propositions{!stories.length && ' après analyse'}.</p>
        </div>}
        <Button variant="outline" className="h-auto min-h-10" disabled={busy || fullSelected} onClick={() => onSelect(FULL_STORY_ID)}>
          {fullSelected ? 'Histoire intégrale retenue' : tooLong ? 'Garder toute l’histoire quand même' : 'Garder toute l’histoire'}
        </Button>
      </article>
    </div>
    {!!state.blocks.length && <p>Repartir d’une autre histoire remplace le brouillon, l’accroche et le point de vue. L’enregistrement devra être vérifié à nouveau.</p>}
  </section>;
}
