"""Push a topic summary to a Notion database as a new page.

Schema-aware: inspects the target database's properties and only fills in
Date/URL/Tags-style properties if they actually exist, rather than assuming
a fixed schema.

Notion's API models a database as one or more "data sources" — the property
schema and page parenting live on the data source, not the database itself
(client.databases.retrieve() no longer returns `properties` directly; use
client.data_sources.retrieve() on the database's first data source). This
targets that current model, with a fallback to the older single-schema
shape for any database that hasn't been migrated to it.
"""
from __future__ import annotations

from notion_client import Client

MAX_BLOCK_CHARS = 1900  # stay safely under Notion's 2000-char rich_text limit


def _find_property(properties: dict, types: set[str], name_hints: list[str]) -> tuple[str, str] | None:
    """Find a property by matching either its type or a name hint. Returns
    (property_name, property_type) or None."""
    lowered_hints = [h.lower() for h in name_hints]
    for name, prop in properties.items():
        if prop.get("type") in types and name.lower() in lowered_hints:
            return name, prop["type"]
    for name, prop in properties.items():
        if prop.get("type") in types:
            return name, prop["type"]
    return None


def _paragraph_blocks(body: str) -> list[dict]:
    blocks = []
    for para in body.split("\n\n"):
        para = para.strip()
        if not para:
            continue
        for i in range(0, len(para), MAX_BLOCK_CHARS):
            chunk = para[i : i + MAX_BLOCK_CHARS]
            blocks.append(
                {
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {"rich_text": [{"type": "text", "text": {"content": chunk}}]},
                }
            )
    return blocks


def _resolve_target(client: Client, database_id: str) -> tuple[dict, dict]:
    """Returns (parent, properties_schema) for the database, handling both
    the current data-source model and the legacy single-schema shape."""
    db = client.databases.retrieve(database_id=database_id)
    data_sources = db.get("data_sources")

    if data_sources:
        data_source_id = data_sources[0]["id"]
        ds = client.data_sources.retrieve(data_source_id=data_source_id)
        return {"data_source_id": data_source_id}, ds["properties"]

    return {"database_id": database_id}, db["properties"]


def push_topic(
    token: str,
    database_id: str,
    topic_title: str,
    body: str,
    published_date: str,
    source_url: str,
) -> str:
    """Creates a new page in the database for this topic. Returns the new
    page's URL."""
    client = Client(auth=token)
    parent, properties_schema = _resolve_target(client, database_id)

    title_prop = next((name for name, p in properties_schema.items() if p.get("type") == "title"), None)
    if not title_prop:
        raise ValueError("Target Notion database has no title property.")

    properties = {title_prop: {"title": [{"type": "text", "text": {"content": topic_title}}]}}

    date_prop = _find_property(properties_schema, {"date"}, ["date", "published"])
    if date_prop and published_date:
        properties[date_prop[0]] = {"date": {"start": published_date}}

    url_prop = _find_property(properties_schema, {"url"}, ["source", "url", "link"])
    if url_prop and source_url:
        properties[url_prop[0]] = {"url": source_url}

    page = client.pages.create(
        parent=parent,
        properties=properties,
        children=_paragraph_blocks(body),
    )
    return page.get("url", "")
