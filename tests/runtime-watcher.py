#!/usr/bin/env python3
"""The watcher's stderr, end to end: the real Panel.qml and Service in an
isolated offscreen Quickshell (foamy-kit's harness) with a fake helper.

  python3 tests/runtime-watcher.py

The plugin runs from a temporary copy whose bin/<arch>/foamy-github-helper
is a script: each `watch` writes "FAKE-WATCH-HEAD run <n>" and 16 MiB to
stderr and exits 3 (a failure, not 126/127), except the third, which exits 3
without writing anything; every other subcommand runs the real helper
(FOAMY_HELPER, see tests/blackbox.py) over one empty folder. The watcher
starts once at shell start, with that folder: the bar's first member makes
the Service active before the leader hands over the settings, and the
setting handlers fire for their first values too. The Service keeps
only the first 4096 characters of the watcher's stderr, shows 240 of them,
and starts afresh when the watcher starts again (the detail of a restarted
watcher names its own run). The warning says the failure in the panel's
language (one run in English, one in Norwegian) and then the detail as it
is; after the silent run the detail is the Service's own "Filesystem
watcher exited", in the panel's language too. Every helper start also
records the shell's peak resident memory (VmHWM of its parent, qs): reading
that stderr must not raise it by 16 MiB (keeping it whole, as a
StdioCollector did, costs several times the stderr). Temp HOME; no network.
Requires ~/GitHub/Personal/foamy-kit (or FOAMY_KIT).

Runs on Omarchy 4.5: the installed /usr/share/omarchy/shell, or the tree in
FOAMY_HOST_45."""

import atexit
import itertools
import json
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
sys.path.insert(0, str(PLUGIN / "tests"))
from blackbox import PREFIX  # noqa: E402  (the real helper, FOAMY_HELPER)

STDERR_BYTES = 16 * 1024 * 1024
# Peak memory growth allowed while the watchers' stderr is consumed (kB).
GROWTH_LIMIT_KB = 16 * 1024
HEAD = "FAKE-WATCH-HEAD run "
# The warning line's failure with refreshIntervalSec 0, before the detail.
WARNING = "Live monitoring unavailable; automatic local refresh is disabled."
# The Service's detail when the watcher wrote nothing.
EXITED = "Filesystem watcher exited"

SHELL = r"""
import QtQuick
import Quickshell
import "harness"
import "plugin" as Plugin

ShellRoot {
  id: shell
  property var panel: null
  property int checks: 0
  // Watcher starts since the panel was made (the Service stamps each one).
  property int watchStarts: 0
  readonly property string warning: "@WARNING@"
  readonly property string exited: "@EXITED@"
  function check(ok, what) { checks++; if (!ok) console.log("FOAMY FAIL " + what) }
  function finish() { console.log("FOAMY CHECKS " + checks); console.log("FOAMY DONE"); Qt.quit() }

  FloatingWindow {
    implicitWidth: 800; implicitHeight: 600
    Item { id: host; width: 800; height: 600 }
  }
  Component { id: barComponent; MockBar { barSize: 34 } }
  Connections {
    id: startCount
    target: null
    function onWatchStartedAtChanged() { shell.watchStarts++ }
  }

  Component.onCompleted: {
    var component = Qt.createComponent(Qt.resolvedUrl("plugin/Panel.qml"))
    if (component.status !== Component.Ready) { console.log("FOAMY FAIL load " + component.errorString()); Qt.quit(); return }
    var bar = barComponent.createObject(shell, { moduleName: "foamy.github" })
    panel = component.createObject(host, { bar: bar, width: 40, height: 34,
      settings: { repositoryFolders: [{ path: "@FOLDER@", depth: 2 }], inotifyEnabled: true, refreshIntervalSec: 0, fetchIntervalSec: 86400 } })
    // The Service is made with the panel; a watcher started while it was
    // made, before this could listen, has stamped its start already.
    watchStarts = Plugin.Service.watchStartedAt > 0 ? 1 : 0
    startCount.target = Plugin.Service
    poll.start()
  }

  // Wait for the first watcher to fail, check what was kept, start the
  // watcher again and wait for the new run to fail; then once more, for the
  // run that writes nothing.
  Timer {
    id: poll
    interval: 100
    repeat: true
    property int phase: 0
    property int firstRun: 0
    onTriggered: {
      var service = Plugin.Service
      // Wait for the status the failure starts too (one more helper start,
      // so the shell's peak memory is recorded after the stderr was read).
      if (!service.watchFailed || service.refreshing) return
      check(!service.helperUnavailable, "exit 3 is a failure, not an unavailable helper")
      // What the panel's warning line shows: the failure in the panel's
      // language, then the head of the watcher's stderr as it is.
      var shown = service.lastError
      var warned = shown.indexOf(shell.warning) === 0
      check(warned, "the warning names the failure: " + JSON.stringify(shown.slice(0, 80)))
      var detail = warned ? shown.slice(shell.warning.length) : ""
      if (phase === 2) {
        // Without stderr the detail is the Service's own text.
        check(detail === shell.exited, "a silent watcher's detail is in the panel's language: " + JSON.stringify(detail))
        stop()
        shell.finish()
        return
      }
      check(detail.length > 0 && detail.length <= 240, "detail shown, at most 240 characters: " + detail.length)
      var head = /^@HEAD@([0-9]+)\n/.exec(detail)
      check(head !== null, "detail starts with the watcher's first bytes: " + JSON.stringify(detail.slice(0, 40)))
      var run = head ? Number(head[1]) : -1
      if (phase === 0) {
        // Up to its first failure, the only watcher is the one shell start made.
        check(shell.watchStarts === 1, "the watcher starts once at shell start: " + shell.watchStarts + " starts")
        firstRun = run
        service.restartWatcher(true)
        check(!service.watchFailed && service.watchDetail === "", "a restart clears the failure")
        phase = 1
      } else {
        check(run > firstRun && firstRun > 0, "a restarted watcher starts its stderr afresh: run " + run + " after run " + firstRun)
        service.restartWatcher(true)
        phase = 2
      }
    }
  }
  Timer { interval: 40000; running: true; onTriggered: { check(false, "timed out"); shell.finish() } }
}
""".replace("@HEAD@", HEAD)


