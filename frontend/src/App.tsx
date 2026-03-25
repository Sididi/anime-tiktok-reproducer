import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import {
  ProjectSetup,
  SceneValidation,
  MatchValidation,
  TranscriptionPage,
  ScriptRestructurePage,
  ProcessingPage,
  GapResolutionPage,
} from "@/pages";

function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<ProjectSetup />} />
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
          path="/project/:projectId/script"
          element={<ScriptRestructurePage />}
        />
        <Route
          path="/project/:projectId/processing"
          element={<ProcessingPage />}
        />
        <Route
          path="/project/:projectId/gaps"
          element={<GapResolutionPage />}
        />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  );
}

export default App;
