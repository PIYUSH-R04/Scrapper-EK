# ekantipur-scraper

Playwright-based scraper for ekantipur.com.

## Setup

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
# Install uv (skip if already installed)
curl -LsSf https://astral.sh/uv/install.sh | sh        # macOS / Linux
# powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"   # Windows

# Install dependencies
uv sync

# Install the Chromium browser used by Playwright
uv run playwright install chromium
```

## Run

Default run (headless, 5 articles, writes to `output.json`):

```bash
uv run python scraper.py
```

With flags:

```bash
uv run python scraper.py --no-headless --output output-test.json --count 10 --verbose
```

### Flags

| Flag | Default | Description |
|---|---|---|
| `--headless` / `--no-headless` | `--headless` | Run browser headless. Use `--no-headless` to see the browser. |
| `--output` | `output.json` | Path to the output JSON file. |
| `--count` | `5` | Number of entertainment articles to scrape. |
| `--verbose` | off | Enable DEBUG-level logging. |

## Output

A single JSON file with three top-level keys:

- `entertainment_news` — list of articles, each with `title`, `image_url`, `category`, `author`.
- `cartoon_of_the_day` — object with `title`, `image_url`, `author`.
- `_meta` — scrape timestamp, source URL, scraper version, article count.

All strings are written with `ensure_ascii=False` so Devanagari text renders natively.