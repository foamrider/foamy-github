#!/usr/bin/env python3
"""A stand-in NetworkManager for runtime tests, on a test's own system bus.

  python3 tests/fake_nm.py <bus address> <connectivity file>

It owns org.freedesktop.NetworkManager with no devices and the
connectivity named in the file (NetworkManager's NMConnectivityState: 4
full, 1 none; 4 when the file is missing). It re-reads the file every
100 ms and sends PropertiesChanged when the value changes, so a test can
take the network down and up. Quickshell's Networking reads only this
object. Prints "FAKE NM READY" once it owns the name. Needs dbus-python and
PyGObject (the system's)."""

import sys
from pathlib import Path

import dbus
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib

NAME = "org.freedesktop.NetworkManager"
PATH = "/org/freedesktop/NetworkManager"


def connectivity(file):
    try:
        return int(Path(file).read_text().strip() or 4)
    except (OSError, ValueError):
        return 4


class NetworkManager(dbus.service.Object):
    def __init__(self, bus, file):
        super().__init__(bus, PATH)
        self.file = file
        self.value = connectivity(file)

    def properties(self):
        # 70: NM_STATE_CONNECTED_GLOBAL, 20: NM_STATE_DISCONNECTED.
        return {
            "Connectivity": dbus.UInt32(self.value),
            "State": dbus.UInt32(70 if self.value == 4 else 20),
            "Devices": dbus.Array([], signature="o"),
            "AllDevices": dbus.Array([], signature="o"),
            "ActiveConnections": dbus.Array([], signature="o"),
            "PrimaryConnection": dbus.ObjectPath("/"),
            "NetworkingEnabled": True,
            "WirelessEnabled": False,
            "WirelessHardwareEnabled": False,
            "ConnectivityCheckAvailable": True,
            "ConnectivityCheckEnabled": True,
            "Version": "1.50.0",
        }

    @dbus.service.method(NAME, out_signature="ao")
    def GetDevices(self):
        return dbus.Array([], signature="o")

    @dbus.service.method(NAME, out_signature="ao")
    def GetAllDevices(self):
        return dbus.Array([], signature="o")

    @dbus.service.method(NAME, out_signature="u")
    def CheckConnectivity(self):
        return dbus.UInt32(self.value)

    @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature="ss", out_signature="v")
    def Get(self, interface, name):
        return self.properties()[name]

    @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface):
        return self.properties() if interface == NAME else {}

    @dbus.service.signal(dbus.PROPERTIES_IFACE, signature="sa{sv}as")
    def PropertiesChanged(self, interface, changed, invalidated):
        pass

    def poll(self):
        value = connectivity(self.file)
        if value != self.value:
            self.value = value
            props = self.properties()
            self.PropertiesChanged(NAME, {k: props[k] for k in ("Connectivity", "State")}, [])
        return True


def main():
    address, file = sys.argv[1:3]
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.bus.BusConnection(address)
    manager = NetworkManager(bus, file)
    # Kept referenced: the name is released when the BusName is collected.
    manager.bus_name = dbus.service.BusName(NAME, bus)
    GLib.timeout_add(100, manager.poll)
    print("FAKE NM READY", flush=True)
    GLib.MainLoop().run()


if __name__ == "__main__":
    main()
