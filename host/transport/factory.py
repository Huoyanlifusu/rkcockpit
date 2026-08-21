"""Utilities for host.transport.factory."""
from host.transport.base import TransportError
from host.transport.local import LocalTransport


def make_transport(device, control_dir=None, scheduler=None, device_id=None,
                   workload="foreground"):
    """Handle make transport."""
    kind = (device.get("type") or "").lower()
    if kind == "local":
        return LocalTransport(device.get("local_root"))
    if kind == "ssh":
        from host.transport.ssh import SSHTransport
        return SSHTransport(host=device.get("host"), port=int(device.get("port") or 22),
                            user=device.get("user") or "root",
                            password=device.get("_password"),
                            key_path=device.get("_key_path"),
                            control_dir=control_dir,
                            scheduler=scheduler,
                            device_id=device_id or device.get("id"),
                            workload=workload)
    if kind == "adb":
        from host.transport.adb import AdbTransport
        return AdbTransport(serial=device.get("host"))
    raise TransportError("未知设备类型: %r" % kind)
