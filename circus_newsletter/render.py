"""Render reviewed issue content into a simple HTML email and plain-text counterpart."""

from __future__ import annotations

import html
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from string import Template
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from circus_newsletter.core import _issue_date as issue_close_date
from circus_newsletter.settings import load_settings


def _escape(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Issue text must be a string")
    return html.escape(value, quote=True)


def _paragraphs(value: str) -> str:
    return "".join(
        f'<p style="margin:0 0 14px;font-size:16px;line-height:1.6;word-wrap:break-word;overflow-wrap:anywhere;">'
        + _escape(part.strip()).replace("\n", "<br>") + "</p>"
        for part in re.split(r"\n\s*\n", value.strip()) if part.strip()
    )


def _section(heading: str, content: str, *, show_empty: bool = False) -> str:
    if not content and not show_empty:
        return ""
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" '
        'style="border-collapse:collapse;width:100%;margin-top:24px;">'
        '<tr><td style="border-top:1px solid #d7dcd6;padding-top:22px;word-wrap:break-word;overflow-wrap:anywhere;">'
        f'<h2 style="margin:0 0 19px;color:#20463a;font-size:13px;line-height:1.4;'
        f'font-weight:700;letter-spacing:1.4px;">{heading}</h2>{content}</td></tr></table>'
    )


def _group_positions(items: list[dict]) -> list[dict]:
    """Combine wording variants without guessing whether different opinions agree."""
    grouped = {}
    for item in items:
        text = item["text"].strip()
        key = re.sub(r"\s+", " ", text).rstrip(".!? ").casefold()
        if key not in grouped:
            grouped[key] = {"text": text, "names": []}
        names = item.get("contributors")
        if names is None:  # Compatibility with earlier preview/issue data.
            names = [item["attribution"]]
        if not isinstance(names, list) or not names:
            raise ValueError("Long/Short contributors must be a nonempty list")
        for name in names:
            if not isinstance(name, str) or not name.strip():
                raise ValueError("Long/Short contributor names must be nonempty strings")
            if name.strip() not in grouped[key]["names"]:
                grouped[key]["names"].append(name.strip())
    return [{"text": item["text"], "attribution": ", ".join(item["names"])}
            for item in grouped.values()]


def _sleepless_take(issue: dict, *, allow_empty: bool = False) -> str:
    take = issue.get("sleepless_take", "")
    if not isinstance(take, str) or (not take.strip() and not allow_empty):
        raise ValueError("Sleepless’s Take is empty")
    return take.strip()


