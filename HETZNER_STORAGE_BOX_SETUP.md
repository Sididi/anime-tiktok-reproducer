# Hetzner Storage Box Setup Guide

This guide explains how to configure a Hetzner Storage Box for this project.

It is written for the Storage Box integration implemented in this repository:

- Storage Box is the canonical remote origin for library releases
- the app accesses it over SFTP via `asyncssh`
- SSH key authentication is the recommended mode
- `known_hosts` pinning is supported
- the project can use either port `22` or `23`

For this project, the recommended setup is:

- use a dedicated Hetzner Storage Box
- create a dedicated sub-account for this app
- enable `SSH Support`
- use SFTP on port `23`
- authenticate with an Ed25519 SSH key
- pin the host key in a dedicated `known_hosts` file
- keep `ATR_STORAGE_BOX_PASSWORD` empty in `.env`

Port `22` also works, but it is not the preferred path for this project.

## 1. Official Hetzner behavior you need to know

Before configuring anything, keep these Hetzner specifics in mind:

- SSH port `22` is always active, but only for `SFTP` and `SCP`. There is no interactive SSH shell on port `22`.
- Enabling `SSH Support` in Hetzner Console enables port `23`.
- Port `23` supports interactive SSH, `rsync`, `BorgBackup`, and SFTP/SCP as well.
- If you access the box from your local machine, you must enable `External Reachability`.
- Hetzner supports sub-accounts. A sub-account only sees its own subdirectory. The main account sees everything.
- The hostname format is:
  - main account: `<username>.your-storagebox.de`
  - sub-account: `<username>-subX.your-storagebox.de`
- SSH key format depends on the port:
  - port `22`: RFC4716 public key format
  - port `23`: normal OpenSSH public key format
- If you want to use both ports with SSH keys, Hetzner says both formats must be present.

Project recommendation:

- use port `23` unless you have a specific reason not to
- set `ATR_STORAGE_BOX_PORT=23` explicitly in `.env`

Why port `23` is the recommended path here:

- it uses the normal OpenSSH public key format
- it avoids the RFC4716 conversion step required by port `22`
- Hetzner documents better SFTP performance on port `23`
- it is the cleanest setup for this app

## 2. Recommended topology for this project

Use this model:

- one Storage Box
- one dedicated app sub-account
- one app-private root inside that sub-account

Recommended ownership model:

- main account:
  - admin only
  - used for manual recovery, inspection, and emergency access
- app sub-account:
  - used by this backend only
  - write access
  - isolated from unrelated files

This is the safest arrangement for the current project because:

- the app credentials do not need full main-account access
- the app can only see its own subdirectory
- you can rotate the app credentials without touching the main account

## 3. Create the Storage Box in Hetzner Console

In Hetzner Console:

1. Open your project.
2. Go to `Storage Boxes`.
3. Click `Create Storage Box`.

Recommended choices:

- `Location`: choose the closest location to the machine running this app
- `Type`: choose a box large enough for releases plus snapshots
- `Access`:
  - if you already have an SSH key in Hetzner Console, you can select it during creation
  - otherwise set a password and add the SSH key afterward
- `Additional settings`:
  - enable `SSH Support`
  - enable `External Reachability` if this app runs from your local PC or any machine outside Hetzner's internal network

Important:

- Hetzner states that after the Storage Box is created, you cannot add an SSH key later via Hetzner Console
- for an existing box, SSH keys must be added on the Storage Box host itself
- selecting an SSH key during creation does not disable password authentication on the Storage Box

## 4. Create a dedicated sub-account

After the box exists, create a dedicated sub-account for this app.

Recommended:

- create exactly one write-enabled sub-account for the backend
- keep the main account for manual administration only

Example:

- main account:
  - username: `u123456`
  - host: `u123456.your-storagebox.de`
- app sub-account:
  - username: `u123456-sub1`
  - host: `u123456-sub1.your-storagebox.de`

