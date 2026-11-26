from procular.util.icons import app_keys, is_app_process

VSCODE = app_keys("com.microsoft.VSCode", "/usr/share/code/code", "Code", "/usr/share/code/code %F")


def proc(name, exe, cmdline="", **extra):
    return dict(name=name, exe=exe, cmdline=cmdline or exe, **extra)


def test_app_keys_ignore_vendor_and_wrappers():
    assert VSCODE == {"com.microsoft.vscode", "vscode", "code"}
    keys = app_keys("org.gnome.Console", "/usr/bin/flatpak", "", "/usr/bin/flatpak run --command=kgx org.gnome.Console")
    assert "kgx" in keys and "flatpak" not in keys and "gnome" not in keys


def test_tools_started_by_an_app_are_not_the_app():
    assert is_app_process(proc("code", "/usr/share/code/code"), VSCODE)
    for name, exe in (("bash", "/usr/bin/bash"), ("timeout", "/usr/bin/timeout"), ("Xvfb", "/usr/bin/Xvfb")):
        assert not is_app_process(proc(name, exe), VSCODE)
    assert not is_app_process(proc("python3.14", "/usr/bin/python3.14", "python3 -m procular"), VSCODE)
    # the terminal's shell, although its options name files of the application
    assert not is_app_process(proc("bash", "/usr/bin/bash", "/usr/bin/bash --init-file /usr/share/code/resources/"
                                   "app/out/vs/workbench/contrib/terminal/common/scripts/shellIntegration-bash.sh"),
                              VSCODE)
    assert not is_app_process(proc("bash", "/usr/bin/bash", "/bin/bash -c source /usr/share/code/x.sh"), VSCODE)
    assert not is_app_process(proc("sh", "/usr/bin/dash", "/bin/sh /usr/bin/xvfb-run -a python3"), VSCODE)
    # a tool below the home directory, in a folder that happens to be called like the application
    assert not is_app_process(proc("tool", "/home/me/code/build/tool"), VSCODE)
    assert not is_app_process(proc("bash", "/usr/bin/bash", "bash /home/me/code/build.sh"), VSCODE)


def test_differently_named_app_processes():
    chrome = app_keys("google-chrome", "/usr/bin/google-chrome-stable", "Google-chrome")
    assert is_app_process(proc("chrome", "/opt/google/chrome/chrome"), chrome)
    # the kernel cuts process names off at 15 characters
    assert is_app_process(proc("org.gnome.Weath", ""), app_keys("org.gnome.Weather"))
    # named after neither the id nor the program in the desktop file, but installed under its name
    office = app_keys("libreoffice-writer", "/usr/bin/libreoffice", "libreoffice-writer")
    assert is_app_process(proc("soffice.bin", "/usr/lib/libreoffice/program/soffice.bin"), office)
    # a script run by an interpreter
    assert is_app_process(proc("python3", "/usr/bin/python3.12", "python3 /usr/bin/lollypop"),
                          app_keys("org.gnome.Lollypop"))
    assert is_app_process(proc("gjs", "/usr/bin/gjs-console",
                               "/usr/bin/gjs -m /usr/share/org.gnome.Weather/org.gnome.Weather"),
                          app_keys("org.gnome.Weather"))
    assert is_app_process(proc("sh", "/usr/bin/dash", "/bin/sh /usr/share/code/bin/code --wait"), VSCODE)


def test_flatpak_app_files():
    keys = app_keys("org.example.Editor", "/usr/bin/flatpak", "", "flatpak run --command=editor-wrapper org.example.Editor")
    assert is_app_process(proc("main-bin", "/app/libexec/main-bin", sandboxed="flatpak"), keys)
    assert not is_app_process(proc("bwrap", "/usr/bin/bwrap", sandboxed="flatpak"), keys)


def test_icon_cache_distinguishes_service_from_regular_process():
    from procular.util.icons import GENERIC_ICON, SERVICE_ICON, IconResolver

    resolver = IconResolver()
    resolver._index = {}
    svc = {"name": "worker", "exe": "/usr/libexec/worker", "unit_kind": "service", "unit_scope": "system"}
    usr = {"name": "worker", "exe": "/usr/local/bin/worker", "unit_kind": "", "unit_scope": ""}
    assert resolver.resolve(svc) is SERVICE_ICON
    assert resolver.resolve(usr) is GENERIC_ICON


def test_lookup_app_skips_unshowable_xpm_icon(tmp_path, monkeypatch):
    from gi.repository import Gio

    from procular.util.icons import IconResolver

    xpm = tmp_path / "app.xpm"
    xpm.write_text("/* XPM */")
    fake_info = type("FakeInfo", (), {"get_icon": lambda self: Gio.FileIcon.new(Gio.File.new_for_path(str(xpm)))})()
    monkeypatch.setattr(Gio.DesktopAppInfo, "new", lambda _name: fake_info)
    resolver = IconResolver()
    resolver._index = {}
    assert resolver.lookup_app("example") is None
