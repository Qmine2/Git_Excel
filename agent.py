"""
Weekly PR -> Sheet agent (LangChain + LangGraph), all in one script.

What it does
------------
1. Works out the current work week (default: starts on Saturday, ends "now").
2. Pulls every pull request opened by one GitHub user inside that window.
3. Groups the PRs by day -> one day = one row.
4. Asks an LLM to write a short task description for each PR.
5. Writes one row per day into a spreadsheet (Google Sheets or a local .xlsx).

Everything that touches the outside world is a LangChain @tool, and the
pipeline is a LangGraph StateGraph so you can see the data flow node by node.

Dependencies are managed with uv (`uv sync`), so run it with:
    uv run agent.py                 # real run
    uv run agent.py --dry-run       # everything except writing the sheet
    uv run agent.py --print-rows    # show the rows it would write
    uv run agent.py --no-llm        # skip the LLM, use raw PR titles
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Annotated, Any, TypedDict

import requests
import yaml
from dotenv import load_dotenv
from langchain_core.tools import tool
from langgraph.graph import END, START, StateGraph

try:  # Python 3.9+
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore


# --------------------------------------------------------------------------- #
# 0. Configuration
# --------------------------------------------------------------------------- #

load_dotenv()

WEEKDAYS = {
    "MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6,
}
WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday",
                 "Friday", "Saturday", "Sunday"]

GITHUB_API = os.getenv("GITHUB_API_URL", "https://api.github.com").rstrip("/")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "y", "on"}


def tz():
    """Timezone used for 'which day does this PR belong to'."""
    name = _env("TIMEZONE", "UTC")
    if ZoneInfo is None:
        return None
    try:
        return ZoneInfo(name)
    except Exception:
        print(f"[warn] unknown TIMEZONE '{name}', falling back to UTC")
        return ZoneInfo("UTC")


@dataclass
class SheetConfig:
    """Parsed sheet_config.yaml - this is the part you adapt to YOUR sheet."""

    backend: str = "google"                    # google | xlsx
    worksheet: str = "Sheet1"
    header_row: int = 1
    first_data_row: int = 2
    write_mode: str = "upsert_by_date"         # upsert_by_date | append
    match_column: str | None = None            # which column holds the date key
    skip_empty_days: bool = False
    create_worksheet_if_missing: bool = True
    date_format: str = "%Y-%m-%d"
    task_line_template: str = "#{number}: {description}"
    task_separator: str = "\n"
    empty_day_text: str = ""
    columns: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def load(cls, path: str) -> "SheetConfig":
        if not os.path.exists(path):
            raise FileNotFoundError(
                f"Sheet config '{path}' not found. Copy sheet_config.yaml "
                f"from the repo and edit it to match your spreadsheet."
            )
        with open(path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}

        sheet = raw.get("sheet", {}) or {}
        fmt = raw.get("formatting", {}) or {}
        cfg = cls(
            backend=_env("SHEET_BACKEND", "") or sheet.get("backend", "google"),
            worksheet=_env("WORKSHEET_NAME", "") or sheet.get("worksheet", "Sheet1"),
            header_row=int(sheet.get("header_row", 1)),
            first_data_row=int(sheet.get("first_data_row", 2)),
            write_mode=sheet.get("write_mode", "upsert_by_date"),
            match_column=sheet.get("match_column"),
            skip_empty_days=bool(sheet.get("skip_empty_days", False)),
            create_worksheet_if_missing=bool(
                sheet.get("create_worksheet_if_missing", True)),
            date_format=fmt.get("date_format", "%Y-%m-%d"),
            task_line_template=fmt.get("task_line_template",
                                       "#{number}: {description}"),
            task_separator=fmt.get("task_separator", "\n"),
            empty_day_text=fmt.get("empty_day_text", ""),
            columns=raw.get("columns", []) or [],
        )
        if not cfg.columns:
            raise ValueError("sheet_config.yaml has no 'columns:' entries.")
        return cfg


# --------------------------------------------------------------------------- #
# 1. Date helpers - the "this week" logic
# --------------------------------------------------------------------------- #

def week_start(today: date, start_day: str) -> date:
    """Most recent `start_day` (inclusive) on or before `today`.

    With WEEK_START_DAY=SAT and today = Wednesday, this returns last Saturday.
    If today IS Saturday, it returns today.
    """
    target = WEEKDAYS[start_day.upper()[:3]]
    delta = (today.weekday() - target) % 7
    return today - timedelta(days=delta)


def daterange(start: date, end: date):
    cur = start
    while cur <= end:
        yield cur
        cur += timedelta(days=1)


def parse_gh_datetime(value: str) -> datetime:
    """GitHub returns ISO-8601 UTC like 2026-10-21T09:12:33Z."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


