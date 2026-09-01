# Yahoo Deleted Mail → OneDrive

This independent service copies Yahoo messages that have deletion evidence into
OneDrive for convenient browsing from Windows. The immutable Backblaze B2
archive remains the source of truth.

## Guarantees

- Runs continuously on Ubuntu; Windows does not need to be on.
- Reads immutable B2 deletion-event JSON and archived RFC822 `.eml` files.
- Has no Yahoo credentials and cannot alter the Yahoo mailbox.
- Uses a bucket-restricted, read-only B2 application key.
- Uses only `rclone copyto --immutable`; it never syncs, moves, or deletes.
- Queues failed copies in its own crash-safe SQLite database and retries hourly.
- Deduplicates by full SHA-256, so Trash and permanent-deletion events do not
  create duplicate message files.
- Ignores Bulk, Spam, and Junk events.

The exporter treats these events as deletion evidence:

- `trash_observed`
- `trash_disappeared`
- `unexplained_disappearance`

An event without an archived message SHA-256 is recorded as unavailable because
there is no verified `.eml` that can be copied.

## OneDrive layout

```text
Yahoo Deleted Mail/
  <yahoo-account>/
    <deletion-year>/
      <message-date>_<source-folder>_<subject>_<hash>.eml
```

There are no month or day subfolders.

## Install on the Ubuntu archive VM

Clone the repository at its production path:

```bash
sudo git clone https://github.com/antalgebra/yah-deleted-onedrive.git \
  /opt/yah-deleted-onedrive/src
sudo bash /opt/yah-deleted-onedrive/src/deploy/install.sh
```

The installer creates the unprivileged `yahdeleted` service account, a separate
Python environment, protected configuration/state directories, and the systemd
unit. It does not start the exporter before configuration is verified.

## Backblaze key

Create a new application key in Backblaze with:

- Access limited to the mail archive bucket
- **Read Only** access
- No expiration unless intentional

Do not reuse the archiver's write key. The onboarding wizard validates
`listFiles` and `readFiles` and rejects a key that cannot read the archive.

## Microsoft authorization from Windows

The exporter supports both Personal and Microsoft 365 Business OneDrive. Rclone
asks which drive to use during authorization.

Open the SSH connection with a temporary browser-authentication tunnel:

```powershell
ssh -L 53682:localhost:53682 <admin-user>@<tailscale-ip>
```

Then, in that remote Ubuntu session, start the onboarding wizard:

```bash
sudo /opt/yah-deleted-onedrive/venv/bin/python \
  /opt/yah-deleted-onedrive/src/onboard.py
```

The wizard automatically discovers the configured Yahoo account IDs. It asks
once for the read-only B2 key, validates it, and launches rclone only if an
OneDrive remote does not already exist. Its refreshable OAuth configuration is
kept in the service-owned state directory so rclone can update it atomically.

When rclone opens:

1. Create a remote named `onedrive`.
2. Choose `Microsoft OneDrive`.
3. Leave Microsoft client ID and client secret blank.
4. Do not use advanced configuration.
5. Choose browser authentication and open the displayed localhost URL in the
   Windows browser. The SSH tunnel carries the callback to the VM.
6. Sign into either Personal or Business OneDrive and select the desired drive.

Keep rclone's `Configuration complete` block private because it contains live
Microsoft OAuth tokens.

The wizard verifies that it can create `Yahoo Deleted Mail`, enables the service,
runs an immediate scan, and then scans once every 3,600 seconds.

## Operations

```bash
systemctl status yah-deleted-onedrive.service --no-pager
sudo journalctl -u yah-deleted-onedrive.service -n 50 --no-pager
```

Run an immediate one-time pass without waiting for the hourly interval:

```bash
sudo -u yahdeleted /opt/yah-deleted-onedrive/venv/bin/python \
  /opt/yah-deleted-onedrive/src/exporter.py --once
```

Rerun `onboard.py` to refresh the discovered Yahoo account list, rotate the B2
read-only key, or select a different OneDrive remote/folder. Existing OneDrive
copies and B2 objects are never removed.
