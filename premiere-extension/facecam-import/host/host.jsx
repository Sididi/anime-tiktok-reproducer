function facecamRun(scriptPath) {
  try {
    var file = new File(scriptPath);
    if (!file.exists || file.name.toLowerCase() !== "import_project.jsx") return "Script introuvable";
    app.enableQE();
    $.evalFile(file);
    return "ok";
  } catch (error) {
    return String(error) + " (ligne " + error.line + ")";
  }
}