# --------------------------------------------------------------------------- #
# 2. TOOLS  (LangChain tools - the agent's hands)
# --------------------------------------------------------------------------- #

@tool
def fetch_pull_requests(author: str, start: str, end: str) -> list[dict]:
    """Fetch GitHub pull requests by `author` between `start` and `end` (YYYY-MM-DD).

    Uses the GitHub Search API. Scope (repo / org / state) comes from the
    GITHUB_REPO, GITHUB_ORG and PR_STATE environment variables.
    Returns a list of plain dicts: number, title, body, url, repo, timestamp.
    """
    token = _env("GITHUB_TOKEN")
    if not token:
        raise RuntimeError("GITHUB_TOKEN is missing. See .env.example.")

    date_field = _env("PR_DATE_FIELD", "created").lower()
    if date_field not in {"created", "merged", "closed", "updated"}:
        raise ValueError(
            f"PR_DATE_FIELD must be created|merged|closed|updated, got {date_field}")
    # 'merged' is a valid search qualifier too, so the query can use it directly.
    search_field = date_field

    qualifiers = ["type:pr", f"author:{author}", f"{search_field}:{start}..{end}"]

    for repo in [r.strip() for r in _env("GITHUB_REPO").split(",") if r.strip()]:
        qualifiers.append(f"repo:{repo}")
    org = _env("GITHUB_ORG")
    if org:
        qualifiers.append(f"org:{org}")

    state = _env("PR_STATE").lower()          # open | closed | merged | "" (all)
    if state == "merged":
        qualifiers.append("is:merged")
    elif state in {"open", "closed"}:
        qualifiers.append(f"state:{state}")

    extra = _env("GITHUB_SEARCH_EXTRA")
    if extra:
        qualifiers.append(extra)

    query = " ".join(qualifiers)
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    items: list[dict] = []
    page = 1
    while True:
        resp = requests.get(
            f"{GITHUB_API}/search/issues",
            headers=headers,
            params={"q": query, "per_page": 100, "page": page,
                    "sort": "created", "order": "asc"},
            timeout=30,
        )
        if resp.status_code == 422:
            raise RuntimeError(
                f"GitHub rejected the search query: {query}\n{resp.text}")
        resp.raise_for_status()
        payload = resp.json()
        items.extend(payload.get("items", []))
        if len(items) >= payload.get("total_count", 0) or page >= 10:
            break
        page += 1

    prs: list[dict] = []
    for item in items:
        repo_full = ""
        match = re.search(r"/repos/([^/]+/[^/]+)$", item.get("repository_url", ""))
        if match:
            repo_full = match.group(1)

        stamp = item.get("created_at")
        if date_field == "closed":
            stamp = item.get("closed_at") or stamp
        elif date_field == "updated":
            stamp = item.get("updated_at") or stamp
        elif date_field == "merged":
            # the search result carries no merged_at - fetch the PR itself
            pr_url = (item.get("pull_request") or {}).get("url")
            if pr_url:
                detail = requests.get(pr_url, headers=headers, timeout=30)
                if detail.ok:
                    stamp = detail.json().get("merged_at") or stamp

        prs.append({
            "number": item.get("number"),
            "title": item.get("title", ""),
            "body": (item.get("body") or "")[:4000],
            "url": item.get("html_url", ""),
            "repo": repo_full,
            "state": item.get("state", ""),
            "timestamp": stamp,
            "author": (item.get("user") or {}).get("login", author),
        })
    return prs


