#!/usr/bin/env python3
"""The Service's helper processes across unload, trust edits and IPC, end
to end: the real Panel.qml and Service in an isolated offscreen Quickshell
(foamy-kit's harness) with a fake helper and a fake NetworkManager on the
run's own system bus.

  python3 tests/runtime-lifecycle.py

The fake helper logs every start, SIGTERM and end to calls.log, with the
trust it was given, and holds each subcommand named in the `block` file
until the test removes it (at most 30 s), and fails each subcommand named
in the `fail` file (fetch with only stderr, any other with a JSON reply). A
push or pull answers with the JSON in the `reply` file when it has one. Its
status reports one
repository using Git LFS, trusted when its trust arguments name anything
and labelled "other" when its folders name /foamy-other; its pick-folder
answers /foamy-picked.

Scenes, one shell, each step waiting on an event (a timeout fails the run):
- unload: a status check, a fetch and a push still running when the last
  bar goes are stopped (the helper got SIGTERM) and leave no error and no
  action text on the shared Service.
- trust: revoking trust (an entry removed) stops a running status check
  and fetch that were started with it; the check is redone and the fetch
  runs again with the new trust, with no error. Granting trust stops
  neither, and the check started before the grant is never shown.
- folders: a check that a folder change overtook is never shown; the new
  folders are checked instead.
- lfs: the row's LFS button, its note and "Trust this repository", with
  the keys: the identity is checked again and the trust saved.
- chooser: a folder picked in the folder chooser fills the row and is saved.
- errors: a failed push and a failed fetch stay on the warning line through
  the status refresh that follows them; the next fetch clears the failure,
  and so does the last bar's unload. A failed status check shows its own
  failure, also over a kept push failure, until a status check works.
- language: a push the helper refuses with a text that names the
  repository, and a push whose text names its commit count, show in the
  panel's language (Norwegian, switched live); the kept failure follows a
  switch back to English.
- footer: with no folders, the text under the empty list stays "No
  repositories yet" while a failed fetch is kept on the warning line, and
  says the status is unavailable only when the status check fails; the
  failure on the warning line reads at 4.5:1 (the run's theme has a dim
  red).
- ipc: `qs ipc call foamy.github refresh` answers "ok" when a fetch
  started, "busy" while one runs, "offline" without the network and
  "unavailable" once the helper exits 127, which the panel then says;
  `status` answers the keys users' scripts read.

Timed polling only (no live monitoring); temp HOME; no network. Requires
~/GitHub/Personal/foamy-kit (or FOAMY_KIT).

Runs on Omarchy 4.5: the installed /usr/share/omarchy/shell, or the tree in
FOAMY_HOST_45."""

import atexit
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

KIT = Path(os.environ.get("FOAMY_KIT", Path.home() / "GitHub/Personal/foamy-kit"))
sys.path.insert(0, str(KIT / "preview"))
import harness  # noqa: E402
import hostenv  # noqa: E402
import fake_nm  # noqa: E402

PLUGIN = Path(__file__).resolve().parents[1]
REPO = "/foamy-test/repo"
# What the fake helper says when a push or a fetch fails (Git diagnostics,
# which pass through the catalogs as they are). Their paths hold "{length}"
# and "{constructor}", which a translation with values would fill in.
PUSH_FAILURE = "error: failed to push some refs to /srv/{length}/{constructor}/repo.git"
FETCH_FAILURE = "fatal: unable to access /srv/{length}/{constructor}/origin.git"
# Two replies of the real helper, whose texts name the repository ("repo")
# and a commit count, and what the panel shows for each.
CATALOGS = {language: json.loads((PLUGIN / "i18n" / f"{language}.json").read_text()) for language in ("en", "no")}
REFUSED_KEY = "%1 has remote changes; pull or rebase first"
PUSHED_KEY = "Pushed %1 commits for %2"
REPLIES = {
    "refused": {
        "reply": {
            "ok": False,
            "operation": "push",
            "message": "repo has remote changes; pull or rebase first",
            "key": REFUSED_KEY,
            "values": ["repo"],
        },
        "en": CATALOGS["en"][REFUSED_KEY].replace("%1", "repo"),
        "no": CATALOGS["no"][REFUSED_KEY].replace("%1", "repo"),
    },
    "pushed": {
        "reply": {
            "ok": True,
            "operation": "push",
            "message": "Pushed 1 commit for repo",
            "key": PUSHED_KEY,
            "values": [1, "repo"],
            "count": 1,
        },
        "no": CATALOGS["no"][PUSHED_KEY]["one"].replace("%1", "1").replace("%2", "repo"),
    },
}
for entry in REPLIES.values():
    entry["reply"] = json.dumps(entry["reply"], separators=(",", ":"))

