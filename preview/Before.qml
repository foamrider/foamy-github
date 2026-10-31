import QtQuick
import qs.Ui
import ".." as Dev

// The dev UI for `render.py --trio` (Before column). Dev's popup lives inside
// Panel.qml with a real Service, so this loads dev's Panel (folders: none, so
// its helper sees zero repositories in the harness's temp HOME) and then
// feeds the same mock repositories as Preview.qml into that Service.
Item {
  id: preview
  required property QtObject bar
  required property Item anchorItem
  property var previewStates: ["repositories", "lfs-untrusted", "lfs-trusted", "settings"]   // dev had no LFS UI: lfs-* show its repository list
  property string previewState: "repositories"
  property int settleMs: 2500
  property var popup: null
  readonly property double now: Date.now() / 1000

  function row(label, branch, ahead, behind, dirty) {
    return { path: "/home/me/GitHub/" + label, name: label.split("/").pop(), owner: "GitHub", label: label,
      branch: branch, upstream: "origin/" + branch, ahead: ahead, behind: behind, dirty: dirty > 0, dirtyCount: dirty,
      affected: ahead + behind + dirty > 0, complete: true, error: "" }
  }
  readonly property var mockStatus: ({ ok: true, repoCount: 48, affectedCount: 4,
    totals: { aheadRepos: 1, behindRepos: 1, dirtyRepos: 2, failedRepos: 3, aheadCommits: 3, behindCommits: 2, dirtyFiles: 11, unavailableRepos: 0 },
    repos: [row(".dotfiles", "main", 0, 0, 1), row("Personal/foamy-kit", "main", 0, 0, 10),
      row("Personal/foamy-weather", "fix/omarchy-4.5-review", 3, 0, 0), row("Work/api-gateway", "main", 0, 2, 0)],
    sync: { syncing: false, stale: false, lastAttempt: now - 600, lastSuccess: now - 600, mode: "fetch", updated: [], skipped: [],
      failures: [
        { path: "/home/me/GitHub/Personal/foamy-kit", label: "Personal/foamy-kit", message: "No origin remote configured. Add one in lazygit, then refresh." },
        { path: "/home/me/GitHub/Work/api-gateway", label: "Work/api-gateway", message: "git@github.com: Permission denied (publickey)." },
        { path: "/home/me/GitHub/Work/infra", label: "Work/infra", message: "fatal: unable to access remote" }] } })

  Dev.Panel {
    id: panel
    parent: preview.anchorItem
    anchors.fill: parent
    bar: preview.bar
    settings: ({ repositoryFolders: [{ path: "~/GitHub", depth: 2 }, { path: "~/Work", depth: 1 }], inotifyEnabled: false, refreshIntervalSec: 0, fetchIntervalSec: 900 })
  }

  function service() {
    for (var i = 0; i < panel.children.length; i++) if (panel.children[i].helperPath !== undefined) return panel.children[i]
    return null
  }

  function findPopup() {
    var kids = FakeDisplay.overlay ? FakeDisplay.overlay.children : []
    for (var i = 0; i < kids.length; i++) if (kids[i] && kids[i].owner !== undefined && kids[i].anchorItem !== undefined) return kids[i]
    return null
  }

  Component.onCompleted: panel.open()

  // After dev's own (empty) status run, show the mock repositories.
  Timer {
    interval: 700
    running: true
    repeat: true
    onTriggered: {
      if (!preview.popup) preview.popup = preview.findPopup()
      var s = preview.service()
      if (s && s.status !== preview.mockStatus) { s.status = preview.mockStatus; s.lastChecked = preview.now }
      if (s) s.actionMessages = []
      if (preview.previewState === "settings" && !panel.editingSettings) panel.openSettings()
      if (preview.previewState !== "settings" && panel.editingSettings) panel.closeSettings()
    }
  }
}
