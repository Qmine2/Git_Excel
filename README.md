# Weekly PR → Sheet Agent

A small **LangChain + LangGraph** agent that reads your GitHub pull requests for the
current work week, asks an LLM to describe each one in a sentence, and writes
**one row per day** into a spreadsheet.

```
Saturday 2026-10-17 | Saturday  | 2 | #12: built the login endpoint with JWT
                                     #13: fixed signup form validation
Sunday   2026-10-18 | Sunday    | 0 |
Monday   2026-10-19 | Monday    | 0 |
Tuesday  2026-10-20 | Tuesday   | 1 | #14: added the dashboard revenue chart
```

This is a teaching project. Everything lives in **one script** (`agent.py`) so you
can read it top to bottom, and the spreadsheet layout lives in a **separate YAML
file** (`sheet_config.yaml`) so you can point it at your own sheet without
touching Python.

---

## Table of contents

1. [What the agent does, step by step](#1-what-the-agent-does-step-by-step)
2. [Files in this project](#2-files-in-this-project)
3. [Install](#3-install)
4. [Setup A — GitHub token](#4-setup-a--github-token)
5. [Setup B — the LLM key](#5-setup-b--the-llm-key)
6. [Setup C — the spreadsheet](#6-setup-c--the-spreadsheet)
   - [Option 1: local .xlsx (easiest)](#option-1-local-xlsx-easiest)
   - [Option 2: Google Sheets + Google Cloud Console](#option-2-google-sheets--google-cloud-console)
7. [Adapting the sheet to YOUR format](#7-adapting-the-sheet-to-your-format)
8. [Running it](#8-running-it)
9. [How "this week" is calculated](#9-how-this-week-is-calculated)
10. [Reading the code (LangGraph tour)](#10-reading-the-code-langgraph-tour)
11. [Running it automatically every day](#11-running-it-automatically-every-day)
12. [Do I need SSH?](#12-do-i-need-ssh)
13. [Troubleshooting](#13-troubleshooting)
14. [Exercises](#14-exercises)

---

## 1. What the agent does, step by step

The agent is a **LangGraph state graph** — a pipeline of six nodes, each one a
plain Python function that receives the shared state and returns the part of it
it changed.

```
START
  │
  ├─ resolve_window   work out the week: last Saturday → today
  ├─ fetch_prs        GitHub Search API → list of PRs          [TOOL]
  ├─ describe_prs     LLM writes one description per PR        [TOOL]
  ├─ group_by_day     bucket the PRs into calendar days
  ├─ render_rows      apply sheet_config.yaml → cell values
  └─ write_sheet      Google Sheets or .xlsx                   [TOOL]
  │
END
```

The three `[TOOL]` steps are real LangChain tools (`@tool`), so you can also call
them from a tool-calling agent later (`agent.TOOLS` is the list). The graph calls
them directly, which keeps the run cheap, predictable and easy to debug.

---

## 2. Files in this project

| File | What it is |
|---|---|
| `agent.py` | **The whole agent** — config, 3 tools, LLM layer, LangGraph, CLI. |
| `sheet_config.yaml` | The spreadsheet mapping. **Edit this**, not the Python. |
| `.env.example` | Template for your secrets. Copy it to `.env`. |
| `pyproject.toml` | Dependencies, managed by **uv**. |
| `uv.lock` | Exact pinned versions. Commit it — it is what makes the project reproducible. |
| `.gitignore` | Keeps `.env` and `service_account.json` out of git. |

---

## 3. Install

This project uses **[uv](https://docs.astral.sh/uv/)** — one tool that installs
Python, creates the virtual environment and resolves the dependencies.

Install uv once:

```bash
# Windows PowerShell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Then, in the project folder:

```bash
uv sync
```

That one command reads `pyproject.toml`, creates `.venv/`, installs everything
from `uv.lock`, and downloads a suitable Python if you don't have one. There is
no `pip install` step and **you never have to activate the venv** — prefix
commands with `uv run`:

```bash
uv run agent.py --dry-run --print-rows
```

Finally, create your own env file:

```bash
cp .env.example .env
```

(On PowerShell: `Copy-Item .env.example .env`)

### The uv commands you will actually use

| Command | What it does |
|---|---|
| `uv sync` | Make the venv match `pyproject.toml` + `uv.lock`. Run it after every `git pull`. |
| `uv run agent.py` | Run the agent inside that venv. |
| `uv add <package>` | Add a dependency (updates `pyproject.toml` **and** the lockfile). |
| `uv remove <package>` | Drop one. |
| `uv sync --extra anthropic` | Also install an optional LLM provider (see below). |
| `uv lock --upgrade` | Refresh the pinned versions on purpose. |
| `uv run python` | A REPL with the project importable — handy for poking at `agent.py`. |

> **Why a lockfile?** `pyproject.toml` says *what* you depend on
> (`langgraph>=0.2.50`); `uv.lock` records the exact versions that were resolved,
> for every platform. Everyone who runs `uv sync` gets byte-identical packages,
> so "it works on my machine" stops being a thing. Commit both files.

---

## 4. Setup A — GitHub token

The agent reads your PRs through the GitHub REST API, which needs a
**Personal Access Token (PAT)**. A token is not the same thing as an SSH key —
see [section 12](#12-do-i-need-ssh).

1. Go to <https://github.com/settings/tokens>.
2. **Fine-grained tokens** → *Generate new token*.
   - **Repository access**: *Only select repositories* → pick the repos you
     work on (or *All repositories* if you prefer).
   - **Permissions → Repository permissions**:
     - `Pull requests` → **Read-only**
     - `Metadata` → **Read-only** (it is selected automatically)
   - Expiration: 90 days is a sensible default.
3. Click *Generate token* and **copy it immediately** — GitHub shows it once.
4. Paste it into `.env`:

```ini
GITHUB_TOKEN=github_pat_xxxxxxxxxxxxxxxx
GITHUB_USERNAME=your-github-login
```

> **Classic token alternative:** *Generate new token (classic)* → tick `repo`
> for private repositories, or `public_repo` if everything is public.

### Narrowing the search

By default the agent searches **every repository the token can see**. Usually you
want to narrow it:

```ini
GITHUB_REPO=my-org/backend,my-org/frontend   # comma-separated, "owner/name"
GITHUB_ORG=my-org                            # or the whole org
PR_STATE=                                    # open | closed | merged | empty = all
```

> ⚠️ A **fine-grained** token only ever sees the repositories you selected when
> you created it. If a repo is missing from the results, check the token's
> repository access first.

---

## 5. Setup B — the LLM key

The LLM writes the task descriptions. The default provider is **Groq**, which is
free to start with and fast.

### Groq (default)

1. Go to <https://console.groq.com> and sign in (GitHub or Google account).
2. Left menu → **API Keys** → **Create API key**, name it `pr-agent`.
3. Copy it — the console shows the key once — and put it in `.env`:

```ini
LLM_PROVIDER=groq
GROQ_API_KEY=gsk_...
LLM_MODEL=llama-3.3-70b-versatile
```

Groq has no card requirement to start; the free tier is rate-limited per minute
and per day, which is plenty here — the agent makes **one** LLM call per run.

Production models you can put in `LLM_MODEL`:

| Model | When to use it |
|---|---|
| `llama-3.3-70b-versatile` | the default: good descriptions, generous limits |
| `llama-3.1-8b-instant` | fastest and cheapest, slightly blunter wording |
| `openai/gpt-oss-120b` | the strongest of the four |
| `openai/gpt-oss-20b` | middle ground |

Model IDs do get retired — if you get a `model_decommissioned` error, check the
current list at <https://console.groq.com/docs/models>.

```ini
LLM_JSON_MODE=true
```

This turns on Groq's **JSON mode**, which forces the model to answer with valid
JSON — exactly what `describe_pull_requests` asks for. Set it to `false` if a
model ever rejects the `response_format` parameter; the agent still copes, because
it parses the answer defensively either way.

### The other providers

Each one lives in an *optional* dependency group, so you only install what you
use:

```bash
uv sync --extra anthropic     # or --extra openai, --extra ollama
```

**Anthropic** — key at <https://console.anthropic.com> → *API Keys*:

```ini
LLM_PROVIDER=anthropic
LLM_MODEL=claude-sonnet-5-5
ANTHROPIC_API_KEY=sk-ant-...
```

**OpenAI** — key at <https://platform.openai.com/api-keys>:

```ini
LLM_PROVIDER=openai
LLM_MODEL=gpt-4o-mini
OPENAI_API_KEY=sk-...
```

**Ollama** (free, runs on your own machine, no key at all) — install
<https://ollama.com>, then `ollama pull llama3.1`:

```ini
LLM_PROVIDER=ollama
LLM_MODEL=llama3.1
```

### Shaping the output

You can change how the descriptions read without touching the prompt in the code:

```ini
DESCRIPTION_LANGUAGE=English        # or French, Arabic, ...
DESCRIPTION_MAX_WORDS=18
DESCRIPTION_STYLE=past tense, concrete, what was actually done
```

**No key yet?** Run with `--no-llm` and the agent uses the raw PR titles instead.
Everything else works identically.

---

## 6. Setup C — the spreadsheet

### Option 1: local .xlsx (easiest)

Nothing to configure. In `.env`:

```ini
SHEET_BACKEND=xlsx
XLSX_PATH=weekly_report.xlsx
```

The file is created on the first run, with the headers from `sheet_config.yaml`.
Start here — get the pipeline working, then move to Google Sheets.

### Option 2: Google Sheets + Google Cloud Console

Google Sheets has no "API key" — a program authenticates as a **service account**,
which is a robot user with its own email address. You then **share** your sheet
with that robot, exactly like sharing with a colleague.

#### Step 1 — Create a project

1. Open <https://console.cloud.google.com>.
2. Project dropdown (top bar) → **New Project**.
3. Name it e.g. `pr-sheet-agent` → **Create**, then make sure it is selected.

#### Step 2 — Enable the APIs

1. Left menu → **APIs & Services → Library**.
2. Search **"Google Sheets API"** → open it → **Enable**.
3. Go back to Library, search **"Google Drive API"** → **Enable**.
   (Needed so the service account can open files shared with it.)

#### Step 3 — Create the service account

1. **APIs & Services → Credentials**.
2. **+ Create credentials → Service account**.
3. Name: `sheet-writer`. → **Create and continue**.
4. Role: you can skip it (leave blank) — permission comes from sharing the
   sheet, not from a project role. → **Continue** → **Done**.

#### Step 4 — Download the JSON key

1. In **Credentials**, click the service account you just made.
2. Tab **Keys** → **Add key → Create new key** → **JSON** → **Create**.
3. A `.json` file downloads. Rename it to `service_account.json` and put it
   **next to `agent.py`**.
4. **Never commit this file.** It is already in `.gitignore`.

#### Step 5 — Share the sheet with the robot

1. Open `service_account.json` and copy the `"client_email"` value. It looks
   like `sheet-writer@pr-sheet-agent.iam.gserviceaccount.com`.
2. Open your Google Sheet → **Share** → paste that email → give it **Editor** →
   untick "Notify people" → **Share**.

> This is the step people forget. Without it every call fails with a
> `SpreadsheetNotFound` or `403` error, even though the key is valid.

#### Step 6 — Point the agent at the sheet

The spreadsheet id is the long part of its URL:

```
https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMnOpQrStUvWxYz1234567890/edit
                                        └──────────── this ────────────────┘
```

In `.env`:

```ini
SHEET_BACKEND=google
GOOGLE_SHEET_ID=1AbCdEfGhIjKlMnOpQrStUvWxYz1234567890
GOOGLE_SERVICE_ACCOUNT_FILE=service_account.json
```

And in `sheet_config.yaml`, set `worksheet:` to the **tab name** at the bottom of
the sheet (e.g. `Week`, `Feuille 1`, `October`).

---

## 7. Adapting the sheet to YOUR format

This is the part you are meant to change. Open `sheet_config.yaml`.

### The shape of a column entry

Each entry writes **one cell** of the day's row. Address the column by letter or
by header text, and fill it with a single value (`key:`) or a template (`value:`).

```yaml
columns:
  - column: "A"          # spreadsheet letter
    header: "Date"       # written if the sheet is empty; also documentation
    key: date            # one value, untouched

  - header: "Tasks"      # no letter: find the column whose header says "Tasks"
    key: tasks

  - column: "E"
    value: "{weekday} — {pr_count} PR(s) in {repos}"   # template
```

### Values you can use

| Name | Example |
|---|---|
| `date` | `2026-10-17` (formatted with `date_format`) |
| `iso_date` | `2026-10-17` (always ISO) |
| `weekday` / `weekday_short` | `Saturday` / `Sat` |
| `week_start` / `week_end` | first / last day of the window |
| `author` | `your-github-login` |
| `pr_count` | `2` |
| `pr_numbers` / `pr_numbers_plain` | `#12, #13` / `12, 13` |
| `pr_urls` | one PR link per line |
| `repos` | `org/api, org/front` |
| `tasks` | the multi-line `#12: description` block |
| `titles` | raw PR titles, one per line |
| `generated_at` | `2026-10-21 18:30` |

### The tasks cell

That is the cell you described: for a day with several PRs, each one gets its own
line inside the same cell.

```yaml
formatting:
  task_line_template: "#{number}: {description}"
  task_separator: "\n"
```

Change the template to taste — all the per-PR values are available:

```yaml
  task_line_template: "{number} — {description} ({repo})"
  task_line_template: "• {description} [{url}]"
  task_line_template: "{title} → {description}"
```

Change the separator if you prefer one long line:

```yaml
  task_separator: " | "
```

### Other knobs

```yaml
sheet:
  header_row: 1              # where your titles are
  first_data_row: 2          # where data starts (set to 5 if you have a banner)
  write_mode: upsert_by_date # re-running updates today's row instead of duplicating
  match_column: "A"          # which column holds the date key (default: the `key: date` one)
  skip_empty_days: false     # true = don't write days with no PR

formatting:
  date_format: "%d/%m/%Y"    # French style
  empty_day_text: "Day off"  # what an empty day shows
```

Add `skip_if_empty: true` to a column and the agent leaves that cell alone when
it has nothing to write — useful when a human adds notes in the same column.

### Several sheets, several configs

```bash
uv run agent.py --config sheets/team_a.yaml
uv run agent.py --config sheets/team_b.yaml
```

---

## 8. Running it

```bash
# see what it WOULD write — no API key for the sheet needed, nothing is written
uv run agent.py --dry-run --print-rows
```

```bash
# the real thing
uv run agent.py
```

Useful flags:

| Flag | Effect |
|---|---|
| `--author someone` | collect someone else's PRs |
| `--start 2026-10-17 --end 2026-10-21` | a fixed window instead of "this week" |
| `--dry-run` | run everything, write nothing |
| `--print-rows` | print every rendered row |
| `--no-llm` | skip the LLM, use PR titles |
| `--config path.yaml` | use a different sheet mapping |

Typical first run:

```
  - window: 2026-10-17 -> 2026-10-21 (week starts SAT)
  - fetched 3 pull request(s)
  - described 3 pull request(s)
  - 5 day row(s), 2 with pull requests
  - rendered 5 row(s)
  - sheet: {'written': 5, 'backend': 'xlsx', ...}

Done - wrote 5 row(s) via the 'xlsx' backend.
```

---

## 9. How "this week" is calculated

The week **starts on the most recent `WEEK_START_DAY`** (inclusive) and **ends on
the day you run the agent**. The default is Saturday, because Saturday is a work
day here.

```ini
WEEK_START_DAY=SAT     # MON TUE WED THU FRI SAT SUN
```

Run it on Wednesday 21 October 2026 → the window is **Sat 17 → Wed 21**, and the
agent writes five day-rows. Run it again on Thursday → **Sat 17 → Thu 22**, six
rows; the existing five are updated in place (`write_mode: upsert_by_date`), not
duplicated.

Two more settings decide which day a PR belongs to:

```ini
PR_DATE_FIELD=created     # created | merged | closed | updated
TIMEZONE=Africa/Tunis     # a PR opened at 23:30 UTC may be "tomorrow" locally
```

Always set `TIMEZONE` to your own zone — otherwise late-evening PRs drift to the
wrong row.

---

## 10. Reading the code (LangGraph tour)

Open `agent.py`. It is organised in six numbered sections.

**Section 0 — config.** `.env` is loaded by `python-dotenv`; `SheetConfig.load()`
parses the YAML into a dataclass. Environment variables win over YAML, so you can
override the backend per run without editing the file.

**Section 1 — dates.** `week_start()` is the whole "this week" rule:

```python
delta = (today.weekday() - target) % 7
return today - timedelta(days=delta)
```

**Section 2 — the tools.** Three functions decorated with `@tool`:

- `fetch_pull_requests(author, start, end)` — builds a GitHub search query
  (`type:pr author:you created:2026-10-17..2026-10-21`) and pages through it.
- `describe_pull_requests(prs)` — the LLM layer. One call for all the PRs,
  answering with JSON `{"12": "built the login endpoint"}`. If the model returns
  something unparseable, `_parse_json_object` digs the JSON out of the prose, and
  any PR still missing falls back to its title. **An LLM step should never be able
  to break the pipeline** — that is the lesson in that function.
- `write_rows_to_sheet(rows, dry_run)` — dispatches to the Google or xlsx writer.

Calling a tool uses `.invoke({...})` with a dict of arguments — that is the
LangChain tool interface, the same one a tool-calling model would use.

**Section 3 — the LLM factory.** `build_llm()` returns a chat model based on
`LLM_PROVIDER`. The import happens *inside* each branch, which is why the other
providers can sit in `pyproject.toml` as optional extras: nothing imports
`langchain_anthropic` unless you ask for it. The Groq branch also sets
`response_format={"type": "json_object"}`, so the model is constrained to emit
valid JSON — belt, while `_parse_json_object` is braces.

**Section 4 — the writers.** Column letters ↔ indexes, header lookup, and the
upsert logic (read the existing date column, update that row if the day is
already there).

**Section 5 — the graph.** `AgentState` is a `TypedDict`: each node returns a
partial state and LangGraph merges it in. Note this line:

```python
log: Annotated[list[str], lambda a, b: a + b]
```

That `Annotated` is a **reducer**: without it, each node's `log` would overwrite
the previous one; with it, the lists are concatenated. This is the core idea of
LangGraph state — you declare *how* values merge.

The graph itself is a straight line, and you can print it as a Mermaid diagram:

```bash
uv run python -c "import agent; print(agent.build_graph().get_graph().draw_mermaid())"
```

(`draw_ascii()` also exists but needs `uv add grandalf`.)

**Section 6 — the CLI.** `argparse`, then `app.invoke(state)`.

---

## 11. Running it automatically every day

**Windows — Task Scheduler**

1. Open *Task Scheduler* → *Create Basic Task*.
2. Trigger: Daily, 18:00.
3. Action: *Start a program*
   - Program: `C:\path\to\project\.venv\Scripts\python.exe`
   - Arguments: `agent.py`
   - Start in: `C:\path\to\project`

   (Pointing straight at the venv's `python.exe` avoids depending on where `uv`
   is installed for the scheduler's user. `uv run agent.py` works too, if you
   give the full path to `uv.exe`.)

**macOS / Linux — cron** (`crontab -e`):

```
0 18 * * * cd /path/to/project && .venv/bin/python agent.py >> run.log 2>&1
```

cron has a minimal `PATH`, so either use the venv's Python as above, or spell out
the full path to `uv` (`~/.local/bin/uv run agent.py`).

Because of `write_mode: upsert_by_date`, running it several times a day is safe —
today's row is rewritten, not duplicated.

---

## 12. Do I need SSH?

**No.** This is a common confusion, so here is the distinction:

| | SSH key | Personal Access Token |
|---|---|---|
| Used for | `git clone` / `git push` over `git@github.com:...` | the GitHub **REST API** (HTTPS) |
| Lives in | `~/.ssh/id_ed25519` | your `.env` |
| Needed by this agent | ❌ no | ✅ yes |

The agent never runs `git`. It only makes HTTPS calls to `api.github.com`, which
authenticate with the token.

You only need SSH if **you** want to clone this project over SSH:

```bash
ssh-keygen -t ed25519 -C "you@example.com"
cat ~/.ssh/id_ed25519.pub          # paste into github.com/settings/keys
ssh -T git@github.com              # should greet you by name
```

Same for Google: the service account JSON is not an SSH key and does not go in
`~/.ssh`. It sits next to `agent.py`, ignored by git.

---

## 13. Troubleshooting

**`GITHUB_TOKEN is missing`** — you copied `.env.example` but did not rename it to
`.env`, or you are running from a different folder. `python-dotenv` reads `.env`
from the **current working directory**.

**0 pull requests found, but you know there are some**
- Check the window: `uv run agent.py --dry-run --print-rows` prints it on line 1.
- `PR_DATE_FIELD=created` only counts PRs *opened* this week. If you opened a PR
  last week and merged it Monday, use `PR_DATE_FIELD=merged`.
- A fine-grained token only sees the repos you granted it.
- `GITHUB_USERNAME` must be your **login**, not your display name.

**`GitHub rejected the search query` (422)** — usually a typo in `GITHUB_REPO`
(it must be `owner/name`) or an invalid `GITHUB_SEARCH_EXTRA`.

**`403 rate limit exceeded`** — the Search API allows 30 requests/minute. Wait a
minute; the agent already pages 100 results at a time.

**`SpreadsheetNotFound` / `403 PERMISSION_DENIED`** — you did not share the sheet
with the service account's `client_email`, or you pasted the whole URL into
`GOOGLE_SHEET_ID` instead of just the id.

**`WorksheetNotFound`** — `worksheet:` in `sheet_config.yaml` must match the tab
name exactly, accents and spaces included.

**`Header 'Tasks' not found in row 1`** — you used `header:` in the config but
your sheet's header row says something else. Either fix the header text or
address the column by letter.

**The tasks cell shows everything on one line** — in Google Sheets, select the
column → *Format → Wrapping → Wrap*. The newlines are there; the cell is just not
wrapping them.

**`could not parse the LLM answer as JSON`** — a warning, not a crash: the agent
falls back to PR titles. Small local models do this; keep `LLM_JSON_MODE=true`,
or try a bigger model.

**`ModuleNotFoundError: No module named 'langchain_groq'`** — the venv is out of
date: run `uv sync`. For another provider, `uv sync --extra anthropic` (or
`--extra openai` / `--extra ollama`).

**`GROQ_API_KEY is missing`** — the key is not in `.env`, or you are running
`python agent.py` instead of `uv run agent.py` from the project folder.

**Groq `model_decommissioned`** — Groq retired that model ID. Pick a current one
from <https://console.groq.com/docs/models> and set `LLM_MODEL`.

**Groq `rate_limit_exceeded`** — the free tier has per-minute and per-day caps.
The agent makes one LLM call per run, so this normally means something else on the
key is busy; wait a minute, or use `llama-3.1-8b-instant`, which has the highest
limits.

**Groq `response_format is not supported`** — set `LLM_JSON_MODE=false`.

**`unknown TIMEZONE 'Europe/Paris', falling back to UTC`** — on Windows, Python
has no timezone database of its own. `tzdata` is already a dependency, so
`uv sync` fixes this; the agent only warns and falls back to UTC.

---

## 14. Exercises

Ordered roughly by difficulty.

1. **Add a "Hours" column** that is always empty so a human fills it in — and make
   sure a re-run does not erase it (hint: `skip_if_empty`).
2. **Switch the descriptions to French** without editing `agent.py`.
3. **Add commit counts.** Write a fourth tool that calls
   `GET /repos/{owner}/{repo}/commits?author=...&since=...&until=...`, add a node
   between `fetch_prs` and `group_by_day`, and expose `commit_count` in the
   render context.
4. **Skip weekends** — add a `skip_weekdays: [SUN]` option to the config and
   honour it in `node_group_by_day`.
5. **Add a conditional edge.** If there are zero PRs all week, route straight to a
   `no_op` node instead of calling the LLM (`graph.add_conditional_edges`).
6. **Turn it into a real tool-calling agent.** `agent.TOOLS` already holds the
   three tools — bind them to the model with `llm.bind_tools(TOOLS)` and let the
   model decide the order instead of the fixed graph. Then compare: which version
   is cheaper, and which one do you trust on a Monday morning?