HELPER = r"""#!/usr/bin/env bash
# Fake foamy-github-helper (see tests/runtime-lifecycle.py).
state=@STATE@
sub=${1-}
folders=${3-}
trust=""
args=("$@")
for ((i = 0; i < ${#args[@]}; i++)); do
  case "${args[i]}" in
    --trusted-json) trust=${args[i + 1]-} ;;
    --trust-repository-config) trust='*' ;;
  esac
done
[ -e "$state/fail127" ] && exit 127
echo "start $sub $$ $trust" >> "$state/calls.log"
trap 'echo "term $sub $$" >> "$state/calls.log"; exit 143' TERM
case "$sub" in
  identity) echo "1:2:3|1:2:3"; exit 0 ;;
  pick-folder) printf '{"ok":true,"path":"/foamy-picked"}\n'; exit 0 ;;
  watch) printf '{"ok":true,"repoCount":0,"affectedCount":0,"repos":[]}\n'; exec cat > /dev/null ;;
esac
# Before the settings arrive the Service runs with no folders: never held.
if [ "$folders" != "[]" ]; then
  for ((i = 0; i < 600; i++)); do grep -qx "$sub" "$state/block" 2>/dev/null || break; sleep 0.05; done
fi
echo "end $sub $$" >> "$state/calls.log"
case "$sub" in
  push | pull)
    if [ -s "$state/reply" ]; then
      cat "$state/reply"
      grep -q '"ok":true' "$state/reply"
      exit $?
    fi
    ;;
esac
if grep -qx "$sub" "$state/fail" 2>/dev/null; then
  case "$sub" in
    fetch) echo "@FETCH_FAILURE@" >&2 ;;
    *) printf '{"ok":false,"operation":"%s","message":"@PUSH_FAILURE@"}\n' "$sub" ;;
  esac
  exit 1
fi
case "$sub" in
  status)
    if [ "$folders" = "[]" ]; then
      printf '{"ok":true,"repoCount":0,"affectedCount":0,"repos":[]}\n'
      exit 0
    fi
    trusted=false
    [ -n "$trust" ] && [ "$trust" != "[]" ] && trusted=true
    label=repo
    case "$folders" in *foamy-other*) label=other ;; esac
    printf '{"ok":true,"repoCount":1,"affectedCount":1,"totals":{"dirtyRepos":1},"repos":[{"path":"@REPO@","name":"%s","owner":"","label":"%s","branch":"main","upstream":"origin/main","ahead":1,"behind":0,"dirty":true,"dirtyCount":1,"affected":true,"complete":true,"error":"","usesLfs":true,"trusted":%s,"identity":"1:2:3|1:2:3","syncError":""}],"sync":{"stale":false}}\n' "$label" "$label" "$trusted"
    ;;
  fetch) printf '{"ok":true,"failures":[]}\n' ;;
  *) printf '{"ok":true,"message":"done"}\n' ;;
esac
"""

