import importlib.util
import os
import re

import pytest

from procular.util import privileged

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# pkg/ is no Python package (and "packaging" would clash with the PyPI module): load by path
_spec = importlib.util.spec_from_file_location("pkgversion", os.path.join(ROOT, "pkg", "pkgversion.py"))
pkgversion = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pkgversion)


@pytest.mark.parametrize("version, deb, rpm", [
    ("1.2.3", "1.2.3", "1.2.3"),
    ("v1.2.3", "1.2.3", "1.2.3"),
    ("1.2.3-dirty", "1.2.3+dirty", "1.2.3^dirty"),
    ("1.2.3-4-gabc1234", "1.2.3+git4.gabc1234", "1.2.3^git4.gabc1234"),
    ("1.2.3-4-gabc1234-dirty", "1.2.3+git4.gabc1234.dirty", "1.2.3^git4.gabc1234.dirty"),
    ("0.0.0-gabc1234", "0.0.0+gabc1234", "0.0.0^gabc1234"),
    ("0.0.0-g1234567-dirty", "0.0.0+g1234567.dirty", "0.0.0^g1234567.dirty"),
    ("1.2.3rc1", "1.2.3~rc1", "1.2.3~rc1"),
    ("1.2.3-beta.1", "1.2.3~beta.1", "1.2.3~beta.1"),
    ("1.2.3rc1-2-gabc1234", "1.2.3~rc1+git2.gabc1234", "1.2.3~rc1^git2.gabc1234"),
])
def test_package_version(version, deb, rpm):
    assert pkgversion.package_version(version, "deb") == deb
    assert pkgversion.package_version(version, "rpm") == rpm
    # a native Debian version has no revision, an RPM version no hyphen at all
    assert re.fullmatch(r"[0-9][A-Za-z0-9.+~]*", deb)
    assert re.fullmatch(r"[0-9][A-Za-z0-9.^~_]*", rpm)


@pytest.mark.parametrize("version", ["", "abc", "1.2.3 foo", "1.2.3-x"])
def test_package_version_rejects(version):
    with pytest.raises(ValueError):
        pkgversion.package_version(version, "deb")


def test_package_version_unknown_format():
    with pytest.raises(ValueError):
        pkgversion.package_version("1.2.3", "apk")


def test_installed_helper_order(tmp_path):
    packaged, local = tmp_path / "usr-libexec", tmp_path / "usr-local-libexec"
    for path in (packaged, local):
        path.write_text("#!/bin/sh\n")
        path.chmod(0o755)
    installed = (str(packaged), str(local))
    assert privileged.helper_command(installed) == ["pkexec", str(packaged)]
    packaged.unlink()
    assert privileged.helper_command(installed) == ["pkexec", str(local)]
    local.chmod(0o644)  # not executable: the bundled script instead
    cmd = privileged.helper_command(installed)
    assert cmd[0] == "pkexec" and cmd[-1] == privileged.SOURCE_HELPER


def test_policy_names_the_helper_install_sh_installs():
    # Makefile rewrites this path to $(LIBEXECDIR)/procular-helper
    with open(os.path.join(ROOT, "data", "io.github.ouned.Procular.policy")) as fh:
        assert "/usr/local/libexec/procular-helper" in fh.read()
    with open(os.path.join(ROOT, "Makefile")) as fh:
        assert "s|/usr/local/libexec/procular-helper|$(HELPER)|" in fh.read()
