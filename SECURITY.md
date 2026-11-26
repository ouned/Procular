# Security policy

Procular runs as your user, but it can install a helper that runs as root through `pkexec`
([procular/helper/procular_helper.py](procular/helper/procular_helper.py), allowed by
[the polkit policy](data/io.github.ouned.Procular.policy)). Bugs there matter most.

## Reporting a vulnerability

Please do not open a public issue. Report it privately through
[GitHub's security advisories](https://github.com/ouned/Procular/security/advisories/new)
(the repository's **Security** tab, **Report a vulnerability**).

Include what an attacker needs (local user, another user, a process with certain capabilities),
the steps or a proof of concept, and the Procular version (About dialog, or `git describe`).

This is a hobby project maintained by one person. I will confirm the report as soon as I can,
fix it in a new release and credit you in the advisory unless you would rather not be named.

## Supported versions

Only the latest release gets security fixes.

## What counts

In scope, above all:

- The root helper: anything that lets a caller do more than its operations promise (signal,
  renice, ionice, affinity, OOM score, read whitelisted `/proc` files, list sockets and handles,
  close a socket), for example reading arbitrary files, acting on the wrong process, or
  escaping its input validation.
- The polkit policy and how [install.sh](install.sh), the [Makefile](Makefile) and the `.deb` /
  `.rpm` packages install the helper (paths, owners, permissions).
- The app trusting data from other users' processes in a way that lets them run code or
  commands as you.

Known and intended: after you have entered the administrator password once, polkit remembers it
for a few minutes (`auth_admin_keep`). During that time other programs running as your user can
also use the helper without asking. Install the helper only if you accept that, or run without
it: Procular then asks for the password on every privileged action.
