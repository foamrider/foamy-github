#!/usr/bin/env python3
"""Invalid stored lists, end to end: the real Panel.qml and Service in an
isolated offscreen Quickshell (foamy-kit's harness) with the real helper
over no folders.

  python3 tests/runtime-stored.py

repositoryFolders and trustedRepositories hold the user's data. A list that
fails the schema (a hand edit in shell.json) reads as empty, and FoamyPrefs
refuses to replace it. The Panel must say which list is invalid, lock its
editor (no save, the Add folder button off), and keep the old global trust
switch until the trusted list can be saved; once shell.json is fixed, all of
that lifts. Temp HOME; no network. Requires ~/GitHub/Personal/foamy-kit (or
FOAMY_KIT).

Runs on Omarchy 4.5: the installed /usr/share/omarchy/shell, or the tree in
FOAMY_HOST_45."""

import json
import os
import sys
from pathlib import Path

KIT = Path(os.environ.get("FOAMY_KIT", Path.home() / "GitHub/Personal/foamy-kit"))
sys.path.insert(0, str(KIT / "preview"))
import harness  # noqa: E402

PLUGIN = Path(__file__).resolve().parents[1]
FOLDERS = "The repository folders saved in shell.json aren't valid, so no folders are checked. Fix them in shell.json."
TRUST = "The trusted repositories saved in shell.json aren't valid, so no repository is trusted. Fix the list in shell.json."

SHELL = r"""
import QtQuick
import Quickshell
import "harness"
import "plugin" as Plugin

ShellRoot {
  id: shell
  property var panel: null
  property int checks: 0
  readonly property var service: Plugin.Service
  function check(ok, what) { checks++; if (!ok) console.log("FOAMY FAIL " + what) }
  function collect(item, test) {
    var found = item && test(item) ? [item] : []
    var kids = item ? item.children || [] : []
    for (var i = 0; i < kids.length; i++) found = found.concat(collect(kids[i], test))
    return found
  }
  function savedKeys() {
    var keys = {}
    panel.bar.saved.forEach(function(entry) {
      Object.keys(entry.settings || {}).forEach(function(key) { keys[key] = (keys[key] || []).concat([entry.settings[key]]) })
    })
    return keys
  }
  function settingsWith(folders, trusted) {
    return { repositoryFolders: folders, inotifyEnabled: false, refreshIntervalSec: 0, fetchIntervalSec: 86400,
             trustRepositoryConfig: true, trustedRepositories: trusted }
  }

  FloatingWindow {
    implicitWidth: 1920; implicitHeight: 1080
    Item { id: host; width: 1920; height: 1080 }
  }
  Component { id: barComponent; MockBar { barSize: 34 } }
  FoamyWait { id: wait }

  readonly property string foldersText: @FOLDERS@
  readonly property string trustText: @TRUST@

  Component.onCompleted: {
    var component = Qt.createComponent(Qt.resolvedUrl("plugin/Panel.qml"))
    if (component.status !== Component.Ready) { console.log("FOAMY FAIL load " + component.errorString()); Qt.quit(); return }
    panel = component.createObject(host, { bar: barComponent.createObject(shell, { moduleName: "foamy.github" }),
                                           x: 1500, y: 0, width: 40, height: 34 })
    // Both lists invalid: a relative folder, a trusted entry without a real path.
    panel.settings = settingsWith([{ path: "relative", depth: 2 }], [{ path: "relative", identity: "x" }])
    wait.waitFor(function() { return service.lastChecked > 0 && !service.refreshing }, 15000, stepFolders, "the first scan")
  }

  function stepFolders() {
    check(panel.pageError === foldersText, "the invalid folder list is named: " + panel.pageError)
    check(service.folderArguments === "[]", "an invalid folder list checks no folders")
    // A first complete scan arrived, so the legacy switch would migrate now.
    check(savedKeys().trustRepositoryConfig === undefined && savedKeys().trustedRepositories === undefined,
      "the legacy trust switch is kept while the trusted list is invalid")
    panel.openSettings()
    wait.waitFor(function() { return panel.popup.bodyItem !== null && panel.editingSettings }, 10000, function() {
      var add = collect(panel.popup.bodyItem, function(item) { return item.iconName === "plus" && item.actionEnabled !== undefined })
      check(add.length === 1 && add[0].actionEnabled === false, "Add folder is off while the folder list is invalid")
      panel.saveFolders([{ path: "~/GitHub", depth: 2 }])
      check(!(savedKeys().repositoryFolders || []).some(function(value) { return JSON.stringify(value).indexOf("~/GitHub") >= 0 }),
        "no folder list is saved over the invalid one")
      check(panel.pageError === foldersText, "a folder edit says why it wasn't saved: " + panel.pageError)
      panel.settingsError = ""
      panel.settings = settingsWith([], [{ path: "relative", identity: "x" }])
      wait.waitFor(function() { return panel.pageError === trustText }, 10000, stepTrust, "the invalid trusted list is named")
    }, "settings open")
  }

  function stepTrust() {
    var add = collect(panel.popup.bodyItem, function(item) { return item.iconName === "plus" && item.actionEnabled !== undefined })
    check(add.length === 1 && add[0].actionEnabled === true, "Add folder is on again with a valid folder list")
    panel.setTrusted({ path: "/r/a", identity: "1:1:1|2:2:2" }, false)
    check((savedKeys().trustedRepositories || []).every(function(value) { return JSON.stringify(value).indexOf("relative") >= 0 }),
      "no trusted list is saved over the invalid one")
    check(panel.settingsError === trustText, "a trust edit says why it wasn't saved: " + panel.settingsError)
    panel.settingsError = ""
    panel.settings = settingsWith([], [])
    // Fixed: no message, and the legacy switch migrates after the next scan.
    service.refresh()
    wait.waitFor(function() {
      var keys = savedKeys()
      return keys.trustRepositoryConfig !== undefined && keys.trustRepositoryConfig[keys.trustRepositoryConfig.length - 1] === false
    }, 15000, function() {
      check(panel.pageError === "", "no message once shell.json is fixed: " + panel.pageError)
      console.log("FOAMY CHECKS " + checks)
      console.log("FOAMY DONE")
      Qt.quit()
    }, "the legacy switch migrates once the trusted list is valid")
  }

  Timer { interval: 60000; running: true; onTriggered: { check(false, "timed out"); console.log("FOAMY DONE"); Qt.quit() } }
}
""".replace("@FOLDERS@", json.dumps(FOLDERS)).replace("@TRUST@", json.dumps(TRUST))


def main():
    status = 0
    for host in harness.available_hosts(what="runtime-stored"):
        run = harness.Run(host, SHELL, plugin=PLUGIN, prefix="fgh-stored-").run(timeout=90)
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
            print(f"{host}: stored lists passed ({checks[0].split('FOAMY CHECKS')[1].strip() if checks else '?'} checks)")
            run.cleanup()
        else:
            status = 1
            print(f"{host}: stored lists FAILED; log at {run.base}/qs.log")
            print("\n".join(run.failures() or run.log.splitlines()[-30:]))
    return status


sys.exit(main())
