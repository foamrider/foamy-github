#!/usr/bin/env python3
"""A status check that a trust change overtook, end to end: the real
Panel.qml and Service in an isolated offscreen Quickshell (foamy-kit's
harness) with a fake helper.

  python3 tests/runtime-stale-status.py

Timed polling only (no live monitoring). The fake helper's first `status`
blocks until the test has saved trust for its one repository; every
`status` reports that repository as trusted exactly when its own
--trusted-json named something, and names its run ("run1", "run2", ...).
The first scan was started with the old trust, so its result is stale: the
Service must drop it and check again with the new trust, and the first
status it shows is run 2, trusted. (It used to show run 1, untrusted, until
the next refresh, which with refresh off never came.) Each step waits on an
event, not on time. Temp HOME; no network. Requires ~/GitHub/Personal/foamy-kit
(or FOAMY_KIT).

Runs on Omarchy 4.5: the installed /usr/share/omarchy/shell, or the tree in
FOAMY_HOST_45."""

import atexit
import os
import shlex
import shutil
import sys
import tempfile
from pathlib import Path

KIT = Path(os.environ.get("FOAMY_KIT", Path.home() / "GitHub/Personal/foamy-kit"))
sys.path.insert(0, str(KIT / "preview"))
import harness  # noqa: E402
import hostenv  # noqa: E402


PLUGIN = Path(__file__).resolve().parents[1]
REPO = "/foamy-test/repo"

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
  // Keys in the order Omarchy hands them over, so the folders read the same
  // before and after the trust edit (only trust changes).
  readonly property var base: ({ repositoryFolders: [{ depth: 1, path: "/foamy-test" }], inotifyEnabled: false,
                                 refreshIntervalSec: 0, fetchIntervalSec: 86400, trustedRepositories: [] })
  function check(ok, what) { checks++; if (!ok) console.log("FOAMY FAIL " + what) }
  function finish() { console.log("FOAMY CHECKS " + checks); console.log("FOAMY DONE"); Qt.quit() }

  FloatingWindow {
    implicitWidth: 800; implicitHeight: 600
    Item { id: host; width: 800; height: 600 }
  }
  Component { id: barComponent; MockBar { barSize: 34 } }
  // Lets the fake helper's first status finish.
  Process { id: release; command: ["touch", "@RELEASE@"] }

  Component.onCompleted: {
    var component = Qt.createComponent(Qt.resolvedUrl("plugin/Panel.qml"))
    if (component.status !== Component.Ready) { console.log("FOAMY FAIL load " + component.errorString()); Qt.quit(); return }
    var bar = barComponent.createObject(shell, { moduleName: "foamy.github" })
    panel = component.createObject(host, { bar: bar, width: 40, height: 34, settings: base })
    poll.start()
  }

  Timer {
    id: poll
    interval: 50
    repeat: true
    property int phase: 0
    onTriggered: {
      var service = Plugin.Service
      if (phase === 0) {
        // The first scan of the configured folder runs (and waits in the
        // fake helper): trust the repository now.
        if (!service.refreshing || service.inotifyEnabled || service.folderArguments === "[]"
            || service.scannedFolders !== service.folderArguments) return
        panel.settings = Object.assign({}, base, { trustedRepositories: [{ path: "@REPO@", identity: "1:2:3|1:2:3" }] })
        phase = 1
      } else if (phase === 1) {
        // One tick later the Service has seen the new trust (its restart
        // runs through Qt.callLater); only then may the first scan finish.
        check(service.trustArguments.indexOf("@REPO@") >= 0, "the Service reads the new trust")
        release.running = true
        phase = 2
      } else if (service.repos.length > 0 && !service.refreshing) {
        var repo = service.repos.length === 1 ? service.repos[0] : null
        check(repo !== null && repo.name !== "run1", "the scan started with the old trust is not shown: " + (repo ? repo.name : "no repository"))
        check(repo !== null && repo.trusted === true, "the shown status was checked with the new trust")
        stop()
        shell.finish()
      }
    }
  }
  Timer { interval: 40000; running: true; onTriggered: { check(false, "timed out"); shell.finish() } }
}
"""


def fake_plugin(work):
    """A copy of the plugin whose helper is a script (fake `status`)."""
    counter = work / "status-runs"
    release = work / "release"
    script = f"""#!/usr/bin/env bash
