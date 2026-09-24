function defaultStatus() {
  return {
    ok: true,
    repoCount: 0,
    affectedCount: 0,
    totals: {
      aheadRepos: 0,
      behindRepos: 0,
      dirtyRepos: 0,
      failedRepos: 0,
      aheadCommits: 0,
      behindCommits: 0,
      dirtyFiles: 0
    },
    repos: [],
    sync: {
      syncing: false,
      stale: true,
      lastAttempt: 0,
      lastSuccess: 0,
      mode: "",
      failures: [],
      updated: [],
      skipped: []
    }
  }
}

function parseStatus(raw) {
  var text = String(raw || "").trim()
  if (text === "") return failedStatus("No repository status received")
  try {
    var parsed = JSON.parse(text)
    if (!parsed || typeof parsed !== "object") return failedStatus("Invalid repository status")
    var fallback = defaultStatus()
    parsed.totals = Object.assign({}, fallback.totals, parsed.totals || {})
    parsed.repos = Array.isArray(parsed.repos) ? parsed.repos : []
    parsed.sync = Object.assign({}, fallback.sync, parsed.sync || {})
    parsed.sync.failures = Array.isArray(parsed.sync.failures) ? parsed.sync.failures : []
    parsed.sync.updated = Array.isArray(parsed.sync.updated) ? parsed.sync.updated : []
    parsed.sync.skipped = Array.isArray(parsed.sync.skipped) ? parsed.sync.skipped : []
    return parsed
  } catch (e) {
    return failedStatus("Failed to parse repository status")
  }
}

function failedStatus(message) {
  var status = defaultStatus()
  status.ok = false
  status.error = message
  return status
}

function relativeTime(timestampSec, nowMs) {
  var timestamp = Number(timestampSec || 0)
  if (!isFinite(timestamp) || timestamp <= 0) return "Never"
  var now = nowMs === undefined ? Date.now() : Number(nowMs)
  var seconds = Math.max(0, Math.floor((now - timestamp * 1000) / 1000))
  if (seconds < 45) return "Just now"
  var minutes = Math.floor(seconds / 60)
  if (minutes < 60) return minutes + "m ago"
  var hours = Math.floor(minutes / 60)
  if (hours < 24) return hours + "h ago"
  var days = Math.floor(hours / 24)
  return days + "d ago"
}

function repositoryMeta(repo) {
  if (!repo) return ""
  var parts = []
  var branch = String(repo.branch || "detached")
  parts.push(branch)
  if (repo.ahead > 0) parts.push("↑" + repo.ahead + " commit" + (repo.ahead === 1 ? "" : "s"))
  if (repo.behind > 0) parts.push("↓" + repo.behind + " commit" + (repo.behind === 1 ? "" : "s"))
  if (repo.dirtyCount > 0) parts.push(repo.dirtyCount + " changed")
  return parts.join(" · ")
}

function repositoryState(repo) {
  if (!repo) return "Clean"
  var states = []
  if (repo.ahead > 0) states.push("ahead")
  if (repo.behind > 0) states.push("behind")
  if (repo.dirtyCount > 0) states.push("dirty")
  return states.length > 0 ? states.join(" + ") : "clean"
}

function barText(status, vertical) {
  if (status.sync.syncing) return "󰑐"
  if (vertical) return "󰊤"
  var parts = ["󰊤"]
  if (status.totals.aheadRepos > 0) parts.push(status.totals.aheadRepos + "↑")
  if (status.totals.behindRepos > 0) parts.push(status.totals.behindRepos + "↓")
  if (status.totals.dirtyRepos > 0) parts.push(status.totals.dirtyRepos + "*")
  if (status.totals.failedRepos > 0) parts.push(status.totals.failedRepos + "!")
  if (status.sync.stale && status.totals.failedRepos === 0) parts.push("?")
  return parts.join(" ")
}

function actionSummary(status) {
  var sync = status && status.sync ? status.sync : defaultStatus().sync
  var parts = []
  if (sync.updated.length > 0) parts.push("Updated " + sync.updated.length)
  if (sync.skipped.length > 0) parts.push("Skipped " + sync.skipped.length)
  if (sync.failures.length > 0) parts.push("Failed " + sync.failures.length)
  return parts.join(" · ")
}

function repositoryFolders(settings) {
  var value = settings ? settings.repositoryFolders : undefined
  // Omarchy can provide a QML sequence wrapper rather than a JavaScript array.
  return value === undefined ? [] : JSON.parse(JSON.stringify(value))
}

function parseFolderRows(rows) {
  if (!Array.isArray(rows) || rows.length > 32) return { error: "Use at most 32 repository folders." }
  var folders = []
  for (var i = 0; i < rows.length; i++) {
    // Existing string settings retain their original two-level scan.
    var row = typeof rows[i] === "string" ? {path: rows[i], depth: 2} : rows[i]
    if (!row || typeof row.path !== "string" || !/^(\/|~(?:\/|$))/.test(row.path.trim()))
      return { error: "Use an absolute path or ~/ for every folder." }
    if (!Number.isInteger(row.depth) || row.depth < 0 || row.depth > 5)
      return { error: "Choose a scan depth from 0 to 5 levels." }
    var path = row.path.trim()
    var existing = folders.find(function(folder) { return folder.path === path })
    if (existing) existing.depth = Math.max(existing.depth, row.depth)
    else folders.push({path: path, depth: row.depth})
  }
  return { folders: folders, error: "" }
}

if (typeof module !== "undefined") {
  module.exports = {
    repositoryFolders: repositoryFolders,
    parseFolderRows: parseFolderRows,
    defaultStatus: defaultStatus,
    parseStatus: parseStatus,
    relativeTime: relativeTime,
    repositoryMeta: repositoryMeta,
    repositoryState: repositoryState,
    barText: barText,
    actionSummary: actionSummary
  }
}
