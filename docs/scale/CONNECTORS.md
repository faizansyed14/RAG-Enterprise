# Connectors

Every source speaks one contract (`app/scale/connectors/base.py`): a connector turns a place that holds files into a stream of
`upsert` / `delete` changes, each upsert carrying the file's version and the people who may read it, and can open a file's
bytes on demand. Everything after that (queue, parsing, chunking, indexing, access control) is the same for every source.

There are **31 connector kinds**. The goal was "about 50 sources"; this is fewer, and the table below says exactly what exists.
Many commercial sources are the same protocol under a different brand, and those are covered by one kind (`s3` serves eight
object stores, `webdav` serves Nextcloud, ownCloud and others, `sql` serves PostgreSQL and SQLite read-only, other dialects need a
read-only guard added). The number of connector implementations is 31; no count of brand names is claimed.

## What each kind does

| kind | source | finds changes by | who may read a file | extra package |
|---|---|---|---|---|
| `local` | a folder or mount (also the right adapter for an on-prem app that exports files) | full crawl, version = mtime+size | folder rules in the connection config | none |
| `smb` | Windows file server / NAS | full crawl | folder rules in the config. **Does not read NTFS permissions yet** | `smbprotocol` |
| `sharepoint` | SharePoint Online sites and their document libraries (Entra app, client secret). Guided setup in Sources | delta query per library + saved delta link | per file (mirrors the file's sharing, fails closed), everyone, or a fixed list | none |
| `onedrive` | named users' OneDrive, or every user's with `all_users` (Entra app, client secret) | delta query per drive | same three access modes | none |
| `graph` | Microsoft 365 drives by drive id (advanced; SharePoint and OneDrive above are easier) | delta query + saved delta link | **per file**, read from Graph; SharePoint site groups need `site_group_map` | none |
| `s3` | AWS S3, Cloudflare R2, MinIO, Wasabi, Backblaze B2, DigitalOcean Spaces, Oracle OCI, Ceph | resumable key-ordered listing | prefix rules | `boto3` |
| `azure_blob` | Azure Blob Storage container | resumable listing | prefix rules | none |
| `gcs` | Google Cloud Storage bucket | resumable listing | prefix rules | none |
| `webdav` | Nextcloud, ownCloud, Apache/nginx DAV, on-prem DAV | PROPFIND crawl, resumable | folder rules | none |
| `ftp` | FTP and explicit FTPS | crawl, resumable | folder rules | none |
| `sftp` | SFTP | crawl, resumable | folder rules | `paramiko` |
| `gdrive` | Google Drive, shared drives | changes feed + page token | **per file**, from Drive permissions | none |
| `dropbox` | Dropbox personal / Business | list_folder cursor | folder rules | none |
| `box` | Box | folder walk, resumable | folder rules | none |
| `confluence` | Confluence Cloud and Data Center | CQL `lastmodified` watermark | space/prefix rules | none |
| `jira` | Jira Cloud and Data Center issues + comments | JQL `updated` watermark | project rules | none |
| `notion` | pages and database rows | last-edited watermark | workspace rules | none |
| `slack` | channel history, one file per channel per day | day watermark | channel rules | none |
| `discord` | server channel history | snowflake position | channel rules | none |
| `teams` | Teams channel posts | Graph delta | channel rules | none |
| `outlook_mail` | Outlook mail folders | Graph delta | mailbox rules | none |
| `gmail` | Gmail as `.eml` | history id | mailbox rules | none |
| `imap` | any IMAP mailbox as `.eml`, read-only | UID position | mailbox rules | none |
| `github` | repos, issues, pull requests (github.com and Enterprise) | tree sha + `since` | repo rules | none |
| `gitlab` | repos, wiki, issues, merge requests | tree + `updated_after` | project rules | none |
| `bitbucket` | Bitbucket Cloud repos, issues | commit + diffstat | repo rules | none |
| `zendesk` | Help Center articles, tickets | incremental export | brand/section rules | none |
| `asana` | tasks + comments | `modified_since` | project rules | none |
| `airtable` | records | table scan | base rules | none |
| `sql` | one read-only SQL query; every row becomes a document | key / updated column | query rules | `sqlalchemy` + driver |
| `web` | sites, sitemaps, RSS/Atom; honours robots.txt | conditional GET | site rules | none |

"Rules" means the `acl_rules` of the connection (see `AclRules` in `base.py`): you state once that everything under a prefix is
readable by certain principals, and every file below inherits that. That is a coarser model than the source's own permissions.
Only `graph` and `gdrive` read permissions per file. Files with no matching rule get an empty access list, so **nobody** can
read them (fail closed).

Credentials are never stored in the database. A connection's config names the environment variable that holds the secret
(`client_secret_env`, `password_env`, `token_env`, ...).

## SharePoint and OneDrive (read only)

Code: `connectors/graph_common.py` (shared), `sharepoint.py`, `onedrive.py`.

**Entra app.** Register an app, add *application* permissions, grant admin consent, create a client secret, and put everything in the
server's `.env` (`SHAREPOINT_TENANT_ID`, `SHAREPOINT_CLIENT_ID`, `SHAREPOINT_CLIENT_SECRET`, and the same with `ONEDRIVE_`; optional scope
variables are listed in `.env.dev.example` and `app/scale/managed.py`). The Connectors screen has no fields for these: it shows which variables
are set (names only) and offers **Connect** and **Disconnect**. The stored connection is just `{"from_env": true}`, so `.env` stays the single
source of truth; after editing it, restart the backend and worker. The secret never passes through the browser or the database. (The API
still accepts a hand-written config with `client_secret_env` naming a variable; a config with an inline secret is refused.)

