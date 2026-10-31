"""Select unmounted USB filesystems for installer catch-up (no device operations)."""
import json
import sys


def walk(device):
    yield device
    for child in device.get("children", []):
        yield from walk(child)


def check_system_disks(devices):
    for disk in devices:
        if disk.get("tran") == "usb" and any(
            mount in ("/", "/boot", "/boot/firmware")
            for node in walk(disk) for mount in (node.get("mountpoints") or [])
        ):
            raise ValueError("USB automount cannot be enabled with a USB system/boot disk; choose Disabled.")


def candidates(devices):
    for disk in devices:
        nodes = list(walk(disk))
        if disk.get("tran") != "usb" or any(
            any(node.get("mountpoints") or []) for node in nodes
        ):
            continue
        for node in nodes:
            if (node.get("type") in ("disk", "part") and node.get("fstype")
                    and node.get("uuid") and node.get("name", "").startswith("/dev/")):
                yield node["name"]


if __name__ == "__main__":
    with open(sys.argv[1], encoding="utf-8") as stream:
        data = json.load(stream)
    if "--check-system" in sys.argv[2:]:
        try:
            check_system_disks(data["blockdevices"])
        except ValueError as error:
            sys.exit(str(error))
    else:
        for device in candidates(data["blockdevices"]):
            print(device)