def fake_plugin(work):
    """A copy of the plugin whose helper is a script (fake `watch`)."""
    counter = work / "watch-runs"
    memory = work / "memory"
    script = f"""#!/usr/bin/env bash
# Fake foamy-github-helper: `watch` fails loudly; the rest is the real helper.
# Every start records its parent (qs) and that process's peak memory.
echo "$PPID $(awk '/^VmHWM:/ {{ print $2 }}' /proc/$PPID/status)" >> {shlex.quote(str(memory))}
if [ "${{1-}}" = watch ]; then
  runs=$(( $(cat {shlex.quote(str(counter))} 2>/dev/null || echo 0) + 1 ))
  echo "$runs" > {shlex.quote(str(counter))}
  # The third run fails without writing anything.
  [ "$runs" -lt 3 ] || exit 3
  {{ printf '%s%s\\n' {shlex.quote(HEAD)} "$runs"; head -c {STDERR_BYTES} /dev/zero | tr '\\0' x; }} >&2
  exit 3
fi
exec {shlex.join(PREFIX)} "$@"
"""
    plugin = hostenv.fake_helper_plugin(PLUGIN, work / "plugin", "foamy-github-helper", script)
    return plugin, counter, memory


status = 0
for host, language in itertools.product(harness.available_hosts(what="runtime-watcher"), ("en", "no")):
    work = Path(tempfile.mkdtemp(prefix="foamy-github-watcher-"))
    atexit.register(shutil.rmtree, work, True)
    plugin, counter, memory = fake_plugin(work)
    # The saved folder, which differs from the default (none) as a user's does.
    folder = work / "repositories"
    folder.mkdir()
    catalog = json.loads((PLUGIN / "i18n" / f"{language}.json").read_text())
    warning = catalog[WARNING] + " "
    # No entry: nothing can match, so the run fails and shows what was there.
    exited = catalog.get(EXITED, "")
    shell_qml = (
        SHELL.replace("@WARNING@", json.dumps(warning)[1:-1])
        .replace("@EXITED@", json.dumps(exited)[1:-1])
        .replace("@FOLDER@", json.dumps(str(folder))[1:-1])
    )
    run = harness.Run(host, shell_qml, plugin=plugin, language=language, prefix=f"foamy-github-watcher-{host}-").start()
    shell_pid = run.process.pid
    run.wait(60)
    run.stop()
    run.ok = "FOAMY DONE" in run.log and not run.failures()
    runs = counter.read_text().strip() if counter.exists() else "0"
    if int(runs) < 3:
        run.ok = False
        print(f"the fake watcher ran {runs} times, expected at least 3")
    samples = [line.split() for line in (memory.read_text().splitlines() if memory.exists() else [])]
    if len(samples) < 3 or any(len(sample) != 2 or sample[0] != str(shell_pid) for sample in samples):
        run.ok = False
        print(f"expected peak memory samples from qs (pid {shell_pid}), got {samples}")
    else:
        growth = max(int(sample[1]) for sample in samples) - int(samples[0][1])
        print(
            f"{host}: qs peak memory grew {growth} kB over {runs} watcher runs of {STDERR_BYTES >> 20} MiB stderr each"
        )
        if growth >= GROWTH_LIMIT_KB:
            run.ok = False
            print(
                f"the shell's peak memory grew {growth} kB (limit {GROWTH_LIMIT_KB} kB): the watcher's stderr was kept"
            )
    # Any QML warning from the plugin's own files fails the run too (the
    # Service's own "foamy.github:" report of the failure is expected).
    warnings = [
        line
        for line in run.log.splitlines()
        if "WARN" in line and "/plugin/" in line and "/plugin/kit/" not in line and "foamy.github:" not in line
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
            f"{host} ({language}): watcher stderr passed ({checks[0].split('FOAMY CHECKS')[1].strip() if checks else '?'} checks)"
        )
        run.cleanup()
    else:
        status = 1
        print(f"{host} ({language}): watcher stderr FAILED; log at {run.base}/qs.log")
        print("\n".join(run.failures() or run.log.splitlines()[-30:]))
sys.exit(status)