Use the app sub-account credentials in `.env`, not the main account.

## 5. Generate the SSH key used by the app

Generate a dedicated Ed25519 key for this project.

Linux/macOS:

```bash
mkdir -p ~/.ssh
ssh-keygen -t ed25519 -a 100 -f ~/.ssh/storagebox_ed25519 -C "anime-tiktok-reproducer"
chmod 600 ~/.ssh/storagebox_ed25519
chmod 644 ~/.ssh/storagebox_ed25519.pub
```

Recommendations:

- use a dedicated key for this app, not your default `id_ed25519`
- use a passphrase if your deployment model allows it
- if the backend must run unattended, use OS-level file protections and keep the private key readable only by the app user

Windows note:

- the same key strategy works on Windows
- use an absolute path in `.env`, for example:
  - `C:\\Users\\sid\\.ssh\\storagebox_ed25519`

## 6. Add the SSH key to the Storage Box

### Recommended path: port 23

Because this project should use port `23`, upload the public key using the port `23` flow.

If your OpenSSH is recent enough:

```bash
ssh-copy-id -i ~/.ssh/storagebox_ed25519.pub -p 23 -s u123456-sub1@u123456-sub1.your-storagebox.de
```

If you prefer the Hetzner-documented generic installer:

```bash
cat ~/.ssh/storagebox_ed25519.pub | ssh -p 23 u123456-sub1@u123456-sub1.your-storagebox.de install-ssh-key
```

If you need a manual fallback:

```bash
ssh -p 23 u123456-sub1@u123456-sub1.your-storagebox.de mkdir .ssh
scp -P 23 ~/.ssh/storagebox_ed25519.pub u123456-sub1@u123456-sub1.your-storagebox.de:.ssh/authorized_keys
```

### Fallback path: port 22 only

Use this only if you intentionally do not want to enable `SSH Support`.

Hetzner requires RFC4716 format for public keys on port `22`.

Convert and upload:

```bash
ssh-keygen -e -f ~/.ssh/storagebox_ed25519.pub > ~/.ssh/storagebox_ed25519_rfc.pub
echo "mkdir .ssh" | sftp u123456-sub1@u123456-sub1.your-storagebox.de
scp ~/.ssh/storagebox_ed25519_rfc.pub u123456-sub1@u123456-sub1.your-storagebox.de:.ssh/authorized_keys
```

If you later use both ports:

- keep both key formats available on the Storage Box as Hetzner documents

## 7. Pin the host key with `known_hosts`

This project supports a dedicated `known_hosts` path:

- `ATR_STORAGE_BOX_KNOWN_HOSTS_PATH`

Use it.

Create a dedicated file instead of reusing a large shared `known_hosts` when possible:

```bash
mkdir -p ~/.ssh
ssh-keyscan -p 23 u123456-sub1.your-storagebox.de > ~/.ssh/known_hosts_storage_box
chmod 644 ~/.ssh/known_hosts_storage_box
```

Then verify the fingerprint before trusting it:

```bash
ssh-keygen -lf ~/.ssh/known_hosts_storage_box
ssh-keygen -E sha256 -lf ~/.ssh/known_hosts_storage_box
```

Verify the result against:

- the fingerprints shown in the Storage Box overview in Hetzner Console
- or the official Hetzner Storage Box fingerprint list in the Storage Box overview/general documentation

Do not skip this step.

`ssh-keyscan` only fetches the server key; it does not prove authenticity by itself.

## 8. Smoke-test the Storage Box manually

Before wiring the backend to it, verify that the app user can connect.

### Test SFTP on the recommended port 23

```bash
sftp -P 23 -i ~/.ssh/storagebox_ed25519 \
  u123456-sub1@u123456-sub1.your-storagebox.de
```

Inside the prompt, test:

```text
pwd
ls -ahl
quit
```

### Optional interactive SSH test on port 23