SHELL = r"""
import QtQuick
import Quickshell
import Quickshell.Io
import "harness"
import "plugin" as Plugin
import "plugin/kit" as Kit

ShellRoot {
  id: shell
  property var panel: null
  property int checks: 0
  readonly property string state: "@STATE@"
  readonly property var service: Plugin.Service
  function check(ok, what) { checks++; if (!ok) console.log("FOAMY FAIL " + what) }

  FloatingWindow {
    implicitWidth: 800; implicitHeight: 600
    Item { id: host; width: 800; height: 600 }
  }
  Component { id: barComponent; MockBar { barSize: 34 } }
  FoamyWait { id: wait }

  FileView { id: callsFile; path: shell.state + "/calls.log"; blockLoading: true; printErrors: false }
  FileView { id: blockFile; path: shell.state + "/block"; printErrors: false }
  function calls() { callsFile.reload(); return String(callsFile.text() || "").split("\n").filter(function(l) { return l !== "" }) }
  // Lines of calls.log that start with `prefix` ("start fetch", "term status"…).
  function count(prefix) { return calls().filter(function(l) { return l.indexOf(prefix + " ") === 0 }).length }
  // The pid of the last calls.log line that starts with `prefix`.
  function lastPid(prefix) {
    var lines = calls().filter(function(l) { return l.indexOf(prefix + " ") === 0 })
    return lines.length ? lines[lines.length - 1].split(" ")[2] : ""
  }
  // A status that must never be shown while staleTest is set.
  property var staleTest: null
  property bool staleShown: false
  Connections {
    target: Plugin.Service
    function onStatusChanged() { if (shell.staleTest && shell.staleTest()) shell.staleShown = true }
  }
  function watchStale(test) { staleShown = false; staleTest = test }
  // Fetches started with no trusted repository.
  function untrustedFetches() { return calls().filter(function(l) { return /^start fetch [0-9]+ \[\]$/.test(l) }).length }
  // Holds `names` (the helper polls the file); with `then`, once the file
  // says so (FileView writes asynchronously, and a job started before that
  // would not be held).
  FileView { id: blockRead; path: shell.state + "/block"; blockLoading: true; printErrors: false }
  function block(names, then) {
    var text = names.join("\n") + "\n"
    blockFile.setText(text)
    if (then) until(function() { blockRead.reload(); return blockRead.text() === text }, "the block file is written", then)
  }

  // Settings as Omarchy hands them over (assigned, key order fixed).
  function settingsWith(trusted, folders) {
    return { repositoryFolders: folders || [{ depth: 1, path: "/foamy-test" }], inotifyEnabled: false,
             refreshIntervalSec: 0, fetchIntervalSec: 86400, trustedRepositories: trusted }
  }
  readonly property var trustRepo: [{ path: "@REPO@", identity: "1:2:3|1:2:3" }]
  function newPanel(trusted) {
    var component = Qt.createComponent(Qt.resolvedUrl("plugin/Panel.qml"))
    if (component.status !== Component.Ready) { console.log("FOAMY FAIL load " + component.errorString()); Qt.quit(); return }
    var bar = barComponent.createObject(shell, { moduleName: "foamy.github" })
    panel = component.createObject(host, { bar: bar, width: 40, height: 34 })
    panel.settings = settingsWith(trusted)
  }
  function dropPanel() { panel.destroy(); panel = null }

  // Steps run in order; each gets `next`.
  property int at: 0
  function go() {
    if (at >= steps.length) {
      console.log("FOAMY CHECKS " + checks)
      console.log("FOAMY DONE")
      Qt.quit()
      return
    }
    steps[at++](go)
  }
  function until(condition, label, then, timeoutMs) { wait.waitFor(condition, timeoutMs || 15000, function() { then() }, label) }
  // A new panel whose first scan and automatic fetch (2 s after the network
  // is ready) are done.
  function settled(trusted, then) {
    var fetches = count("end fetch")
    newPanel(trusted)
    until(function() {
      return service.networkReady && service.repos.length === 1 && count("end fetch") > fetches && !service.busy
    }, "a new panel settles", then)
  }

  readonly property var steps: [].concat(unloadSteps, trustSteps, folderSteps, lfsSteps, chooserSteps, errorSteps, languageSteps, footerSteps, ipcSteps)
  function collect(item, test) {
    var found = item && test(item) ? [item] : []
    var kids = item ? item.children || [] : []
    for (var i = 0; i < kids.length; i++) found = found.concat(collect(kids[i], test))
    return found
  }
  function named(item, name) { return collect(item, function(i) { return i.objectName === name })[0] }
  // The view's first repository row. The harness's popup has no window, so
  // its list never runs its own layout pass: run it, as a shown window would.
  function firstRow(view) {
    collect(view, function(i) { return typeof i.forceLayout === "function" && i.cacheBuffer !== undefined })
      .forEach(function(list) { list.forceLayout() })
    return collect(view, function(i) { return i.repo !== undefined && i.rowStop !== undefined && !!i.repo })[0]
  }

  // unload: no error is left on the shared Service by a job the last bar's
  // unload stopped (its exit handler ran after `active` went false).
  readonly property var unloadSteps: [
    function(next) {
      block(["status"], function() {
        var started = count("start status")
        newPanel([])
        until(function() { return service.refreshing && count("start status") > started && service.scannedFolders !== "[]" },
          "unload: the first scan runs", next)
      })
    },
    function(next) {
      var stopped = count("term status")
      dropPanel()
      until(function() { return !service.active && !service.refreshing && count("term status") > stopped },
        "unload: the scan is stopped", function() {
          check(service.operationError === "", "unload: a stopped scan leaves no error: " + service.operationError)
          check(service.status.ok !== false, "unload: a stopped scan leaves no unavailable status")
          block([])
          next()
        })
    },
    function(next) { settled([], next) },
    function(next) {
      block(["fetch"], function() {
        var started = count("start fetch")
        service.fetch()
        until(function() { return service.syncing && count("start fetch") > started }, "unload: the fetch runs", next)
      })
    },
    function(next) {
      var stopped = count("term fetch")
      dropPanel()
      until(function() { return !service.active && !service.syncing && count("term fetch") > stopped }, "unload: the fetch is stopped", function() {
        check(service.operationError === "", "unload: a stopped fetch leaves no error: " + service.operationError)
        check(service.actionStatus === "", "unload: a stopped fetch leaves no action text: " + service.actionStatus)
        block([])
        next()
      })
    },
    function(next) { settled([], next) },
    function(next) {
      block(["push"], function() {
        var started = count("start push")
        service.pushRepository(service.repos[0])
        until(function() { return service.repositoryActionRunning && count("start push") > started }, "unload: the push runs", next)
      })
    },
    function(next) {
      var stopped = count("term push")
      dropPanel()
      until(function() { return !service.active && !service.repositoryActionRunning && count("term push") > stopped }, "unload: the push is stopped", function() {
        check(service.operationError === "", "unload: a stopped push leaves no error: " + service.operationError)
        check(service.actionStatus === "", "unload: a stopped push leaves no action text: " + service.actionStatus)
        block([])
        next()
      })
    }
  ]

  // trust: a revocation stops the running check and fetch that still have
  // the revoked entry; a grant lets them run on.
  readonly property var trustSteps: [
    function(next) { settled(trustRepo, next) },
    function(next) {
      block(["status"], function() {
        var started = count("start status")
        service.refresh()
        until(function() { return service.refreshing && count("start status") > started && service.scannedTrust.indexOf("@REPO@") >= 0 },
          "trust: a check with the trusted repository runs", next)
      })
    },
    function(next) {
      var stopped = count("term status")
      panel.settings = settingsWith([])
      until(function() { return count("term status") > stopped }, "trust: revoking stops the running check", function() {
        block([])
        until(function() { return !service.refreshing && service.repos.length === 1 && service.repos[0].trusted === false },
          "trust: the check is redone without the trust", function() {
            check(service.operationError === "", "trust: the stopped check leaves no error: " + service.operationError)
            next()
          })
      }, 5000)
    },
    function(next) {
      block(["status"], function() {
        var started = count("start status")
        service.refresh()
        until(function() { return service.refreshing && count("start status") > started && service.scannedTrust === "[]" },
          "trust: a check without trust runs", next)
      })
    },
    function(next) {
      // The running check ends on its own; its untrusted result is never
      // shown, and the check is redone with the trust.
      var pid = lastPid("start status")
      panel.settings = settingsWith(trustRepo)
      watchStale(function() { return service.repos.length === 1 && service.repos[0].trusted === false })
      block([])
      // The held check ends on its own: a stopped one logs "term", never "end".
      until(function() {
        return !service.refreshing && service.repos.length === 1 && service.repos[0].trusted === true
          && calls().indexOf("end status " + pid) >= 0
      }, "trust: the granted trust is shown, and the running check ended on its own", function() {
          check(calls().indexOf("term status " + pid) < 0, "trust: granting does not stop the running check")
          check(!shell.staleShown, "trust: the check started before the grant is never shown")
          watchStale(null)
          next()
        })
    },
    function(next) {
      block(["fetch"], function() {
        var started = count("start fetch")
        service.fetch()
        until(function() { return service.syncing && count("start fetch") > started }, "trust: a fetch with the trust runs", next)
      })
    },
    function(next) {
      var stopped = count("term fetch")
      var ends = count("end fetch")
      var untrusted = untrustedFetches()
      panel.settings = settingsWith([])
      until(function() { return count("term fetch") > stopped }, "trust: revoking stops the running fetch", function() {
        until(function() { return untrustedFetches() > untrusted }, "trust: the fetch runs again without the trust", function() {
          block([])
          until(function() { return !service.busy && count("end fetch") > ends }, "trust: the new fetch ends", function() {
            check(service.operationError === "", "trust: the stopped fetch leaves no error: " + service.operationError)
            next()
          })
        })
      }, 5000)
    },
    function(next) {
      block(["fetch"], function() {
        var started = count("start fetch")
        service.fetch()
        until(function() { return service.syncing && count("start fetch") > started }, "trust: a fetch without trust runs", next)
      })
    },
    function(next) {
      // A revocation would stop the fetch at once, while it is still held.
      var pid = lastPid("start fetch")
      var started = count("start fetch")
      panel.settings = settingsWith(trustRepo)
      block([])
      var seen = []
      until(function() { seen = calls(); return !service.busy && seen.indexOf("end fetch " + pid) >= 0 }, "trust: the fetch ends on its own", function() {
        check(seen.indexOf("term fetch " + pid) < 0 && seen.filter(function(l) { return l.indexOf("start fetch ") === 0 }).length === started,
          "trust: granting does not stop or restart the fetch")
        check(service.operationError === "", "trust: a fetch that a grant overlapped ends without error: " + service.operationError)
        next()
      })
    }
  ]

  // folders: a check that a folder change overtook is not shown.
  readonly property var otherFolders: [{ depth: 1, path: "/foamy-other" }]
  readonly property var folderSteps: [
    function(next) {
      block(["status"], function() {
        var started = count("start status")
        service.refresh()
        until(function() { return service.refreshing && count("start status") > started && service.scannedFolders.indexOf("/foamy-test") >= 0 },
          "folders: a check of the first folder runs", next)
      })
    },
    function(next) {
      var started = count("start status")
      panel.settings = settingsWith(trustRepo, otherFolders)
      watchStale(function() { return service.repos.length === 1 && service.repos[0].label === "repo" })
      block([])
      // calls.log is read as of each poll: count the redone check there.
      var redone = 0
      until(function() {
        redone = count("start status") - started
        return !service.refreshing && service.repos.length === 1 && service.repos[0].label === "other" && redone > 0
      }, "folders: the new folders are checked", function() {
          check(!shell.staleShown, "folders: the check a folder change overtook is never shown")
          check(redone === 1, "folders: the check is redone once: " + redone)
          watchStale(null)
          next()
        })
    },
    function(next) {
      panel.settings = settingsWith(trustRepo)
      until(function() { return !service.busy && service.repos.length === 1 && service.repos[0].label === "repo" }, "folders: back to the first folder", next)
    }
  ]

  // lfs: an untrusted repository using Git LFS is trusted from its row,
  // with the keys: LFS button, its note, "Trust this repository".
  readonly property var lfsSteps: [
    function(next) {
      panel.settings = settingsWith([])
      panel.open()
      until(function() {
        return panel.popup.bodyItem !== null && !service.busy && service.repos.length === 1 && service.repos[0].trusted === false
      }, "lfs: the popup lists the untrusted repository", next)
    },
    function(next) {
      var view = panel.popup.bodyItem
      var row = firstRow(view)
      var lfs = named(view, "githubLfsButton")
      view.moveRequested(0, 1)
      check(row && row.hasCursor, "lfs: Down: the row")
      view.moveRequested(0, 1)
      check(lfs && lfs.hasCursor, "lfs: Down: its LFS button")
      view.activateRequested()
      check(view.lfsNotePath === "@REPO@", "lfs: Enter opens the note: " + view.lfsNotePath)
      view.moveRequested(0, 1)
      var trust = named(view, "githubTrustButton")
      check(trust && trust.hasCursor, "lfs: Down: Trust this repository")
      var saved = panel.bar.saved.length
      var identities = count("start identity")
      view.activateRequested()
      until(function() { return count("start identity") > identities && panel.bar.saved.length > saved }, "lfs: the trust is saved", function() {
        var entry = panel.bar.saved[panel.bar.saved.length - 1]
        var last = entry ? entry.settings.trustedRepositories : null
        check(JSON.stringify(last) === JSON.stringify(trustRepo), "lfs: the repository is trusted with its identity: " + JSON.stringify(last))
        check(view.lfsNotePath === "", "lfs: the note closes")
        check(firstRow(view).hasCursor, "lfs: the cursor is back on the row")
        panel.close()
        panel.settings = settingsWith(trustRepo)
        until(function() { return !panel.opened && !service.busy && service.repos[0].trusted === true }, "lfs: the popup closes", next)
      })
    }
  ]

  // chooser: the folder the desktop's chooser returns fills the row and is saved.
  readonly property var chooserSteps: [
    function(next) {
      panel.openSettings()
      until(function() { return panel.popup.bodyItem !== null && panel.editingSettings }, "chooser: settings open", function() {
        var saved = panel.bar.saved.length
        panel.popup.bodyItem.browseRequested(0)
        check(!panel.opened, "chooser: the popup is released for the chooser")
        until(function() { return panel.opened && panel.editingSettings && !panel.returningFromBrowser && panel.bar.saved.length > saved },
          "chooser: the chosen folder is saved", function() {
            var entry = panel.bar.saved[panel.bar.saved.length - 1]
            var folders = entry ? entry.settings.repositoryFolders : null
            check(Array.isArray(folders) && JSON.stringify(folders) === JSON.stringify([{ path: "/foamy-picked", depth: 1 }]),
              "chooser: the picked folder replaces the row's path: " + JSON.stringify(folders) + " " + panel.settingsError)
            panel.close()
            panel.settings = settingsWith(trustRepo)
            until(function() { return !panel.opened && !service.busy }, "chooser: the popup closes", next)
          })
      })
    }
  ]

  // errors: what the warning line says after a failed push or fetch.
  FileView { id: actionFailFile; path: shell.state + "/fail"; printErrors: false }
  FileView { id: actionFailRead; path: shell.state + "/fail"; blockLoading: true; printErrors: false }
  // Fails `names` from the next helper start on, once the file says so.
  function failing(names, then) {
    var text = names.join("\n") + "\n"
    actionFailFile.setText(text)
    until(function() { actionFailRead.reload(); return actionFailRead.text() === text }, "the fail file is written", then)
  }
  readonly property string pushFailure: "@PUSH_FAILURE@"
  readonly property string fetchFailure: "@FETCH_FAILURE@"
  // The fake helper's failed status has no stderr: the Service's own text.
  readonly property string statusFailure: "Repository status failed"
  // A status check with `names` failing, then `then` once it has ended.
  function refreshFailing(names, label, then) {
    failing(names, function() {
      var statuses = count("end status")
      service.refresh()
      until(function() { return !service.busy && count("end status") > statuses }, label, then)
    })
  }
  readonly property var errorSteps: [
    function(next) { failing(["push", "fetch"], function() { until(function() { return !service.busy }, "errors: idle", next) }) },
    function(next) {
      var statuses = count("end status")
      service.pushRepository(service.repos[0])
      check(service.repositoryActionRunning, "errors: the push starts")
      until(function() { return !service.busy && count("end status") > statuses }, "errors: the push and the status refresh after it end", function() {
        check(service.operationError === shell.pushFailure, "errors: the failed push is kept: " + service.operationError)
        check(service.lastError === shell.pushFailure, "errors: the warning line still shows the failed push: " + service.lastError)
        check(service.status.ok === true && service.repos.length === 1, "errors: the refresh after the push was shown")
        next()
      })
    },
    function(next) {
      refreshFailing(["push", "fetch", "status"], "errors: the failed status check ends", function() {
        check(service.status.ok === false && service.lastError === shell.statusFailure,
          "errors: a newer failed status check shows over the kept push failure: " + service.lastError)
        check(service.operationError === shell.pushFailure, "errors: the push failure is still kept: " + service.operationError)
        refreshFailing(["push", "fetch"], "errors: the status check works again", function() {
          check(service.status.ok === true && service.lastError === shell.pushFailure,
            "errors: the kept push failure shows again once a status check works: " + service.lastError)
          next()
        })
      })
    },
    function(next) {
      var statuses = count("end status")
      check(service.fetch() === "ok", "errors: a fetch starts")
      check(service.operationError === "", "errors: a new fetch clears the earlier failure: " + service.operationError)
      until(function() { return !service.busy && count("end status") > statuses }, "errors: the fetch and the status refresh after it end", function() {
        check(service.lastError === shell.fetchFailure, "errors: the warning line still shows the failed fetch: " + service.lastError)
        next()
      })
    },
    function(next) {
      failing(["push"], function() {
        var ends = count("end fetch")
        check(service.fetch() === "ok", "errors: a second fetch starts")
        until(function() { return !service.busy && count("end fetch") > ends }, "errors: the second fetch ends", function() {
          check(service.operationError === "" && service.lastError === "", "errors: a fetch that works leaves no failure: " + service.lastError)
          next()
        })
      })
    },
    function(next) {
      refreshFailing(["push", "status"], "errors: a status check fails with no other failure", function() {
        check(service.status.ok === false && service.lastError === shell.statusFailure,
          "errors: the warning line shows the failed status check: " + service.lastError)
        refreshFailing(["push"], "errors: the status check works after its failure", function() {
          check(service.status.ok === true && service.lastError === "", "errors: a status check that works removes its failure: " + service.lastError)
          next()
        })
      })
    },
    function(next) {
      service.pushRepository(service.repos[0])
      until(function() { return !service.busy && service.operationError === shell.pushFailure }, "errors: the push fails again", function() {
        dropPanel()
        until(function() { return !service.active }, "errors: the last bar goes", function() {
          check(service.operationError === "" && service.lastError === "", "errors: the last bar's unload clears the failure: " + service.lastError)
          failing([], function() { settled(trustRepo, next) })
        })
      })
    }
  ]

  // language: the helper's texts that name the repository or a commit
  // count, in the panel's language.
  FileView { id: languageFile; path: Quickshell.env("OMARCHY_LANGUAGE_FILE"); printErrors: false }
  FileView { id: replyFile; path: shell.state + "/reply"; printErrors: false }
  FileView { id: replyRead; path: shell.state + "/reply"; blockLoading: true; printErrors: false }
  readonly property var replies: @REPLIES@
  // The fake's push and pull answer with `text` ("": as before), once the file says so.
  function replying(text, then) {
    replyFile.setText(text)
    until(function() { replyRead.reload(); return replyRead.text() === text }, "the reply file is written", then)
  }
  function speaking(language, then) {
    languageFile.setText(language + "\n")
    until(function() { return Kit.FoamyI18n.language === language }, "the panel speaks " + language, then)
  }
  readonly property var languageSteps: [
    function(next) {
      speaking("no", function() {
        replying(shell.replies.refused.reply, function() {
          var statuses = count("end status")
          service.pushRepository(service.repos[0])
          until(function() { return !service.busy && count("end status") > statuses }, "language: the refused push and the status refresh after it end", function() {
            check(service.lastError === shell.replies.refused.no, "language: the refused push names the repository in Norwegian: " + service.lastError)
            speaking("en", function() {
              check(service.lastError === shell.replies.refused.en, "language: the kept failure follows the language: " + service.lastError)
              next()
            })
          })
        })
      })
    },
    function(next) {
      speaking("no", function() {
        replying(shell.replies.pushed.reply, function() {
          var ends = count("end push")
          service.pushRepository(service.repos[0])
          // The title shows it for 5 s from the push's end.
          until(function() { return !service.repositoryActionRunning && count("end push") > ends }, "language: the push ends", function() {
            check(service.actionStatus === shell.replies.pushed.no && service.lastError === "",
              "language: the push's commit count and repository in Norwegian: " + service.actionStatus + " | " + service.lastError)
            replying("", function() { speaking("en", function() { until(function() { return !service.busy }, "language: idle", next) }) })
          })
        })
      })
    }
  ]

  // footer: the text under an empty list (no folders: the status check
  // works and finds no repository).
  function emptyListText() { var text = named(panel.popup.bodyItem, "githubEmptyList"); return text ? text.text : "(none)" }
  readonly property string noRepositories: "No repositories yet. Add a folder in Settings."
  readonly property var footerSteps: [
    function(next) {
      panel.settings = settingsWith(trustRepo, [])
      panel.open()
      until(function() {
        return panel.popup.bodyItem !== null && !service.busy && service.scannedFolders === "[]" && service.status.repoCount === 0
      }, "footer: no repository is found", function() {
        check(emptyListText() === shell.noRepositories, "footer: no repository yet: " + emptyListText())
        failing(["fetch"], function() {
          var statuses = count("end status")
          check(service.fetch() === "ok", "footer: a fetch starts")
          until(function() { return !service.busy && count("end status") > statuses }, "footer: the failed fetch and the status refresh after it end", function() {
            check(service.status.ok === true && service.lastError === shell.fetchFailure, "footer: the failed fetch is kept: " + service.lastError)
            check(emptyListText() === shell.noRepositories, "footer: a kept fetch failure is no failed status check: " + emptyListText())
            next()
          })
        })
      })
    },
    function(next) {
      refreshFailing(["fetch", "status"], "footer: the status check fails", function() {
        check(service.status.ok === false && emptyListText() === "Repository status is unavailable. Try Refresh again.",
          "footer: a failed status check says so: " + emptyListText())
        // The run's theme has a dim red: the warning line still reads.
        var palette = Kit.FoamyPalette
        var warning = named(panel.popup.bodyItem, "githubStatusWarning")
        check(palette.contrastRatio(palette.error, palette.background) < 4.5, "footer: the theme's red is dim: " + palette.error)
        check(warning && warning.text === shell.statusFailure
              && palette.contrastRatio(warning.color, palette.background) >= 4.5,
          "footer: the failed status check reads on the popup: " + (warning && warning.text) + " in " + (warning && warning.color)
          + " at " + (warning && palette.contrastRatio(warning.color, palette.background).toFixed(2)) + ":1")
        failing([], function() {
          panel.close()
          panel.settings = settingsWith(trustRepo)
          until(function() { return !panel.opened && !service.busy && service.repos.length === 1 }, "footer: the repository is back", next)
        })
      })
    }
  ]

  // ipc: the real IPC target, called the way omarchy-shell does.
  FileView { id: connectivityFile; path: shell.state + "/connectivity"; printErrors: false }
  Process {
    id: ipcCall
    property var then: null
    property string name: "refresh"
    command: ["qs", "ipc", "-n", "-p", decodeURIComponent(String(Qt.resolvedUrl(".")).replace(/^file:\/\//, "")),
              "call", "foamy.github", name]
    stdout: StdioCollector { id: ipcOutput; waitForEnd: true }
    onExited: function(exitCode) { var then = ipcCall.then; ipcCall.then = null; then(exitCode === 0 ? String(ipcOutput.text).trim() : "exit " + exitCode) }
  }
  function refreshOverIpc(then) { callIpc("refresh", then) }
  function callIpc(name, then) { ipcCall.name = name; ipcCall.then = then; ipcCall.running = true }
  readonly property var ipcSteps: [
    function(next) {
      until(function() { return !service.busy }, "ipc: idle", function() {
        var started = count("start fetch")
        refreshOverIpc(function(answer) {
          check(answer === "ok", "ipc: refresh answers ok when a fetch starts: " + answer)
          until(function() { return count("start fetch") > started && !service.busy }, "ipc: the fetch ran", next)
        })
      })
    },
    function(next) {
      // Users' scripts read these keys.
      callIpc("status", function(answer) {
        var data = null
        try { data = JSON.parse(answer) } catch (error) {}
        check(data !== null && JSON.stringify(Object.keys(data).sort()) === JSON.stringify(["affected", "monitoring", "repositories", "syncing", "totals"])
          && data.repositories === 1 && data.affected === 1 && data.totals.dirtyRepos === 1 && data.syncing === false && data.monitoring === false,
          "ipc: status answers the counts: " + answer)
        next()
      })
    },
    function(next) {
      block(["fetch"], function() {
        var started = count("start fetch")
        service.fetch()
        until(function() { return service.syncing && count("start fetch") > started }, "ipc: a fetch runs", function() {
          refreshOverIpc(function(answer) {
            check(answer === "busy", "ipc: refresh answers busy while a fetch runs: " + answer)
            check(count("start fetch") === started + 1, "ipc: a busy refresh starts nothing")
            block([])
            until(function() { return !service.busy }, "ipc: the fetch ends", next)
          })
        })
      })
    },
    function(next) {
      connectivityFile.setText("1\n")
      until(function() { return !service.networkReady }, "ipc: the network goes down", function() {
        var started = count("start fetch")
        refreshOverIpc(function(answer) {
          check(answer === "offline", "ipc: refresh answers offline without the network: " + answer)
          check(count("start fetch") === started, "ipc: an offline refresh starts nothing")
          next()
        })
      })
    },
    function(next) {
      blockFile.setText("")
      // The next helper start exits 127: no helper for this CPU.
      failFile.setText("1\n")
      until(function() { failRead.reload(); return failRead.text() === "1\n" }, "ipc: the fail file is written", function() {
        service.refresh()
        until(function() { return service.helperUnavailable }, "ipc: the helper is unavailable", function() {
          refreshOverIpc(function(answer) {
            check(answer === "unavailable", "ipc: refresh answers unavailable without a helper: " + answer)
            check(service.lastError === "This plugin's helper isn't available for this CPU.", "ipc: the panel says the helper is missing: " + service.lastError)
            check(service.repos.length === 1 && service.repos[0].complete === false, "ipc: the repository is kept, as unavailable")
            next()
          })
        })
      })
    }
  ]
  FileView { id: failFile; path: shell.state + "/fail127"; printErrors: false }
  FileView { id: failRead; path: shell.state + "/fail127"; blockLoading: true; printErrors: false }

  Component.onCompleted: go()
  Timer { interval: 120000; running: true; onTriggered: { check(false, "timed out at step " + shell.at); console.log("FOAMY DONE"); Qt.quit() } }
}
"""