@tool
def describe_pull_requests(prs: list[dict]) -> dict:
    """Ask the LLM for a one-line task description per pull request.

    Input: list of PR dicts (number, title, body, repo).
    Output: {"<pr number>": "<description>"} covering every PR given.
    """
    if not prs:
        return {}

    llm = build_llm()
    style = _env("DESCRIPTION_STYLE",
                 "past tense, concrete, what was actually done")
    language = _env("DESCRIPTION_LANGUAGE", "English")
    max_words = _env("DESCRIPTION_MAX_WORDS", "18")

    payload = [
        {"number": p["number"], "title": p["title"],
         "repo": p.get("repo", ""), "body": (p.get("body") or "")[:1500]}
        for p in prs
    ]

    system = (
        "You summarise pull requests into short work-log entries for a weekly "
        "timesheet. Write in {lang}. Each description is at most {words} words, "
        "{style}. No markdown, no trailing period, and never repeat the PR "
        "number inside the description. Answer with JSON only: a single object "
        "mapping the PR number (as a string) to its description."
    ).format(lang=language, words=max_words, style=style)

    human = "Pull requests:\n" + json.dumps(payload, ensure_ascii=False, indent=2)

    raw = llm.invoke([{"role": "system", "content": system},
                      {"role": "user", "content": human}]).content
    if isinstance(raw, list):  # some providers return content blocks
        raw = "".join(block.get("text", "") for block in raw
                      if isinstance(block, dict))

    descriptions = _parse_json_object(raw)

    # Fall back to the PR title whenever the model skipped one.
    result: dict[str, str] = {}
    for p in prs:
        key = str(p["number"])
        value = descriptions.get(key) or descriptions.get(p["number"])
        result[key] = str(value).strip() if value else p["title"].strip()
    return result


@tool
def write_rows_to_sheet(rows: list[dict], dry_run: bool = False) -> dict:
    """Write one row per day into the configured spreadsheet.

    `rows` are already-rendered day rows from the graph: `cells` maps the
    position of a `columns:` entry in sheet_config.yaml to its value, and the
    writer turns that position into a real column (letter or header lookup).
    Honours sheet_config.yaml (backend, worksheet, write mode, columns).
    """
    cfg = SHEET_CONFIG
    if dry_run:
        return {"written": 0, "dry_run": True, "rows": len(rows)}
    if cfg.backend == "xlsx":
        return _write_xlsx(cfg, rows)
    return _write_google(cfg, rows)


TOOLS = [fetch_pull_requests, describe_pull_requests, write_rows_to_sheet]


# --------------------------------------------------------------------------- #
# 3. LLM factory
# --------------------------------------------------------------------------- #

def build_llm():
    """Create the chat model named by LLM_PROVIDER / LLM_MODEL.

    Each provider is imported inside its own branch, so you only need the
    package for the provider you actually use.
    """
    provider = _env("LLM_PROVIDER", "groq").lower()
    temperature = float(_env("LLM_TEMPERATURE", "0") or 0)
    max_tokens = int(_env("LLM_MAX_TOKENS", "2000"))

    if provider == "groq":
        from langchain_groq import ChatGroq
        if not _env("GROQ_API_KEY"):
            raise RuntimeError("GROQ_API_KEY is missing. See .env.example.")
        kwargs: dict[str, Any] = {}
        if _env_bool("LLM_JSON_MODE", True):
            # Groq's JSON mode: the model can only answer with valid JSON,
            # which is exactly what describe_pull_requests asks for.
            kwargs["model_kwargs"] = {"response_format": {"type": "json_object"}}
        return ChatGroq(
            model=_env("LLM_MODEL", "llama-3.3-70b-versatile"),
            temperature=temperature,
            max_tokens=max_tokens,
            **kwargs,
        )

    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic
        if not _env("ANTHROPIC_API_KEY"):
            raise RuntimeError("ANTHROPIC_API_KEY is missing. See .env.example.")
        return ChatAnthropic(
            model=_env("LLM_MODEL", "claude-sonnet-5-5"),
            temperature=temperature,
            max_tokens=max_tokens,
        )

    if provider == "openai":
        from langchain_openai import ChatOpenAI
        if not _env("OPENAI_API_KEY"):
            raise RuntimeError("OPENAI_API_KEY is missing. See .env.example.")
        kwargs = {}
        if _env_bool("LLM_JSON_MODE", True):
            kwargs["model_kwargs"] = {"response_format": {"type": "json_object"}}
        return ChatOpenAI(model=_env("LLM_MODEL", "gpt-4o-mini"),
                          temperature=temperature, **kwargs)

    if provider == "ollama":          # free / local option
        from langchain_ollama import ChatOllama
        return ChatOllama(model=_env("LLM_MODEL", "llama3.1"),
                          temperature=temperature)

    raise ValueError(
        f"Unknown LLM_PROVIDER '{provider}' (groq|anthropic|openai|ollama)")


