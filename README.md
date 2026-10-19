# Receiver deal monitor

Checks Canadian retailers every 20 minutes for open-box, refurbished, clearance and sale surround
receivers on a fixed model watchlist. It pushes alerts to an iPhone through [ntfy](https://ntfy.sh)
(or Pushover). It runs free on GitHub Actions.

- **Alerts on:** new listings, price drops, and listings back in stock after 24 h or more. It never repeats an alert.
- **Price rules:** it alerts up to $380 CAD and flags anything over $300 as **OVER BUDGET**.
- **Each alert shows:** model, channels/Atmos, condition, price (plus sale or clearance note), seller (Marketplace sellers by name), tier and caveat, budget flag, and a tap-to-open link.
- **Broken sources:** if a source fails 3 runs in a row, you get a "🛠️ … check is broken" alert, then a "✅ working again" alert when it recovers. Failing sources back off for up to about 2.5 h.
- **Heartbeat:** a weekly low-priority "💤 still alive" check-in, so you know the schedule hasn't silently stopped.

## Sources

| Source | How it's read | Requests per run |
|---|---|---|
| Best Buy Canada (incl. Marketplace, open box, refurbished, clearance, sale prices) | Best Buy's own JSON search API, receiver category plus two cross-category queries, then the JSON availability API so sold-out listings are skipped | ~4 |
| MTC Factory Outlet | WooCommerce Store API (whole catalog, ~224 products) | ~3 |
| Skadi | Shopify `products.json` (whole catalog, ~1,200 products) | ~5 |
| Amazon.ca | **Not monitored** — see below | 0 |

Requests to each site are spaced 2–3.5 s apart with a normal browser User-Agent. Errors 429 and 5xx are retried with backoff.

## Changing what it watches

Edit [config.py](config.py): the budget, the cap, the model list, the exclude words and the timings.

## Commands

```bash
python3 monitor.py --test-notify      # send a test push
python3 monitor.py --dry-run          # check all sources now, print matches, send/save nothing
python3 monitor.py                    # a real check (alerts + updates state.json)
python3 -m unittest discover -s tests # unit tests
```

Locally, put `NTFY_TOPIC=...` in `.env` and load it with `set -a; . ./.env; set +a`.

From anywhere, on GitHub:

```bash
gh workflow run monitor.yml -f mode=test-notify
gh workflow run monitor.yml -f mode=dry-run
gh run watch
```

## Schedule (cron-job.org)

GitHub's own scheduler only ran this about every 4 hours, so a free [cron-job.org](https://cron-job.org)
job starts the workflow every 20 minutes through GitHub's API. The hourly GitHub schedule in the
workflow is only a fallback.

The cron-job.org job sends this request:

| Field | Value |
|---|---|
| URL | `https://api.github.com/repos/caleblanc/receiver-monitor/actions/workflows/monitor.yml/dispatches` |
| Method | `POST` |
| Header | `Authorization: Bearer <fine-grained token>` |
| Header | `Accept: application/vnd.github+json` |
| Header | `X-GitHub-Api-Version: 2022-11-28` |
| Header | `Content-Type: application/json` |
| Body | `{"ref":"main","inputs":{"mode":"check"}}` |

A successful call returns **204 No Content**. The token is a fine-grained personal access token
limited to this repository with **Actions: Read and write** and nothing else. When it expires,
cron-job.org starts failing with 401: make a new token and paste it into the job's header.

## Files

- `monitor.py`: everything (standard library only, nothing to install)
- `config.py`: watchlist and thresholds
- `state.json`: what has already been alerted (committed by the workflow)
- `.github/workflows/monitor.yml`: the schedule