```bash
ssh -p 23 -i ~/.ssh/storagebox_ed25519 \
  u123456-sub1@u123456-sub1.your-storagebox.de pwd
```

### If you intentionally use port 22

```bash
sftp -P 22 -i ~/.ssh/storagebox_ed25519 \
  u123456-sub1@u123456-sub1.your-storagebox.de
```

If this fails from your laptop or workstation, check:

- `External Reachability` is enabled
- `SSH Support` is enabled if you are using port `23`
- the correct hostname is used for the sub-account
- the correct key format is installed for the chosen port
- the host fingerprint was accepted and saved correctly

## 9. Choose the remote root strategy

This app has:

- `ATR_STORAGE_BOX_ROOT`

The repository itself writes under:

```text
<remote_root>/v1/<library_type>/...
```

Recommended choices:

### Option A: dedicated sub-account, empty root

Use this if the sub-account is only for this app.

```env
ATR_STORAGE_BOX_ROOT=
```

Result:

```text
v1/anime/...
v1/simpsons/...
```

This is the cleanest option if the sub-account is already isolated.

### Option B: dedicated app prefix inside the sub-account

Use this if the sub-account will contain other things too.

```env
ATR_STORAGE_BOX_ROOT=anime-tiktok-reproducer
```

Result:

```text
anime-tiktok-reproducer/v1/anime/...
anime-tiktok-reproducer/v1/simpsons/...
```

For most users here, Option A is the best choice if the sub-account is app-only.

## 10. Configure `.env`

### Recommended `.env` for Hetzner Storage Box

Example using:

- a dedicated sub-account
- port `23`
- SSH key auth
- dedicated `known_hosts`
- empty app root because the sub-account is already isolated

```env
ATR_STORAGE_BOX_ENABLED=true
ATR_STORAGE_BOX_HOST=u123456-sub1.your-storagebox.de
ATR_STORAGE_BOX_PORT=23
ATR_STORAGE_BOX_USERNAME=u123456-sub1
ATR_STORAGE_BOX_SSH_KEY_PATH=/home/sid/.ssh/storagebox_ed25519
ATR_STORAGE_BOX_PASSWORD=
ATR_STORAGE_BOX_ROOT=
ATR_STORAGE_BOX_KNOWN_HOSTS_PATH=/home/sid/.ssh/known_hosts_storage_box
ATR_LIBRARY_STATE_DB_PATH=backend/data/library_state.db
ATR_STORAGE_BOX_MAX_CONNECTIONS=3
ATR_STORAGE_BOX_UPLOAD_MAX_PARALLEL=2
ATR_STORAGE_BOX_DOWNLOAD_MAX_PARALLEL=3
```

Project recommendations:

- use absolute paths, not `~`
- leave `ATR_STORAGE_BOX_PASSWORD=` empty once SSH keys work
- keep `ATR_STORAGE_BOX_MAX_CONNECTIONS` conservative
- keep the defaults unless you have measured a reason to raise them

### If you intentionally use main-account credentials

```env
ATR_STORAGE_BOX_ENABLED=true
ATR_STORAGE_BOX_HOST=u123456.your-storagebox.de
ATR_STORAGE_BOX_PORT=23
ATR_STORAGE_BOX_USERNAME=u123456
ATR_STORAGE_BOX_SSH_KEY_PATH=/home/sid/.ssh/storagebox_ed25519
ATR_STORAGE_BOX_PASSWORD=
ATR_STORAGE_BOX_ROOT=anime-tiktok-reproducer
ATR_STORAGE_BOX_KNOWN_HOSTS_PATH=/home/sid/.ssh/known_hosts_storage_box
ATR_LIBRARY_STATE_DB_PATH=backend/data/library_state.db
ATR_STORAGE_BOX_MAX_CONNECTIONS=3
ATR_STORAGE_BOX_UPLOAD_MAX_PARALLEL=2
ATR_STORAGE_BOX_DOWNLOAD_MAX_PARALLEL=3
```

