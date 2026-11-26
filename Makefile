# System-wide installation, used by the .deb and .rpm builds (pkg/).
#   make install DESTDIR=/tmp/root PREFIX=/usr
# For a per-user installation use ./install.sh instead.

PREFIX     ?= /usr/local
BINDIR     ?= $(PREFIX)/bin
DATADIR    ?= $(PREFIX)/share
LIBEXECDIR ?= $(PREFIX)/libexec
APPDIR     ?= $(DATADIR)/procular
# polkit reads actions only from here, whatever the prefix
POLKITDIR  ?= /usr/share/polkit-1/actions
PYTHON     ?= python3

APP_ID = io.github.ouned.Procular
HELPER = $(LIBEXECDIR)/procular-helper

all:
	@:

install:
	install -d $(DESTDIR)$(APPDIR)
	tar -c --exclude=__pycache__ --exclude='*.pyc' --exclude=_version.txt \
	    --owner=0 --group=0 --mode=u=rwX,go=rX procular | tar -x -C $(DESTDIR)$(APPDIR)
	# git describe in a checkout, else the version stamped into the source tree
	$(PYTHON) procular/_version.py > $(DESTDIR)$(APPDIR)/procular/_version.txt
	# -B: a Python other than the one the package was byte-compiled for must not write
	# __pycache__ into $(APPDIR) when run as root (uninstalling would leave it behind)
	install -d $(DESTDIR)$(BINDIR)
	printf '#!/usr/bin/python3 -sPB\nimport sys\nsys.path.insert(0, "%s")\nfrom procular.__main__ import main\nsys.exit(main())\n' \
	    '$(APPDIR)' > $(DESTDIR)$(BINDIR)/procular
	chmod 755 $(DESTDIR)$(BINDIR)/procular
	install -D -m 755 procular/helper/procular_helper.py $(DESTDIR)$(HELPER)
	install -d $(DESTDIR)$(POLKITDIR)
	sed 's|/usr/local/libexec/procular-helper|$(HELPER)|' data/$(APP_ID).policy \
	    > $(DESTDIR)$(POLKITDIR)/$(APP_ID).policy
	chmod 644 $(DESTDIR)$(POLKITDIR)/$(APP_ID).policy
	install -D -m 644 data/$(APP_ID).desktop $(DESTDIR)$(DATADIR)/applications/$(APP_ID).desktop
	install -D -m 644 data/$(APP_ID).metainfo.xml $(DESTDIR)$(DATADIR)/metainfo/$(APP_ID).metainfo.xml
	install -D -m 644 data/$(APP_ID).svg $(DESTDIR)$(DATADIR)/icons/hicolor/scalable/apps/$(APP_ID).svg
	install -D -m 644 data/$(APP_ID)-16.svg $(DESTDIR)$(DATADIR)/icons/hicolor/16x16/apps/$(APP_ID).svg
	install -D -m 644 data/$(APP_ID)-32.svg $(DESTDIR)$(DATADIR)/icons/hicolor/32x32/apps/$(APP_ID).svg

uninstall:
	rm -rf $(DESTDIR)$(APPDIR)
	rm -f $(DESTDIR)$(BINDIR)/procular $(DESTDIR)$(HELPER) $(DESTDIR)$(POLKITDIR)/$(APP_ID).policy \
	    $(DESTDIR)$(DATADIR)/applications/$(APP_ID).desktop \
	    $(DESTDIR)$(DATADIR)/metainfo/$(APP_ID).metainfo.xml \
	    $(DESTDIR)$(DATADIR)/icons/hicolor/scalable/apps/$(APP_ID).svg \
	    $(DESTDIR)$(DATADIR)/icons/hicolor/16x16/apps/$(APP_ID).svg \
	    $(DESTDIR)$(DATADIR)/icons/hicolor/32x32/apps/$(APP_ID).svg

.PHONY: all install uninstall
