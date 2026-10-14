"""Zendesk: Help Center articles and, optionally, Support tickets with their comments, as virtual files.

Endpoints (Zendesk REST API: "Help Center API: Articles, Sections, Categories", "Support API: Incremental Exports
(cursor-based), Tickets, Ticket Comments", "Introduction: Pagination, Rate limits, Security and authentication"):

  GET /api/v2/help_center/{locale}/articles?per_page=100              the full listing of one locale: `next_page` links.
  GET /api/v2/help_center/incremental/articles?start_time=<epoch>     articles changed since the watermark; `next_page`
        is followed until it is absent / the page is empty / `end_of_stream` is true. Used after a finished scan.
  GET /api/v2/help_center/{locale}/sections, .../categories           names for the path (`next_page` links).
  GET /api/v2/help_center/{locale}/articles/{id}                      open().
  GET /api/v2/incremental/tickets/cursor.json?start_time=<epoch>&per_page=1000, then ?cursor=<after_cursor>
        until `end_of_stream` is true. A ticket with status "deleted" becomes a delete.
  GET /api/v2/tickets/{id}/comments.json?include=users&sort_order=asc&per_page=100     `next_page` links; /tickets/{id}.json

Config: subdomain (`acme` => https://acme.zendesk.com) or base_url, email + token_env (an API token: sent as HTTP Basic
`email/token:TOKEN` as Zendesk documents; secrets are never in the config), auth ("token", the default, or "bearer" for
an OAuth access token: then no email), locales (["en-us"]), include_articles (true), include_tickets (false),
include_restricted_articles (false), include_drafts (false), include_internal_comments (false), tickets_since (ISO date;
default: every ticket), page_size (100), max_items, max_pages, full_resync_hours (24), overlap_minutes (5), acl_rules,
default_acl.

Objects (external ids are the stable identity, `version` the change detector = a hash of the path and the rendered text,
so it is stable under vote counts and other metadata churn):
  article external_id `zendesk:article/<locale>/<id>`   path `zendesk/help/<locale>/<category>/<section>/<id>-<title>.md`
  ticket  external_id `zendesk:ticket/<id>`             path `zendesk/tickets/group-<group id>/<id>-<subject>.md`
        (the group is the unit agents' access follows, so a rule per group prefix can mirror it; `group-none` otherwise)
Scans: the per-locale article listing of a full scan is authoritative for `zendesk:article/<locale>/` and the ticket export
from its start to `end_of_stream` for `zendesk:ticket/`, when they ran from the beginning without hitting `max_items` /
`max_pages`. After a finished scan only the incremental endpoints are asked, from the watermark minus `overlap_minutes`;
every `full_resync_hours` a full pass runs (the Help Center incremental feed reports no deletions, so that is how removed
articles are noticed). (Until the sync layer persists the cursor of a finished scan, every scan is a full one.)

Fail closed: draft articles and articles restricted to a user segment (`user_segment_id` set: signed-in users, agents,
...) are NOT indexed unless the config says so, and are deleted from the index if they become so; internal notes of a
ticket (`public: false`) are never rendered unless `include_internal_comments` is on. Tickets are customer data: they
are off by default.

Permissions: PREFIX-LEVEL MAPPING CONFIGURED BY THE CLIENT (`acl_rules` keyed by the path; no match and no `default_acl`
=> nobody). It is not Zendesk's user segments, permission groups or agent roles. Mirror them in the rules.

What this does NOT do: translations of an article other than the listed locales, article attachments and comments,
ticket attachments, side conversations, ticket audits / events, users and organisations as objects, Guide posts (community),
Talk / Chat transcripts, deleted-ticket purges (a purged ticket is noticed at the next full resync). The Help Center
incremental feed is filtered by the article's own `locale`, so a translation edited without the article changing is
picked up at the next full resync. Zendesk limits incremental exports to about 10 requests a minute: 429 is retried
after `Retry-After`.

Not exercised against a live Zendesk; tested against fixtures shaped like the vendor documentation. Not certain of: the
exact shape of the Help Center incremental response (`end_time`, `end_of_stream`), and whether `generated_timestamp`
is present on every ticket (`updated_at` is used when it is not).
"""

