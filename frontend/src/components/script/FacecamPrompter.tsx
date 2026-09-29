import { useEffect, useRef, useState } from 'react';
import { Monitor, Pause, Play, RotateCcw } from 'lucide-react';
import { Button } from '@/components/ui';
import type { FacecamBlock } from '@/api/facecam';
import {
  initialPrompterStatus, normalizePrompterSettings, prompterBlockLabel, prompterChannel,
  readPrompterSettings, savePrompterSettings,
  type PrompterCommand, type PrompterMessage, type PrompterSettings,
} from '@/utils/facecamPrompter';

interface Props {
  projectId: string; title: string; revision: number; blocks: FacecamBlock[];
  saved: boolean; busy: boolean; prepare: () => Promise<void>;
}

export function FacecamPrompter({ projectId, title, revision, blocks, saved, busy, prepare }: Props) {
  const [settings, setSettings] = useState(readPrompterSettings);
  const [status, setStatus] = useState(initialPrompterStatus);
  const [connected, setConnected] = useState(false);
  const [opening, setOpening] = useState(false);
  const [error, setError] = useState('');
  const channel = useRef<BroadcastChannel | null>(null);
  const popup = useRef<Window | null>(null);
  const lastSeen = useRef(0);
  const latest = useRef({ projectId, title, revision, blocks, saved });

  useEffect(() => {
    latest.current = { projectId, title, revision, blocks, saved };
    if (saved) channel.current?.postMessage({ type: 'document', document: latest.current } satisfies PrompterMessage);
  }, [projectId, title, revision, blocks, saved]);

  useEffect(() => {
    const bus = new BroadcastChannel(prompterChannel(projectId));
    channel.current = bus;
    bus.onmessage = ({ data }: MessageEvent<PrompterMessage>) => {
      if (data.type === 'ready') {
        if (latest.current.saved) bus.postMessage({ type: 'document', document: latest.current } satisfies PrompterMessage);
      } else if (data.type === 'status') {
        lastSeen.current = Date.now();
        setConnected(true); setStatus(data.status); setSettings(data.settings);
      } else if (data.type === 'closed') {
        setConnected(false);
      }
    };
    bus.postMessage({ type: 'ping' } satisfies PrompterMessage);
    const timer = setInterval(() => {
      if (Date.now() - lastSeen.current > 4000 || popup.current?.closed) setConnected(false);
      bus.postMessage({ type: 'ping' } satisfies PrompterMessage);
    }, 1500);
    return () => { clearInterval(timer); bus.close(); channel.current = null; };
  }, [projectId]);

  function command(value: PrompterCommand) {
    channel.current?.postMessage({ type: 'command', command: value } satisfies PrompterMessage);
  }

  function changeSettings(patch: Partial<PrompterSettings>) {
    const next = normalizePrompterSettings({ ...settings, ...patch });
    setSettings(next); savePrompterSettings(next);
    channel.current?.postMessage({ type: 'settings', settings: next } satisfies PrompterMessage);
  }

  async function open() {
    // Reserve the window during the click, before awaiting the draft save.
    const target = window.open('', `facecam-prompter-${projectId}`, 'popup=yes,width=1024,height=600');
    if (!target) { setError('Autorisez les fenêtres contextuelles pour ouvrir le prompteur.'); return; }
    popup.current = target;
    const blank = target.location.href === 'about:blank';
    if (blank) {
      target.document.title = 'Prompteur Facecam — Préparation';
      target.document.body.style.cssText = 'background:#080808;color:white;font:24px sans-serif;padding:40px';
      target.document.body.textContent = 'Préparation du texte…';
    }
    setOpening(true); setError('');
    try {
      await prepare();
      if (target.closed) return;
      const path = `/project/${encodeURIComponent(projectId)}/prompter`;
      if (target.location.pathname !== path) target.location.replace(path);
      else channel.current?.postMessage({ type: 'ping' } satisfies PrompterMessage);
      target.focus();
    } catch (e) {
      if (blank) target.close();
      setError(`Le texte n’a pas pu être préparé : ${(e as Error).message}`);
    } finally { setOpening(false); }
  }

  return <section className="facecam-prompter" aria-label="Prompteur">
    <div className="facecam-prompter-heading">
      <div><h3><Monitor size={18}/> Lire au prompteur</h3><p>Gardez ces commandes sur votre écran et le texte devant la caméra.</p></div>
      <Button variant="outline" disabled={busy || opening} onClick={() => void open()}>
        <Monitor size={16} className="mr-2"/>{opening ? 'Préparation…' : connected ? 'Afficher le prompteur' : 'Ouvrir le prompteur'}
      </Button>
    </div>
    {error && <p role="alert" className="facecam-error">{error}</p>}
    <div className="facecam-prompter-controls">
      <Button disabled={!connected || !saved || busy} onClick={() => command({ action: 'toggle' })}>
        {status.playing ? <Pause size={16} className="mr-2"/> : <Play size={16} className="mr-2"/>}{status.playing ? 'Pause' : status.finished ? 'Relire' : 'Lire'}
      </Button>
      <Button variant="outline" disabled={!connected} onClick={() => command({ action: 'restart' })}><RotateCcw size={16} className="mr-2"/>Recommencer</Button>
      <span role="status">{connected ? `${status.finished ? 'Fin du texte' : status.playing ? 'Lecture en cours' : 'En pause'} · ${Math.round(status.progress * 100)} %` : 'Fenêtre de lecture fermée'}</span>
    </div>
    <div className="facecam-prompter-settings">
      <label>Vitesse · {settings.speed} px/s<input aria-label="Vitesse de lecture" type="range" min="5" max="120" step="1" value={settings.speed} onChange={e => changeSettings({ speed: Number(e.target.value) })}/></label>
      <label>Taille du texte · {settings.fontSize} px<input aria-label="Taille du texte" type="range" min="28" max="80" step="1" value={settings.fontSize} onChange={e => changeSettings({ fontSize: Number(e.target.value) })}/></label>
      <label>Reprendre à un passage<select aria-label="Passage à lire" disabled={!connected} value={status.blockId ?? ''} onChange={e => command({ action: 'seek', blockId: e.target.value })}>
        <option value="" disabled>Choisir un passage</option>
        {blocks.map((block, index) => <option key={block.id} value={block.id}>{prompterBlockLabel(block, index)}</option>)}
      </select></label>
      <label className="facecam-prompter-mirror"><input type="checkbox" checked={settings.mirror} onChange={e => changeSettings({ mirror: e.target.checked })}/>Effet miroir horizontal</label>
    </div>
    <p className="facecam-prompter-hint">Dans la fenêtre de lecture : Espace pour la pause, ↑ / ↓ pour la vitesse, F pour le plein écran. Une modification enregistrée remet la lecture au début.</p>
  </section>;
}
