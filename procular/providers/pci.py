"""PCI device names from the pci.ids database."""

_PCI_IDS = ("/usr/share/hwdata/pci.ids", "/usr/share/misc/pci.ids", "/usr/share/pci.ids")


def pci_device_name(vendor, device, paths=_PCI_IDS):
    """Look up a PCI device in pci.ids, e.g. (0x1002, 0x1681) -> "Rembrandt [Radeon 680M]"."""
    ven, dev = f"{vendor:04x}", f"{device:04x}"
    for path in paths:
        try:
            fh = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            in_vendor = False
            for line in fh:
                if not line.strip() or line.startswith("#"):
                    continue
                if not line[0].isspace():  # vendor line, or a later non-vendor section (classes etc.)
                    if in_vendor:
                        return None
                    in_vendor = line[:4].lower() == ven
                elif in_vendor and line.startswith("\t") and not line.startswith("\t\t"):
                    if line[1:5].lower() == dev:
                        return line[5:].strip()
        return None
    return None