from __future__ import annotations

import io
import re
import time
from typing import BinaryIO, Callable, Iterator
from urllib.parse import urlparse

import httpx

from app.scale.connectors._http import AuthError, Http, HttpError, env_secret, render_document, safe_name
from app.scale.connectors._objects import Budget, ScanState, basic_auth, html_to_text, parse_dt, redacted, short_hash
from app.scale.connectors.base import AclRules, Change, ScanBatch, SourceFile

_LOCALE = re.compile(r"^[A-Za-z0-9_-]+$")
_SUBDOMAIN = re.compile(r"^[A-Za-z0-9-]+$")
_EXTERNAL_ID = re.compile(r"zendesk:(article)/([A-Za-z0-9_-]+)/(\d+)|zendesk:(ticket)/(\d+)")
ARTICLES_API = "/api/v2/help_center"


class ZendeskConnector:
    kind = "zendesk"

    def __init__(
        self, config: dict, *, transport: httpx.BaseTransport | None = None, client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time,
    ) -> None:
        self.cfg = config
        subdomain = config.get("subdomain")
        if subdomain and not _SUBDOMAIN.match(str(subdomain)):
            raise ValueError(f"zendesk: unusable subdomain {subdomain!r}")
        base = config.get("base_url") or (f"https://{subdomain}.zendesk.com" if subdomain else None)
        if not base:
            raise ValueError("zendesk: config needs `subdomain` (acme => https://acme.zendesk.com) or `base_url`")
        self.base = base.rstrip("/")
        self.auth = config.get("auth", "token")
        if self.auth not in ("token", "bearer"):
            raise ValueError("zendesk: `auth` must be \"token\" or \"bearer\"")
        if self.auth == "token" and not config.get("email"):
            raise ValueError("zendesk: token auth needs `email` (the agent the API token belongs to)")
        self.locales = list(config.get("locales") or ["en-us"])
        if not all(_LOCALE.match(str(loc)) for loc in self.locales):
            raise ValueError(f"zendesk: unusable locales {self.locales!r}")
        self.articles = bool(config.get("include_articles", True))
        self.tickets = bool(config.get("include_tickets", False))
        if not (self.articles or self.tickets):
            raise ValueError("zendesk: nothing to index (include_articles / include_tickets)")
        self.phases = [p for p, on in (("articles", self.articles), ("tickets", self.tickets)) if on]
        self.restricted = bool(config.get("include_restricted_articles", False))
        self.drafts = bool(config.get("include_drafts", False))
        self.internal = bool(config.get("include_internal_comments", False))
        self.tickets_since = parse_dt(config.get("tickets_since"))
        self.page_size = int(config.get("page_size", 100))
        self.full_hours = float(config.get("full_resync_hours", 24))
        self.overlap = float(config.get("overlap_minutes", 5)) * 60
        self.rules = AclRules(config.get("acl_rules"), config.get("default_acl"))
        self.clock = clock
        self.http = Http(self.base, transport=transport, client=client, sleep=sleep)
        self._name_cache: dict[str, tuple[dict, dict]] = {}

    # ── http ──

    def _headers(self) -> tuple[dict[str, str], str]:
        secret = env_secret(self.cfg.get("token_env"), "Zendesk API token")
        value = basic_auth(f"{self.cfg['email']}/token", secret) if self.auth == "token" else f"Bearer {secret}"
        return {"Authorization": value, "Accept": "application/json"}, secret

    def _guard(self, url: str) -> str:
        if url.startswith(("http://", "https://")) and urlparse(url).netloc != urlparse(self.base).netloc:
            raise HttpError(f"zendesk: refusing to follow a link to another host ({urlparse(url).netloc})")
        return url

    def _get(self, url: str, params: dict | None = None) -> httpx.Response:
        headers, secret = self._headers()
        encoded = headers["Authorization"].split(" ", 1)[1]
        try:
            return self.http.get(self._guard(url), params=params, headers=headers)
        except AuthError as exc:
            raise redacted(exc, secret, encoded, hint=f"check the credentials in environment variable {self.cfg.get('token_env')}") from None
        except HttpError as exc:
            raise redacted(exc, secret, encoded) from None

    def _json(self, url: str, params: dict | None = None):
        return self._get(url, params).json()

    def _all(self, url: str, key: str, params: dict | None = None) -> list[dict]:
        """Every item under `key` of a `next_page`-paginated list."""
        items: list[dict] = []
        guard = Budget()
        while url:
            guard.allow_page(url)
            body = self._get(url, params).json()
            items.extend(body.get(key) or [])
            url, params = body.get("next_page"), None
        return items

    @staticmethod
    def _epoch(ts: float) -> int:
        return max(int(ts), 0)

    # ── articles ──

    def _names(self, locale: str) -> tuple[dict, dict]:
        if locale not in self._name_cache:
            sections = {s["id"]: s for s in self._all(f"{ARTICLES_API}/{locale}/sections", "sections", {"per_page": 100})}
            categories = {c["id"]: c for c in self._all(f"{ARTICLES_API}/{locale}/categories", "categories", {"per_page": 100})}
            self._name_cache[locale] = (sections, categories)
        return self._name_cache[locale]

    def _visible(self, article: dict) -> bool:
        if article.get("draft") and not self.drafts:
            return False
        return self.restricted or article.get("user_segment_id") in (None, "")

    def _article_path(self, article: dict) -> str:
        locale = article.get("locale") or self.locales[0]
        sections, categories = self._names(locale)
        section = sections.get(article.get("section_id")) or {}
        category = categories.get(section.get("category_id")) or {}
        folders = [safe_name(category.get("name") or f"category-{section.get('category_id', 'none')}", "category", 80),
                   safe_name(section.get("name") or f"section-{article.get('section_id', 'none')}", "section", 80)]
        title = article.get("title") or f"article {article['id']}"
        return f"zendesk/help/{locale}/{'/'.join(folders)}/{safe_name(f'{article['id']}-{title}', str(article['id']), 100)}.md"

    @staticmethod
    def _article_text(article: dict) -> bytes:
        meta = {"type": "help center article", "locale": article.get("locale"), "labels": ", ".join(article.get("label_names") or []) or None,
                "created": article.get("created_at"), "url": article.get("html_url")}
        return render_document(article.get("title") or f"Article {article['id']}", meta, html_to_text(article.get("body") or "").strip())

    def _article_change(self, article: dict) -> Change:
        locale = article.get("locale") or self.locales[0]
        ext = f"zendesk:article/{locale}/{article['id']}"
        if not self._visible(article):
            return Change("delete", ext)
        path, text = self._article_path(article), self._article_text(article)
        sf = SourceFile(
            external_id=ext, path=path, name=path.rsplit("/", 1)[-1], size=len(text), modified_at=parse_dt(article.get("updated_at")),
            version=short_hash(path, text), acl=self.rules.for_path(path), mime="text/markdown",
            extra={"locale": locale, "id": article["id"]},
        )
        return Change("upsert", ext, sf)

    def _article_sources(self, state: ScanState) -> list[dict]:
        if state.incremental and state.since:
            start = self._epoch((parse_dt(state.since) or parse_dt(0)).timestamp() - self.overlap)
            return [{"url": f"{ARTICLES_API}/incremental/articles", "params": {"start_time": start}, "scope": None, "only": {loc.lower() for loc in self.locales}}]
        return [{"url": f"{ARTICLES_API}/{loc}/articles", "params": {"per_page": self.page_size}, "scope": f"zendesk:article/{loc}/", "only": None}
                for loc in self.locales]

    def _articles_phase(self, state: ScanState, budget: Budget, source: int, nxt: str | None) -> Iterator[ScanBatch]:
        sources = self._article_sources(state)
        for index in range(source, len(sources)):
            spec = sources[index]
            resumed = index == source and nxt is not None
            url, first = (nxt if resumed else spec["url"]), not resumed
            while True:
                if not budget.allow_page(url):
                    return
                body = self._get(url, spec["params"] if first else None).json()
                page_rows = body.get("articles") or []
                rows = page_rows if spec["only"] is None else [a for a in page_rows if str(a.get("locale", "")).lower() in spec["only"]]
                changes = []
                for article in budget.clip(rows):
                    changes.append(self._article_change(article))
                    state.observe(article.get("updated_at"))
                previous, url, first = url, body.get("next_page"), False
                # The time-based incremental feed keeps answering with the same `next_page` once it has caught up.
                if url and url != previous and page_rows and not body.get("end_of_stream"):
                    yield ScanBatch(changes, state.cursor(phase="articles", src=index, next=url))
                    continue
                last_source = index == len(sources) - 1
                cursor = self._after_articles(state) if last_source else state.cursor(phase="articles", src=index + 1)
                complete = spec["scope"] if (spec["scope"] and state.mode == "full" and not resumed and not budget.truncated) else None
                yield ScanBatch(changes, cursor, complete_scope=complete)
                break

    def _after_articles(self, state: ScanState) -> dict:
        return state.cursor(phase="tickets") if self.tickets else state.cursor(phase="done")

    # ── tickets ──

    def _comments(self, ticket_id: int) -> tuple[list[dict], dict]:
        comments: list[dict] = []
        users: dict = {}
        guard = Budget()
        url, params = f"/api/v2/tickets/{ticket_id}/comments.json", {"include": "users", "sort_order": "asc", "per_page": 100}
        while url:
            guard.allow_page(url)
            body = self._get(url, params).json()
            comments.extend(body.get("comments") or [])
            users.update({u["id"]: u for u in body.get("users") or [] if "id" in u})
            url, params = body.get("next_page"), None
        return comments, users

    def _render_ticket(self, ticket: dict) -> tuple[str, bytes]:
        comments, users = self._comments(int(ticket["id"]))
        name = lambda uid: (users.get(uid) or {}).get("name") or (f"user {uid}" if uid else "unknown")        # noqa: E731
        rendered = []
        for comment in comments:
            if comment.get("public") is False and not self.internal:
                continue
            body = (comment.get("plain_body") or comment.get("body") or "").strip() or html_to_text(comment.get("html_body") or "").strip()
            if body:
                rendered.append(f"### {name(comment.get('author_id'))} ({comment.get('created_at', '')})\n\n{body}")
        number = int(ticket["id"])
        subject = ticket.get("subject") or ticket.get("raw_subject") or f"Ticket {number}"
        meta = {
            "type": "ticket", "status": ticket.get("status"), "priority": ticket.get("priority"), "ticket type": ticket.get("type"),
            "requester": name(ticket.get("requester_id")) if ticket.get("requester_id") else None, "tags": ", ".join(ticket.get("tags") or []) or None,
            "organization": ticket.get("organization_id"), "created": ticket.get("created_at"),
            "url": f"{self.base}/agent/tickets/{number}",
        }
        text = render_document(f"Ticket #{number}: {subject}", meta, "\n\n".join(rendered))
        group = ticket.get("group_id")
        path = f"zendesk/tickets/group-{group if group else 'none'}/{safe_name(f'{number}-{subject}', str(number), 100)}.md"
        return path, text

    def _ticket_change(self, ticket: dict) -> Change:
        ext = f"zendesk:ticket/{ticket['id']}"
        if ticket.get("status") == "deleted":
            return Change("delete", ext)
        path, text = self._render_ticket(ticket)
        sf = SourceFile(
            external_id=ext, path=path, name=path.rsplit("/", 1)[-1], size=len(text), modified_at=parse_dt(ticket.get("updated_at")),
            version=short_hash(path, text), acl=self.rules.for_path(path), mime="text/markdown", extra={"id": ticket["id"]},
        )
        return Change("upsert", ext, sf)

    def _tickets_phase(self, state: ScanState, budget: Budget, cursor: str | None) -> Iterator[ScanBatch]:
        if state.incremental and state.since:
            start = (parse_dt(state.since) or parse_dt(0)).timestamp() - self.overlap
        else:
            start = self.tickets_since.timestamp() if self.tickets_since else 0
        url, per_page = "/api/v2/incremental/tickets/cursor.json", min(self.page_size, 1000)
        resumed = cursor is not None
        params = {"cursor": cursor, "per_page": per_page} if resumed else {"start_time": self._epoch(start), "per_page": per_page}
        while True:
            if not budget.allow_page(params.get("cursor") or ("start", params["start_time"])):
                return
            try:
                body = self._get(url, params).json()
            except HttpError as exc:
                if not (resumed and "cursor" in params and exc.status in (400, 404, 410, 422)):
                    raise
                resumed, params = False, {"start_time": self._epoch(start), "per_page": per_page}      # the cursor expired (an hour): start over
                continue
            changes = []
            for ticket in budget.clip(body.get("tickets") or []):
                changes.append(self._ticket_change(ticket))
                state.observe(ticket.get("updated_at"))
                if ticket.get("generated_timestamp"):
                    state.observe(ticket["generated_timestamp"])
            after = body.get("after_cursor")
            if not body.get("end_of_stream"):
                if not after:                          # neither "more" nor "that was all": do not mistake it for the end
                    raise HttpError("zendesk: the ticket export answered with neither `after_cursor` nor `end_of_stream`")
                yield ScanBatch(changes, state.cursor(phase="tickets", cursor=after))
                params = {"cursor": after, "per_page": per_page}
                continue
            complete = "zendesk:ticket/" if (state.mode == "full" and not resumed and not budget.truncated) else None
            yield ScanBatch(changes, state.cursor(phase="done"), complete_scope=complete)
            return

    # ── scan ──

    def scan(self, cursor: dict) -> Iterator[ScanBatch]:
        state = ScanState(cursor, full_resync_hours=self.full_hours, clock=self.clock)
        budget = Budget.from_config(self.cfg)
        self._name_cache = {}
        order = self.phases + ["done"]
        phase0 = state.pos.get("phase")
        if phase0 not in order:                       # the configuration changed since the cursor was written
            state.pos, phase0 = {}, order[0]
        for phase in self.phases:
            if order.index(phase) < order.index(phase0):
                continue
            resumed = phase == phase0
            if phase == "articles":
                yield from self._articles_phase(state, budget, int(state.pos.get("src", 0)) if resumed else 0, state.pos.get("next") if resumed else None)
            else:
                yield from self._tickets_phase(state, budget, state.pos.get("cursor") if resumed else None)
        yield ScanBatch([], state.final(truncated=budget.truncated), done=True)

    # ── bytes and permissions ──

    def open(self, file: SourceFile) -> BinaryIO:
        match = _EXTERNAL_ID.fullmatch(file.external_id)
        if not match:
            raise HttpError(f"zendesk: {file.external_id!r} is not a Zendesk object id")
        if match.group(1):
            locale, number = match.group(2), match.group(3)
            article = self._json(f"{ARTICLES_API}/{locale}/articles/{number}").get("article") or {}
            if not article.get("id"):
                raise HttpError(f"zendesk: {file.external_id!r} came back without an article", 404)
            if not self._visible(article):
                raise HttpError(f"zendesk: {file.external_id!r} is a draft or restricted and is not indexed", 403)
            return io.BytesIO(self._article_text(article))
        ticket = self._json(f"/api/v2/tickets/{match.group(5)}.json").get("ticket") or {}
        if not ticket.get("id") or ticket.get("status") == "deleted":
            raise HttpError(f"zendesk: {file.external_id!r} is gone", 404)
        return io.BytesIO(self._render_ticket(ticket)[1])

    def refresh_acl(self, file: SourceFile) -> list[str]:
        return self.rules.for_path(file.path)
