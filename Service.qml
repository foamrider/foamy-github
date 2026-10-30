pragma Singleton
import QtQuick
import Quickshell
import Quickshell.Io
import Quickshell.Networking
import "kit"
import "Model.js" as Model

// Repository state and every Git helper, once per shell. 4.5 draws one bar
// (and so one Panel) per monitor; all of them read this singleton, and only
// the FoamyMember leader feeds it settings. It runs while at least one bar
// instance exists and stops its helpers when the last one goes.
Item {
  id: root

  property var settings: ({})
  readonly property bool active: FoamyShared.memberCount("github") > 0
  function tr(label, values) { return FoamyI18n.t(label, values) }
  property var status: Model.defaultStatus()
  property double lastChecked: 0
  property string scannedFolders: ""
  property bool refreshing: false
  property bool syncing: false
  property bool repositoryActionRunning: false
  property bool scheduledFetchPending: false
  // Keep message data so an in-flight operation follows language changes.
  property var actionMessages: []
  readonly property string actionStatus: actionMessages.map(function(message) {
    return message.count !== undefined ? FoamyI18n.tn(message.label, message.count) : root.tr(message.label, message.values)
  }).join(" · ")
  function setActionStatus(label, values) {
    actionMessages = label ? [{label: label, values: values || []}] : []
  }
  property string operationError: ""
  property bool watchFailed: false
  readonly property string watchError: !inotifyEnabled || !watchFailed ? ""
    : refreshIntervalSec > 0 ? "Live monitoring unavailable; using timed refresh."
    : "Live monitoring unavailable; automatic local refresh is disabled."
  property string watchDetail: ""
  readonly property string lastError: operationError || (watchError ? watchError + (watchDetail ? " " + watchDetail : "") : "")
    || (totals.unavailableRepos > 0 ? "Some repository checks are incomplete. Open the affected repository in lazygit, then refresh." : "")
  property bool monitoring: false
  property bool watchRestartRequested: false
  property string watchedFolders: ""
  // Watcher retry backoff: 30 s doubling to 30 min; reset after a stable run.
  property int watchRetryMs: Model.WATCH_RETRY_MIN_MS
  property double watchStartedAt: 0

  readonly property string helperPath: decodeURIComponent(Qt.resolvedUrl("github_status.py").toString().replace(/^file:\/\//, ""))
  readonly property var repositoryFolders: Model.repositoryFolders(settings)
  readonly property string folderArguments: JSON.stringify(repositoryFolders)
  onFolderArgumentsChanged: Qt.callLater(function() { root.restartWatcher(true) })
  readonly property bool trustRepositoryConfig: setting("trustRepositoryConfig", false) === true
  onTrustRepositoryConfigChanged: Qt.callLater(function() { root.restartWatcher(true) })
  function helperArguments() {
    var args = ["--folders-json", folderArguments]
    if (trustRepositoryConfig) args.push("--trust-repository-config")
    return args
  }
  readonly property var repos: status.repos || []
  readonly property var affectedRepos: repos.filter(function(repo) { return repo.affected === true })
  readonly property var totals: status.totals || Model.defaultStatus().totals
  readonly property var sync: status.sync || Model.defaultStatus().sync
  readonly property bool inotifyEnabled: setting("inotifyEnabled", true) !== false
  onInotifyEnabledChanged: Qt.callLater(function() { root.restartWatcher(true) })
  readonly property int refreshIntervalSec: setting("refreshIntervalSec", 30) === 0 ? 0 : intSetting("refreshIntervalSec", 30, 10, 3600)
  readonly property int fetchIntervalSec: intSetting("fetchIntervalSec", 900, 300, 86400)
  readonly property bool busy: refreshing || syncing || repositoryActionRunning
  // A limited connectivity probe can still allow Git access; block offline/portal states.
  readonly property bool networkReady: Networking.connectivity === NetworkConnectivity.Full
    || Networking.connectivity === NetworkConnectivity.Limited

  onNetworkReadyChanged: {
    if (networkReady && active) networkReadyTimer.restart()
    else networkReadyTimer.stop()
  }

  onActiveChanged: {
    if (active) {
      root.restartWatcher(true)
      if (networkReady) networkReadyTimer.start()
      return
    }
    // The last bar instance went away (plugin disabled): stop every helper.
    networkReadyTimer.stop()
    watchProcess.running = false
    statusProcess.running = false
    syncProcess.running = false
    repositoryActionProcess.running = false
    monitoring = false
  }

  // Popup opened: with live monitoring the rows are already current, so
  // don't rebuild every watch plan; otherwise take one fresh status.
  function popupOpened() {
    if (!(root.inotifyEnabled && root.monitoring)) root.refresh()
  }

  function setting(name, fallback) {
    var value = settings ? settings[name] : undefined
    return value === undefined || value === null ? fallback : value
  }

  function intSetting(name, fallback, minimum, maximum) {
    var value = parseInt(String(setting(name, fallback)), 10)
    if (!isFinite(value)) value = fallback
    return Math.max(minimum, Math.min(maximum, value))
  }

  function restartWatcher(resetBackoff) {
    if (!root.active) return
    if (resetBackoff) root.watchRetryMs = Model.WATCH_RETRY_MIN_MS
    root.monitoring = false
    if (watchProcess.running) {
      root.watchRestartRequested = true
      watchProcess.running = false
      return
    }
    root.watchRestartRequested = false
    root.watchFailed = false
    root.watchDetail = ""
    if (!root.inotifyEnabled) {
      root.refresh()
      return
    }
    root.watchedFolders = root.folderArguments
    watchProcess.command = ["python3", root.helperPath.replace(/github_status\.py$/, "github_watch.py")].concat(root.helperArguments())
    root.watchStartedAt = Date.now()
    root.refreshing = true
    watchProcess.running = true
  }

  function refresh() {
    if (!root.active) return
    if (root.inotifyEnabled && root.monitoring && watchProcess.running) {
      root.refreshing = true
      watchProcess.write("refresh\n")
      return
    }
    if (statusProcess.running) return
    refreshing = true
    scannedFolders = folderArguments
    statusProcess.command = ["python3", helperPath, "status"].concat(helperArguments())
    statusProcess.running = true
  }

  function fetch() {
    synchronize()
  }

  function fetchWhenIdle() {
    if (!root.networkReady) {
      scheduledFetchPending = false
      return
    }
    if (root.syncing) {
      scheduledFetchPending = false
      return
    }
    if (root.refreshing || root.repositoryActionRunning) {
      // Keep a timer collision or foreground action from dropping the fetch.
      scheduledFetchPending = true
      return
    }
    scheduledFetchPending = false
    root.fetch()
  }

  function retryScheduledFetch() {
    if (!scheduledFetchPending) return
    Qt.callLater(function() { root.fetchWhenIdle() })
  }

  function synchronize() {
    if (root.busy || !root.active) return
    if (!root.networkReady) {
      setActionStatus("Waiting for network…")
      actionMessageTimer.restart()
      return
    }
    syncing = true
    operationError = ""
    // An earlier completion timer must not clear the new progress title.
    actionMessageTimer.stop()
    setActionStatus("Fetching repositories…")
    syncProcess.command = ["python3", helperPath, "fetch"].concat(helperArguments())
    syncProcess.running = true
  }

  function pushRepository(repo) {
    runRepositoryAction("push", repo)
  }

  function pullRepository(repo) {
    runRepositoryAction("pull", repo)
  }

  function runRepositoryAction(mode, repo) {
    if (root.busy || !root.active || !repo || !repo.path || repo.complete !== true) return
    repositoryActionRunning = true
    operationError = ""
    actionMessageTimer.stop()
    setActionStatus(mode === "push" ? "Pushing %1…" : "Pulling %1…",
      [String(repo.label || repo.name || "repository")])
    repositoryActionProcess.command = ["python3", helperPath, mode, String(repo.path)].concat(helperArguments())
    repositoryActionProcess.running = true
  }

  function applyStatus(raw) {
    var next = Model.parseStatus(raw)
    if (!next.ok) {
      failStatus(next.error || "Failed to read repository status")
      return false
    }
    status = next
    lastChecked = Date.now() / 1000
    operationError = ""
    return true
  }

  function failStatus(message) {
    operationError = message
    status = Model.unavailableStatus(status, message)
  }

  function openRepository(repo) {
    if (!repo || !repo.path) return
    Quickshell.execDetached([
      "uwsm-app", "--", "xdg-terminal-exec",
      "--app-id=org.omarchy.terminal",
      "--title=lazygit - " + String(repo.name || "repository"),
      "-e", "bash", "-c", "cd -- \"$1\" && exec lazygit", "_", String(repo.path)
    ])
  }

  Timer {
    interval: Math.max(1, root.refreshIntervalSec) * 1000
    repeat: true
    running: root.active && root.refreshIntervalSec > 0 && !root.monitoring
    triggeredOnStart: false
    onTriggered: root.refresh()
  }

  // A failed watcher keeps the existing interval as fallback and retries with
  // backoff, so a deterministic failure (watch limit, one huge repository)
  // costs a full scan every 30 minutes at most, not every 30 seconds.
  Timer {
    id: watchRetry
    interval: Model.WATCH_RETRY_MIN_MS
    repeat: false
    onTriggered: if (root.active && root.inotifyEnabled && !watchProcess.running) root.restartWatcher(false)
  }

  Process {
    id: watchProcess
    stdinEnabled: true
    stdout: SplitParser {
      onRead: function(line) {
        if (!root.inotifyEnabled || root.watchRestartRequested || root.watchedFolders !== root.folderArguments) return
        if (!root.applyStatus(line)) {
          watchProcess.running = false
          return
        }
        root.monitoring = true
        root.watchFailed = false
        root.refreshing = false
        root.retryScheduledFetch()
      }
    }
    stderr: StdioCollector { id: watchOutput; waitForEnd: true }
    onExited: function(exitCode) {
      root.monitoring = false
      root.refreshing = false
      if (root.watchRestartRequested) {
        Qt.callLater(function() { root.restartWatcher() })
        return
      }
      if (!root.active) return
      root.watchFailed = true
      root.watchDetail = String(watchOutput.text || "Filesystem watcher exited").trim().slice(0, 240)
      var ran = Date.now() - root.watchStartedAt
      var retry = Model.watchRetry(root.watchRetryMs, ran)
      watchRetry.interval = retry.delay
      root.watchRetryMs = retry.next
      console.warn("foamy.github:", root.watchDetail, "(retrying in", Math.round(retry.delay / 1000), "s)")
      if (root.inotifyEnabled) watchRetry.restart()
      root.refresh()
      root.retryScheduledFetch()
    }
  }

  Timer {
    id: networkReadyTimer
    // Let DNS and routes settle briefly after NetworkManager reports Full.
    interval: 2000
    repeat: false
    onTriggered: {
      if (root.syncing) return
      if (root.refreshing || root.repositoryActionRunning) {
        restart()
        return
      }
      root.fetchWhenIdle()
    }
  }

  Timer {
    interval: root.fetchIntervalSec * 1000
    repeat: true
    running: root.active && root.networkReady
    triggeredOnStart: false
    onTriggered: root.fetchWhenIdle()
  }

  Timer {
    id: actionMessageTimer
    interval: 5000
    repeat: false
    onTriggered: root.setActionStatus("")
  }

  Process {
    id: statusProcess
    stdout: StdioCollector { id: statusOutput; waitForEnd: true }
    stderr: StdioCollector { id: statusError; waitForEnd: true }
    onExited: function(exitCode) {
      root.refreshing = false
      // Do not display a scan of the old folders after settings change mid-scan.
      if (root.scannedFolders !== root.folderArguments) {
        root.refresh()
        return
      }
      if (root.monitoring) return
      if (exitCode === 0) root.applyStatus(statusOutput.text)
      else root.failStatus(String(statusError.text || "Repository status failed").trim().slice(0, 240))
      root.retryScheduledFetch()
    }
  }

  Process {
    id: syncProcess
    stdout: StdioCollector { id: syncOutput; waitForEnd: true }
    stderr: StdioCollector { id: syncError; waitForEnd: true }
    onExited: function(exitCode) {
      root.syncing = false
      var result = null
      try { result = JSON.parse(String(syncOutput.text || "{}")) } catch (e) {}
      if (result && result.busy) {
        root.setActionStatus(result.message || "Repository synchronization is already running")
      } else if (result) {
        var failed = (result.failures || []).length
        root.actionMessages = failed > 0 ? [{label: "%1 repositories failed to fetch", values: [failed], count: failed}]
          : [{label: "Fetch complete", values: []}]
      } else {
        root.setActionStatus("")
        root.operationError = String(syncError.text || "Repository synchronization failed").trim()
      }
      actionMessageTimer.restart()
      root.refresh()
    }
  }

  Process {
    id: repositoryActionProcess
    stdout: StdioCollector { id: repositoryActionOutput; waitForEnd: true }
    stderr: StdioCollector { id: repositoryActionError; waitForEnd: true }
    onExited: function(exitCode) {
      root.repositoryActionRunning = false
      var result = null
      try {
        result = JSON.parse(String(repositoryActionOutput.text || "{}"))
      } catch (e) {}
      if (result && (result.ok || result.busy)) {
        root.setActionStatus(String(result.message || "Repository action complete"))
        root.operationError = ""
      } else if (result) {
        root.setActionStatus("")
        root.operationError = String(result.message || "Repository action failed")
      } else {
        root.setActionStatus("")
        root.operationError = String(repositoryActionError.text
          || "Repository action failed").trim()
      }
      actionMessageTimer.restart()
      root.refresh()
      root.retryScheduledFetch()
    }
  }
}
