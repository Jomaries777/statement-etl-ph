"""Tests for finding statements in Gmail: the search, the attachment, and
the check that the bank itself sent the message."""

from __future__ import annotations

from statement_etl_ph.gmail.fetch import build_gmail_query

BASE_Q = 'from:noreply-transhistory@gcash.com subject:"Your GCash Transaction History" has:attachment'

def test_today_adds_an_after_clause():
    from datetime import date, timedelta

    q = build_gmail_query(BASE_Q, "today", "Asia/Manila")
    assert "after:" in q
    # Deliberately one day generous: Gmail's after: boundary is ambiguous, and
    # erring wide is safe (the label and import_id both prevent reprocessing)
    # whereas erring narrow would silently miss the statement.
    assert q.endswith((date.today() - timedelta(days=1)).strftime("after:%Y/%m/%d")) or \
           q.endswith(date.today().strftime("after:%Y/%m/%d"))


def test_yesterday_is_wider_than_today():
    today_q = build_gmail_query(BASE_Q, "today")
    yday_q = build_gmail_query(BASE_Q, "yesterday")
    assert today_q != yday_q


def test_relative_ages_pass_through_to_gmail():
    assert build_gmail_query(BASE_Q, "7d").endswith("newer_than:7d")
    assert build_gmail_query(BASE_Q, "2w").endswith("newer_than:2w")
    assert build_gmail_query(BASE_Q, "3m").endswith("newer_than:3m")


def test_all_and_blank_mean_no_window():
    """The backfill escape hatch: search everything unlabelled."""
    assert build_gmail_query(BASE_Q, "all") == BASE_Q
    assert build_gmail_query(BASE_Q, "") == BASE_Q


def test_a_bad_window_does_not_produce_a_broken_query():
    """Better to search wide than to emit Gmail syntax that matches nothing."""
    assert build_gmail_query(BASE_Q, "last tuesday") == BASE_Q


def test_a_bad_timezone_still_produces_a_window():
    q = build_gmail_query(BASE_Q, "today", "Not/AZone")
    assert "after:" in q


def test_base_query_whitespace_is_collapsed():
    """config.yaml uses a YAML folded block, which leaves ragged whitespace."""
    messy = "from:x\n   subject:\"y\"\n   has:attachment\n"
    assert build_gmail_query(messy, "all") == 'from:x subject:"y" has:attachment'


def _pdf_part(name):
    return {"filename": name, "mimeType": "application/pdf", "body": {"attachmentId": "a" + name}}


def _message_with(*filenames):
    return {"mimeType": "multipart/mixed", "parts": [_pdf_part(n) for n in filenames]}


def test_the_named_attachment_is_chosen_not_the_first():
    """GoTyme sends the deposit and savings statements in one email.

    Taking "the first PDF" is document order, not a choice — and filing
    savings rows against the deposit account would be wrong in a way nothing
    downstream could detect.
    """
    from statement_etl_ph.gmail.fetch import GmailClient

    client = GmailClient.__new__(GmailClient)
    payload = _message_with(
        "GoTyme_BankStatement_SavingsAccounts_20260829.pdf",
        "GoTymeStatementForEverydayDepositAccount_20260829.pdf",
    )
    part = client._find_pdf_part(payload, ("EverydayDepositAccount",))
    assert part["filename"].startswith("GoTymeStatementForEverydayDeposit")


def test_an_attachment_pattern_that_matches_nothing_is_not_a_fallback():
    """Falling back to the first PDF would import the wrong account silently.

    Returning nothing leaves the email unlabelled, so it is retried once the
    pattern is fixed rather than being quietly mis-imported.
    """
    from statement_etl_ph.gmail.fetch import GmailClient

    client = GmailClient.__new__(GmailClient)
    payload = _message_with("GoTyme_BankStatement_SavingsAccounts_20260829.pdf")
    assert client._find_pdf_part(payload, ("EverydayDepositAccount",)) is None


def test_no_pattern_still_takes_the_first_pdf():
    """The GCash path passes no pattern and must behave exactly as before."""
    from statement_etl_ph.gmail.fetch import GmailClient

    client = GmailClient.__new__(GmailClient)
    payload = _message_with("first.pdf", "second.pdf")
    assert client._find_pdf_part(payload, ())["filename"] == "first.pdf"


GCASH = ("gcash.com",)
GOTYME = ("gotyme.com.ph",)


def _auth_results(dkim="pass", domain="gcash.com", spf="pass", authserv="mx.google.com"):
    return (
        f"{authserv}; dkim={dkim} header.i=@{domain} header.s=sel1 header.b=AbCdEf12; "
        f"spf={spf} (google.com: domain of noreply@{domain} designates 192.0.2.10 as "
        f"permitted sender) smtp.mailfrom=noreply@{domain}; "
        f"dmarc=pass (p=REJECT sp=REJECT dis=NONE) header.from={domain}"
    )


