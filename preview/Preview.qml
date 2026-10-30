import QtQuick
import "../kit"
import ".." as Plugin

// Preview states for the foamy-kit harness (no processes, mock data only).
Item {
  id: preview
  required property QtObject bar
  required property Item anchorItem
  property var previewStates: ["repositories", "clean", "empty", "unavailable", "settings", "settings-error"]
  property string previewState: "repositories"
  readonly property Item popup: popup
  readonly property double now: Date.now() / 1000

  function row(label, branch, ahead, behind, dirty, extra) {
    return Object.assign({ path: "/home/me/GitHub/" + label, name: label.split("/").pop(), owner: "GitHub", label: label,
      branch: branch, upstream: "origin/" + branch, ahead: ahead, behind: behind, dirty: dirty > 0, dirtyCount: dirty,
      affected: ahead + behind + dirty > 0, complete: true, error: "", syncError: "" }, extra || {})
  }

  readonly property var sample: [
    row(".dotfiles", "main", 0, 0, 1),
    row("Personal/foamy-kit", "main", 0, 0, 10, { syncError: "No origin remote configured. Add one in lazygit, then refresh." }),
    row("Personal/foamy-weather", "fix/omarchy-4.5-review", 3, 0, 0),
    row("Work/api-gateway", "feature/rate-limit-burst-handling", 0, 2, 4, { syncError: "git@github.com: Permission denied (publickey)." }),
    row("Work/infra", "main", 0, 0, 0, { syncError: "fatal: unable to access 'https://git.example.com/infra.git/': Could not resolve host: git.example.com" })
  ]

  QtObject {
    id: mock
    readonly property bool clean: preview.previewState === "clean"
    readonly property bool empty: preview.previewState === "empty"
    readonly property bool unavailable: preview.previewState === "unavailable"
    readonly property var affectedRepos: clean || empty ? [] : unavailable
      ? [preview.row("Work/monorepo", "main", 0, 0, 0, { complete: false, error: "Git inspection timed out. Open the repository in lazygit and retry." })]
      : preview.sample
    readonly property var status: ({ repoCount: empty ? 0 : 48, affectedCount: unavailable ? 1 : clean ? 0 : 4 })
    readonly property var totals: ({ aheadRepos: clean || empty || unavailable ? 0 : 1, behindRepos: clean || empty || unavailable ? 0 : 1,
      dirtyRepos: clean || empty || unavailable ? 0 : 2, failedRepos: clean || empty || unavailable ? 0 : 3, unavailableRepos: unavailable ? 1 : 0 })
    readonly property var sync: ({ stale: false, syncing: false, lastSuccess: preview.now - 600, failures: clean || empty || unavailable ? [] : [1, 2, 3] })
    readonly property string lastError: unavailable ? "Live monitoring unavailable; using timed refresh." : ""
    readonly property double lastChecked: preview.now
    readonly property string actionStatus: ""
    readonly property bool syncing: false
    readonly property bool refreshing: false
    readonly property bool repositoryActionRunning: false
    readonly property bool busy: false
    function fetch() {}
    function pushRepository(repo) {}
    function pullRepository(repo) {}
  }

  FoamyPopup {
    id: popup
    anchorItem: preview.anchorItem
    bar: preview.bar
    open: true
    body: Component {
      Plugin.GithubView {
        service: mock
        settings: ({ repositoryFolders: [{ path: "~/GitHub", depth: 2 }, { path: "~/Work", depth: 1 }], inotifyEnabled: true,
          refreshIntervalSec: 30, fetchIntervalSec: 900, trustRepositoryConfig: false })
        editingSettings: preview.previewState.indexOf("settings") === 0
        settingsError: preview.previewState === "settings-error" ? "Couldn't save the setting." : ""
        retryable: preview.previewState === "settings-error"
      }
    }
  }
}
