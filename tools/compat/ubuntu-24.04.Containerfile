# Oldest supported stack: libadwaita 1.5, GTK 4.14, Python 3.12, PyGObject 3.48.
# Used by tools/compat-test.sh.
FROM docker.io/library/ubuntu:24.04

# e.g. --build-arg MIRROR=http://de.archive.ubuntu.com/ubuntu when archive.ubuntu.com is slow
ARG MIRROR=
RUN if [ -n "$MIRROR" ]; then \
      sed -i "s|http://archive.ubuntu.com/ubuntu|$MIRROR|" /etc/apt/sources.list.d/ubuntu.sources; fi \
 && apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1 python3-psutil python3-pytest \
      mutter dbus-daemon librsvg2-common adwaita-icon-theme fonts-ubuntu fonts-dejavu-core \
      git procps iproute2 libegl1 libgl1-mesa-dri \
      gsettings-desktop-schemas dconf-gsettings-backend ubuntu-settings \
 && rm -rf /var/lib/apt/lists/*

# The off-screen session (tools/headless-session.sh) has no desktop settings. Without a DPI, libadwaita 1.5
# converts "sp" sizes to negative pixels: Adw.Clamp then squeezes pages to their minimum
# width and "max-width: …sp" breakpoints never apply. A real desktop always sets both.
# Font: Ubuntu's default UI font on that release. tools/compat-test.sh uses the same
# settings for the host screenshots, so the two are comparable.
COPY gtk-settings.ini /etc/gtk-4.0/settings.ini

ENV LANG=C.UTF-8 PYTHONDONTWRITEBYTECODE=1
