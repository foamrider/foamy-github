#!/usr/bin/env python3
"""The Service's helper processes across unload, trust edits and IPC, end
to end: the real Panel.qml and Service in an isolated offscreen Quickshell
(foamy-kit's harness) with a fake helper and a fake NetworkManager on the
run's own system bus.

  python3 tests/runtime-lifecycle.py

The fake helper logs every start, SIGTERM and end to calls.log, with the
trust it was given, and holds each subcommand named in the `block` file
until the test removes it (at most 30 s). Its status reports one
repository, trusted when its trust arguments name anything.

Scenes, one shell, each step waiting on an event (a timeout fails the run):
- unload: a status check, a fetch and a push still running when the last
  bar goes leave no error and no action text on the shared Service.
- trust: revoking trust (an entry removed) stops a running status check
  and fetch that were started with it; the check is redone and the fetch
  runs again with the new trust, with no error. Granting trust stops
  neither.

Timed polling only (no live monitoring); temp HOME; no network. Requires
~/GitHub/Personal/foamy-kit (or FOAMY_KIT).

Runs on Omarchy 4.5: the installed /usr/share/omarchy/shell, or the tree in
FOAMY_HOST_45."""

import atexit
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

KIT = Path(os.environ.get("FOAMY_KIT", Path.home() / "GitHub/Personal/foamy-kit"))
sys.path.insert(0, str(KIT / "preview"))
import harness  # noqa: E402
import hostenv  # noqa: E402

PLUGIN = Path(__file__).resolve().parents[1]
REPO = "/foamy-test/repo"

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
  watch) printf '{"ok":true,"repoCount":0,"affectedCount":0,"repos":[]}\n'; exec cat > /dev/null ;;
esac
# Before the settings arrive the Service runs with no folders: never held.
if [ "$folders" != "[]" ]; then
  for ((i = 0; i < 600; i++)); do grep -qx "$sub" "$state/block" 2>/dev/null || break; sleep 0.05; done
fi
echo "end $sub $$" >> "$state/calls.log"
case "$sub" in
  status)
    if [ "$folders" = "[]" ]; then
      printf '{"ok":true,"repoCount":0,"affectedCount":0,"repos":[]}\n'
      exit 0
    fi
    trusted=false
    [ -n "$trust" ] && [ "$trust" != "[]" ] && trusted=true
    printf '{"ok":true,"repoCount":1,"affectedCount":1,"totals":{"dirtyRepos":1},"repos":[{"path":"@REPO@","name":"repo","owner":"","label":"repo","branch":"main","upstream":"origin/main","ahead":1,"behind":0,"dirty":true,"dirtyCount":1,"affected":true,"complete":true,"error":"","usesLfs":true,"trusted":%s,"identity":"1:2:3|1:2:3","syncError":""}],"sync":{"stale":false}}\n' "$trusted"
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
  function settingsWith(trusted) {
    return { repositoryFolders: [{ depth: 1, path: "/foamy-test" }], inotifyEnabled: false,
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

  readonly property var steps: [].concat(unloadSteps, trustSteps)

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
      dropPanel()
      until(function() { return !service.active && !service.syncing }, "unload: the fetch is stopped", function() {
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
      dropPanel()
      until(function() { return !service.active && !service.repositoryActionRunning }, "unload: the push is stopped", function() {
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
      var stopped = count("term status")
      panel.settings = settingsWith(trustRepo)
      // Negative check: give a wrong stop time to arrive.
      wait.after(1000, function() {
        check(count("term status") === stopped, "trust: granting does not stop the running check")
        block([])
        until(function() { return !service.refreshing && service.repos.length === 1 && service.repos[0].trusted === true },
          "trust: the granted trust is shown", next)
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
      var stopped = count("term fetch")
      var started = count("start fetch")
      panel.settings = settingsWith(trustRepo)
      wait.after(1000, function() {
        check(count("term fetch") === stopped && count("start fetch") === started, "trust: granting does not stop or restart the fetch")
        block([])
        until(function() { return !service.busy }, "trust: the fetch ends", function() {
          check(service.operationError === "", "trust: a fetch that a grant overlapped ends without error: " + service.operationError)
          next()
        })
      })
    }
  ]

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
        helper = HELPER.replace("@STATE@", str(state)).replace("@REPO@", REPO)
        plugin = hostenv.fake_helper_plugin(PLUGIN, work / "plugin", "foamy-github-helper", helper)
        shell = SHELL.replace("@STATE@", str(state)).replace("@REPO@", REPO)
        # A short prefix: the run's IPC socket path must stay under 108 bytes.
        run = harness.Run(host, shell, plugin=plugin, prefix="fgh-life-", system_bus="private").start()
        nm = subprocess.Popen(
            [sys.executable, str(PLUGIN / "tests/fake_nm.py"), run.system_bus_address, str(state / "connectivity")],
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            run.wait(180)
        finally:
            nm.kill()
            nm.wait()
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
