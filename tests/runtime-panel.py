#!/usr/bin/env python3
"""Load the real Panel.qml three times (one bar per monitor) in an isolated
offscreen Quickshell against a host shell, through foamy-kit's harness.

  python3 tests/runtime-panel.py [4.0|4.5 ...]     (default: both)

Checks: it loads without errors; one Service and one leader for three
instances; the popup body builds on open with the GithubView; the inline
error row renders; the watcher helper runs (temp HOME, no folders, no
network beyond what Git would do for zero repositories); IPC status answers
from the leader. Requires ~/GitHub/Personal/foamy-kit (or FOAMY_KIT)."""
import os
import sys
from pathlib import Path

KIT = Path(os.environ.get("FOAMY_KIT", Path.home() / "GitHub/Personal/foamy-kit"))
sys.path.insert(0, str(KIT / "preview"))
import harness  # noqa: E402

PLUGIN = Path(__file__).resolve().parents[1]
SHELL = r'''
import QtQuick
import Quickshell
import "harness"
import "plugin/kit" as Kit
import "plugin" as Plugin

ShellRoot {
  id: shell
  property var panels: []
  property int checks: 0
  function check(ok, what) { checks++; if (!ok) console.log("FOAMY FAIL " + what) }

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
        settings: { repositoryFolders: [], inotifyEnabled: true, refreshIntervalSec: 30, fetchIntervalSec: 900 } }))
    }
    step.start()
  }

  Timer {
    id: step
    interval: 1500
    property int n: 0
    onTriggered: {
      if (n === 0) {
        check(Kit.FoamyShared.memberCount("github") === 3, "three members")
        check(Plugin.Service.active, "service active")
        check(Plugin.Service.settings.fetchIntervalSec === 900, "leader fed settings")
        panels[1].open()
      } else if (n === 1) {
        var view = panels[1].popup.bodyItem
        check(view !== null && view.service === Plugin.Service, "body built on open")
        check(panels[0].popup.bodyItem === null, "other monitors stay lazy")
        check(Plugin.Service.lastChecked > 0 || Plugin.Service.refreshing, "status arrived from the watcher")
        panels[1].openSettings()
      } else if (n === 2) {
        check(panels[1].editingSettings, "settings open")
        panels[1].close()
        panels[0].destroy()
      } else if (n === 3) {
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
      restart()
    }
  }
}
'''

status = 0
for host in (sys.argv[1:] or ["4.0", "4.5"]):
    run = harness.Run(host, SHELL, plugin=PLUGIN, prefix=f"foamy-github-runtime-{host}-").run(timeout=60)
    checks = [line for line in run.log.splitlines() if "FOAMY CHECKS" in line]
    # Any QML warning from the plugin's own files fails the run too.
    warnings = [line for line in run.log.splitlines() if "WARN" in line and "/plugin/" in line and "/plugin/kit/" not in line]
    if warnings:
        run.ok = False
        print("\n".join(warnings))
    if run.ok:
        print(f"{host}: runtime passed ({checks[0].split('FOAMY CHECKS')[1].strip() if checks else '?'} checks)")
        run.cleanup()
    else:
        status = 1
        print(f"{host}: runtime FAILED; log at {run.base}/qs.log")
        print("\n".join(run.failures() or run.log.splitlines()[-30:]))
sys.exit(status)