def _url(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Link URL must be a string")
    if any(char.isspace() or ord(char) < 32 for char in value):
        raise ValueError("Issue link contains whitespace or control characters")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError(f"Invalid issue link: {value!r}")
    return html.escape(value, quote=True)


def _image_src(value: str) -> str:
    if value.startswith("cid:") and re.fullmatch(r"cid:[A-Za-z0-9._@+-]+", value):
        return html.escape(value, quote=True)
    if value.startswith("https://"):
        return _url(value)
    raise ValueError("Gallery image_src must be a cid: reference or HTTPS URL")


def _issue_date(week: str, schedule: dict) -> date:
    return issue_close_date(week, schedule)


def issue_number(week: str, schedule: dict) -> int:
    first = schedule.get("first_issue_number")
    if not isinstance(first, int) or first < 0:
        raise ValueError("first_issue_number must be a nonnegative integer")
    distance = (_issue_date(week, schedule) - _issue_date(schedule["first_issue_week"], schedule)).days
    if distance < 0 or distance % 7:
        raise ValueError("Issue week precedes the numbering baseline")
    return first + distance // 7


def issue_label(week: str, schedule: dict) -> str:
    return f"№{issue_number(week, schedule)}"


def next_deadline(week: str, schedule: dict) -> str:
    closing = _issue_date(week, schedule) + timedelta(days=7)
    hour, minute = map(int, schedule["cutoff_time"].split(":"))
    zone = ZoneInfo(schedule.get("cutoff_timezone", schedule["timezone"]))
    moment = datetime(closing.year, closing.month, closing.day, hour, minute, tzinfo=zone)
    return moment.strftime("%A %d %B %Y, %H:%M %Z")


def render_html(issue: dict, schedule: dict, template_path: Path, *, preview: bool = False,
                allow_empty_take: bool = False, settings: dict | None = None) -> str:
    """The issue data supplies words; this function owns layout and escaping."""
    identity = (settings if settings is not None else load_settings(template_path.parent.parent))["newsletter"]
    headings = identity["headings"]
    number = issue_label(issue["issue_week"], schedule)
    title = _escape(issue["title"])
    summary = _paragraphs(issue["summary"])
    if not summary:
        raise ValueError("Issue summary is empty")
    take = _sleepless_take(issue, allow_empty=allow_empty_take)
# "feed" is the stable internal JSON key; the reader-facing section is "The Lead". "feed" remains as an artefact from before the output heading name was changed from feed to lead.
    feed = []
    for item in issue["feed"]:
        feed.append(
            '<div style="padding:0 0 20px;">'
            f'<h3 style="margin:0 0 7px;color:#193c32;font-size:19px;line-height:1.3;font-weight:700;">'
            f'{_escape(item["headline"])}</h3>'
            f'{_paragraphs(item["text"])}'
            f'<p style="margin:0;color:#6b736d;font-size:13px;line-height:1.4;">'
            f'{_escape(item["attribution"])}</p></div>'
        )

    worth = []
    for item in issue.get("worth_your_time", []):
        worth.append(
            '<div style="padding:0 0 16px;">'
            f'<a href="{_url(item["url"])}" style="color:#244c40;font-size:16px;line-height:1.5;'
            'font-weight:700;text-decoration:underline;word-wrap:break-word;overflow-wrap:anywhere;">'
            f'{_escape(item["label"])}</a>'
            f'<p style="margin:5px 0 0;color:#263a34;font-size:15px;line-height:1.55;">'
            f'{_escape(item["note"])}</p></div>'
        )

    updates = []
    for item in issue.get("personal_updates", []):
        updates.append(
            '<div style="padding:0 0 16px;">'
            f'<p style="margin:0 0 5px;color:#193c32;font-size:16px;line-height:1.4;font-weight:700;">'
            f'{_escape(item["name"])}</p>{_paragraphs(item["text"])}</div>'
        )

    positions = issue.get("long_and_short", {})
    position_columns = []
    if any(positions.get(side) for side in ("long", "short")):
        for side, label in (("long", "Long"), ("short", "Short")):
            entries = "".join(
                '<p style="margin:0 0 16px;font-size:16px;line-height:1.6;overflow-wrap:anywhere;">'
                + _escape(item["text"])
                + ' <span style="color:#6b736d;font-size:13px;">- '
                + _escape(item["attribution"]) + '</span></p>'
                for item in _group_positions(positions.get(side, []))
            )
            position_columns.append(
                '<td class="circus-position-column" width="50%" valign="top" '
                'style="width:50%;padding:0 18px 0 0;vertical-align:top;">'
                '<h2 style="margin:0 0 19px;color:#20463a;font-size:13px;line-height:1.4;'
                'font-weight:700;letter-spacing:1.4px;">'
                + _escape(headings.get(side, label).upper()) + '</h2>' + entries + '</td>'
            )
    positions_html = (
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" '
        'style="border-collapse:collapse;width:100%;margin-top:24px;">'
        '<tr><td style="border-top:1px solid #d7dcd6;padding-top:22px;">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" '
        'style="border-collapse:collapse;width:100%;table-layout:fixed;"><tr>'
        + "".join(position_columns) + '</tr></table></td></tr></table>'
    ) if position_columns else ""

    gallery = []
    for item in issue.get("gallery", []):
        src = item.get("image_src")
        if src:
            image = (f'<img src="{_image_src(src)}" alt="{_escape(item["alt"])}" width="544" '
                     'style="display:block;width:100%;max-width:544px;height:auto;border:0;">')
        elif preview:
            image = ('<div style="background:#e9eee8;border:1px solid #d7dcd6;padding:56px 12px;'
                     'text-align:center;" role="img" aria-label="'
                     + _escape(item["alt"]) + '">&nbsp;</div>')
        else:
            raise ValueError("Gallery item is missing image_src")
        gallery.append(
            '<div style="padding:0 0 21px;">' + image +
            f'<p style="margin:7px 0 0;color:#596b61;font-size:13px;line-height:1.5;">'
            f'{_escape(item["caption"])} · {_escape(item["credit"])}</p></div>'
        )

    contributors = issue["ledger"]["contributors"]
    if not contributors:
        raise ValueError("The Ledger needs at least one contributor")
    ledger = (
        '<p style="margin:0 0 7px;color:#263a34;font-size:15px;line-height:1.6;">'
        '<strong>Contributors:</strong> ' + ", ".join(_escape(name) for name in contributors) + '</p>'
        '<p style="margin:0;color:#263a34;font-size:15px;line-height:1.6;">'
        '<strong>Next submission deadline:</strong> ' + _escape(next_deadline(issue["issue_week"], schedule)) + '</p>'
    )
    layout = Template(template_path.read_text(encoding="utf-8"))
    return layout.substitute(
        email_title=f"{_escape(identity['name'])} {number}: {title}",
        newsletter_name=_escape(identity["name"].upper()),
        issue_number=number, title=title, summary_html=summary,
        feed_html=_section(_escape(headings["feed"].upper()), "".join(feed), show_empty=True),
        worth_html=_section(_escape(headings["worth_your_time"].upper()), "".join(worth)),
        updates_html=_section(_escape(headings["personal_updates"].upper()), "".join(updates)),
        long_and_short_html=positions_html,
        gallery_html=_section(_escape(headings["gallery"].upper()), "".join(gallery)),
        take_html=_section(_escape(headings["editorial"].upper()), _paragraphs(take), show_empty=allow_empty_take),
        ledger_html=_section(_escape(headings["ledger"].upper()), ledger),
    )


def render_text(issue: dict, schedule: dict, *, allow_empty_take: bool = False,
                settings: dict | None = None) -> str:
    """Plain-text companion for a future multipart/alternative email."""
    take = _sleepless_take(issue, allow_empty=allow_empty_take)
    identity = (settings if settings is not None else load_settings())["newsletter"]
    headings = identity["headings"]
    lines = [f"{identity['name'].upper()} | {issue_label(issue['issue_week'], schedule)}",
             issue["title"], "=" * min(len(issue["title"]), 72), "", issue["summary"], "", headings["feed"].upper(), ""]
    for item in issue["feed"]:
        lines.extend([item["headline"], item["text"], f"By {item['attribution']}", ""])
    for key, items in (("worth_your_time", issue.get("worth_your_time", [])),
                       ("personal_updates", issue.get("personal_updates", [])),
                       ("long_and_short", issue.get("long_and_short", {})),
                       ("gallery", issue.get("gallery", []))):
        if key == "long_and_short":
            if any(items.get(side) for side in ("long", "short")):
                for side, label in (("long", "Long"), ("short", "Short")):
                    lines.extend([headings.get(side, label).upper(), ""])
                    for item in _group_positions(items.get(side, [])):
                        lines.extend([f"{item['text']} - {item['attribution']}", ""])
            continue
        if items:
            lines.extend([headings[key].upper(), ""])
            for item in items:
                if key == "worth_your_time":
                    lines.extend([f"{item['label']}: {item['url']}", item["note"], ""])
                elif key == "personal_updates":
                    lines.extend([f"{item['name']}: {item['text']}", ""])
                else:
                    lines.extend([f"{item['caption']} — {item['credit']}", ""])
    lines.extend([headings["editorial"].upper(), "", take, "", headings["ledger"].upper(), "",
                  "Contributors: " + ", ".join(issue["ledger"]["contributors"]),
                  "Next submission deadline: " + next_deadline(issue["issue_week"], schedule), ""])
    return "\n".join(lines)
