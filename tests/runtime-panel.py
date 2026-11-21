#!/usr/bin/env python3
"""Load the real Panel.qml three times (one bar per monitor) in an isolated
offscreen Quickshell against a host shell, through foamy-kit's harness.

  python3 tests/runtime-panel.py

Checks: it loads without errors; one Service and one leader for three
instances; the popup body builds on open with the GithubView; the keys move
the cursor from Settings to the footer's Refresh (no Pull all with nothing
to pull) and open Settings; a paste error, a folder edit and a trust edit
reach the Panel and the bar; the folder chooser (the helper's pick-folder
on the run's private bus, where no portal runs) releases the popup and
comes back to Settings with its error. Temp HOME, no folders, no network.
Requires ~/GitHub/Personal/foamy-kit (or FOAMY_KIT).

Runs on Omarchy 4.5: the installed /usr/share/omarchy/shell, or the tree in
FOAMY_HOST_45."""

import os
import sys
from pathlib import Path

KIT = Path(os.environ.get("FOAMY_KIT", Path.home() / "GitHub/Personal/foamy-kit"))
sys.path.insert(0, str(KIT / "preview"))
import harness  # noqa: E402


PLUGIN = Path(__file__).resolve().parents[1]
SHELL = r"""
import QtQuick
import Quickshell
import "harness"
import "plugin/kit" as Kit
import "plugin" as Plugin

ShellRoot {
  id: shell
  property var panels: []
  property string folderBefore: ""
  property int checks: 0
  function check(ok, what) { checks++; if (!ok) console.log("FOAMY FAIL " + what) }
  function collect(item, test, out) {
    out = out || []
    if (!item) return out
    if (test(item)) out.push(item)
    var kids = item.children || []
    for (var i = 0; i < kids.length; i++) collect(kids[i], test, out)
    return out
  }

  FloatingWindow {
    implicitWidth: 1920; implicitHeight: 1080
    Item { id: host; width: 1920; height: 1080 }
  }
  Component { id: barComponent; MockBar { barSize: 34 } }

  Component.onCompleted: {
    var component = Qt.createComponent(Qt.resolvedUrl("plugin/Panel.qml"))
    if (component.status !== Component.Ready) { console.log("FOAMY FAIL load " + component.errorString()); Qt.quit(); return }
    for (var i = 0; i < 3; i++) {
      var bar = barComponent.createObject(shell, { moduleName: "foamy.github" })
      panels.push(component.createObject(host, { bar: bar, x: 1500, y: 0, width: 40, height: 34,
        settings: { repositoryFolders: [], inotifyEnabled: @INOTIFY@, refreshIntervalSec: 30, fetchIntervalSec: 900,
                    trustRepositoryConfig: true,
                    trustedRepositories: [{ path: "/r/a", identity: "1:1:1|2:2:2" }, { path: "/r/b", identity: "3:3:3|4:4:4" }] } }))
    }
    step.next()
  }

  // Each step runs once the state the previous one started has arrived (a
  // condition, not a fixed delay); a step that never becomes ready fails.
  FoamyWait { id: wait }
  readonly property var ready: [
    function() { return Kit.FoamyShared.memberCount("github") === 3 && Plugin.Service.active
      && Plugin.Service.settings.fetchIntervalSec === 900 },
    // The first complete scan arrived and the legacy trust was migrated.
    function() {
      var saves = panels[0].bar.saved
      return panels[1].popup.bodyItem !== null && Plugin.Service.lastChecked > 0 && !Plugin.Service.busy
        && saves.length > 0 && saves[saves.length - 1].settings.trustRepositoryConfig === false
    },
    function() { return panels[1].editingSettings && !panels[1].prefs.busy },
    // Both identity checks ended; the destroyed leader left.
    function() {
      return JSON.stringify(panels[1].bar.saved).indexOf("@IDENT@") >= 0
        && panels[2].settingsError !== "" && Kit.FoamyShared.memberCount("github") === 2
    },
    // Back from the folder chooser, with its answer.
    function() { return panels[1].opened && !panels[1].returningFromBrowser && panels[1].settingsError !== "" },
    function() { return !Plugin.Service.active }
  ]
  QtObject {
    id: step
    property int n: 0
    function next() { wait.waitFor(shell.ready[n], 20000, run, "step " + n + " ready") }
    function run() {
      if (n === 0) {
        check(Kit.FoamyShared.memberCount("github") === 3, "three members")
        check(Plugin.Service.active, "service active")
        check(Plugin.Service.settings.fetchIntervalSec === 900, "leader fed settings")
        // The service reads every setting through the schema: a hand-edited
        // value out of range or of the wrong type is the default (no clamp),
        // and 0 keeps timed refresh off.
        var fed = JSON.parse(JSON.stringify(Plugin.Service.settings))
        check(Plugin.Service.refreshIntervalSec === 30 && Plugin.Service.legacyTrustAll
          && Plugin.Service.trustedRepositories.length === 2, "service reads the fed settings")
        Plugin.Service.settings = { inotifyEnabled: @INOTIFY@, refreshIntervalSec: 5000, fetchIntervalSec: 60,
          repositoryFolders: [{ path: "relative", depth: 2 }], trustRepositoryConfig: true, trustedRepositories: "x" }
        check(Plugin.Service.refreshIntervalSec === 30 && Plugin.Service.fetchIntervalSec === 900, "out-of-range intervals read as the default")
        check(Plugin.Service.folderArguments === "[]" && Plugin.Service.trustedRepositories.length === 0, "invalid lists read as the default")
        Plugin.Service.settings = Object.assign({}, fed, { refreshIntervalSec: 5 })
        check(Plugin.Service.refreshIntervalSec === 5, "an in-range interval runs as saved")
        Plugin.Service.settings = Object.assign({}, fed, { refreshIntervalSec: 0 })
        check(Plugin.Service.refreshIntervalSec === 0, "0 keeps timed refresh off")
        Plugin.Service.settings = fed
        panels[1].open()
      } else if (n === 1) {
        var view = panels[1].popup.bodyItem
        check(view !== null && view.service === Plugin.Service, "body built on open")
        check(panels[0].popup.bodyItem === null, "other monitors stay lazy")
        var footer = collect(view, function(item) { return item.objectName === "githubFooter" })[0]
        check(footer !== undefined && footer.source === "0 repositories", "footer leads with the repository count")
        var band = collect(view, function(item) { return item.trailing !== undefined && item.settingsHasCursor !== undefined })[0]
        var pullAll = collect(view, function(item) { return item.objectName === "pullAllAction" })[0]
        // The keys the host sends: with no rows and nothing to pull, Down
        // goes to Settings in the header, then to Refresh in the footer.
        var pullAllHadCursor = false
        view.moveRequested(0, 1)
        pullAllHadCursor = pullAllHadCursor || pullAll.hasCursor
        check(band.settingsHasCursor && !footer.refreshHasCursor, "Down: Settings is the first stop")
        view.moveRequested(0, 1)
        pullAllHadCursor = pullAllHadCursor || pullAll.hasCursor
        check(footer.refreshHasCursor && !band.settingsHasCursor, "Down: the footer's Refresh follows Settings")
        view.moveRequested(0, 1)
        check(footer.refreshHasCursor, "Refresh is the last stop")
        check(!pullAll.visible && !pullAllHadCursor, "Pull all is hidden and no stop with nothing to pull")
        // Enter on Refresh asks for a fetch; the harness has no network, so none runs.
        view.activateRequested()
        check(view.statusTitle === "Waiting for network…" && !Plugin.Service.syncing,
          "Enter on Refresh waits for the network: " + view.statusTitle)
        // Legacy global trust migrates once after the first complete scan, in
        // both live-monitoring and timed-polling mode.
        var leaderSaves = panels[0].bar.saved
        check(leaderSaves.length > 0 && leaderSaves[leaderSaves.length - 1].settings.trustRepositoryConfig === false,
              "legacy trust migrated (" + (Plugin.Service.inotifyEnabled ? "live monitoring" : "polling") + ")")
        // Up and Enter on Settings open it (step 2 waits for that).
        view.moveRequested(0, -1)
        check(band.settingsHasCursor, "Up: back on Settings")
        view.activateRequested()
      } else if (n === 2) {
        check(panels[1].editingSettings, "settings open")
        var view1 = panels[1].popup.bodyItem
        // A paste error of the folder field reaches the Panel, which owns
        // settingsError, so clearing it there leaves the page's binding intact.
        var field = collect(view1, function(item) { return item.pasteError !== undefined && item.placeholderText === "~/Projects" })[0]
        var pasteText = "Clipboard paste timed out. Copy the text again."
        field.pasteError = pasteText
        check(panels[1].settingsError === pasteText && view1.settingsError === pasteText, "a paste error reaches the Panel and the page: " + view1.settingsError)
        panels[1].settingsError = ""
        check(view1.settingsError === "", "settingsError binding survives a paste error")
        field.pasteError = ""
        var bar1 = panels[1].bar
        panels[1].save("fetchIntervalSec", 1800)
        check(bar1.saved.length > 0 && bar1.saved[bar1.saved.length - 1].settings.fetchIntervalSec === 1800, "saved inline")
        // A folder edit goes from the view to the Panel through a `var`
        // signal argument, which Qt 6.12 hands over as a sequence wrapper.
        var savedBefore = bar1.saved.length
        view1.setFolderPath(0, "relative")
        check(panels[1].settingsError === "Use an absolute path or ~/ for every folder." && bar1.saved.length === savedBefore,
          "a relative folder is reported, not saved: " + panels[1].settingsError)
        view1.setFolderPath(0, "~/GitHub")
        var folders = bar1.saved[bar1.saved.length - 1].settings.repositoryFolders
        check(bar1.saved.length === savedBefore + 1 && Array.isArray(folders) && JSON.stringify(folders) === JSON.stringify([{ path: "~/GitHub", depth: 2 }]),
          "an edited folder is saved as an array: " + panels[1].settingsError + " " + JSON.stringify(folders))
        // The helpers get the folders and exactly one form of trust.
        var args = Plugin.Service.helperArguments()
        var listAt = args.indexOf("--trusted-json"), legacyAt = args.indexOf("--trust-repository-config")
        check(args[0] === "--folders-json" && args[1] === Plugin.Service.folderArguments && (listAt >= 0) !== (legacyAt >= 0)
          && (Plugin.Service.legacyTrustAll ? legacyAt === args.length - 1
              : JSON.stringify(JSON.parse(args[listAt + 1])) === JSON.stringify(Plugin.Service.trustedRepositories)),
          "helpers get the folders and the trust: " + JSON.stringify(args))
        // Trust edits queued behind an in-flight save compose (Codex: the
        // second edit used to undo the first): untrust A, then B.
        var prefs1 = panels[1].prefs
        prefs1.inFlight = true
        panels[1].setTrusted({ path: "/r/a", identity: "1:1:1|2:2:2" }, false)
        panels[1].setTrusted({ path: "/r/b", identity: "3:3:3|4:4:4" }, false)
        var queued = prefs1.waiting.filter(function(item) { return item.key === "trustedRepositories" })
        check(queued.length === 1 && queued[0].value.length === 0, "queued trust edits compose")
        prefs1.waiting = []
        prefs1.inFlight = false
        // Trusting re-verifies the identity from a fresh pin before saving:
        // the real repository's identity is saved, a stale one refused.
        panels[1].setTrusted({ path: "@REPO@", identity: "@IDENT@" }, true)
        panels[2].setTrusted({ path: "@REPO@", identity: "9:9:9|9:9:9" }, true)
        panels[1].close()
        panels[0].destroy()
      } else if (n === 3) {
        var saved1 = panels[1].bar.saved
        check(saved1.length > 0 && JSON.stringify(saved1[saved1.length - 1].settings.trustedRepositories).indexOf("@IDENT@") >= 0,
              "trust saved after re-verifying the identity")
        check(panels[2].settingsError === "Repository changed; refresh and try again.", "a changed repository is refused")
        check(!panels[2].bar.saved.some(function(entry) { return JSON.stringify(entry.settings.trustedRepositories || []).indexOf("9:9:9") >= 0 }),
              "nothing saved for a changed repository")
        check(!panels[1].opened && !panels[1].editingSettings, "settings reset when the popup closes")
        panels[1].openSettings()
        check(panels[1].opened && panels[1].editingSettings, "openSettings() on a closed popup lands on Settings")
        // The folder chooser: the popup is released while the helper's
        // pick-folder runs, then reopens on Settings with its answer.
        var view3 = panels[1].popup.bodyItem
        shell.folderBefore = collect(view3, function(item) { return item.pasteError !== undefined && item.placeholderText === "~/Projects" })[0].text
        view3.browseRequested(0)
        check(!panels[1].opened, "the popup is released for the folder chooser")
      } else if (n === 4) {
        check(panels[1].editingSettings, "the folder chooser round trip keeps Settings")
        check(panels[1].settingsError === "@PICK_ERROR@", "the chooser's error is shown: " + panels[1].settingsError)
        var row = collect(panels[1].popup.bodyItem, function(item) { return item.pasteError !== undefined && item.placeholderText === "~/Projects" })[0]
        check(row.text === shell.folderBefore, "the folder row is unchanged: " + row.text)
        panels[1].close()

        check(Kit.FoamyShared.memberCount("github") === 2, "leader left")
        check(Plugin.Service.active, "service still active after re-election")
        panels[1].destroy(); panels[2].destroy()
      } else {
        check(!Plugin.Service.active, "service stops with the last bar")
        console.log("FOAMY CHECKS " + checks)
        console.log("FOAMY DONE")
        Qt.quit()
        return
      }
      n++
      next()
    }
  }
}
"""

