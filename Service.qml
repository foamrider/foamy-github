import QtQuick
import Quickshell
import Quickshell.Io
import Quickshell.Networking
import "Model.js" as Model
import "Preferences.js" as Preferences

Item {
  id: root

  property var settings: ({})
  property string language: "en"
  function tr(label, values) { return Preferences.text(label, language, values) }
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
    return root.tr(message.label, message.values)
  }).join(" · ")
  function setActionStatus(label, values) {
    actionMessages = label ? [{label: label, values: values || []}] : []
  }
  property string operationError: ""
  property bool watchFailed: false
  readonly property string watchError: !inotifyEnabled || !watchFailed ? ""
    : refreshIntervalSec > 0 ? "Live monitoring unavailable; using timed refresh."
    : "Live monitoring unavailable; automatic local refresh is disabled."
  readonly property string lastError: watchError || operationError
  property bool monitoring: false
  property bool watchRestartRequested: false
  property string watchedFolders: ""

  readonly property string helperPath: decodeURIComponent(Qt.resolvedUrl("github_status.py").toString().replace(/^file:\/\//, ""))
  readonly property var repositoryFolders: Model.repositoryFolders(settings)
  readonly property string folderArguments: JSON.stringify(repositoryFolders)
  onFolderArgumentsChanged: Qt.callLater(function() { root.restartWatcher() })
  readonly property var repos: status.repos || []
  readonly property var affectedRepos: repos.filter(function(repo) { return repo.affected === true })
  readonly property var totals: status.totals || Model.defaultStatus().totals
  readonly property var sync: status.sync || Model.defaultStatus().sync
  readonly property bool inotifyEnabled: setting("inotifyEnabled", true) !== false
  onInotifyEnabledChanged: Qt.callLater(function() { root.restartWatcher() })
  readonly property int refreshIntervalSec: setting("refreshIntervalSec", 30) === 0 ? 0 : intSetting("refreshIntervalSec", 30, 10, 3600)
  readonly property int fetchIntervalSec: intSetting("fetchIntervalSec", 900, 300, 86400)
  readonly property bool busy: refreshing || syncing || repositoryActionRunning
  // A limited connectivity probe can still allow Git access; block offline/portal states.
  readonly property bool networkReady: Networking.connectivity === NetworkConnectivity.Full
    || Networking.connectivity === NetworkConnectivity.Limited

  onNetworkReadyChanged: {
    if (networkReady) networkReadyTimer.restart()
    else networkReadyTimer.stop()
  }

  Component.onCompleted: {
    root.restartWatcher()
    if (networkReady) networkReadyTimer.start()
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

  function restartWatcher() {
    root.monitoring = false
    if (watchProcess.running) {
      root.watchRestartRequested = true
      watchProcess.running = false
      return
    }
    root.watchRestartRequested = false
    root.watchFailed = false
    if (!root.inotifyEnabled) {
      root.refresh()
      return
    }
    root.watchedFolders = root.folderArguments
    watchProcess.command = ["python3", root.helperPath.replace(/github_status\.py$/, "github_watch.py"),
      "--folders-json", root.watchedFolders]
    root.refreshing = true
    watchProcess.running = true
  }

  function refresh() {
    if (root.inotifyEnabled && root.monitoring && watchProcess.running) {
      root.refreshing = true
      watchProcess.write("refresh\n")
      return
    }
    if (statusProcess.running) return
    refreshing = true
    scannedFolders = folderArguments
    statusProcess.command = ["python3", helperPath, "status", "--folders-json", folderArguments]
    statusProcess.running = true
  }

  function fetch() {
    synchronize("fetch")
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

  function update() {
    synchronize("update")
  }

  function synchronize(mode) {
    if (root.busy) return
    if (!root.networkReady) {
      setActionStatus("Waiting for network…")
      actionMessageTimer.restart()
      return
    }
    syncing = true
    operationError = ""
    // An earlier completion timer must not clear the new progress title.
    actionMessageTimer.stop()
    setActionStatus(mode === "update" ? "Fetching and safely updating…" : "Fetching repositories…")
    syncProcess.command = ["python3", helperPath, mode, "--folders-json", folderArguments]
    syncProcess.running = true
  }

  function pushRepository(repo) {
    runRepositoryAction("push", repo)
  }

  function pullRepository(repo) {
    runRepositoryAction("pull", repo)
  }

  function runRepositoryAction(mode, repo) {
    if (root.busy || !repo || !repo.path) return
    repositoryActionRunning = true
    operationError = ""
    actionMessageTimer.stop()
    setActionStatus(mode === "push" ? "Pushing %1…" : "Pulling %1…",
      [String(repo.label || repo.name || "repository")])
    repositoryActionProcess.command = [
      "python3", helperPath, mode, String(repo.path), "--folders-json", folderArguments
    ]
    repositoryActionProcess.running = true
  }

  function applyStatus(raw) {
    var next = Model.parseStatus(raw)
    if (!next.ok) {
      operationError = next.error || "Failed to read repository status"
      return
    }
    status = next
    lastChecked = Date.now() / 1000
    operationError = ""
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
    running: root.refreshIntervalSec > 0 && !root.monitoring
    triggeredOnStart: false
    onTriggered: root.refresh()
  }

  // A failed watcher keeps the existing interval as fallback and retries slowly.
  Timer {
    interval: 30000
    repeat: true
    running: root.inotifyEnabled && !watchProcess.running
    onTriggered: root.restartWatcher()
  }

  Process {
    id: watchProcess
    stdinEnabled: true
    stdout: SplitParser {
      onRead: function(line) {
        if (!root.inotifyEnabled || root.watchRestartRequested || root.watchedFolders !== root.folderArguments) return
        root.applyStatus(line)
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
      root.watchFailed = true
      console.warn("foamy.github:", String(watchOutput.text || "Filesystem watcher exited").trim())
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
    running: root.networkReady
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
      else root.operationError = String(statusError.text || "Repository status failed").trim()
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
        var summary = []
        if ((result.updated || []).length > 0) summary.push({label: "Updated %1", values: [result.updated.length]})
        if ((result.skipped || []).length > 0) summary.push({label: "Skipped %1", values: [result.skipped.length]})
        if ((result.failures || []).length > 0) summary.push({label: "Failed %1", values: [result.failures.length]})
        if (summary.length > 0) root.actionMessages = summary
        else root.setActionStatus(result.mode === "update" ? "Everything already current" : "Fetch complete")
        if (exitCode !== 0) root.operationError = "Some repositories could not be synchronized"
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
