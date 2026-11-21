"""The cases of kit/clipboard.sh and SafeTextField that foamy-kit's own
tests do not have yet: the producer stops when the caller kills the
clipboard.sh wrapper, and the single-line field's paste flow in QML. The
other clipboard.sh cases (unicode, limits, refused input, the 2 s deadline)
are foamy-kit's tests/test_clipboard.py, on the same file.

The stand-in wl-paste, clipboard-fixture and timeout come from the kit's
hostenv.clipboard_fakes(). Requires ~/GitHub/Personal/foamy-kit (or
FOAMY_KIT); the shell case needs no Quickshell or Omarchy host, so
tests/suite.sh --ci runs it."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "kit/clipboard.sh"
KIT = Path(os.environ.get("FOAMY_KIT", Path.home() / "GitHub/Personal/foamy-kit"))
if (KIT / "preview/hostenv.py").is_file():
    sys.path.insert(0, str(KIT / "preview"))
    import hostenv
else:
    hostenv = None


class ClipboardTests(unittest.TestCase):
    def setUp(self):
        if hostenv is None:
            self.skipTest(f"foamy-kit not found at {KIT} (set FOAMY_KIT)")
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.env = dict(os.environ, **hostenv.clipboard_fakes(self.base / "bin"))
        self.state = Path(self.env["TEST_CLIPBOARD_STATE"])

    def assert_producer_stopped(self):
        pid = Path(str(self.state) + ".pid").read_text()
        status = Path(f"/proc/{pid}/stat")
        # The group leader can close stdout before a killed child is scheduled
        # to exit. Observe the child's state with a bounded wait, without the
        # exists/read race when /proc removes it between those two operations.
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            try:
                state = status.read_text().split()[2]
            except (FileNotFoundError, ProcessLookupError):  # gone before the open, or before the read
                return
            if state == "Z":
                return
            time.sleep(0.01)
        self.fail("clipboard producer survived its deadline")

    def test_watchdog_survives_wrapper_destruction(self):
        self.state.write_text("stall")
        with subprocess.Popen(
            ["bash", str(HELPER), "clipboard"], env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        ) as process:
            pid_file = Path(str(self.state) + ".pid")
            deadline = time.monotonic() + 2
            while not pid_file.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(pid_file.exists(), "clipboard producer did not start")
            process.kill()
            process.communicate(timeout=4)
        self.assert_producer_stopped()

    def test_qml_editing_and_paste(self):
        runner = shutil.which("qs")
        if not runner:
            self.skipTest("Quickshell is unavailable")
        if os.environ.get("FOAMY_SUITE_CI"):
            self.skipTest("tests/suite.sh --ci runs no Quickshell")

        # Quickshell's statically registered Io plugin requires the qs executable.
        # A stock qmltestrunner cannot load it as a dynamic QML plugin.
        fixture = self.base / "shell.qml"
        fixture.write_text(
            """import QtQuick
import Quickshell
ShellRoot {
    FloatingWindow {
        title: "Foamy clipboard tests"
        visible: true
        implicitWidth: 500
        implicitHeight: 250
        Loader { anchors.fill: parent; source: %s }
    }
}
"""
            % json.dumps(str(ROOT / "tests/tst_clipboard.qml"))
        )
        # The kit's SafeTextField imports qs.Commons and qs.Ui from the host shell (read-only links).
        host = Path(os.environ.get("OMARCHY_SHELL_PATH", "/usr/share/omarchy/shell"))
        for name in ("Commons", "Ui"):
            (self.base / name).symlink_to(host / name, target_is_directory=True)
        runtime = self.base / "runtime"
        runtime.mkdir(mode=0o700)
        env = dict(
            self.env,
            QT_QPA_PLATFORM=os.environ.get("FOAMY_TEST_PLATFORM", "offscreen"),
            QT_QUICK_BACKEND="software",
            QT_QPA_PLATFORMTHEME="",
            XDG_RUNTIME_DIR=str(runtime),
        )
        wayland = env["QT_QPA_PLATFORM"] == "wayland"
        # The harness's isolation: no Omarchy bin dir or AppImage libraries,
        # no audio server, system or session bus, and the live compositor
        # only in wayland mode.
        hostenv.isolate(env, self.base, display=wayland)
        if wayland:
            # Keep test logs/IPC private without hiding the host compositor socket.
            display = Path(os.environ["WAYLAND_DISPLAY"])
            if not display.is_absolute():
                env["WAYLAND_DISPLAY"] = str(Path(os.environ["XDG_RUNTIME_DIR"]) / display)
        # Its own process group, so the pastes its Processes started stop
        # with it. Each paste may take up to 60 s on a loaded machine.
        log = self.base / "qs.log"
        with log.open("w") as out:
            with hostenv.ProcessGroup(
                [runner, "-p", str(fixture), "--no-color"], env=env, stdout=out, stderr=subprocess.STDOUT
            ) as shell:
                code = shell.wait(300)
        output = log.read_text(errors="replace")
        self.assertIsNotNone(code, "qs did not finish within 300 s:\n" + output)
        self.assertEqual(code, 0, output)
        self.assertRegex(output, r"CLIPBOARD_RESULT passed=[1-9][0-9]* failed=0")
        self.assertIn("failed=0", output, output)
        print(output[output.index("CLIPBOARD_RESULT") :].splitlines()[0])


if __name__ == "__main__":
    unittest.main()