def _parse_json_object(raw: str) -> dict:
    """Models like to wrap JSON in prose or ``` fences. Dig it out."""
    raw = (raw or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", raw, re.S)
    if fenced:
        raw = fenced.group(1).strip()
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        pass
    brace = re.search(r"\{.*\}", raw, re.S)
    if brace:
        try:
            parsed = json.loads(brace.group(0))
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            pass
    print("[warn] could not parse the LLM answer as JSON; using PR titles instead")
    return {}


# --------------------------------------------------------------------------- #
# 4. Spreadsheet backends
# --------------------------------------------------------------------------- #

def col_letter_to_index(letter: str) -> int:
    """'A' -> 1, 'AB' -> 28."""
    idx = 0
    for ch in str(letter).strip().upper():
        if not ch.isalpha():
            raise ValueError(f"Bad column letter: {letter}")
        idx = idx * 26 + (ord(ch) - 64)
    return idx


def col_index_to_letter(index: int) -> str:
    letters = ""
    while index > 0:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def resolve_columns(cfg: SheetConfig, headers: list[str]) -> list[tuple[int, dict]]:
    """Turn each config column entry into (1-based column index, entry).

    The result keeps the order of `columns:` in sheet_config.yaml, so position i
    of this list is position i of the rendered row's `cells`.

    An entry may address its column by letter (`column: "C"`) or by the text in
    the header row (`header: "Tasks"`). Header lookup is case-insensitive.
    """
    lowered = [str(h).strip().lower() for h in headers]
    resolved: list[tuple[int, dict]] = []
    for entry in cfg.columns:
        if entry.get("column"):
            idx = col_letter_to_index(entry["column"])
        elif entry.get("header"):
            name = str(entry["header"]).strip().lower()
            if name not in lowered:
                raise ValueError(
                    f"Header '{entry['header']}' not found in row "
                    f"{cfg.header_row}. Found: {headers}"
                )
            idx = lowered.index(name) + 1
        else:
            raise ValueError(f"Column entry needs 'column' or 'header': {entry}")
        resolved.append((idx, entry))
    return resolved


def _match_column_index(cfg: SheetConfig, resolved: list[tuple[int, dict]]) -> int:
    """Which column identifies a day, for upsert mode."""
    if cfg.match_column:
        return col_letter_to_index(cfg.match_column)
    for idx, entry in resolved:
        if entry.get("key") == "date":
            return idx
    return resolved[0][0]


def _plan_targets(cfg: SheetConfig, rows: list[dict],
                  existing: dict[str, int], next_row: int) -> list[tuple[int, dict]]:
    """Decide the target row number for each rendered row."""
    plan: list[tuple[int, dict]] = []
    for row in rows:
        key = str(row.get("_match", "")).strip()
        if cfg.write_mode == "upsert_by_date" and key in existing:
            target = existing[key]
        else:
            target = next_row
            next_row += 1
        plan.append((target, row))
    return plan


def _write_google(cfg: SheetConfig, rows: list[dict]) -> dict:
    import gspread
    from google.oauth2.service_account import Credentials

    sheet_id = _env("GOOGLE_SHEET_ID")
    if not sheet_id:
        raise RuntimeError("GOOGLE_SHEET_ID is missing. See .env.example.")
    creds_path = _env("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")
    if not os.path.exists(creds_path):
        raise RuntimeError(
            f"Service account file '{creds_path}' not found. "
            f"See the Google Cloud Console section of the README."
        )

    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file(creds_path, scopes=scopes)
    client = gspread.authorize(creds)
    book = client.open_by_key(sheet_id)

    try:
        ws = book.worksheet(cfg.worksheet)
    except gspread.WorksheetNotFound:
        if not cfg.create_worksheet_if_missing:
            raise
        ws = book.add_worksheet(title=cfg.worksheet, rows=200, cols=26)

    values = ws.get_all_values()
    headers = values[cfg.header_row - 1] if len(values) >= cfg.header_row else []
    resolved = resolve_columns(cfg, headers)
    match_idx = _match_column_index(cfg, resolved)

    existing: dict[str, int] = {}
    for r in range(cfg.first_data_row, len(values) + 1):
        row = values[r - 1]
        key = row[match_idx - 1].strip() if len(row) >= match_idx else ""
        if key:
            existing[key] = r

    next_row = max(len(values) + 1, cfg.first_data_row)
    updates: list[dict] = []
    for target, row in _plan_targets(cfg, rows, existing, next_row):
        for pos, (idx, _entry) in enumerate(resolved):
            value = row["cells"].get(pos)
            if value is None:
                continue
            updates.append({
                "range": f"{col_index_to_letter(idx)}{target}",
                "values": [[value]],
            })

    if updates:
        ws.batch_update(updates, value_input_option="USER_ENTERED")
    return {"written": len(rows), "backend": "google",
            "spreadsheet": book.title, "worksheet": cfg.worksheet,
            "cells": len(updates)}


def _write_xlsx(cfg: SheetConfig, rows: list[dict]) -> dict:
    from openpyxl import Workbook, load_workbook
    from openpyxl.styles import Alignment

    path = _env("XLSX_PATH", "weekly_report.xlsx")
    if os.path.exists(path):
        book = load_workbook(path)
    else:
        book = Workbook()
        book.active.title = cfg.worksheet

    if cfg.worksheet in book.sheetnames:
        ws = book[cfg.worksheet]
    elif cfg.create_worksheet_if_missing:
        ws = book.create_sheet(cfg.worksheet)
    else:
        raise RuntimeError(f"Worksheet '{cfg.worksheet}' not found in {path}")

    headers = ([c.value or "" for c in ws[cfg.header_row]]
               if ws.max_row >= cfg.header_row else [])
    if not any(str(h).strip() for h in headers):
        # Brand new sheet: lay out the headers declared in the config.
        _write_headers_from_config(cfg, ws)
        headers = [c.value or "" for c in ws[cfg.header_row]]

    resolved = resolve_columns(cfg, [str(h) for h in headers])
    match_idx = _match_column_index(cfg, resolved)
    existing: dict[str, int] = {}
    for r in range(cfg.first_data_row, ws.max_row + 1):
        key = ws.cell(row=r, column=match_idx).value
        if key not in (None, ""):
            existing[str(key).strip()] = r

    next_row = max(ws.max_row + 1, cfg.first_data_row)
    for target, row in _plan_targets(cfg, rows, existing, next_row):
        for pos, (idx, _entry) in enumerate(resolved):
            value = row["cells"].get(pos)
            if value is None:
                continue
            cell = ws.cell(row=target, column=idx, value=value)
            if isinstance(value, str) and "\n" in value:
                cell.alignment = Alignment(wrap_text=True, vertical="top")

    book.save(path)
    return {"written": len(rows), "backend": "xlsx", "path": os.path.abspath(path)}


def _write_headers_from_config(cfg: SheetConfig, ws) -> None:
    """Put the header names from sheet_config.yaml into the header row."""
    for pos, entry in enumerate(cfg.columns):
        name = entry.get("header")
        if not name:
            continue
        idx = (col_letter_to_index(entry["column"]) if entry.get("column")
               else pos + 1)
        ws.cell(row=cfg.header_row, column=idx, value=name)


# --------------------------------------------------------------------------- #
# 5. LangGraph state + nodes
# --------------------------------------------------------------------------- #

class AgentState(TypedDict, total=False):
    author: str
    start: str                  # YYYY-MM-DD
    end: str                    # YYYY-MM-DD
    dry_run: bool
    prs: list[dict]
    descriptions: dict[str, str]
    days: list[dict]            # one entry per calendar day in the window
    rows: list[dict]            # rendered cells, ready for the sheet
    report: dict
    log: Annotated[list[str], lambda a, b: a + b]


def node_resolve_window(state: AgentState) -> AgentState:
    """Work out which days this run covers."""
    today = datetime.now(tz()).date()
    start_day = _env("WEEK_START_DAY", "SAT")

    start = (date.fromisoformat(state["start"]) if state.get("start")
             else week_start(today, start_day))
    end = date.fromisoformat(state["end"]) if state.get("end") else today
    if end < start:
        raise ValueError(f"end date {end} is before start date {start}")

    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "log": [f"window: {start} -> {end} (week starts {start_day.upper()})"],
    }