This works, but it is less isolated than using a dedicated sub-account.

## 11. Security recommendations for this project

Recommended minimum:

- use a dedicated sub-account for the app
- use a dedicated SSH key for the app
- use `known_hosts` pinning
- keep the app password out of `.env`
- keep `External Reachability` enabled only if you actually need it
- keep only the required protocols enabled

Practical recommendation:

- enable `SSH Support`
- do not enable FTP/FTPS/SMB/WebDAV unless you truly need them

Reason:

- Hetzner explicitly notes that disabled protocols reduce attack surface

Also recommended:

- keep the main account credentials out of the application
- store the Storage Box password in a password manager as emergency recovery only
- rotate the app key if it is ever copied to another machine or CI runner

## 12. Snapshots

Snapshots are optional, but recommended if the Storage Box becomes your canonical library origin.

In Hetzner Console:

1. open the Storage Box
2. go to `Snapshots`
3. either:
   - take a manual snapshot
   - enable automatic snapshots

Important:

- snapshots consume Storage Box space
- set a slot limit appropriate for your plan

Practical recommendation:

- enable automatic snapshots only after the initial library upload is stable
- keep a small slot count if storage headroom is tight

## 13. Connection and concurrency guidance

Hetzner documents a maximum of 10 simultaneous connections per account in their Storage Box SSH/rsync/Borg documentation.

For this project, stay conservative:

- `ATR_STORAGE_BOX_MAX_CONNECTIONS=3`
- `ATR_STORAGE_BOX_UPLOAD_MAX_PARALLEL=2`
- `ATR_STORAGE_BOX_DOWNLOAD_MAX_PARALLEL=3`

Do not increase these blindly.

If you later see throttling or transfer instability:

- reduce parallelism first
- verify the app is using the sub-account hostname
- verify no other backup tools are using the same credentials at the same time

## 14. Final checklist

Before using the Storage Box in this app, confirm all of the following:

- Storage Box exists
- `External Reachability` is enabled if the app runs outside Hetzner
- `SSH Support` is enabled if you want port `23`
- dedicated sub-account exists
- SSH key is uploaded for the chosen port
- `known_hosts` file exists and fingerprint was verified
- manual `sftp` login works
- `.env` points to the correct host, user, port, key, and known_hosts file
- `ATR_STORAGE_BOX_PASSWORD` is empty unless you are intentionally using password fallback
- the app sub-account has enough free storage space
- snapshots are configured the way you want

## 15. Recommended final config for this repo

If you want the shortest safe answer for this project, use this:

- create the Storage Box in Hetzner Console
- enable `SSH Support`
- enable `External Reachability`
- create a dedicated sub-account
- generate a dedicated Ed25519 SSH key
- upload the key using port `23`
- create and verify a dedicated `known_hosts` file
- set:

```env
ATR_STORAGE_BOX_PORT=23
ATR_STORAGE_BOX_PASSWORD=
```

- use the sub-account host and username in `.env`
- leave `ATR_STORAGE_BOX_ROOT=` empty if the sub-account is app-only

## 16. Official Hetzner references

These are the official docs used to write this guide:

- Creating a Storage Box:
  - https://docs.hetzner.com/storage/storage-box/getting-started/creating-a-storage-box/
- Storage Box overview:
  - https://docs.hetzner.com/storage/storage-box/general/
- Access with SFTP/SCP:
  - https://docs.hetzner.com/storage/storage-box/access/access-sftp-scp/
- Access overview:
  - https://docs.hetzner.com/storage/storage-box/access/access-overview/
- Add SSH keys:
  - https://docs.hetzner.com/storage/storage-box/backup-space-ssh-keys/
- Access with SSH/rsync/BorgBackup:
  - https://docs.hetzner.com/storage/storage-box/access/access-ssh-rsync-borg/
- Creating snapshots:
  - https://docs.hetzner.com/storage/storage-box/getting-started/creating-snapshots/

