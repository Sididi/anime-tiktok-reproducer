import { Check, Loader2 } from 'lucide-react';
import { Button } from '@/components/ui';
import type { FacecamState } from '@/api/facecam';

export function FacecamStories({state, busy, onAnalyze, onSelect}: {
  state: FacecamState; busy: boolean; onAnalyze: () => void; onSelect: (id: string) => void;
}) {
  const stories = state.stories ?? [];
  return <section className="facecam-card" aria-labelledby="facecam-stories-title">
    <h2 id="facecam-stories-title">1. Choisir l’histoire à raconter</h2>
    <p>{!stories.length
      ? 'Lancez l’analyse de la source pour repérer les histoires possibles et ce qui mérite votre minute.'
      : stories.length === 1
        ? state.selected_story_id ? 'Une seule histoire détectée : elle est retenue automatiquement.' : 'Une seule histoire détectée. Vous pouvez repartir de ce récit.'
        : `${stories.length} histoires détectées. Choisissez celle que vous souhaitez développer.`}</p>
    {!stories.length && <Button variant="outline" disabled={busy} onClick={onAnalyze}>
      {busy && <Loader2 size={16} className="animate-spin mr-2"/>}
      {busy ? 'Analyse en cours…' : state.job?.status === 'error' ? 'Réessayer l’analyse' : 'Analyser les histoires'}
    </Button>}
    {!!stories.length && state.job?.status === 'error' && <Button variant="outline" disabled={busy} onClick={onAnalyze}>Actualiser l’analyse</Button>}
    <div className="facecam-stories">{stories.map(story => {
      const selected = state.selected_story_id === story.id;
      return <article key={story.id} className={selected ? 'selected' : ''} aria-label={story.title}>
        <h3>{selected && <Check size={16} aria-label="Histoire retenue"/>}{story.title}</h3>
        <p>{story.premise}</p>
        <ol>{story.steps.map((step, index) => <li key={index}>{step}</li>)}</ol>
        <p><strong>Point d’arrivée : </strong>{story.ending}</p>
        {!!story.omitted.length && <details><summary>Ce récit laisse de côté</summary><ul>{story.omitted.map((item, index) => <li key={index}>{item}</li>)}</ul></details>}
        <Button variant={selected ? 'outline' : 'default'} disabled={busy || selected} onClick={() => onSelect(story.id)}>
          {selected ? 'Histoire retenue' : state.blocks.length ? 'Repartir de cette histoire' : 'Choisir cette histoire'}
        </Button>
      </article>;
    })}</div>
    {!!stories.length && !!state.blocks.length && <p>Repartir d’une autre histoire remplace le brouillon, l’accroche et le point de vue. L’enregistrement devra être vérifié à nouveau.</p>}
  </section>;
}
