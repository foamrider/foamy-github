"""`pick-folder` through the process: the JSON line and exit code the panel
reads (Model.folderPickReply), on a bus the test owns or on none.

Black-box (see blackbox.py). The helper never sees the caller's session bus:
every call sets or removes DBUS_SESSION_BUS_ADDRESS itself and runs with the
test's own XDG_RUNTIME_DIR. The private dbus-daemon has no service
directory, so nothing can be started on it, and no portal answers."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest

from blackbox import SYSTEM_PATH, HelperTest

DBUS_DAEMON = shutil.which("dbus-daemon", path=SYSTEM_PATH)
# A unix socket path must fit in sun_path (108 bytes with its terminator).
SOCKET_PATH_MAX = 100


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
        result = self.run_helper("pick-folder", *args, env=env, timeout=30)
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

    @unittest.skipUnless(DBUS_DAEMON, "needs dbus-daemon")
    def test_a_real_bus_without_a_portal_is_unavailable(self):
        # A real daemon accepts the client's authentication, Hello, AddMatch
        # and OpenFile; with no portal on the bus the reply is "unavailable".
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
