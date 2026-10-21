"""Find a bank's statement email in Gmail, check it is genuine, and download it."""

from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Optional

logger = logging.getLogger(__name__)

# The Google SDK is imported lazily, inside GmailClient. Everything else in
# this module — the query builder, the subject parsing — is pure, and the
# tests exercise it without needing an API client installed at all.

_PERIOD_IN_SUBJECT = re.compile(
    r"\(([A-Za-z]{3}\.?\s+\d{1,2},\s+\d{4})\s*-\s*([A-Za-z]{3}\.?\s+\d{1,2},\s+\d{4})\)"
)
# Gmail's own relative-age tokens: 1d, 2w, 3m, 1y.
_RELATIVE_AGE = re.compile(r"^\d+[dwmy]$", re.I)


# The authserv-id Gmail stamps on the verdict it adds itself.
_GMAIL_AUTHSERV_ID = "mx.google.com"
_COMMENT = re.compile(r"\([^()]*\)")
_DKIM_RESULT = re.compile(r"^\s*dkim\s*=\s*([a-z]+)\b", re.IGNORECASE)
_SIGNING_DOMAIN = re.compile(r"\bheader\.([di])\s*=\s*\"?([^\s;\"]+)", re.IGNORECASE)


def _signing_domains(clause: str) -> list[str]:
    """The domains a ``dkim=`` clause names, from ``header.d`` or ``header.i``.

    ``header.i`` is an identity, ``@gcash.com`` or ``someone@gcash.com``;
    only the part after the ``@`` is a domain.
    """
    domains = []
    for tag, value in _SIGNING_DOMAIN.findall(clause):
        if tag.lower() == "i":
            value = value.rpartition("@")[2]
        domain = value.lower().rstrip(".")
        if domain:
            domains.append(domain)
    return domains


def _under(domain: str, expected: str) -> bool:
    """Is ``domain`` the expected one or a subdomain of it?

    The dot is what stops ``evilgcash.com`` passing for ``gcash.com``.
    """
    return domain == expected or domain.endswith("." + expected)


def _passed_email_authentication(
    auth_results: list[str], dkim_domains: tuple[str, ...]
) -> bool:
    """Did the bank's own domain sign this message, per Gmail's verdict?

    A ``From`` header proves nothing — it is trivially spoofable, and it is
    the only thing a Gmail search can match a sender on. Gmail stamps every inbound
    message with an ``Authentication-Results`` header carrying its own DKIM
    verdict, and that is what is checked here.

    Three things have to hold:

    * **The verdict is Gmail's.** A message can carry several
      ``Authentication-Results`` headers: one from each server that relayed
      it, or ones the sender wrote themselves. Gmail adds its own above all
      of them, so ``auth_results`` must be in message order and only the
      first counts, and only if its authserv-id is ``mx.google.com``.
    * **A ``dkim=pass`` names the bank's domain** (or a subdomain of it) in
      ``header.d`` or ``header.i``. Any passing signature is not enough: a
      sender with their own valid domain can set ``From:`` to the bank's
      address, and a forwarded copy carries the forwarder's own passing
      signature even when the bank's broke on the way.
    * **SPF is not required.** After a forward it describes the forwarder,
      not the bank, so it would either reject every forwarded statement or
      prove nothing about the bank.

    GoTyme sends through Amazon SES, so its verdict carries two ``dkim=``
    results, one for ``gotyme.com.ph`` and one for ``amazonses.com``. Only
    the ``gotyme.com.ph`` one counts, wherever it sits: SES signs mail for
    anyone who sends through it.

    Fails closed: a missing header, a verdict from anyone but Gmail, or no
    expected domains at all is treated as failed rather than unknown. A
    message this cheap to forge should not be trusted by default just
    because Gmail did not say it was bad.
    """
    expected = tuple(
        d.lower().lstrip("@.").rstrip(".") for d in dkim_domains if d and d.strip("@.")
    )
    if not expected or not auth_results:
        return False

    verdict = _COMMENT.sub(" ", auth_results[0])
    authserv_id, _, results = verdict.partition(";")
    if authserv_id.lower().split()[:1] != [_GMAIL_AUTHSERV_ID]:
        return False

    for clause in results.split(";"):
        match = _DKIM_RESULT.match(clause)
        if not match or match.group(1).lower() != "pass":
            continue
        if any(_under(d, e) for d in _signing_domains(clause) for e in expected):
            return True
    return False