status = 0
import subprocess
import tempfile

sys.path.insert(0, str(PLUGIN / "tests"))
from blackbox import command  # noqa: E402  (FOAMY_HELPER, as in the unit tests)

TRUST_REPO = tempfile.mkdtemp(prefix="foamy-github-runtime-repo-")
import atexit, shutil  # noqa: E401

atexit.register(shutil.rmtree, TRUST_REPO, True)
subprocess.run(["git", "init", "-q", TRUST_REPO], check=True)
TRUST_IDENTITY = subprocess.run(
    command("identity", TRUST_REPO), capture_output=True, text=True, check=True
).stdout.strip()
SHELL = SHELL.replace("@REPO@", TRUST_REPO).replace("@IDENT@", TRUST_IDENTITY)

# Without bubblewrap the identity is empty, and every trust check would
# match "" and pass with nothing trusted.
if not TRUST_IDENTITY:
    print("runtime-panel: helper identity is empty (bubblewrap missing?); the trust checks cannot run")
    sys.exit(1)

# The helper's pick-folder runs for real against the run's private session
# bus, which has no portal; without dbus-daemon the harness leaves a dead
# address.
DBUS_DAEMON = shutil.which("dbus-daemon")
if DBUS_DAEMON:
    SHELL = SHELL.replace("@PICK_ERROR@", "No folder browser is running (xdg-desktop-portal). Enter a path instead.")
