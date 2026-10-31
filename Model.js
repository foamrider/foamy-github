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
      dirtyFiles: 0,
      unavailableRepos: 0
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
  if (String(raw || "").length > 8 * 1024 * 1024) return failedStatus("Repository summary is too large. Select fewer repository folders.")
  var text = String(raw || "").trim()
  if (text === "") return failedStatus("No repository status received")
  try {
    var parsed = JSON.parse(text)
    if (!parsed || typeof parsed !== "object") return failedStatus("Invalid repository status")
    var fallback = defaultStatus()
    parsed.totals = Object.assign({}, fallback.totals, parsed.totals || {})
    parsed.repos = Array.isArray(parsed.repos) ? parsed.repos : []
    if (parsed.repos.length > 256) return failedStatus("Too many repositories. Select fewer repository folders.")
    for (var repo of parsed.repos) {
      if (!repo || typeof repo.complete !== "boolean" || typeof repo.affected !== "boolean")
        return failedStatus("Invalid repository status")
      if (repo.syncError === undefined) repo.syncError = ""
      if (repo.usesLfs === undefined) repo.usesLfs = false
      if (repo.trusted === undefined) repo.trusted = false
      if (repo.identity === undefined) repo.identity = ""
      if (typeof repo.usesLfs !== "boolean" || typeof repo.trusted !== "boolean") return failedStatus("Invalid repository status")
      for (var field of ["path", "name", "owner", "label", "branch", "upstream", "error", "syncError", "identity"]) {
        if (typeof repo[field] !== "string" || repo[field].length > 4096)
          return failedStatus("Invalid repository status")
      }
      for (var count of ["ahead", "behind", "dirtyCount"]) {
        if (!Number.isSafeInteger(repo[count]) || repo[count] < 0)
          return failedStatus("Invalid repository status")
      }
    }
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

function unavailableStatus(previous, message) {
  var status = failedStatus(message)
  // Preserve repository identities for recovery, but never reuse stale safety checks.
  status.repos = (previous.repos || []).map(function(repo) {
    return Object.assign({}, repo, {complete: false, affected: true, error: message,
      ahead: 0, behind: 0, dirty: null, dirtyCount: 0})
  })
  status.repoCount = status.repos.length
  status.affectedCount = status.repos.length
  status.totals.unavailableRepos = status.repos.length
  return status
}

function translate(label, values) {
  return label.replace(/%([1-9][0-9]*)/g, function(token, index) {
    return values && Number(index) <= values.length ? String(values[Number(index) - 1]) : token
  })
}

function relativeTime(timestampSec, nowMs, tr) {
  tr = tr || translate
  var timestamp = Number(timestampSec || 0)
  if (!isFinite(timestamp) || timestamp <= 0) return tr("Never")
  var now = nowMs === undefined ? Date.now() : Number(nowMs)
  var seconds = Math.max(0, Math.floor((now - timestamp * 1000) / 1000))
  if (seconds < 45) return tr("Just now")
  var minutes = Math.floor(seconds / 60)
  if (minutes < 60) return tr("%1m ago", [minutes])
  var hours = Math.floor(minutes / 60)
  if (hours < 24) return tr("%1h ago", [hours])
  var days = Math.floor(hours / 24)
  return tr("%1d ago", [days])
}

function plural(key, count) {
  return translate(key, [count])
}

function updatedText(timestampSec, nowMs, tr, tn) {
  tr = tr || translate
  tn = tn || plural
  if (!isFinite(timestampSec) || timestampSec <= 0) return ""
  var minutes = Math.max(0, Math.floor((nowMs - timestampSec * 1000) / 60000))
  if (minutes === 0) return tr("Updated just now")
  return tn("Updated %1 minutes ago", minutes)
}

// Repositories the popup lists: anything affected locally, plus clean ones
// whose last fetch failed, so their inline error stays visible.
function displayedRepos(repos) {
  return (repos || []).filter(function(repo) {
    return !!repo && (repo.affected === true || (typeof repo.syncError === "string" && repo.syncError !== ""))
  })
}

// Trusted repositories from settings: [{ path, identity }] (absolute path plus
// the Git directory identity reported by the helper). Bare paths from an
// earlier build carry no identity and are dropped (untrusted again).
function trustedRepositories(settings) {
  var value = settings ? settings.trustedRepositories : undefined
  var list = value === undefined || value === null ? [] : JSON.parse(JSON.stringify(value))
  if (!Array.isArray(list)) return []
  var out = []
  for (var i = 0; i < list.length && out.length < 256; i++) {
    var item = list[i]
    if (item && typeof item === "object" && typeof item.path === "string" && item.path.charAt(0) === "/"
        && typeof item.identity === "string" && item.identity !== ""
        && !out.some(function(entry) { return entry.path === item.path }))
      out.push({ path: item.path, identity: item.identity })
  }
  return out
}

function isTrusted(list, repo) {
  return !!repo && (list || []).some(function(entry) { return entry.path === repo.path && entry.identity === repo.identity })
}

// The list a trust edit must start from: the newest queued save of
// trustedRepositories if one waits behind an in-flight save, else the
// effective settings (saved + in flight). Composing over anything older
// would undo an earlier queued edit (untrust B, then C, restoring B).
function trustBase(effectiveSettings, waiting) {
  var queued = (waiting || []).filter(function(item) { return item && item.key === "trustedRepositories" })
  return queued.length > 0 ? trustedRepositories({ trustedRepositories: queued[queued.length - 1].value })
    : trustedRepositories(effectiveSettings)
}

// What a fetch helper run means: "busy", "done" (with failures) or "error".
// A non-zero exit without a structured result is an error (stderr), never
// "Fetch complete".
function fetchOutcome(exitCode, stdout, stderr) {
  var result = null
  try { result = JSON.parse(String(stdout || "")) } catch (e) {}
  var structured = !!result && typeof result === "object" && !Array.isArray(result)
    && (result.busy === true || (typeof result.ok === "boolean" && Array.isArray(result.failures)))
  if (structured && result.busy === true) return { status: "busy", message: String(result.message || ""), failed: 0 }
  if (structured) return { status: "done", message: "", failed: result.failures.length }
  var detail = String(stderr || "").trim().slice(0, 240)
  return { status: "error", message: detail || "Repository synchronization failed", failed: 0, exitCode: exitCode }
}

// The trusted list with `repo` (its path and identity) added or removed.
function withTrust(list, repo, trusted) {
  var out = (list || []).filter(function(entry) { return entry.path !== repo.path })
  if (trusted && repo.identity) out.push({ path: String(repo.path), identity: String(repo.identity) })
  return out
}

// Repositories Settings → Git LFS lists, by label: those that use Git LFS,
// plus any with a trust record in `trusted` (trustedRepositories), so trust
// in effect stays visible and revocable, e.g. for a repository that stopped
// using LFS. Records of repositories not found are kept, never pruned.
function lfsRepos(repos, trusted) {
  return (repos || []).filter(function(repo) { return !!repo && (repo.usesLfs === true || isTrusted(trusted, repo)) })
    .sort(function(a, b) { return String(a.label).toLowerCase() < String(b.label).toLowerCase() ? -1 : 1 })
}

// Validation for the trustedRepositories setting.
function validTrustedList(value) {
  if (!Array.isArray(value) || value.length > 256) return "Use at most 256 trusted repositories."
  for (var i = 0; i < value.length; i++) {
    var item = value[i]
    if (!item || typeof item.path !== "string" || item.path.charAt(0) !== "/" || item.path.length > 4096
        || typeof item.identity !== "string" || item.identity === "" || item.identity.length > 128)
      return "Trusted repositories must be absolute paths."
  }
  return true
}

// The inline fetch error after a row's status line ("main · 3 changed · …").
function syncErrorText(repo, tr) {
  tr = tr || translate
  return repo && repo.syncError ? tr(String(repo.syncError)) : ""
}

function repositoryMeta(repo, tr, tn) {
  tr = tr || translate
  tn = tn || plural
  if (!repo) return ""
  if (repo.complete === false) return tr("Status unavailable") + " · " + String(repo.error || "")
  var parts = []
  var branch = String(repo.branch || tr("detached"))
  parts.push(branch)
  if (repo.ahead > 0) parts.push("↑" + tn("%1 commits", repo.ahead))
  if (repo.behind > 0) parts.push("↓" + tn("%1 commits", repo.behind))
  if (repo.dirtyCount > 0) parts.push(tr("%1 changed", [repo.dirtyCount]))
  return parts.join(" · ")
}

// Filesystem watcher retry: 30 s, doubling to 30 min while it keeps failing
// soon after starting; a run that lasted 10 min starts over at 30 s.
var WATCH_RETRY_MIN_MS = 30 * 1000
var WATCH_RETRY_MAX_MS = 30 * 60 * 1000
var WATCH_STABLE_MS = 10 * 60 * 1000

function watchRetry(currentMs, ranMs) {
  var delay = Number(ranMs) >= WATCH_STABLE_MS ? WATCH_RETRY_MIN_MS
    : Math.max(WATCH_RETRY_MIN_MS, Math.min(WATCH_RETRY_MAX_MS, Number(currentMs) || WATCH_RETRY_MIN_MS))
  return { delay: delay, next: Math.min(WATCH_RETRY_MAX_MS, delay * 2) }
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
    unavailableStatus: unavailableStatus,
    relativeTime: relativeTime,
    updatedText: updatedText,
    repositoryMeta: repositoryMeta,
    watchRetry: watchRetry,
    displayedRepos: displayedRepos,
    trustedRepositories: trustedRepositories,
    withTrust: withTrust,
    isTrusted: isTrusted,
    trustBase: trustBase,
    fetchOutcome: fetchOutcome,
    lfsRepos: lfsRepos,
    validTrustedList: validTrustedList,
    syncErrorText: syncErrorText
  }
}
