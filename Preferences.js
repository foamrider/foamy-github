// Widget settings schema (mirrors manifest.json barWidget.schema). Reading and
// saving go through kit/FoamyPrefs.js; the list settings (repositoryFolders,
// trustedRepositories) are Model.listFields, validated by Model.js.
var fields = [
  { key: "inotifyEnabled", type: "boolean", defaultValue: true },
  { key: "refreshIntervalSec", type: "integer", min: 0, max: 3600, step: 5, defaultValue: 30 },
  { key: "fetchIntervalSec", type: "integer", min: 300, max: 86400, step: 300, defaultValue: 900 },
  // Legacy global "Trust repository config" switch: no longer shown; the panel
  // migrates it once to per-repository trust (trustedRepositories) and clears it.
  { key: "trustRepositoryConfig", type: "boolean", defaultValue: false }
]
if (typeof module !== "undefined") module.exports = { fields: fields }
