import { useEffect } from 'react';
import { useParams } from 'react-router-dom';
import { useProjectStore } from '@/stores';
import { ScriptRestructurePage } from './ScriptRestructurePage';
import { FacecamScriptPage } from './FacecamScriptPage';

export function ScriptPhasePage() {
  const {projectId} = useParams(); const {project, loadProject, error} = useProjectStore();
  useEffect(() => {if (projectId && project?.id !== projectId) void loadProject(projectId);}, [projectId, project?.id, loadProject]);
  if (!project || project.id !== projectId) return <p className="p-8">{error || 'Chargement du projet…'}</p>;
  return project.project_mode === 'facecam' ? <FacecamScriptPage key={project.id} project={project}/> : <ScriptRestructurePage/>;
}
