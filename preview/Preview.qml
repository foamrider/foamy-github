pragma ComponentBehavior: Bound
import QtQuick
import "../kit"
import ".." as Plugin
import "../Model.js" as Model

// Preview states for the foamy-kit harness (no processes, mock data only).
Item {
  id: preview
  required property QtObject bar
  required property Item anchorItem
  property var previewStates: ["repositories", "lfs-untrusted", "lfs-trusted", "clean", "clean-warning", "empty", "unavailable", "settings", "settings-first", "settings-lfs", "settings-no-lfs", "settings-error", "fetching", "pulled"]
  property string previewState: "repositories"
  property int settleMs: previewState === "settings-lfs" || previewState === "settings-no-lfs" ? 1500 : 600
  readonly property var popup: previewPopup
  readonly property double now: Date.now() / 1000
  readonly property var trusted: [
    {
      path: "/home/me/GitHub/Work/design-system",
      identity: "65:1024:1"
    },
    {
      path: "/home/me/GitHub/Work/old-site",
      identity: "65:4096:1"
    },
    {
      path: "/home/me/GitHub/Work/moved-away",
      identity: "65:8192:1"
    }
  ]

  function row(label, branch, ahead, behind, dirty, extra) {
    return Object.assign({
      path: "/home/me/GitHub/" + label,
      name: label.split("/").pop(),
      owner: "GitHub",
      label: label,
      branch: branch,
      upstream: "origin/" + branch,
      ahead: ahead,
      behind: behind,
      dirty: dirty > 0,
      dirtyCount: dirty,
      affected: ahead + behind + dirty > 0,
      complete: true,
      error: "",
      syncError: "",
      usesLfs: false,
      trusted: false
    }, extra || {})
  }

  readonly property var sample: [row(".dotfiles", "main", 0, 0, 1), row("Personal/foamy-kit", "main", 0, 0, 10, {
      syncError: "No origin remote configured. Add one in lazygit, then refresh."
    }), row("Personal/foamy-weather", "fix/omarchy-4.5-review", 3, 0, 0), row("Work/api-gateway", "feature/rate-limit-burst-handling", 0, 2, 4, {
      syncError: "git@github.com: Permission denied (publickey)."
    }), row("Work/infra", "main", 0, 0, 0, {
      syncError: "fatal: unable to access 'https://git.example.com/infra.git/': Could not resolve host: git.example.com"
    })]

  readonly property bool lfsTrusted: previewState === "lfs-trusted"
  readonly property var lfsSample: [row("Personal/game-assets", "main", 0, 0, 12, {
      usesLfs: true,
      trusted: lfsTrusted,
      identity: "65:2048:1"
    }), row(".dotfiles", "main", 0, 0, 1), row("Personal/foamy-kit", "main", 0, 0, 10, {
      syncError: "No origin remote configured. Add one in lazygit, then refresh."
    }), row("Work/api-gateway", "main", 0, 2, 0)]

  QtObject {
    id: mock
    readonly property bool lfsState: preview.previewState.indexOf("lfs-") === 0
    // "clean-warning": every repository clean while a kept failure (here the
    // watcher's) is on the warning line; the status check itself works.
    readonly property bool warned: preview.previewState === "clean-warning"
    readonly property bool clean: preview.previewState === "clean" || warned
    readonly property bool empty: preview.previewState === "empty"
    readonly property bool unavailable: preview.previewState === "unavailable"
    readonly property bool fetching: preview.previewState === "fetching"
    // After a Pull all: one repository pulled, one failed with Git's error
    // and one refused by the helper with a text that names it.
    readonly property bool pulled: preview.previewState === "pulled"
    readonly property var affectedRepos: clean || empty ? [] : unavailable ? [preview.row("Work/monorepo", "main", 0, 0, 0, {
        complete: false,
        error: "Git inspection timed out. Open the repository in lazygit and retry."
      })] : lfsState ? preview.lfsSample : preview.sample
    readonly property var status: ({
        repoCount: empty ? 0 : 48,
        affectedCount: unavailable ? 1 : clean ? 0 : 4
      })
    readonly property var totals: ({
        aheadRepos: clean || empty || unavailable ? 0 : 1,
        behindRepos: clean || empty || unavailable ? 0 : 1,
        dirtyRepos: clean || empty || unavailable ? 0 : 2,
        failedRepos: clean || empty || unavailable ? 0 : 3,
        unavailableRepos: unavailable ? 1 : 0
      })
    readonly property var sync: ({
        stale: false,
        syncing: false,
        lastSuccess: preview.now - 600,
        failures: clean || empty || unavailable ? [] : [1, 2, 3]
      })
    readonly property var refusal: Model.actionMessage({
      ok: false,
      message: "Work/docs has no upstream branch",
      key: "%1 has no upstream branch",
      values: ["Work/docs"]
    }, "")
    readonly property var pullFailures: [
      {
        label: "fatal: '/home/me/GitHub/old-host/notes.git' does not appear to be a git repository",
        values: []
      },
      refusal]
    readonly property string lastError: unavailable || warned ? "Live monitoring unavailable; using timed refresh." : pulled ? Model.messageText(pullFailures, FoamyI18n.t, FoamyI18n.tn) : ""
    readonly property double lastChecked: preview.now
    readonly property string actionStatus: pulled ? FoamyI18n.tn("Pulled %1 repositories", 1) : ""
    readonly property bool syncing: fetching
    readonly property bool refreshing: false
    readonly property bool repositoryActionRunning: false
    readonly property bool busy: fetching
    // Every repository found (Settings lists only those using LFS: neither
    // .dotfiles nor Work/old-site, which stopped using LFS but keeps its trust record).
    // "settings-first" is a new user's: no folders saved, so none.
    readonly property var repos: preview.previewState === "settings-first" ? [] : preview.previewState === "settings-no-lfs" ? preview.sample : [preview.row(".dotfiles", "main", 0, 0, 1, {
        identity: "65:512:1"
      }), preview.row("Personal/game-assets", "main", 0, 0, 12, {
        usesLfs: true,
        trusted: false,
        identity: "65:2048:1"
      }), preview.row("Work/design-system", "main", 0, 0, 0, {
        usesLfs: true,
        trusted: true,
        identity: "65:1024:1"
      }), preview.row("Work/old-site", "main", 0, 0, 0, {
        trusted: true,
        identity: "65:4096:1"
      })]
    readonly property var lfsRepos: Model.lfsRepos(repos)
    readonly property var pullableRepos: Model.pullableRepos(affectedRepos)
    readonly property bool pullingAll: false
    function fetch() {
    }
    function pullAll() {
    }
    function pushRepository(repo) {
    }
    function pullRepository(repo) {
    }
  }

  FoamyPopup {
    id: previewPopup
    anchorItem: preview.anchorItem
    bar: preview.bar
    open: true
    body: Component {
      Plugin.GithubView {
        service: mock
        settings: ({
            repositoryFolders: preview.previewState === "settings-first" ? [] : [
              {
                path: "~/GitHub",
                depth: 2
              },
              {
                path: "~/Work",
                depth: 1
              }
            ],
            inotifyEnabled: true,
            refreshIntervalSec: 30,
            fetchIntervalSec: 900,
            trustedRepositories: preview.trusted
          })
        opened: true
        editingSettings: preview.previewState.indexOf("settings") === 0
        settingsError: preview.previewState === "settings-error" ? "Couldn't save the setting." : ""
        retryable: preview.previewState === "settings-error"
      }
    }
  }

  // Open the first row's Git LFS note, with the keyboard cursor on its LFS
  // button; "fetching": the cursor on the footer's spinning Refresh;
  // Settings: the folder rows of this state's settings, which the page
  // loads only as it opens (restarted on every state: the body is kept
  // between states).
  Timer {
    id: lfsTimer
    interval: 300
    running: !!preview.popup.bodyItem
    onTriggered: {
      preview.popup.bodyItem.clearCursor()
      if (preview.previewState === "lfs-untrusted")
        preview.popup.bodyItem.showLfsInfo(0)
      if (preview.previewState === "fetching")
        preview.popup.bodyItem.showRefreshCursor()
      if (preview.previewState.indexOf("settings") === 0)
        preview.popup.bodyItem.loadFolders()
    }
  }
  // Settings → the end of the page (Git LFS, when shown), once the card has its height.
  Timer {
    interval: 900
    running: !!preview.popup.bodyItem && (preview.previewState === "settings-lfs" || preview.previewState === "settings-no-lfs")
    onTriggered: preview.popup.bodyItem.showSettingsEnd()
  }
  onPreviewStateChanged: if (preview.popup.bodyItem) {
    preview.popup.bodyItem.clearCursor()
    lfsTimer.restart()
  }
}
