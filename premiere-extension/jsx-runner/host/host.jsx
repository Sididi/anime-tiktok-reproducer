/**
 * JSX Runner - ExtendScript Host
 *
 * Runs in Premiere Pro's ExtendScript engine.
 * Called from the CEP panel via csInterface.evalScript().
 */

/**
 * Execute a .jsx script file with error handling.
 * @param {string} scriptPath - Absolute path to the .jsx file (forward slashes)
 * @returns {string} "OK" on success, "ERROR: ..." on failure
 */
function runScript(scriptPath) {
    try {
        var file = new File(scriptPath);
        if (!file.exists) {
            return "ERROR: File not found: " + scriptPath;
        }
        $.evalFile(file);
        return "OK";
    } catch (e) {
        return "ERROR: " + e.message + " (line " + e.line + ")";
    }
}
