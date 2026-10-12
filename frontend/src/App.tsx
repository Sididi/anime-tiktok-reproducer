import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import {
  ProjectSetup,
  CleanupPage,
  SceneValidation,
  MatchValidation,
  TranscriptionPage,
  RawSceneValidationPage,
  ProcessingPage,
  GapResolutionPage,
  PlanningPage,
} from "@/pages";
import { ScriptPhasePage } from "@/pages/ScriptPhasePage";
import { FacecamPrompterPage } from "@/pages/FacecamPrompterPage";
import { DownloadProgressCard } from "@/components/DownloadProgressCard";
import { ProjectAccessGate } from "@/components/ProjectAccessGate";

function App() {
  return (
    <BrowserRouter>
      <DownloadProgressCard />
      <Routes>
        <Route path="/" element={<ProjectSetup />} />
        <Route path="/planning" element={<PlanningPage />} />
        <Route element={<ProjectAccessGate />}>
          <Route path="/project/:projectId/prompter" element={<FacecamPrompterPage />} />
          <Route
            path="/project/:projectId/cleanup"
            element={<CleanupPage />}
          />
          <Route
            path="/project/:projectId/scenes"
            element={<SceneValidation />}
          />
          <Route
            path="/project/:projectId/matches"
            element={<MatchValidation />}
          />
          <Route
            path="/project/:projectId/transcription"
            element={<TranscriptionPage />}
          />
          <Route
            path="/project/:projectId/raw-scenes"
            element={<RawSceneValidationPage />}
          />
          <Route
            path="/project/:projectId/script"
            element={<ScriptPhasePage />}
          />
          <Route
            path="/project/:projectId/processing"
            element={<ProcessingPage />}
          />
          <Route
            path="/project/:projectId/gaps"
            element={<GapResolutionPage />}
          />
          <Route path="/projects/:projectId/*" element={<Navigate to="/" replace />} />
          <Route path="/project/:projectId/*" element={<Navigate to="/" replace />} />
        </Route>
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  );
}

export default App;
