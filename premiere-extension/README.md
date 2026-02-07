# JSX Runner - CEP Extension for Premiere Pro 2025

A lightweight CEP panel that lets you run `.jsx` automation scripts in Premiere Pro 2025 by double-clicking a `.bat` file from Windows Explorer, or by browsing from within Premiere Pro.

## How It Works

```
Double-click run_in_premiere.bat (from extracted project .zip)
  -> .bat writes the .jsx path to a trigger file in a "hot folder"
  -> CEP panel's Node.js watcher detects the trigger file
  -> Panel calls $.evalFile() via csInterface.evalScript()
  -> ExtendScript executes in PPro's engine (full QE DOM access)
  -> Panel shows success/error status
```

## Prerequisites

- Adobe Premiere Pro 2025 (v25.x)
- Windows 10/11

## Installation (one-time)

1. Double-click `install_extension.bat`
2. Restart Premiere Pro 2025
3. Verify: `Window > Extensions > JSX Runner` panel appears with a green status dot

The installer:
- Copies the extension to `%APPDATA%\Adobe\CEP\extensions\jsx-runner\`
- Sets the `PlayerDebugMode` registry key to allow unsigned extensions
- Creates the hot folder at `%APPDATA%\Adobe\JSXRunner\inbox\`

## Usage

### From Windows Explorer (primary)

1. Extract a project `.zip` to a folder
2. Make sure Premiere Pro is running with the JSX Runner panel loaded
3. Double-click `run_in_premiere.bat`
4. Check the JSX Runner panel for execution status

### From within Premiere Pro (fallback)

1. Open the JSX Runner panel (`Window > Extensions > JSX Runner`)
2. Click **Browse & Run**
3. Select the `import_project.jsx` file

### Recent Scripts

The panel remembers the last 8 scripts you ran. Click any entry in the "Recent Scripts" list to re-run it.

## Troubleshooting

**Panel doesn't appear in Window > Extensions**
- Make sure you ran `install_extension.bat` and restarted Premiere Pro
- Check that `%APPDATA%\Adobe\CEP\extensions\jsx-runner\CSXS\manifest.xml` exists
- Verify the registry key: `HKCU\Software\Adobe\CSXS.12\PlayerDebugMode` = `"1"`

**Status dot is grey (not green)**
- The hot folder may not have been created. Run `install_extension.bat` again.

**Script not executing after double-clicking .bat**
- Premiere Pro must be running with the JSX Runner panel open
- If you closed the panel, reopen it from `Window > Extensions > JSX Runner`
- Check the panel's log for error messages

**"EvalScript error" in the log**
- The .jsx script has a syntax or runtime error
- Open the script in a text editor and check for issues
- Paths with special characters may need escaping

## Technical Notes

- CEP (Common Extension Platform) is supported in Premiere Pro 2025 through at least September 2026
- The panel uses Node.js (enabled via CEP manifest) for file system watching
- `AutoVisible=true` ensures the panel loads automatically when Premiere Pro starts
- If Adobe deprecates CEP, a UXP migration would be needed

## Uninstalling

1. Delete `%APPDATA%\Adobe\CEP\extensions\jsx-runner\`
2. Optionally delete `%APPDATA%\Adobe\JSXRunner\`
3. Optionally remove the registry key: `HKCU\Software\Adobe\CSXS.12\PlayerDebugMode`