def build_gmail_query(base: str, date_filter: str = "", timezone: str = "Asia/Manila") -> str:
    """Combine the base search with a date window.

    Without a window the search matches every unlabelled statement in the
    mailbox, which on a first run means importing months of backlog at once.
    Scoping it is the difference between "process what just arrived" and
    "process everything I have ever been sent".

    Accepted values for ``date_filter``:

    * ``today``     — messages received today in ``timezone``
    * ``yesterday`` — today and yesterday, which is the safe choice for a
      daily schedule because a single missed run does not lose a statement
    * ``1d`` / ``2w`` / ``3m`` — Gmail's own relative age, a rolling window
      measured backwards from now rather than a calendar boundary
    * ``""`` / ``all`` — no window; use deliberately, for a backfill
    """
    base = " ".join(base.split())
    token = (date_filter or "").strip().lower()

    if not token or token == "all":
        return base

    try:
        from zoneinfo import ZoneInfo

        today = datetime.now(ZoneInfo(timezone)).date()
    except Exception:  # noqa: BLE001 - bad tz name shouldn't break the run
        logger.warning("unknown timezone %r; falling back to system local time", timezone)
        today = datetime.now().date()

    if token == "today":
        # Gmail's after: is exclusive of the named day, so step back one.
        clause = f"after:{(today - timedelta(days=1)):%Y/%m/%d}"
    elif token == "yesterday":
        clause = f"after:{(today - timedelta(days=2)):%Y/%m/%d}"
    elif _RELATIVE_AGE.match(token):
        clause = f"newer_than:{token}"
    else:
        logger.warning(
            "unrecognised gmail date_filter %r; searching without a date window", date_filter
        )
        return base

    return f"{base} {clause}"


@dataclass
class FetchedStatement:
    """A statement PDF pulled out of a Gmail message."""

    message_id: str
    thread_id: str
    subject: str
    received_at: datetime
    filename: str
    content: bytes
    # Plain-text body, used to tell accounts apart when several send
    # identical From and Subject headers. GCash prints a masked "Mobile
    # number:" here, which is the only thing distinguishing two of them.
    body_text: str = ""

    @property
    def period_label(self) -> str:
        """A filesystem-safe period string derived from the subject line."""
        match = _PERIOD_IN_SUBJECT.search(self.subject)
        if not match:
            return self.received_at.strftime("%Y-%m-%d")
        try:
            start = _parse_subject_date(match.group(1))
            end = _parse_subject_date(match.group(2))
            return f"{start:%Y-%m-%d}_to_{end:%Y-%m-%d}"
        except ValueError:
            return self.received_at.strftime("%Y-%m-%d")