def _passes(results, domains=GCASH):
    from statement_etl_ph.gmail.fetch import _passed_email_authentication

    if isinstance(results, str):
        results = [results]
    return _passed_email_authentication(results, domains)


def test_a_direct_bank_email_authenticates():
    assert _passes(_auth_results())


def test_a_failing_bank_signature_does_not_authenticate():
    assert not _passes(_auth_results(dkim="fail"))
    assert not _passes(_auth_results(dkim="temperror"))


def test_spf_is_not_required():
    """After a forward SPF describes the forwarder, not the bank, so it proves
    nothing about the statement either way."""
    assert _passes(_auth_results(spf="softfail"))


def test_another_domains_signature_with_the_banks_from_does_not_authenticate():
    """A sender with their own DKIM-valid domain can set From: to the bank's
    address. Any passing signature used to be enough; now it must be the
    bank's."""
    spoof = (
        "mx.google.com; dkim=pass header.i=@lookalike-sender.example header.s=s1 "
        "header.b=Zz9Yy8Xx; spf=pass (google.com: domain of a@lookalike-sender.example "
        "designates 192.0.2.20 as permitted sender) smtp.mailfrom=a@lookalike-sender.example; "
        "dmarc=fail (p=NONE sp=NONE dis=NONE) header.from=gcash.com"
    )
    assert not _passes(spoof)


def test_a_forwarded_email_with_the_bank_signature_intact_authenticates():
    """Gmail's verdict on a forwarded copy: the bank's signature survived, and
    the forwarder signed it too. SPF describes the forwarder."""
    forwarded = (
        "mx.google.com; dkim=pass header.i=@gcash.com header.s=sel1 header.b=AbCdEf12; "
        "dkim=pass header.i=@outlook.example header.s=selector1 header.b=QwErTy34; "
        "arc=pass (i=1 spf=pass spfdomain=gcash.com dkim=pass dkdomain=gcash.com); "
        "spf=pass (google.com: domain of someone@outlook.example designates 192.0.2.30 "
        "as permitted sender) smtp.mailfrom=someone@outlook.example"
    )
    assert _passes(forwarded)


def test_a_forwarded_email_whose_bank_signature_broke_does_not_authenticate():
    """The forwarder's own DKIM and SPF pass, but they prove only that the
    forwarder sent it. Safe Links rewriting or a banner breaks the bank's."""
    broken = (
        "mx.google.com; dkim=fail header.i=@gcash.com header.s=sel1 header.b=AbCdEf12; "
        "dkim=pass header.i=@outlook.example header.s=selector1 header.b=QwErTy34; "
        "spf=pass (google.com: domain of someone@outlook.example designates 192.0.2.30 "
        "as permitted sender) smtp.mailfrom=someone@outlook.example"
    )
    assert not _passes(broken)


def test_only_gmails_own_verdict_counts_not_a_forged_one_below_it():
    """A message can carry Authentication-Results headers written by relays or
    by the sender. Gmail adds its own above them all, and only that one is
    read."""
    gmails = _auth_results(dkim="fail")
    forged = _auth_results()
    assert not _passes([gmails, forged])
    assert _passes([forged, gmails])  # the order fetch_statement preserves


def test_a_verdict_not_from_gmail_does_not_authenticate():
    assert not _passes(_auth_results(authserv="mx.attacker.example"))
    assert not _passes(_auth_results(authserv="mx.google.com.attacker.example"))


def test_a_subdomain_of_the_bank_authenticates():
    assert _passes(_auth_results(domain="mail.gcash.com"))
    header_d = "mx.google.com; dkim=pass header.d=notify.gcash.com header.s=s1 header.b=x"
    assert _passes(header_d)


def test_a_lookalike_domain_does_not_authenticate():
    assert not _passes(_auth_results(domain="evilgcash.com"))
    assert not _passes(_auth_results(domain="gcash.com.evil.example"))


def test_a_missing_header_or_no_expected_domain_fails_closed_not_open():
    """No verdict is not "unknown, so allow it" — the same fail-closed
    posture throughout this package."""
    assert not _passes([])
    assert not _passes("")
    assert not _passes(_auth_results(), domains=())


def test_an_expected_domain_is_normalised():
    assert _passes(_auth_results(), domains=("@GCash.com.",))


_GOTYME_STYLE_AUTH_RESULTS = (
    "mx.google.com;\n"
    "       dkim=pass header.i=@gotyme.com.ph header.s=examplegotymeselector000000000000 "
    "header.b=AAAAaaaa;\n"
    "       dkim=pass header.i=@amazonses.com header.s=examplesesselector0000000000000000 "
    "header.b=BBBBbbbb;\n"
    "       spf=pass (google.com: domain of "
    "0000000000000000-00000000-0000-0000-0000-000000000000-000000@ap-southeast-1.amazonses.com "
    "designates 192.0.2.53 as permitted sender) "
    "smtp.mailfrom=0000000000000000-00000000-0000-0000-0000-000000000000-000000"
    "@ap-southeast-1.amazonses.com;\n"
    "       dmarc=pass (p=REJECT sp=REJECT dis=NONE) header.from=gotyme.com.ph"
)


