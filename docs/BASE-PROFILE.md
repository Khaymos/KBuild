# KhaymOS installation profiles

## Base

Base is the complete headless KhaymOS operating system. It is also the
foundation for server workloads; there is no separate Server installation
profile.

Base includes package and account administration, OpenDoas, hardware
diagnostics, network diagnostics, everyday terminal utilities, OpenRC,
networking, and time synchronization.

OpenDoas is the default privilege-escalation tool. Its policy permits members
of the `wheel` group to execute commands as root. The installer must create a
regular user and explicitly offer wheel membership; installing Doas alone
does not create an unprivileged account.

Chrony is enabled for time synchronization. Logrotate is installed with a
configuration file but is not scheduled until KhaymOS includes a cron daemon
or an equivalent timer facility.

Hardware tools such as `smartctl`, `nvme`, `mmc`, and `efibootmgr` are
manual administrative tools; the operating system does not execute them
automatically.

## Desktop

Desktop is Base plus exactly one selected desktop environment. Planned
choices are XFCE, KDE Plasma, GNOME, and Sway. The installer should offer
Desktop only after desktop package groups, session integration, and required
graphics/audio components are implemented and tested.