def node_fetch(state: AgentState) -> AgentState:
    prs = fetch_pull_requests.invoke({
        "author": state["author"],
        "start": state["start"],
        "end": state["end"],
    })
    return {"prs": prs, "log": [f"fetched {len(prs)} pull request(s)"]}


def node_describe(state: AgentState) -> AgentState:
    prs = state.get("prs") or []
    if not prs or not _env_bool("ENABLE_LLM_DESCRIPTIONS", True):
        return {"descriptions": {str(p["number"]): p["title"] for p in prs},
                "log": ["LLM step skipped (no PRs or disabled)"]}
    descriptions = describe_pull_requests.invoke({"prs": prs})
    return {"descriptions": descriptions,
            "log": [f"described {len(descriptions)} pull request(s)"]}


def node_group_by_day(state: AgentState) -> AgentState:
    """One entry per calendar day between start and end - rows are days."""
    zone = tz()
    buckets: dict[str, list[dict]] = {}
    for pr in state.get("prs") or []:
        stamp = pr.get("timestamp")
        if not stamp:
            continue
        moment = parse_gh_datetime(stamp)
        local = moment.astimezone(zone) if zone else moment
        buckets.setdefault(local.date().isoformat(), []).append(pr)

    start = date.fromisoformat(state["start"])
    end = date.fromisoformat(state["end"])
    days = []
    for day in daterange(start, end):
        day_prs = sorted(buckets.get(day.isoformat(), []),
                         key=lambda p: p.get("timestamp") or "")
        days.append({
            "date": day.isoformat(),
            "weekday": WEEKDAY_NAMES[day.weekday()],
            "prs": day_prs,
        })
    filled = sum(1 for d in days if d["prs"])
    return {"days": days,
            "log": [f"{len(days)} day row(s), {filled} with pull requests"]}


