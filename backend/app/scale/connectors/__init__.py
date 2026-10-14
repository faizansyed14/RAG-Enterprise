"""Connector registry. Credentials are never stored in the database: a connection's config names the
environment variable that holds the secret (client_secret_env, password_env, token_env ...)."""

from __future__ import annotations

import importlib

from app.scale.connectors.base import AclRules, Change, Connector, ScanBatch, SourceFile

__all__ = ["AclRules", "Change", "Connector", "ScanBatch", "SourceFile", "build_connector", "available_kinds", "CONNECTORS"]

# kind -> "module:Class". Imported lazily so a missing optional dependency (boto3, paramiko, smbprotocol ...)
# only breaks the connector that needs it. Tested depth differs a lot between kinds: see docs/scale/CONNECTORS.md.
CONNECTORS: dict[str, str] = {
    # files and storage
    "local": "app.scale.connectors.local_folder:LocalFolderConnector",
    "smb": "app.scale.connectors.smb:SmbConnector",
    "sharepoint": "app.scale.connectors.sharepoint:SharePointConnector",  # SharePoint Online sites and document libraries (read only)
    "onedrive": "app.scale.connectors.onedrive:OneDriveConnector",        # OneDrive for Business, named users or all users (read only)
    "graph": "app.scale.connectors.graph:GraphConnector",          # document libraries by drive id (the two above look them up for you)
    "s3": "app.scale.connectors.s3:S3Connector",                   # AWS S3, Cloudflare R2, MinIO, Wasabi, B2, Spaces, OCI, Ceph
    "azure_blob": "app.scale.connectors.azure_blob:AzureBlobConnector",
    "gcs": "app.scale.connectors.gcs:GcsConnector",
    "webdav": "app.scale.connectors.webdav:WebDavConnector",       # Nextcloud, ownCloud, Apache, on-prem DAV
    "ftp": "app.scale.connectors.ftp:FtpConnector",
    "sftp": "app.scale.connectors.sftp:SftpConnector",
    "gdrive": "app.scale.connectors.gdrive:GDriveConnector",
    "dropbox": "app.scale.connectors.dropbox:DropboxConnector",
    "box": "app.scale.connectors.box:BoxConnector",
    # knowledge, tickets, code, chat, mail, data
    "confluence": "app.scale.connectors.confluence:ConfluenceConnector",
    "jira": "app.scale.connectors.jira:JiraConnector",
    "notion": "app.scale.connectors.notion:NotionConnector",
    "slack": "app.scale.connectors.slack:SlackConnector",
    "discord": "app.scale.connectors.discord:DiscordConnector",
    "teams": "app.scale.connectors.graph_mail_teams:TeamsConnector",
    "github": "app.scale.connectors.github:GitHubConnector",
    "gitlab": "app.scale.connectors.gitlab:GitLabConnector",
    "bitbucket": "app.scale.connectors.bitbucket:BitbucketConnector",
    "zendesk": "app.scale.connectors.zendesk:ZendeskConnector",
    "asana": "app.scale.connectors.asana:AsanaConnector",
    "airtable": "app.scale.connectors.airtable:AirtableConnector",
    "gmail": "app.scale.connectors.gmail:GmailConnector",
    "outlook_mail": "app.scale.connectors.graph_mail_teams:OutlookMailConnector",
    "imap": "app.scale.connectors.imap:ImapConnector",
    "sql": "app.scale.connectors.sql:SqlConnector",
    "web": "app.scale.connectors.web:WebConnector",                # sites, sitemaps, RSS / Atom
}


def available_kinds() -> list[str]:
    return sorted(CONNECTORS)


def build_connector(kind: str, config: dict) -> Connector:
    target = CONNECTORS.get(kind)
    if target is None:
        raise ValueError(f"unknown connector kind {kind!r}; available: {', '.join(available_kinds())}")
    from app.scale.managed import resolve

    module, _, cls = target.partition(":")
    return getattr(importlib.import_module(module), cls)(resolve(kind, config))