def test_gotymes_two_signature_header_authenticates():
    assert _passes(_GOTYME_STYLE_AUTH_RESULTS, GOTYME)


def test_only_gotymes_own_signature_counts_not_the_relays():
    """GoTyme sends through Amazon SES, which signs mail for anyone. Its
    signature passing says nothing about GoTyme, wherever the GoTyme one sits."""
    gotyme_fails = _GOTYME_STYLE_AUTH_RESULTS.replace(
        "dkim=pass header.i=@gotyme.com.ph", "dkim=fail header.i=@gotyme.com.ph"
    )
    assert not _passes(gotyme_fails, GOTYME)

    relay_fails = _GOTYME_STYLE_AUTH_RESULTS.replace(
        "dkim=pass header.i=@amazonses.com", "dkim=fail header.i=@amazonses.com"
    )
    assert _passes(relay_fails, GOTYME)


def test_one_providers_signature_does_not_pass_for_another():
    assert not _passes(_GOTYME_STYLE_AUTH_RESULTS, GCASH)


def _gmail_message(headers: dict, *, pdf=True):
    import base64

    header_list = [{"name": k, "value": v} for k, v in headers.items()]
    payload = {"headers": header_list, "mimeType": "multipart/mixed", "parts": []}
    if pdf:
        payload["parts"].append({
            "filename": "statement.pdf",
            "mimeType": "application/pdf",
            "body": {"data": base64.urlsafe_b64encode(b"%PDF-1.4 fake").decode()},
        })
    return {"id": "m1", "threadId": "t1", "internalDate": "1700000000000", "payload": payload}


class _FakeMessagesResource:
    def __init__(self, message):
        self._message = message

    def get(self, **kwargs):
        return self

    def execute(self):
        return self._message


class _FakeUsersResource:
    def __init__(self, message):
        self._message = message

    def messages(self):
        return _FakeMessagesResource(self._message)


class _FakeGmailService:
    def __init__(self, message):
        self._message = message

    def users(self):
        return _FakeUsersResource(self._message)


def test_a_message_that_fails_authentication_is_never_fetched():
    """The wiring, not just the pure check: fetch_statement must refuse a
    spoofable-looking message before it ever reaches the parser."""
    from statement_etl_ph.gmail.fetch import GmailClient

    client = GmailClient.__new__(GmailClient)
    message = _gmail_message({
        "Subject": "Your GCash Statement",
        "Authentication-Results": _auth_results(dkim="fail"),
    })
    client._service = _FakeGmailService(message)
    assert client.fetch_statement("m1", dkim_domains=GCASH) is None


def test_a_message_that_passes_authentication_is_fetched_as_before():
    from statement_etl_ph.gmail.fetch import GmailClient

    client = GmailClient.__new__(GmailClient)
    message = _gmail_message({
        "Subject": "Your GCash Statement",
        "Authentication-Results": _auth_results(),
    })
    client._service = _FakeGmailService(message)
    fetched = client.fetch_statement("m1", dkim_domains=GCASH)
    assert fetched is not None
    assert fetched.subject == "Your GCash Statement"


def test_without_expected_domains_nothing_is_fetched():
    from statement_etl_ph.gmail.fetch import GmailClient

    client = GmailClient.__new__(GmailClient)
    message = _gmail_message({
        "Subject": "Your GCash Statement",
        "Authentication-Results": _auth_results(),
    })
    client._service = _FakeGmailService(message)
    assert client.fetch_statement("m1") is None


def test_fetch_reads_the_first_authentication_results_not_the_last():
    """Headers repeat. Collapsing them into a dict kept the last one, which is
    the one furthest from Gmail and the easiest to forge."""
    from statement_etl_ph.gmail.fetch import GmailClient

    client = GmailClient.__new__(GmailClient)
    message = _gmail_message({"Subject": "Your GCash Statement"})
    message["payload"]["headers"] += [
        {"name": "Authentication-Results", "value": _auth_results(dkim="fail")},
        {"name": "Authentication-Results", "value": _auth_results()},
    ]
    client._service = _FakeGmailService(message)
    assert client.fetch_statement("m1", dkim_domains=GCASH) is None


def test_a_read_only_token_never_labels():
    """Read-only is a supported choice: the sink's duplicate key guards instead."""
    from statement_etl_ph.gmail.fetch import GmailClient

    client = GmailClient.__new__(GmailClient)
    client.can_label = False
    client._service = None  # any call on it would raise
    client.mark_processed("m1", "statement-etl-ph/gcash")