def node_render_rows(state: AgentState) -> AgentState:
    """Turn each day into cell values, following sheet_config.yaml.

    A row's `cells` is keyed by the *position* of the entry in the config's
    `columns:` list, not by a column letter. The writer resolves positions into
    real columns, because a `header:` entry only knows its column once the
    sheet's header row has been read.
    """
    cfg = SHEET_CONFIG
    descriptions = state.get("descriptions") or {}
    rows: list[dict] = []

    for day in state.get("days") or []:
        prs = day["prs"]
        if cfg.skip_empty_days and not prs:
            continue

        day_date = date.fromisoformat(day["date"])
        lines = [
            cfg.task_line_template.format(
                number=pr["number"],
                description=descriptions.get(str(pr["number"]), pr["title"]),
                title=pr["title"],
                url=pr["url"],
                repo=pr.get("repo", ""),
                state=pr.get("state", ""),
            )
            for pr in prs
        ]

        context = {
            "date": day_date.strftime(cfg.date_format),
            "iso_date": day["date"],
            "weekday": day["weekday"],
            "weekday_short": day["weekday"][:3],
            "week_start": date.fromisoformat(state["start"]).strftime(cfg.date_format),
            "week_end": date.fromisoformat(state["end"]).strftime(cfg.date_format),
            "author": state["author"],
            "pr_count": len(prs),
            "pr_numbers": ", ".join(f"#{p['number']}" for p in prs),
            "pr_numbers_plain": ", ".join(str(p["number"]) for p in prs),
            "pr_urls": cfg.task_separator.join(p["url"] for p in prs),
            "repos": ", ".join(sorted({p.get("repo", "") for p in prs if p.get("repo")})),
            "tasks": cfg.task_separator.join(lines) if lines else cfg.empty_day_text,
            "titles": cfg.task_separator.join(p["title"] for p in prs),
            "generated_at": datetime.now(tz()).strftime("%Y-%m-%d %H:%M"),
        }

        cells: dict[int, Any] = {}
        for pos, entry in enumerate(cfg.columns):
            if "value" in entry:
                raw = entry["value"]
                value = raw.format(**context) if isinstance(raw, str) else raw
            elif "key" in entry:
                value = context.get(entry["key"], "")
            else:
                continue
            if value == "" and entry.get("skip_if_empty", False):
                continue
            cells[pos] = value

        rows.append({"cells": cells, "_match": context["date"], "_day": day["date"]})

    return {"rows": rows, "log": [f"rendered {len(rows)} row(s)"]}