else:
    SHELL = SHELL.replace(
        "@PICK_ERROR@", "The folder browser needs a session bus, and there is none. Enter a path instead."
    )

for host in harness.available_hosts(what="runtime-panel"):
    for mode in ("true", "false"):  # inotifyEnabled: live monitoring, then timed polling
        run = harness.Run(
            host, SHELL.replace("@INOTIFY@", mode), plugin=PLUGIN, prefix=f"foamy-github-runtime-{host}-"
        )
        # The folder chooser must never reach the desktop's bus.
        dead = f"unix:path={run.base}/no-session-bus"
        if run.env["DBUS_SESSION_BUS_ADDRESS"] != dead:
            sys.exit(f"runtime-panel: the run's session bus is {run.env['DBUS_SESSION_BUS_ADDRESS']}, not the harness's")
        run.start_buses()
        expected = f"unix:path={run.sockets / 'bus'}" if DBUS_DAEMON else dead
        if run.env["DBUS_SESSION_BUS_ADDRESS"] != expected or not run.env["XDG_RUNTIME_DIR"].startswith(str(run.sockets)):
            run.stop()
            sys.exit(
                f"runtime-panel: the session bus is {run.env['DBUS_SESSION_BUS_ADDRESS']} with XDG_RUNTIME_DIR "
                f"{run.env['XDG_RUNTIME_DIR']}, not the run's private bus {expected}"
            )
        try:
            run.start()
            run.wait(60)
        finally:
            run.stop()
        run.ok = "FOAMY DONE" in run.log and not run.failures()
        checks = [line for line in run.log.splitlines() if "FOAMY CHECKS" in line]
        # Any QML warning from the plugin's own files fails the run too.
        warnings = [
            line
            for line in run.log.splitlines()
            if "WARN" in line and "/plugin/" in line and "/plugin/kit/" not in line
        ]
        if warnings:
            run.ok = False
            print("\n".join(warnings))
        # The isolated harness stubs every omarchy command; none may run.
        if run.host_commands():
            run.ok = False
            print("host commands ran:", *run.host_commands(), sep="\n  ")

        if run.ok:
            print(
                f"{host} ({'live monitoring' if mode == 'true' else 'polling'}): runtime passed ({checks[0].split('FOAMY CHECKS')[1].strip() if checks else '?'} checks)"
            )
            run.cleanup()
        else:
            status = 1
            print(
                f"{host} ({'live monitoring' if mode == 'true' else 'polling'}): runtime FAILED; log at {run.base}/qs.log"
            )
            print("\n".join(run.failures() or run.log.splitlines()[-30:]))
sys.exit(status)
