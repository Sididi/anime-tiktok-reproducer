import "@/pages/facecam.css";
import { useRef, useState } from 'react';
import { Video, Loader2 } from 'lucide-react';
import { Button } from '@/components/ui';
import { createFacecam } from '@/api/facecam';

export function FacecamCreateButton({projectId}: {projectId: string}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [busy, setBusy] = useState(false), [error, setError] = useState('');
  const [createdUrl, setCreatedUrl] = useState('');
  async function create() {
    const tab = window.open('', '_blank');
    if (tab) tab.opener = null;
    setBusy(true); setError('');
    try { const project = await createFacecam(projectId); const url = `/project/${project.id}/script`; if (tab) {tab.location.href = url; dialog.current?.close();} else setCreatedUrl(url); }
    catch (e) {tab?.close(); setError((e as Error).message);} finally {setBusy(false);}
  }
  return <><Button variant="outline" onClick={() => dialog.current?.showModal()}><Video className="mr-2 h-4 w-4"/>Facecam</Button>
    <dialog ref={dialog} className="facecam-dialog" onCancel={e => {if (busy) e.preventDefault();}}>
      <h2 className="text-xl font-semibold">Créer une version Facecam ?</h2>
      <p className="my-4 text-sm">Un nouveau projet indépendant sera ouvert dans un autre onglet, en français, avec le template Minimal et le modèle GPT. Vous pourrez écrire votre script puis importer votre enregistrement.</p>
      {error && <p role="alert" className="text-red-400 mb-4">{error}</p>}
      {createdUrl ? <a href={createdUrl} target="_blank" rel="noreferrer" className="underline">Ouvrir le projet Facecam</a> : <div className="flex justify-end gap-3"><Button variant="outline" disabled={busy} onClick={() => dialog.current?.close()}>Annuler</Button><Button disabled={busy} onClick={() => void create()}>{busy && <Loader2 className="mr-2 h-4 w-4 animate-spin"/>}Créer le projet</Button></div>}
    </dialog></>;
}