def column_label(cfg: SheetConfig, pos: int) -> str:
    """Human label for a config column, for --print-rows output."""
    entry = cfg.columns[pos]
    if entry.get("column"):
        return str(entry["column"])
    return f"[{entry.get('header', pos)}]"


def node_write(state: AgentState) -> AgentState:
    rows = state.get("rows") or []
    report = write_rows_to_sheet.invoke({"rows": rows,
                                         "dry_run": bool(state.get("dry_run"))})
    return {"report": report, "log": [f"sheet: {report}"]}


def build_graph():
    graph = StateGraph(AgentState)
    graph.add_node("resolve_window", node_resolve_window)
    graph.add_node("fetch_prs", node_fetch)
    graph.add_node("describe_prs", node_describe)
    graph.add_node("group_by_day", node_group_by_day)
    graph.add_node("render_rows", node_render_rows)
    graph.add_node("write_sheet", node_write)

    graph.add_edge(START, "resolve_window")
    graph.add_edge("resolve_window", "fetch_prs")
    graph.add_edge("fetch_prs", "describe_prs")
    graph.add_edge("describe_prs", "group_by_day")
    graph.add_edge("group_by_day", "render_rows")
    graph.add_edge("render_rows", "write_sheet")
    graph.add_edge("write_sheet", END)
    return graph.compile()


# --------------------------------------------------------------------------- #
# 6. CLI
# --------------------------------------------------------------------------- #

SHEET_CONFIG: SheetConfig


def main(argv: list[str] | None = None) -> int:
    global SHEET_CONFIG

    parser = argparse.ArgumentParser(
        description="Weekly GitHub PR -> spreadsheet agent (LangGraph).")
    parser.add_argument("--author", default=_env("GITHUB_USERNAME"),
                        help="GitHub username whose PRs we collect.")
    parser.add_argument("--start", help="Override the window start (YYYY-MM-DD).")
    parser.add_argument("--end", help="Override the window end (YYYY-MM-DD).")
    parser.add_argument("--config",
                        default=_env("SHEET_CONFIG_PATH", "sheet_config.yaml"),
                        help="Path to the sheet mapping file.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Do everything except writing to the sheet.")
    parser.add_argument("--print-rows", action="store_true",
                        help="Print the rendered rows.")
    parser.add_argument("--no-llm", action="store_true",
                        help="Use PR titles instead of calling the LLM.")
    args = parser.parse_args(argv)

    if not args.author:
        parser.error("No GitHub username. Set GITHUB_USERNAME in .env or pass --author.")
    if args.no_llm:
        os.environ["ENABLE_LLM_DESCRIPTIONS"] = "false"

    SHEET_CONFIG = SheetConfig.load(args.config)

    app = build_graph()
    state: AgentState = {"author": args.author, "dry_run": args.dry_run}
    if args.start:
        state["start"] = args.start
    if args.end:
        state["end"] = args.end

    final = app.invoke(state)

    for line in final.get("log", []):
        print(f"  - {line}")

    if args.print_rows:
        print("\nRendered rows:")
        for row in final.get("rows", []):
            cells = {column_label(SHEET_CONFIG, p): v
                     for p, v in sorted(row["cells"].items())}
            print(f"  {row['_day']}: {json.dumps(cells, ensure_ascii=False)}")

    report = final.get("report", {})
    if report.get("dry_run"):
        print(f"\nDry run - {report.get('rows', 0)} row(s) ready, nothing written.")
    else:
        print(f"\nDone - wrote {report.get('written', 0)} row(s) "
              f"via the '{report.get('backend')}' backend.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:  # friendly errors for students
        print(f"\n[error] {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
