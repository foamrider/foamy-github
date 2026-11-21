"""`pick-folder` through the process: the JSON line and exit code the panel
reads (Model.folderPickReply), on a bus the test owns or on none.

Black-box (see blackbox.py). The helper never sees the caller's session bus:
every call sets or removes DBUS_SESSION_BUS_ADDRESS itself and runs with the
test's own XDG_RUNTIME_DIR. The private dbus-daemon has no service
directory, so nothing can be started on it, and no portal answers.

Without an address and without XDG_RUNTIME_DIR the helper falls back to
/run/user/<uid>/bus, the desktop's bus. So that a slip in the helper's
lookup cannot open a real file chooser, every call runs under bubblewrap
with an empty /run/user when bubblewrap works. Without bubblewrap (the CI
runner, which has no desktop session) that fallback is the one path left
to a real bus."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest

from blackbox import BWRAP, SYSTEM_PATH, HelperTest, command

DBUS_DAEMON = shutil.which("dbus-daemon", path=SYSTEM_PATH)
# A unix socket path must fit in sun_path (108 bytes with its terminator).
SOCKET_PATH_MAX = 100
# Hides the desktop's bus at /run/user/<uid>/bus from the helper.
HIDE_USER_RUNTIME = ["bwrap", "--dev-bind", "/", "/", "--tmpfs", "/run/user", "--"] if BWRAP else []


class PickFolderTest(HelperTest):
    def setUp(self):
        super().setUp()
        self.owned = str(self.root)  # where the helper may look for a bus

    def pick(self, *args, bus):
        """Runs pick-folder with `bus` as the session bus address (None: none
        set); returns (exit code, reply)."""
        env = dict(self.env)
        env.pop("DBUS_SESSION_BUS_ADDRESS", None)
        if bus is not None:
            env["DBUS_SESSION_BUS_ADDRESS"] = bus
        self.assertTrue(
            env["XDG_RUNTIME_DIR"].startswith(self.owned),
            f"pick-folder would look for the bus in {env['XDG_RUNTIME_DIR']}",
        )
        # The test's own bus must stay visible under the empty /run/user.
        self.assertFalse(
            HIDE_USER_RUNTIME and self.owned.startswith("/run/user/"), f"{self.owned} is under the hidden /run/user"
        )
        result = subprocess.run(
            [*HIDE_USER_RUNTIME, *command("pick-folder", *args)], capture_output=True, env=env, timeout=30
        )
        lines = result.stdout.splitlines()
        self.assertEqual(len(lines), 1, result)
        return result.returncode, json.loads(lines[0])

    def test_more_than_two_arguments_are_refused(self):
        code, reply = self.pick("a", "b", "c", bus=f"unix:path={self.root}/absent")
        self.assertEqual((code, reply["ok"], reply["code"]), (1, False, "INVALID_ARGUMENT"))

    def test_without_a_session_bus(self):
        # A dead socket, an address without a unix socket, and none at all
        # (then $XDG_RUNTIME_DIR/bus, which the test's directory does not have).
        for bus in (f"unix:path={self.root}/absent", "tcp:host=localhost,port=1", None):
            with self.subTest(bus=bus):
                code, reply = self.pick(bus=bus)
                self.assertEqual((code, reply["ok"], reply["code"]), (1, False, "NO_SESSION_BUS"))

    def test_a_real_bus_without_a_portal_is_unavailable(self):
        # A real daemon accepts the client's authentication, Hello, AddMatch
        # and OpenFile; with no portal on the bus the reply is "unavailable".
        self.assertIsNotNone(DBUS_DAEMON, "dbus-daemon is not in /usr/bin: install dbus, the test starts its own bus")
        runtime = Path(self.env["XDG_RUNTIME_DIR"])
        if len(os.fsencode(runtime / "bus")) > SOCKET_PATH_MAX:
            runtime = Path(tempfile.mkdtemp(prefix="foamy-github-bus-", dir="/tmp"))
            self.addCleanup(shutil.rmtree, runtime, True)
            self.env["XDG_RUNTIME_DIR"] = self.owned = str(runtime)
        socket = runtime / "bus"
        config = self.root / "bus.conf"
        config.write_text(f"""<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <listen>unix:path={socket}</listen>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>
""")
        daemon = subprocess.Popen(
            [DBUS_DAEMON, f"--config-file={config}", "--nofork", "--nopidfile"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=self.env,
        )
        self.addCleanup(lambda: (daemon.terminate(), daemon.wait(5)))
        deadline = time.monotonic() + 10
        while not socket.exists():
            self.assertIsNone(daemon.poll(), "dbus-daemon exited")
            self.assertLess(time.monotonic(), deadline, "dbus-daemon never listened")
            time.sleep(0.02)
        code, reply = self.pick("Title", "Choose", bus=None)  # found through XDG_RUNTIME_DIR
        self.assertEqual((code, reply["ok"], reply["code"]), (1, False, "PORTAL_UNAVAILABLE"))


if __name__ == "__main__":
    unittest.main()
