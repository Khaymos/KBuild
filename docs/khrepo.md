# KhaymOS Repository Manager

`khrepo` is one Python 3 program used in two places:

- **Client (Artix/KBuild host):** validates local `.khmpkg` archives and sends repository operations over SSH.
- **Server (Toshiba/Alpine):** validates uploads again, changes the live package set, stores recoverable copies, and regenerates the KNPM-compatible repository index.

It uses only the Python standard library. There is no repository-admin HTTP endpoint and no additional listening service. The public repository remains static under the existing Nginx configuration.

## Repository paths

The manager uses the existing layout:

- Packages: `/srv/khaymos/repo/x86_64/khaymos/packages/`
- Index: `/srv/khaymos/repo/x86_64/khaymos/index.tsv`
- Official mirrorlist: `/srv/khaymos/repo/mirrorlists/official.list`
- Private administration state: `/srv/khaymos/.khrepo/`

Administrative state is deliberately outside the Nginx-served repository tree. Deleted archives are kept under `.khrepo/trash/`; replaced archives are kept under `.khrepo/backups/`.

## Install

Python 3 is required on both machines.

### Client (Artix)

From the KBuild checkout:

```sh
install -Dm0755 scripts/khrepo.py "$HOME/.local/bin/khrepo"
```

Make sure `$HOME/.local/bin` is in `PATH`. The default SSH host is the existing `khaymos-repo` alias used by `scripts/publish-repo.sh`. Override it with `--host HOST` or `KHAYMOS_REPO_HOST`.

### Server (Toshiba / Alpine)

Copy the same script to the Toshiba:

```sh
scp scripts/khrepo.py khaymos-repo:/tmp/khrepo.py
```

On the Toshiba, install Python 3 if it is not already available, then install the server copy:

```sh
doas apk add python3
doas install -D -m 0755 /tmp/khrepo.py /usr/local/sbin/khrepo
```

The SSH account used by the client needs permission to run this one command as root without an interactive password prompt, because package upload data is streamed over SSH standard input. Add this rule to the active OpenDoas configuration (normally `/etc/doas.conf`), adapting the account name if needed:

```text
permit nopass yaffs2 as root cmd /usr/local/sbin/khrepo
```

Keep the existing doas rules; add this as a separate rule. The privilege is limited to the repository-manager executable. Its server mode accepts only validated repository operations and uses fixed repository paths by default.

## Commands

Run these from the KBuild checkout or anywhere the client executable is installed:

```sh
khrepo status
khrepo list
khrepo upload packages/*.khmpkg
khrepo upload --replace packages/example-1.2.3.khmpkg
khrepo delete example-1.2.3.khmpkg
khrepo trash
khrepo restore 20261119T120000Z-a1b2c3d4--example-1.2.3.khmpkg
khrepo reindex
khrepo mirrorlist config/official.mirrorlist
```

Global options go before the command, for example:

```sh
khrepo --host khaymos-repo status
```

### Upload

The client validates every selected archive locally, then sends all selected files in one SSH batch. The server validates the archives again, checks package identity collisions, stages the complete batch, and publishes a regenerated `index.tsv` only after the package files have been installed.

Re-uploading an identical archive is harmless and reported as unchanged. Replacing an existing filename with different content requires `--replace`; the previous file is preserved in the server backups directory.

### Delete and restore

`delete` asks for confirmation unless `--yes` is provided. Deletion moves packages to recoverable trash and rebuilds the index; it does not permanently erase archives.

`trash` lists both deleted packages and previous versions saved during replacement. Pass an ID from that list to `restore`. Restoring a trash entry requires its filename to be free. Restoring a backup rolls the previous archive back into the live repository and moves the currently active archive into trash.

There is intentionally no permanent-purge command yet.

### Status and index

`status` revalidates all active package archives and compares the computed index bytes with the published `index.tsv`. A stale or missing index returns a non-zero status.

`reindex` validates every active archive and atomically regenerates the index without uploading or deleting packages.

### Mirrorlist

`mirrorlist` validates and atomically publishes the supplied official mirrorlist. The server requires a UTF-8 file containing a `[mirror]` section and an HTTPS URL.

## Safety properties

- SSH handles transport and authentication; no admin API is exposed through Nginx.
- Both client and server validate package metadata and required control members.
- The server rejects unsafe names, symlink uploads, duplicate package identities, malformed archives and oversized batches.
- Package operations are serialized with an exclusive file lock.
- Index files are generated as temporary files and atomically renamed into place.
- Deletions and replacements retain recoverable copies outside the public web tree.
- The manager never automatically purges old package archives.
