import { useEffect, useRef, useState, type CSSProperties } from 'react';
import { useParams } from 'react-router-dom';
import { Maximize, Pause, Play, RotateCcw } from 'lucide-react';
import { facecamRequest } from '@/api/facecam';
import {
  initialPrompterStatus, normalizePrompterSettings, prompterChannel, readPrompterSettings, savePrompterSettings,
  type PrompterCommand, type PrompterDocument, type PrompterMessage, type PrompterSettings,
} from '@/utils/facecamPrompter';
import './prompter.css';

function PrompterReader({ projectId }: { projectId: string }) {
  const [script, setScript] = useState<PrompterDocument | null>(null);
  const [settings, setSettings] = useState(readPrompterSettings);
  const [status, setStatus] = useState(initialPrompterStatus);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('Prêt à lire');
  const viewport = useRef<HTMLDivElement>(null);
  const command = useRef<(value: PrompterCommand) => void>(() => {});
  const changeSettings = useRef<(patch: Partial<PrompterSettings>) => void>(() => {});

  async function fullscreen() {
    try {
      if (document.fullscreenElement) await document.exitFullscreen();
      else await document.documentElement.requestFullscreen();
    } catch { setNotice('Utilisez F11 pour passer le navigateur en plein écran.'); }
  }

  useEffect(() => {
    const bus = new BroadcastChannel(prompterChannel(projectId));
    let disposed = false, current: PrompterDocument | null = null;
    let preferences = readPrompterSettings();
    let playing = false, finished = false, position = 0, frame = 0, lastFrame = 0, lastReport = 0;
    const previousTitle = document.title;
    document.title = 'Prompteur Facecam';

    function blocks() { return Array.from(viewport.current?.querySelectorAll<HTMLElement>('[data-prompter-block]') ?? []); }
    function currentBlock() {
      const line = position + (viewport.current?.clientHeight ?? 0) * .38 + 2;
      const elements = blocks();
      return (elements.filter(el => el.offsetTop <= line).at(-1) ?? elements[0])?.dataset.prompterBlock ?? null;
    }
    function report() {
      const maximum = Math.max(0, (viewport.current?.scrollHeight ?? 0) - (viewport.current?.clientHeight ?? 0));
      const next = { playing, finished, progress: maximum ? Math.min(1, position / maximum) : 0, blockId: currentBlock() };
      if (!disposed) setStatus(next);
      bus.postMessage({ type: 'status', status: next, settings: preferences } satisfies PrompterMessage);
    }
    function pause() {
      playing = false; lastFrame = 0; cancelAnimationFrame(frame); report();
    }
    function move(to: number) {
      const el = viewport.current;
      position = Math.max(0, Math.min(to, el ? el.scrollHeight - el.clientHeight : 0));
      finished = false;
      if (el) el.scrollTop = position;
      report();
    }
    function seek(blockId: string) {
      const target = blocks().find(el => el.dataset.prompterBlock === blockId);
      if (!target) return;
      pause(); move(target.offsetTop - (viewport.current?.clientHeight ?? 0) * .38);
      setNotice('Prêt à reprendre à ce passage');
    }
    function tick(now: number) {
      if (!playing || disposed) return;
      const el = viewport.current;
      if (!el) { pause(); return; }
      const maximum = Math.max(0, el.scrollHeight - el.clientHeight);
      if (lastFrame) position = Math.min(maximum, position + Math.min((now - lastFrame) / 1000, .1) * preferences.speed);
      lastFrame = now;
      el.scrollTop = position;
      if (position >= maximum) {
        finished = true; pause(); setNotice('Fin de la narration'); return;
      }
      if (now - lastReport > 250) { report(); lastReport = now; }
      frame = requestAnimationFrame(tick);
    }
    function act(value: PrompterCommand) {
      if (value.action === 'restart') { pause(); move(0); setNotice('Prêt à lire'); }
      else if (value.action === 'seek') seek(value.blockId);
      else if (playing) { pause(); setNotice('En pause'); }
      else if (current?.blocks.some(block => block.kind !== 'raw' && block.text.trim())) {
        if (finished) move(0);
        else position = viewport.current?.scrollTop ?? position;
        playing = true; lastFrame = 0; setNotice('Lecture en cours'); report(); frame = requestAnimationFrame(tick);
      }
    }
    function updateSettings(patch: Partial<PrompterSettings>) {
      const next = normalizePrompterSettings({ ...preferences, ...patch });
      const reflow = next.fontSize !== preferences.fontSize;
      const anchor = currentBlock();
      if (reflow) pause();
      preferences = next; setSettings(next); savePrompterSettings(next); report();
      // Wait for React to apply the new font before finding this passage again.
      if (reflow && anchor) requestAnimationFrame(() => { if (!disposed) seek(anchor); });
    }
    function receive(next: PrompterDocument) {
      if (next.projectId !== projectId || (current && next.revision < current.revision)) return;
      const text = (value: PrompterDocument | null) => JSON.stringify(value?.blocks.map(b => [b.id, b.kind, b.text]));
      const changed = text(current) !== text(next);
      if (!changed && current?.revision === next.revision && current?.title === next.title) return;
      if (changed) {
        pause(); move(0);
        setNotice(current ? 'Texte actualisé · lecture remise au début' : 'Prêt à lire');
      }
      current = next; setScript(next); setError('');
      document.title = `Prompteur Facecam — ${next.title}`;
      requestAnimationFrame(() => { if (!disposed) report(); });
    }
    command.current = act;
    changeSettings.current = updateSettings;
    bus.onmessage = ({ data }: MessageEvent<PrompterMessage>) => {
      if (data.type === 'document') receive(data.document);
      else if (data.type === 'settings') updateSettings(data.settings);
      else if (data.type === 'command') act(data.command);
      else if (data.type === 'ping') { bus.postMessage({ type: 'ready' } satisfies PrompterMessage); report(); }
    };
    bus.postMessage({ type: 'ready' } satisfies PrompterMessage);
    const heartbeat = setInterval(report, 1000);

    void Promise.all([
      facecamRequest(projectId),
      fetch(`/api/projects/${encodeURIComponent(projectId)}`).then(r => r.ok ? r.json() : null),
    ]).then(([state, project]) => {
      if (!disposed) receive({ projectId, title: project?.anime_name || 'Facecam', revision: state.revision, blocks: state.blocks });
    }).catch(e => { if (!disposed && !current) setError((e as Error).message); });

    function key(event: KeyboardEvent) {
      if (event.ctrlKey || event.metaKey || event.altKey || event.target instanceof HTMLInputElement || event.target instanceof HTMLSelectElement) return;
      if (event.code === 'Space') { event.preventDefault(); act({ action: 'toggle' }); }
      else if (event.code === 'Home') { event.preventDefault(); act({ action: 'restart' }); }
      else if (event.code === 'ArrowUp' || event.code === 'ArrowDown') {
        event.preventDefault(); updateSettings({ speed: preferences.speed + (event.code === 'ArrowUp' ? 5 : -5) });
      } else if (event.code === 'KeyF') { event.preventDefault(); void fullscreen(); }
      else if (event.code === 'PageDown' || event.code === 'PageUp') {
        event.preventDefault();
        const elements = blocks(), at = elements.findIndex(el => el.dataset.prompterBlock === currentBlock());
        const target = elements[Math.max(0, Math.min(elements.length - 1, at + (event.code === 'PageDown' ? 1 : -1)))];
        if (target?.dataset.prompterBlock) seek(target.dataset.prompterBlock);
      }
    }
    function visibility() { if (document.hidden) { pause(); setNotice('En pause · fenêtre masquée'); } }
    function wheel() { pause(); setNotice('Position ajustée manuellement'); }
    function scroll() {
      if (!playing && viewport.current && Math.abs(viewport.current.scrollTop - position) > 1) {
        position = viewport.current.scrollTop; finished = false; report();
      }
    }
    function close() { bus.postMessage({ type: 'closed' } satisfies PrompterMessage); }
    const el = viewport.current;
    el?.addEventListener('wheel', wheel, { passive: true });
    el?.addEventListener('scroll', scroll, { passive: true });
    window.addEventListener('keydown', key);
    window.addEventListener('pagehide', close);
    document.addEventListener('visibilitychange', visibility);
    return () => {
      disposed = true; cancelAnimationFrame(frame); clearInterval(heartbeat); close(); bus.close();
      el?.removeEventListener('wheel', wheel); el?.removeEventListener('scroll', scroll);
      window.removeEventListener('keydown', key); window.removeEventListener('pagehide', close);
      document.removeEventListener('visibilitychange', visibility);
      document.title = previousTitle;
    };
  }, [projectId]);

  const readable = script?.blocks.some(block => block.kind !== 'raw' && block.text.trim());
  return <main className={`prompter-display ${status.playing ? 'is-playing' : ''}`} style={{ '--prompter-font-size': `${settings.fontSize}px` } as CSSProperties}>
    <div ref={viewport} className="prompter-viewport" aria-label="Texte du prompteur" tabIndex={0}>
      <div className={`prompter-script ${settings.mirror ? 'is-mirrored' : ''}`}>
        {script?.blocks.map(block => block.kind === 'raw'
          ? <div key={block.id} data-prompter-block={block.id} className="prompter-raw">Extrait original · ne pas lire</div>
          : block.text.trim() && <p key={block.id} data-prompter-block={block.id}>{block.text}</p>)}
        {readable && <div className="prompter-end">Fin de la narration</div>}
      </div>
    </div>
    <div className="prompter-guide" aria-hidden="true">›</div>
    <header className="prompter-toolbar" aria-label="Commandes de lecture">
      <button disabled={!readable} onClick={() => command.current({ action: 'toggle' })} aria-label={status.playing ? 'Pause' : status.finished ? 'Relire' : 'Lire'}>{status.playing ? <Pause/> : <Play/>}</button>
      <button disabled={!readable} onClick={() => command.current({ action: 'restart' })} aria-label="Recommencer"><RotateCcw/></button>
      <span>{settings.speed} px/s</span>
      <button onClick={() => changeSettings.current({ fontSize: settings.fontSize - 2 })} aria-label="Réduire le texte">A−</button>
      <button onClick={() => changeSettings.current({ fontSize: settings.fontSize + 2 })} aria-label="Agrandir le texte">A+</button>
      <button onClick={() => void fullscreen()} aria-label="Plein écran"><Maximize/></button>
      <span className="prompter-notice" role="status">{notice}</span>
    </header>
    {(!script || !readable) && <div className="prompter-empty" role={error ? 'alert' : 'status'}>
      {error || (script ? 'Aucun texte à lire. Préparez votre script Facecam.' : 'Chargement du texte…')}
      {error && <button onClick={() => window.location.reload()}>Réessayer</button>}
    </div>}
    <footer className="prompter-footer"><span>Espace : pause · ↑ / ↓ : vitesse · Pg. suiv. / préc. : passage · F : plein écran</span>
      <progress aria-label="Progression de la lecture" max="1" value={status.progress}/>
    </footer>
  </main>;
}

export function FacecamPrompterPage() {
  const { projectId } = useParams();
  return projectId ? <PrompterReader key={projectId} projectId={projectId}/> : null;
}
