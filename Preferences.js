// Widget settings schema (mirrors manifest.json barWidget.schema). Saving goes
// through kit/FoamyPrefs.js; repositoryFolders is a list, validated by
// Model.parseFolderRows and saved by Panel.saveFolders().
var fields = [
  { key: "inotifyEnabled", type: "boolean", defaultValue: true },
  { key: "refreshIntervalSec", type: "integer", min: 0, max: 3600, step: 5, defaultValue: 30 },
  { key: "fetchIntervalSec", type: "integer", min: 300, max: 86400, step: 300, defaultValue: 900 },
  // Legacy global "Trust repository config" switch: no longer shown; the panel
  // migrates it once to per-repository trust (trustedRepositories) and clears it.
  { key: "trustRepositoryConfig", type: "boolean", defaultValue: false }
]
if (typeof module !== "undefined") module.exports = { fields: fields }