def main():
    status = 0
    for host in harness.available_hosts(what="runtime-lifecycle"):
        work = Path(tempfile.mkdtemp(prefix="fgh-life-"))
        atexit.register(shutil.rmtree, work, True)
        state = work / "state"
        state.mkdir()
        (state / "calls.log").write_text("")
        (state / "block").write_text("")
        (state / "fail").write_text("")
        (state / "reply").write_text("")
        helper = HELPER.replace("@STATE@", str(state)).replace("@REPO@", REPO)
        helper = helper.replace("@PUSH_FAILURE@", PUSH_FAILURE).replace("@FETCH_FAILURE@", FETCH_FAILURE)
        plugin = hostenv.fake_helper_plugin(PLUGIN, work / "plugin", "foamy-github-helper", helper)
        shell = SHELL.replace("@STATE@", str(state)).replace("@REPO@", REPO)
        shell = shell.replace("@PUSH_FAILURE@", PUSH_FAILURE).replace("@FETCH_FAILURE@", FETCH_FAILURE)
        shell = shell.replace("@REPLIES@", json.dumps(REPLIES))
        # A short prefix: the run's IPC socket path must stay under 108 bytes.
        # A dark theme with a dim red, for the warning line's contrast check.
        run = harness.Run(
            host,
            shell,
            plugin=plugin,
            prefix="fgh-life-",
            system_bus="private",
            theme=KIT / "tests/fixtures/themes/dim-red.toml",
        )
        # The fake must own its name before qs starts: Quickshell looks for
        # NetworkManager only once, at start.
        run.start_buses()
        try:
            with fake_nm.start(run.system_bus_address, state / "connectivity"):
                run.start()
                run.wait(180)
        finally:
            run.stop()
        run.ok = "FOAMY DONE" in run.log and not run.failures()
        if run.host_commands():
            run.ok = False
            print("host commands ran:", *run.host_commands(), sep="\n  ")
        warnings = [
            line
            for line in run.log.splitlines()
            if "WARN" in line and "/plugin/" in line and "/plugin/kit/" not in line
        ]
        if warnings:
            run.ok = False
            print("\n".join(warnings))
        checks = [line for line in run.log.splitlines() if "FOAMY CHECKS" in line]
        if run.ok:
            print(f"{host}: lifecycle passed ({checks[0].split('FOAMY CHECKS')[1].strip() if checks else '?'} checks)")
            run.cleanup()
        else:
            status = 1
            print(f"{host}: lifecycle FAILED; log at {run.base}/qs.log, helper calls at {state}/calls.log")
            atexit.unregister(shutil.rmtree)
            print("\n".join(run.failures() or run.log.splitlines()[-30:]))
    return status


sys.exit(main())