def _parse_subject_date(text: str) -> datetime:
    cleaned = text.replace(".", "").strip()
    for fmt in ("%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
    raise ValueError(f"unrecognised date in subject: {text!r}")


class GmailClient:
    """Reads statements from Gmail and, with the modify scope, labels them done.

    Needs the ``[gmail]`` extra. The Google client is imported here rather
    than at module level, so the pure helpers above work without it.
    """

    def __init__(self, token: dict):
        from googleapiclient.discovery import build

        from .auth import build_credentials, can_label

        self.can_label = can_label(token)
        self._service = build(
            "gmail", "v1", credentials=build_credentials(token), cache_discovery=False
        )

    # -- reading -----------------------------------------------------------

    def find_statements(self, query: str, max_results: int = 25) -> list[dict]:
        """Return message stubs matching the search query, oldest first.

        Oldest first matters: if several weeks have piled up, importing them
        in chronological order keeps a tracker's register sensible.
        """
        response = (
            self._service.users()
            .messages()
            .list(userId="me", q=query, maxResults=max_results)
            .execute()
        )
        messages = response.get("messages", [])
        logger.info("Gmail search matched %d message(s)", len(messages))
        return list(reversed(messages))

    def fetch_statement(
        self,
        message_id: str,
        attachment_patterns: tuple[str, ...] = (),
        *,
        body_only: bool = False,
        dkim_domains: tuple[str, ...] = (),
    ) -> Optional[FetchedStatement]:
        """Download the message's PDF attachment.

        ``dkim_domains`` are the domains the provider signs with. A message
        no one of them signed is refused; with none given, every message is.

        With no ``attachment_patterns`` this takes the first PDF, which is
        right when a message carries exactly one. GoTyme sends two — the
        deposit and savings statements — so a source may name the filename
        fragment it wants. A named pattern that matches nothing is an error
        rather than a fallback: taking "the first PDF" instead would file one
        account's transactions against the other, silently.

        ``body_only`` takes the message body instead, for a provider whose
        receipt *is* the email. The HTML part is preferred over the plain one,
        and only one is taken: both together would print every amount twice.
        """
        message = (
            self._service.users()
            .messages()
            .get(userId="me", id=message_id, format="full")
            .execute()
        )

        raw_headers = message.get("payload", {}).get("headers", [])
        # A dict keeps the *last* of a repeated header, which for
        # Authentication-Results is the one furthest from Gmail. Those are
        # collected separately, in message order.
        headers = {h["name"].lower(): h["value"] for h in raw_headers}
        auth_results = [
            h["value"] for h in raw_headers if h["name"].lower() == "authentication-results"
        ]
        subject = headers.get("subject", "(no subject)")

        if not _passed_email_authentication(auth_results, dkim_domains):
            # The PDF password is the practical barrier to a forged import,
            # and for GCash it is a guessable one (a surname and four digits)
            # — a spoofed From header should not even get that far. Left
            # unlabelled, like the "no PDF attachment" case below, so a
            # legitimate message caught by a transient DKIM hiccup is
            # retried rather than lost.
            logger.error(
                "message %s (%r) is not DKIM-signed by %s per Gmail; leaving it "
                "unlabelled rather than importing it as a statement",
                message_id, subject, " or ".join(dkim_domains) or "any configured domain",
            )
            return None

        received_at = datetime.fromtimestamp(
            int(message.get("internalDate", "0")) / 1000, tz=timezone.utc
        )
        if "date" in headers:
            try:
                received_at = parsedate_to_datetime(headers["date"])
            except (TypeError, ValueError):
                pass

        if body_only:
            part = self._find_body_part(message.get("payload", {}))
            if part is None:
                logger.warning("message %s has no readable body; skipping", message_id)
                return None
            content = self._part_bytes(message_id, part)
            suffix = ".html" if part.get("mimeType", "").lower() == "text/html" else ".txt"
            logger.info("read the body (%d bytes) of %s", len(content), subject)
            return FetchedStatement(
                message_id=message_id,
                thread_id=message.get("threadId", ""),
                subject=subject,
                received_at=received_at,
                filename=f"{message_id}{suffix}",
                content=content,
                body_text=self._extract_body_text(message.get("payload", {})),
            )

        part = self._find_pdf_part(message.get("payload", {}), attachment_patterns)
        if part is None:
            available = self._pdf_filenames(message.get("payload", {}))
            if attachment_patterns and available:
                logger.error(
                    "message %s has no PDF matching %s; its attachments are %s. "
                    "Leaving it unlabelled rather than importing the wrong account.",
                    message_id,
                    " or ".join(repr(p) for p in attachment_patterns),
                    ", ".join(available),
                )
            else:
                logger.warning("message %s has no PDF attachment; skipping", message_id)
            return None

        content = self._part_bytes(message_id, part)
        filename = part.get("filename") or f"gcash-{message_id}.pdf"

        logger.info("downloaded %s (%d bytes) from %s", filename, len(content), subject)
        return FetchedStatement(
            message_id=message_id,
            thread_id=message.get("threadId", ""),
            subject=subject,
            received_at=received_at,
            filename=filename,
            content=content,
            body_text=self._extract_body_text(message.get("payload", {})),
        )

    def _part_bytes(self, message_id: str, part: dict) -> bytes:
        """A part's decoded content, fetched separately when Gmail did not inline it."""
        body = part.get("body", {})
        data = body.get("data")
        if not data:
            attachment = (
                self._service.users()
                .messages()
                .attachments()
                .get(userId="me", messageId=message_id, id=body["attachmentId"])
                .execute()
            )
            data = attachment["data"]
        return base64.urlsafe_b64decode(data.encode("utf-8"))

    def _find_body_part(self, payload: dict) -> Optional[dict]:
        """The HTML body if there is one, else the plain-text one."""
        for wanted in ("text/html", "text/plain"):
            found = self._walk_body_parts(payload, wanted)
            if found is not None:
                return found
        return None

    def _walk_body_parts(self, payload: dict, mime: str) -> Optional[dict]:
        body = payload.get("body", {})
        if (
            (payload.get("mimeType") or "").lower() == mime
            and not payload.get("filename")
            and (body.get("data") or body.get("attachmentId"))
        ):
            return payload
        for part in payload.get("parts", []) or []:
            found = self._walk_body_parts(part, mime)
            if found is not None:
                return found
        return None

    def _extract_body_text(self, payload: dict) -> str:
        """Collect the message's readable text, for source routing.

        Prefers text/plain; falls back to crudely de-tagged text/html, which
        is enough to find a mobile number even when the plain part is absent.
        """
        chunks: list[str] = []

        def walk(part: dict) -> None:
            mime = (part.get("mimeType") or "").lower()
            data = part.get("body", {}).get("data")
            if data and mime in ("text/plain", "text/html"):
                try:
                    text = base64.urlsafe_b64decode(data.encode("utf-8")).decode(
                        "utf-8", errors="replace"
                    )
                except Exception:  # noqa: BLE001 - a bad part must not break the fetch
                    return
                if mime == "text/html":
                    text = re.sub(r"<[^>]+>", " ", text)
                chunks.append(text)
            for child in part.get("parts", []) or []:
                walk(child)

        walk(payload)
        return " ".join(" ".join(chunks).split())

    def _find_pdf_part(
        self, payload: dict, attachment_patterns: tuple[str, ...] = ()
    ) -> Optional[dict]:
        """Depth-first search for a PDF part, since Gmail nests multipart bodies.

        Patterns are tried in order, so a source listing the more specific
        fragment first gets a deterministic choice rather than document order.
        """
        if attachment_patterns:
            for pattern in attachment_patterns:
                found = self._walk_pdf_parts(payload, pattern.lower())
                if found is not None:
                    return found
            return None
        return self._walk_pdf_parts(payload, None)

    def _walk_pdf_parts(self, payload: dict, pattern: Optional[str]) -> Optional[dict]:
        filename = (payload.get("filename") or "").lower()
        mime = (payload.get("mimeType") or "").lower()
        if filename.endswith(".pdf") or mime == "application/pdf":
            if payload.get("body", {}).get("attachmentId") or payload.get("body", {}).get("data"):
                if pattern is None or pattern in filename:
                    return payload
        for part in payload.get("parts", []) or []:
            found = self._walk_pdf_parts(part, pattern)
            if found is not None:
                return found
        return None

    def _pdf_filenames(self, payload: dict) -> list[str]:
        """Every PDF filename on the message, for a useful error."""
        names: list[str] = []
        filename = payload.get("filename") or ""
        if filename.lower().endswith(".pdf"):
            names.append(filename)
        for part in payload.get("parts", []) or []:
            names.extend(self._pdf_filenames(part))
        return names

    # -- marking -----------------------------------------------------------

    def ensure_label(self, name: str) -> str:
        """Return the ID of a label, creating it if this is the first run."""
        existing = self._service.users().labels().list(userId="me").execute()
        for label in existing.get("labels", []):
            if label["name"].lower() == name.lower():
                return label["id"]
        created = (
            self._service.users()
            .labels()
            .create(
                userId="me",
                body={
                    "name": name,
                    "labelListVisibility": "labelShow",
                    "messageListVisibility": "show",
                },
            )
            .execute()
        )
        logger.info("created Gmail label %s", name)
        return created["id"]

    def mark_processed(self, message_id: str, label_name: str) -> None:
        """Apply the processed label so the next run's query skips this message.

        A read-only token cannot label. That is a supported choice, not an
        error: the next run sees the message again, and the sink's duplicate
        key is what stops it importing twice.
        """
        from googleapiclient.errors import HttpError

        if not self.can_label:
            logger.info("read-only Gmail token; not labelling message %s", message_id)
            return
        try:
            label_id = self.ensure_label(label_name)
            self._service.users().messages().modify(
                userId="me", id=message_id, body={"addLabelIds": [label_id]}
            ).execute()
            logger.info("labelled message %s as %s", message_id, label_name)
        except HttpError as exc:
            # Not fatal: the sink's duplicate key still prevents a double import.
            logger.error("could not label message %s: %s", message_id, exc)