| connector | permissions | notes |
|---|---|---|
| `sharepoint` | `Sites.Read.All` + `Files.Read.All`, or `Sites.Selected` plus a read grant per site | `sites` empty = every site the app can see; `libraries` optional |
| `onedrive` | `Files.Read.All`; add `User.Read.All` only for `all_users` | `users` = list of user principal names |

**Read only is enforced in code, not by convention.** Every request passes `assert_read_only`: only GET to graph.microsoft.com, the
token request, and `$batch` whose sub-requests are all GET. Anything else raises before it leaves the process. The bearer token is
sent to graph.microsoft.com only: paging links or saved cursors that point at another host are refused, and file downloads use the
short-lived pre-authenticated URL with no token and a size cap. The test report warns if the app was granted any write role.

**Access** (`access` in the config, fixed when the connection is made):
`{"mode": "source"}` copies each file's own sharing from Graph and treats a file whose permissions cannot be read as private;
`{"mode": "everyone"}`; or `{"mode": "principals", "principals": ["group:finance"]}`.

**Filters** (apply to files seen from now on, not retroactively): `include_extensions`, `exclude_paths`, `modified_after`, `max_file_mb`.
A library that cannot be found on a later scan is never auto-deleted, so a transient lookup failure cannot wipe an index.

**Test before saving.** Sources > SharePoint/OneDrive > *Test connection*, or
`bash scripts/cli.sh local connect test --kind sharepoint --config conn.json`. The report lists the roles in the token, any write
role, the libraries or drives found, a few sample file names, and whether permissions can be read. Nothing is stored.

**Tested against a simulated Microsoft Graph only.** No Microsoft 365 tenant was available, so 45 tests run the connectors against
a local fake of the documented Graph behaviour (paging, delta, 410 resync, deleted items, throttling, token errors, hostile links,
permission batches). Run *Test connection* against your tenant first.

## Uploaded files and Onsite

**Files** (Sources > Files) is an upload source: drag files in, choose who may read them (everyone, or named groups; fixed at creation),
and add or remove files later. Files are stored under `RAG_STORAGE_PATH/scale-uploads` (or `SCALE_UPLOAD_DIR`) on the volume the API
and the worker share, then indexed like any other source. Limits: `SCALE_UPLOAD_MAX_FILES` (100), `SCALE_UPLOAD_REQUEST_MB` (512),
and `SCALE_MAX_FILE_MB` per file. With several hosts the folder must be on shared storage.

**Onsite** has no direct connector: the Onsite site says it has an open API but publishes no endpoints or sign-in method, and one
was not guessed. Two routes work today: connect the SharePoint library Onsite syncs to (its site lists SharePoint, Dropbox and
Google Drive integrations), or export from Onsite and upload the files. A direct connector can be added once Onsite provides API
documentation and credentials.

## How far each was tested, honestly

* The environment this was built and tested in has no accounts at any of these services. **No connector was run against the real
  service.** Each was written from the vendor's published API reference and tested against a faithful fake of that API (about
  1,000 connector tests: paging, resuming a half-finished scan, deleted items, rate-limit headers, auth failures, malformed
  answers, and that a failed scan never deletes anything).
* A fake proves the connector follows the documented contract. It does not prove the vendor's service behaves as documented, so
  expect to fix small things on first contact with each real service. Run each kind against a test account first.
* `smb` was never run against a real SMB server. `graph` was written to Microsoft's documented delta contract and was never run
  against a real tenant.
* Only the kinds you actually need matter. The three that matter for the client brief (SharePoint/OneDrive, SMB file share,
  local storage) are the ones to test first with the client's real data.

## Known gaps

* `smb` does not read NTFS security descriptors, so access follows the folder rules you write, not the file server's real
  permissions. The extension point is `SmbConnector.refresh_acl`.
* Most connectors apply prefix-level rules. If the client needs per-document permissions from Confluence, Jira, Notion and so on,
  that is per-connector work.
* No connector writes anything back to the source.
* Not covered: further SaaS products (CRM and marketing tools, for example) where the work is real API integration per product. Add one by implementing the `Connector` protocol; `s3.py` and `zendesk.py` are good templates.

## Adding a connector

1. Implement `scan(cursor)` yielding `ScanBatch` objects, `open(file)`, and `refresh_acl` if the source can say who may read.
2. Register it in `CONNECTORS` in `app/scale/connectors/__init__.py` (the module is imported lazily, so a missing optional
   package only breaks that kind).
3. Test it with a fake of the vendor's API, and include the cases above: resume, delete, rate limit, auth failure.

Install the optional packages with `pip install -r backend/requirements-connectors.txt`.
