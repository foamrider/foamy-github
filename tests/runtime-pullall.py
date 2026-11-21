#!/usr/bin/env python3
"""Pull all, end to end: the real Panel.qml and Service in an isolated
offscreen Quickshell (foamy-kit's harness) over local Git repositories.

  python3 tests/runtime-pullall.py

Four clones of one local remote are behind it: alpha, beta, delta and
gamma, which also has local changes; beta's origin points at a path that
does not exist. The bar shows the counts the helper reports. The keys walk
the rows, Pull all, Settings and Refresh, and Enter on Pull all pulls the
three clean clones one after the other: beta fails, the queue goes on, the
count and beta's error are reported, and gamma is left alone. Enter on
gamma's row opens lazygit through uwsm-app (a stand-in that records its
argv) with the path as its own argument. Requires ~/GitHub/Personal/foamy-kit
(or FOAMY_KIT).

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


PLUGIN = Path(__file__).resolve().parents[1]
SHELL = r"""
import QtQuick
import Quickshell
import "harness"
import "plugin" as Plugin

ShellRoot {
  id: shell
  property var panel: null
  property int checks: 0
  // The last Pull all error: the status refresh that follows clears
  // operationError, so keep what it said.
  property string pullErrors: ""
  Connections {
    target: Plugin.Service
    function onOperationErrorChanged() { if (Plugin.Service.operationError !== "") shell.pullErrors = Plugin.Service.operationError }
  }
  function check(ok, what) { checks++; if (!ok) console.log("FOAMY FAIL " + what) }
  function collect(item, test) {
    var found = test(item) ? [item] : []
    for (var i = 0; i < item.children.length; i++) found = found.concat(collect(item.children[i], test))
    return found
  }
  function named(item, name) { return collect(item, function(i) { return i.objectName === name })[0] }
  // The repository rows, by label. The harness's popup has no window, so
  // its list never runs the layout pass that creates rows as they scroll
  // into view: run it first, as a shown window would.
  function rows(view) {
    collect(view, function(i) { return typeof i.forceLayout === "function" && i.cacheBuffer !== undefined })
      .forEach(function(list) { list.forceLayout() })
    var found = {}
    collect(view, function(i) { return i.repo !== undefined && i.meta !== undefined && i.rowStop !== undefined && !!i.repo })
      .forEach(function(row) { found[row.repo.label] = row })
    return found
  }
  // The bar's counts, as shown, in order.
  function barCounts() {
    return collect(panel, function(i) { return i.shown !== undefined && i.text !== undefined && i.visible })
      .map(function(i) { return i.text })
  }
  // FoamyIcon falls back to "settings" for a name it does not have (an
  // empty name is a button without an icon, which hides it).
  function unknownIcons(item) {
    return collect(item, function(i) { return i.known === false && i.name !== undefined && i.name !== "" }).map(function(i) { return i.name })
  }
  // Where the cursor is: a row's label, "pull all", "settings" or "refresh".
  // Rows are created as they scroll into view, so look them up each time.
  function cursorAt(view) {
    var byLabel = rows(view)
    var at = Object.keys(byLabel).filter(function(label) { return byLabel[label].hasCursor })
    if (at.length === 1) return at[0]
    if (named(view, "pullAllAction").hasCursor) return "pull all"
    // The band and the header inside it both say so.
    if (collect(view, function(i) { return i.trailing !== undefined && i.settingsHasCursor === true }).length > 0) return "settings"
    return named(view, "githubFooter").refreshHasCursor ? "refresh" : "?"
  }
  function finish() { console.log("FOAMY CHECKS " + checks); console.log("FOAMY DONE"); Qt.quit() }

  FloatingWindow {
    // Tall enough that the popup lists every row without scrolling.
    implicitWidth: 1920; implicitHeight: 1080
    Item { id: host; width: 1920; height: 1080 }
  }
  Component { id: barComponent; MockBar { barSize: 34 } }

  Component.onCompleted: {
    var component = Qt.createComponent(Qt.resolvedUrl("plugin/Panel.qml"))
    if (component.status !== Component.Ready) { console.log("FOAMY FAIL load " + component.errorString()); Qt.quit(); return }
    var bar = barComponent.createObject(shell, { moduleName: "foamy.github" })
    panel = component.createObject(host, { bar: bar, width: 40, height: 34,
      settings: { repositoryFolders: [{ path: "@ROOT@", depth: 2 }], inotifyEnabled: false,
                  refreshIntervalSec: 0, fetchIntervalSec: 86400 } })
    panel.open()
    poll.start()
  }

  // Wait for the first status, walk the cursor with the keys (one move per
  // tick: rows are created as the list scrolls), start Pull all, wait for it
  // to finish, then open gamma's row.
  property var order: []
  property var metas: ({})
  Timer {
    id: poll
    interval: 200
    repeat: true
    property int phase: 0
    onTriggered: {
      var service = Plugin.Service
      var view = panel.popup.bodyItem
      if (phase === 0) {
        if (service.repos.length < 4 || service.busy || view === null) return
        check(service.pullableRepos.length === 3, "three clean clones can pull: " + service.pullableRepos.length)
        // Four behind, one changed, and the remote state unknown: nothing was fetched.
        check(JSON.stringify(barCounts()) === JSON.stringify(["4↓", "1*", "?"]), "the bar's counts: " + JSON.stringify(barCounts()))
        var unknown = unknownIcons(view).concat(unknownIcons(panel))
        check(unknown.length === 0, "every icon is one the kit draws: " + JSON.stringify(unknown))
        view.moveRequested(0, 1)
        phase = 1
      } else if (phase === 1) {
        // Down walks the rows, then Pull all, Settings and Refresh; each row
        // says how far behind it is.
        var at = cursorAt(view)
        shell.order = shell.order.concat([at])
        if (rows(view)[at]) shell.metas[at] = rows(view)[at].meta
        if (shell.order.length < 7) { view.moveRequested(0, 1); return }
        check(JSON.stringify(shell.order) === JSON.stringify(["alpha", "beta", "delta", "gamma", "pull all", "settings", "refresh"]),
          "the cursor order: " + JSON.stringify(shell.order))
        check(JSON.stringify(shell.metas) === JSON.stringify({ alpha: "main · ↓1 commit", beta: "main · ↓1 commit",
          delta: "main · ↓1 commit", gamma: "main · ↓1 commit · 1 changed" }), "each row's status line: " + JSON.stringify(shell.metas))
        view.moveRequested(0, -1)
        view.moveRequested(0, -1)
        phase = 2
      } else if (phase === 2) {
        check(cursorAt(view) === "pull all" && named(view, "pullAllAction").actionEnabled, "Up twice: back on Pull all, which can run")
        view.activateRequested()
        check(service.pullingAll && service.repositoryActionRunning, "Enter on Pull all starts it")
        // P while it runs starts nothing more (the clones' heads show it).
        view.textKey("p", Qt.NoModifier)
        phase = 3
      } else if (phase === 3) {
        if (service.pullingAll || service.busy) return
        check(service.actionStatus === "Pulled 2 repositories", "count reported: " + JSON.stringify(service.actionStatus))
        check(shell.pullErrors.indexOf("missing-origin") >= 0 && shell.pullErrors.indexOf(" · ") < 0,
          "only beta's failure is reported: " + shell.pullErrors)
        check(service.pullableRepos.length === 1 && service.pullableRepos[0].label === "beta", "beta is left to pull")
        check(JSON.stringify(Object.keys(rows(view)).sort()) === JSON.stringify(["beta", "gamma"]), "the clean clones leave the list: " + Object.keys(rows(view)))
        check(barCounts().indexOf("2↓") >= 0, "the bar counts two behind: " + JSON.stringify(barCounts()))
        for (var up = 0; up < 8; up++) view.moveRequested(0, -1)
        view.moveRequested(0, 1)
        view.moveRequested(0, 1)
        phase = 4
      } else if (phase === 4) {
        check(cursorAt(view) === "pull all", "Pull all stays while beta can pull: " + cursorAt(view))
        view.moveRequested(0, -1)
        phase = 5
      } else if (phase === 5) {
        // Enter on gamma's row opens lazygit and closes the popup.
        check(cursorAt(view) === "gamma", "Up from Pull all: gamma's row: " + cursorAt(view))
        var gamma = rows(view).gamma
        console.log("FOAMY GAMMA " + (gamma ? gamma.repo.path : ""))
        view.activateRequested()
        check(!panel.opened, "opening lazygit closes the popup")
        stop()
        finish()
      }
    }
  }
  Timer { interval: 40000; running: true; onTriggered: { check(false, "timed out"); shell.finish() } }
}
"""


def git(cwd, *args):
    subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "GIT_CONFIG_GLOBAL": "/dev/null",
        },
    )


def head(cwd):
    return subprocess.run(["git", "-C", str(cwd), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()


ROOT = Path(tempfile.mkdtemp(prefix="foamy-github-pullall-"))
atexit.register(shutil.rmtree, ROOT, True)
REMOTE = ROOT.parent / (ROOT.name + "-remote.git")
atexit.register(shutil.rmtree, REMOTE, True)
git(ROOT.parent, "init", "-q", "--bare", "-b", "main", str(REMOTE))
seed = ROOT.parent / (ROOT.name + "-seed")
atexit.register(shutil.rmtree, seed, True)
git(ROOT.parent, "clone", "-q", str(REMOTE), str(seed))
git(seed, "checkout", "-q", "-b", "main")
(seed / "a.txt").write_text("one\n")
git(seed, "add", "a.txt")
git(seed, "commit", "-q", "-m", "one")
git(seed, "push", "-q", "-u", "origin", "main")
clones = [ROOT / name for name in ("alpha", "beta", "delta", "gamma")]
for clone in clones:
    git(ROOT, "clone", "-q", str(REMOTE), str(clone))
(seed / "a.txt").write_text("two\n")
git(seed, "commit", "-q", "-am", "two")
git(seed, "push", "-q")
for clone in clones:
    git(clone, "fetch", "-q")
# beta's pull fails: its origin is gone.
git(clones[1], "remote", "set-url", "origin", str(ROOT.parent / (ROOT.name + "-missing-origin.git")))
(clones[3] / "local.txt").write_text("uncommitted\n")
remote_head = head(seed)
# Stands in for uwsm-app, so opening a repository never starts a terminal.
FAKE_UWSM = """#!/bin/sh
printf '%s\\0' "$@" > "$FOAMY_OUT/uwsm-args.tmp" && mv "$FOAMY_OUT/uwsm-args.tmp" "$FOAMY_OUT/uwsm-args"
"""

status = 0
for host in harness.available_hosts(what="runtime-pullall"):
    for clone in clones:  # each host starts behind again
        if head(clone) == remote_head:
            git(clone, "reset", "-q", "--hard", "HEAD~1")
    run = harness.Run(
        host,
        SHELL.replace("@ROOT@", str(ROOT)),
        plugin=PLUGIN,
        prefix=f"foamy-github-pullall-{host}-",
        fakebin={"uwsm-app": FAKE_UWSM},
    )
    stand_in = shutil.which("uwsm-app", path=run.env["PATH"])
    if stand_in != str(run.base / "bin/uwsm-app"):
        sys.exit(f"runtime-pullall: uwsm-app resolves to {stand_in}, not the stand-in")
    run.run(timeout=60)
    pulled = [head(clone) == remote_head for clone in clones]
    if pulled != [True, False, True, False]:
        run.ok = False
        print(f"pulled clones {pulled}, expected [True, False, True, False]")
    # Quickshell starts uwsm-app detached: it may write after qs quit.
    args_file = run.out / "uwsm-args"
    harness.wait_for(args_file.exists, timeout=10)
    shown = [line.split("FOAMY GAMMA", 1)[1].strip() for line in run.log.splitlines() if "FOAMY GAMMA" in line]
    gamma = shown[0] if shown else ""
    expected = [
        "--",
        "xdg-terminal-exec",
        "--app-id=org.omarchy.terminal",
        "--title=lazygit - gamma",
        "-e",
        "bash",
        "-c",
        'cd -- "$1" && exec lazygit',
        "_",
        gamma,
    ]
    argv = args_file.read_bytes().split(b"\0")[:-1] if args_file.exists() else None
    if gamma != os.path.realpath(clones[3]) or argv != [arg.encode() for arg in expected]:
        run.ok = False
        print(f"lazygit argv {argv}, expected {expected} with the path {os.path.realpath(clones[3])}")
    # The isolated harness stubs every omarchy command; none may run.
    if run.host_commands():
        run.ok = False
        print("host commands ran:", *run.host_commands(), sep="\n  ")

    if run.ok:
        print(f"{host}: pull all passed")
        run.cleanup()
    else:
        status = 1
        print(f"{host}: pull all FAILED; log at {run.base}/qs.log")
        print("\n".join(run.failures() or run.log.splitlines()[-30:]))
sys.exit(status)
