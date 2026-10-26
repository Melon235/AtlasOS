"""Fixed capability-retrieval evaluation set for V2 M1 semantic tool search."""

from __future__ import annotations

import dataclasses

from atlasos.tools.search import ToolSearchEntry, ToolSearchIndex

TOOL_CATALOG: tuple[tuple[str, str], ...] = (
    ("github.issue.get", "Retrieve a GitHub issue by number, including labels and comments."),
    ("github.issue.create", "File a new bug report or feature issue in a GitHub repository."),
    ("github.pr.merge", "Merge an approved pull request into the repository."),
    ("jira.ticket.search", "Search JIRA support tickets by project, status, or reporter."),
    ("jira.ticket.create", "Create a JIRA ticket for a support request."),
    ("email.send", "Send an email message to one or more recipients."),
    ("email.inbox.search", "Search the mailbox for messages from a sender or subject."),
    ("calendar.create_event", "Schedule a meeting or appointment on a calendar."),
    ("calendar.availability", "Check free and busy times to find a meeting slot."),
    ("slack.message.send", "Post a message to a Slack channel or teammate."),
    ("docs.search", "Search the internal company knowledge base and policy documents."),
    ("docs.read", "Read a document from the internal knowledge base."),
    ("db.query", "Run a read-only SQL query against the customer database."),
    ("db.schema", "Inspect database tables, columns, and relationships."),
    ("crm.contact.lookup", "Look up a customer contact by name, email, or company."),
    ("crm.deal.update", "Update a sales opportunity in the CRM pipeline."),
    ("payments.refund", "Issue a refund to a customer for a charge."),
    ("payments.status", "Check the status of a payment or charge."),
    ("weather.forecast", "Get the weather forecast for a city and date."),
    ("maps.directions", "Get driving or walking directions between two places."),
    ("translate.text", "Translate text from one language into another."),
    ("summarize.document", "Produce a shorter summary of a long document or report."),
    ("pdf.extract", "Extract text and tables from a PDF file."),
    ("s3.upload", "Upload a file to cloud object storage."),
)

EVAL_QUERIES: tuple[tuple[str, str], ...] = (
    ("file a bug report for a repository", "github.issue.create"),
    ("find an existing bug ticket on github", "github.issue.get"),
    ("merge my code change request", "github.pr.merge"),
    ("look up a support ticket in jira", "jira.ticket.search"),
    ("open a support ticket for a customer", "jira.ticket.create"),
    ("send a message to a coworker by email", "email.send"),
    ("find messages from my manager in my mailbox", "email.inbox.search"),
    ("schedule a meeting on my calendar", "calendar.create_event"),
    ("find when everyone is free for a meeting", "calendar.availability"),
    ("post an update in the team chat", "slack.message.send"),
    ("search company policy documents", "docs.search"),
    ("read the internal onboarding handbook", "docs.read"),
    ("query the customer database for orders", "db.query"),
    ("list database tables and columns", "db.schema"),
    ("find a customer's phone number", "crm.contact.lookup"),
    ("move a sales opportunity to won", "crm.deal.update"),
    ("give a customer their money back", "payments.refund"),
    ("check whether a charge succeeded", "payments.status"),
    ("what is the weather tomorrow", "weather.forecast"),
    ("get directions to the airport", "maps.directions"),
    ("translate this paragraph into Spanish", "translate.text"),
    ("make this long report shorter", "summarize.document"),
    ("pull tables out of a pdf", "pdf.extract"),
    ("upload a backup file to cloud storage", "s3.upload"),
)


@dataclasses.dataclass(frozen=True)
class SearchMetrics:
    recall_at_1: float
    recall_at_3: float
    mrr: float
    cases: int
    per_query_rank: tuple[tuple[str, str, int | None], ...]


def catalog_entries() -> tuple[ToolSearchEntry, ...]:
    return tuple(
        ToolSearchEntry(
            tool_id=tool_id,
            retrieval_text=f"{tool_id}\n{description}",
        )
        for tool_id, description in TOOL_CATALOG
    )


def evaluate(index: ToolSearchIndex) -> SearchMetrics:
    hits_at_1 = 0
    hits_at_3 = 0
    reciprocal_rank = 0.0
    ranks: list[tuple[str, str, int | None]] = []
    for query, expected in EVAL_QUERIES:
        matches = index.search(query, top_k=3)
        ids = [match.tool_id for match in matches]
        rank = ids.index(expected) + 1 if expected in ids else None
        ranks.append((query, expected, rank))
        if rank == 1:
            hits_at_1 += 1
        if rank is not None and rank <= 3:
            hits_at_3 += 1
        if rank is not None:
            reciprocal_rank += 1.0 / rank
    total = len(EVAL_QUERIES)
    return SearchMetrics(
        recall_at_1=hits_at_1 / total,
        recall_at_3=hits_at_3 / total,
        mrr=reciprocal_rank / total,
        cases=total,
        per_query_rank=tuple(ranks),
    )