# Fake foamy-github-helper: one repository, trusted when --trusted-json names
# anything; the first status of that folder waits (at most 30 s) for the
# test's release file.
case "${{1-}}" in
  status|watch) ;;
  *) printf '{{"ok":true,"failures":[]}}\\n'; exit 0 ;;
esac
# Before the settings arrive the Service runs with none: no folders, live
# monitoring. Answer that with an empty status (a watcher then waits for EOF).
if [ "${{1-}}" = watch ] || [ "${{3-}}" = "[]" ]; then
  printf '{{"ok":true,"repoCount":0,"affectedCount":0,"repos":[]}}\\n'
  [ "$1" = watch ] && exec cat > /dev/null
  exit 0
fi
runs=$(( $(cat {shlex.quote(str(counter))} 2>/dev/null || echo 0) + 1 ))
echo "$runs" > {shlex.quote(str(counter))}
trusted=false
args=("$@")
for ((i = 0; i < ${{#args[@]}}; i++)); do
  if [ "${{args[i]}}" = --trusted-json ] && [ "${{args[i+1]-[]}}" != "[]" ]; then trusted=true; fi
done
if [ "$runs" = 1 ]; then
  for ((i = 0; i < 600; i++)); do [ -e {shlex.quote(str(release))} ] && break; sleep 0.05; done
fi
printf '{{"ok":true,"repoCount":1,"affectedCount":1,"totals":{{"dirtyRepos":1}},"repos":[{{"path":"{REPO}","name":"run%s","owner":"","label":"run%s","branch":"main","upstream":"origin/main","ahead":0,"behind":0,"dirty":true,"dirtyCount":1,"affected":true,"complete":true,"error":"","usesLfs":true,"trusted":%s,"identity":"1:2:3|1:2:3","syncError":""}}],"sync":{{"stale":false}}}}\\n' "$runs" "$runs" "$trusted"
"""
    plugin = hostenv.fake_helper_plugin(PLUGIN, work / "plugin", "foamy-github-helper", script)
    return plugin, counter, release


status = 0
for host in harness.available_hosts(what="runtime-stale-status"):
    work = Path(tempfile.mkdtemp(prefix="foamy-github-stale-"))
    atexit.register(shutil.rmtree, work, True)
    plugin, counter, release = fake_plugin(work)
    shell = SHELL.replace("@RELEASE@", str(release)).replace("@REPO@", REPO)
    run = harness.Run(host, shell, plugin=plugin, prefix=f"foamy-github-stale-{host}-").run(timeout=60)
    runs = counter.read_text().strip() if counter.exists() else "0"
    # Any QML warning from the plugin's own files fails the run too.
    warnings = [
        line for line in run.log.splitlines() if "WARN" in line and "/plugin/" in line and "/plugin/kit/" not in line
    ]
    if warnings:
        run.ok = False
        print("\n".join(warnings))
    checks = [line for line in run.log.splitlines() if "FOAMY CHECKS" in line]
    # The isolated harness stubs every omarchy command; none may run.
    if run.host_commands():
        run.ok = False
        print("host commands ran:", *run.host_commands(), sep="\n  ")

    if run.ok:
        print(
            f"{host}: stale status passed ({checks[0].split('FOAMY CHECKS')[1].strip() if checks else '?'} checks, {runs} status runs)"
        )
        run.cleanup()
    else:
        status = 1
        print(f"{host}: stale status FAILED ({runs} status runs); log at {run.base}/qs.log")
        print("\n".join(run.failures() or run.log.splitlines()[-30:]))
sys.exit(status)
