pragma Singleton
import QtQuick
import Quickshell
import Quickshell.Io
import Quickshell.Networking
import "kit"
import "Model.js" as Model
import "Preferences.js" as Schema
import "kit/FoamyPrefs.js" as Prefs
import "kit/FoamyNative.js" as FoamyNative

// Repository state and every Git helper, once per shell. 4.5 draws one bar
// (and so one Panel) per monitor; all of them read this singleton, and only
// the FoamyMember leader feeds it settings. It runs while at least one bar
// instance exists and stops its helpers when the last one goes.
Item {
  id: root

  property var settings: ({})
  // The widget's schema; the panel saves through the same fields. Every
  // setting below is read through it: a missing or invalid value reads as its
  // default, as in Settings. (JSON copy: Omarchy can hand a list over as a QML
  // sequence wrapper.)
  readonly property var fields: Schema.fields.concat(Model.listFields)
  readonly property var preferences: Prefs.values(fields, JSON.parse(JSON.stringify(settings || {})))
  readonly property bool active: FoamyShared.memberCount("github") > 0
  function tr(label, values) { return FoamyI18n.t(label, values) }
  property var status: Model.defaultStatus()
  property double lastChecked: 0
  // The folders and trust the running (or last) status check was started
  // with; a check that a settings change overtook is dropped and repeated.
  property string scannedFolders: ""
  property string scannedTrust: ""
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
  // The start of the running watcher's stderr. The watcher lives as long as
  // the shell, so only watchStderrLimit characters are kept (240 are shown).
  readonly property int watchStderrLimit: 4096
  property string watchStderr: ""

  // The native helper (bin/<arch>/foamy-github-helper). Exit 126/127: none
  // for this CPU; then it is never started again (no retry loop).
  readonly property string pluginDir: FoamyNative.localPath(Qt.resolvedUrl("."))
  property bool helperUnavailable: false
  function helperCommand(args) {
    return FoamyNative.command(root.pluginDir, "foamy-github-helper", args)
  }
  function markUnavailable(exitCode) {
    if (!FoamyNative.unavailable(exitCode)) return false
    if (!root.helperUnavailable) console.warn("foamy.github: the native helper isn't available for this CPU (exit", exitCode + ")")
    root.helperUnavailable = true
    root.operationError = FoamyNative.UNAVAILABLE_TEXT
    status = Model.unavailableStatus(status, FoamyNative.UNAVAILABLE_TEXT)
    return true
  }
  readonly property var repositoryFolders: preferences.repositoryFolders
  readonly property string folderArguments: JSON.stringify(repositoryFolders)
  onFolderArgumentsChanged: Qt.callLater(function() { root.restartWatcher(true) })
  // Per-repository trust: absolute paths whose own Git config runs in
  // automatic checks (the panel offers it for Git LFS repositories). The old
  // global switch stays honoured until the panel migrates it once.
  readonly property bool legacyTrustAll: preferences.trustRepositoryConfig
  readonly property var trustedRepositories: Model.trustedRepositories(preferences)
  readonly property string trustArguments: legacyTrustAll ? "*" : JSON.stringify(trustedRepositories)
  onTrustArgumentsChanged: Qt.callLater(function() { root.restartWatcher(true) })
  function helperArguments() {
    var args = ["--folders-json", folderArguments]
    if (legacyTrustAll) args.push("--trust-repository-config")
    else args.push("--trusted-json", JSON.stringify(trustedRepositories))
    return args
  }
  readonly property var lfsRepos: Model.lfsRepos(repos)
  readonly property var repos: status.repos || []
  // Shown in the popup: local changes, ahead/behind, or a failed last fetch (its error is on the row).
  readonly property var affectedRepos: Model.displayedRepos(repos)
  readonly property var totals: status.totals || Model.defaultStatus().totals
  readonly property var sync: status.sync || Model.defaultStatus().sync
  readonly property bool inotifyEnabled: preferences.inotifyEnabled
  onInotifyEnabledChanged: Qt.callLater(function() { root.restartWatcher(true) })
  // 0 turns timed refresh off.
  readonly property int refreshIntervalSec: preferences.refreshIntervalSec
  readonly property int fetchIntervalSec: preferences.fetchIntervalSec
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
    pullAllQueue = []
    pullAllResult = null
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

  function restartWatcher(resetBackoff) {
    if (!root.active || root.helperUnavailable) return
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
    watchProcess.command = root.helperCommand(["watch"].concat(root.helperArguments()))
    root.watchStartedAt = Date.now()
    root.watchStderr = ""
    root.refreshing = true
    watchProcess.running = true
  }

  function refresh() {
    if (!root.active || root.helperUnavailable) return
    if (root.inotifyEnabled && root.monitoring && watchProcess.running) {
      root.refreshing = true
      watchProcess.write("refresh\n")
      return
    }
    if (statusProcess.running) return
    refreshing = true
    scannedFolders = folderArguments
    scannedTrust = trustArguments
    statusProcess.command = helperCommand(["status"].concat(helperArguments()))
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
    if (root.busy || !root.active || root.helperUnavailable) return
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
    syncProcess.command = helperCommand(["fetch"].concat(helperArguments()))
    syncProcess.running = true
  }

  function pushRepository(repo) {
    runRepositoryAction("push", repo)
  }

  function pullRepository(repo) {
    runRepositoryAction("pull", repo)
  }

  // Pull all: every repository a row's Pull would take, one at a time through
  // the same helper (which checks each again before pulling). The next pull
  // starts as the previous one exits, before its status refresh.
  readonly property var pullableRepos: Model.pullableRepos(repos)
  property var pullAllQueue: []
  property var pullAllResult: null            // { pulled, failures: [message] } while running
  readonly property bool pullingAll: pullAllResult !== null
  function pullAll() {
    if (root.busy || !root.active || pullableRepos.length === 0) return
    pullAllResult = { pulled: 0, failures: [] }
    pullAllQueue = pullableRepos.slice()
    pullNext()
  }
  function pullNext() {
    while (pullAllQueue.length > 0) {
      var repo = pullAllQueue[0]
      pullAllQueue = pullAllQueue.slice(1)
      if (runRepositoryAction("pull", repo, true)) return
    }
    finishPullAll()
  }
  function finishPullAll() {
    var result = pullAllResult
    pullAllResult = null
    pullAllQueue = []
    if (!result) return
    actionMessages = result.pulled > 0 ? [{ label: "Pulled %1 repositories", count: result.pulled }] : []
    operationError = result.failures.join(" · ")
  }

  // `queued`: the next Pull all step, which may follow a status refresh the
  // previous step started (the helper's lock still keeps Git operations apart).
  function runRepositoryAction(mode, repo, queued) {
    if ((queued ? root.repositoryActionRunning || root.syncing : root.busy) || !root.active || root.helperUnavailable
        || !repo || !repo.path || repo.complete !== true) return false
    repositoryActionRunning = true
    operationError = ""
    actionMessageTimer.stop()
    setActionStatus(mode === "push" ? "Pushing %1…" : "Pulling %1…",
      [String(repo.label || repo.name || "repository")])
    repositoryActionProcess.command = helperCommand([mode, String(repo.path)].concat(helperArguments()))
    repositoryActionProcess.running = true
    return true
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
    // Chunks as they arrive (no split), so only watchStderrLimit is ever kept.
    stderr: SplitParser {
      splitMarker: ""
      onRead: function(data) {
        if (root.watchStderr.length < root.watchStderrLimit)
          root.watchStderr += String(data).slice(0, root.watchStderrLimit - root.watchStderr.length)
      }
    }
    onExited: function(exitCode) {
      root.monitoring = false
      root.refreshing = false
      if (root.watchRestartRequested) {
        Qt.callLater(function() { root.restartWatcher() })
        return
      }
      if (!root.active) return
      if (root.markUnavailable(exitCode)) return
      root.watchFailed = true
      root.watchDetail = String(root.watchStderr || "Filesystem watcher exited").trim().slice(0, 240)
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
      // Do not display a scan of the old folders or trust after settings change mid-scan.
      if (root.scannedFolders !== root.folderArguments || root.scannedTrust !== root.trustArguments) {
        root.refresh()
        return
      }
      if (root.monitoring) return
      if (root.markUnavailable(exitCode)) return
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
      if (root.markUnavailable(exitCode)) {
        root.setActionStatus("")
        return
      }
      var outcome = Model.fetchOutcome(exitCode, syncOutput.text, syncError.text)
      if (outcome.status === "busy") {
        root.setActionStatus(outcome.message || "Repository synchronization is already running")
      } else if (outcome.status === "done") {
        root.actionMessages = outcome.failed > 0 ? [{label: "%1 repositories failed to fetch", values: [outcome.failed], count: outcome.failed}]
          : [{label: "Fetch complete", values: []}]
      } else {
        root.setActionStatus("")
        root.operationError = outcome.message
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
      if (root.markUnavailable(exitCode)) {
        root.pullAllQueue = []
        root.pullAllResult = null
        root.setActionStatus("")
        return
      }
      var result = null
      try {
        result = JSON.parse(String(repositoryActionOutput.text || "{}"))
      } catch (e) {}
      if (root.pullingAll) {
        var step = root.pullAllResult
        if (result && result.ok) step.pulled++
        else step.failures.push(result ? String(result.message || "Repository action failed")
          : String(repositoryActionError.text || "Repository action failed").trim())
        root.pullAllResult = step
        root.pullNext()
        if (root.pullingAll) return
        actionMessageTimer.restart()
        root.refresh()
        root.retryScheduledFetch()
        return
      }
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
