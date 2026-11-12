#!/usr/bin/env python3
"""Pull all, end to end: the real Panel.qml and Service in an isolated
offscreen Quickshell (foamy-kit's harness) over local Git repositories.

  python3 tests/runtime-pullall.py

Three clones of one local remote are behind it; one also has local changes.
Pull all pulls the two clean ones, one after the other, leaves the changed
one alone, and reports the count; its header button shows only while
something can be pulled. Requires ~/GitHub/Personal/foamy-kit (or
FOAMY_KIT).

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
SHELL = r'''
import QtQuick
import Quickshell
import "harness"
import "plugin" as Plugin

ShellRoot {
  id: shell
  property var panel: null
  property int checks: 0
  property double startedAt: 0
  function check(ok, what) { checks++; if (!ok) console.log("FOAMY FAIL " + what) }
  function collect(item, test) {
    var found = test(item) ? [item] : []
    for (var i = 0; i < item.children.length; i++) found = found.concat(collect(item.children[i], test))
    return found
  }
  function finish() { console.log("FOAMY CHECKS " + checks); console.log("FOAMY DONE"); Qt.quit() }

  FloatingWindow {
    implicitWidth: 800; implicitHeight: 600
    Item { id: host; width: 800; height: 600 }
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

  // Wait for the first status, start Pull all, then wait for it to finish.
  // The header's Pull all shows only while something can be pulled.
  Timer {
    id: poll
    interval: 200
    repeat: true
    property int phase: 0
    onTriggered: {
      var service = Plugin.Service
      if (phase === 0) {
        if (service.repos.length < 3 || service.busy) return
        check(service.refreshIntervalSec === 0 && service.fetchIntervalSec === 86400, "0 keeps timed refresh off")
        check(service.pullableRepos.length === 2, "two clean clones can pull: " + service.pullableRepos.length)
        var view = panel.popup.bodyItem
        check(view !== null && view.showPullAll && view.pullAllStop === view.stops.count
          && view.settingsStop === view.pullAllStop + 1, "Pull all shown, with its cursor stop before Settings")
        var icons = view ? collect(view, function(item) { return item.name === "arrow-down-to-line" && item.known !== undefined }) : []
        check(icons.length === 1 && icons[0].known, "Pull all draws arrow-down-to-line from the kit")
        service.pullAll()
        check(service.pullingAll && service.repositoryActionRunning, "Pull all started")
        service.pullAll()
        check(service.pullingAll, "a second click while pulling is ignored")
        phase = 1
      } else if (phase === 1) {
        if (service.pullingAll || service.busy) return
        check(service.actionStatus === "Pulled 2 repositories", "count reported: " + JSON.stringify(service.actionStatus))
        check(service.operationError === "", "no failures: " + service.operationError)
        check(service.pullableRepos.length === 0, "nothing left to pull")
        var after = panel.popup.bodyItem
        check(after !== null && !after.showPullAll && after.pullAllStop === -1
          && after.settingsStop === after.stops.count, "Pull all hidden once nothing is left")
        stop()
        finish()
      }
    }
  }
  Timer { interval: 40000; running: true; onTriggered: { check(false, "timed out"); shell.finish() } }
}
'''


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": "/dev/null"})


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
clones = [ROOT / name for name in ("alpha", "beta", "gamma")]
for clone in clones:
    git(ROOT, "clone", "-q", str(REMOTE), str(clone))
(seed / "a.txt").write_text("two\n")
git(seed, "commit", "-q", "-am", "two")
git(seed, "push", "-q")
for clone in clones:
    git(clone, "fetch", "-q")
(clones[2] / "local.txt").write_text("uncommitted\n")
remote_head = head(seed)

status = 0
for host in harness.available_hosts(what="runtime-pullall"):
    for clone in clones:                 # each host starts behind again
        if head(clone) == remote_head:
            git(clone, "reset", "-q", "--hard", "HEAD~1")
    run = harness.Run(host, SHELL.replace("@ROOT@", str(ROOT)), plugin=PLUGIN,
                      prefix=f"foamy-github-pullall-{host}-").run(timeout=60)
    pulled = [head(clone) == remote_head for clone in clones]
    if pulled != [True, True, False]:
        run.ok = False
        print(f"pulled clones {pulled}, expected [True, True, False]")
    if run.ok:
        print(f"{host}: pull all passed")
        run.cleanup()
    else:
        status = 1
        print(f"{host}: pull all FAILED; log at {run.base}/qs.log")
        print("\n".join(run.failures() or run.log.splitlines()[-30:]))
sys.exit(status)
