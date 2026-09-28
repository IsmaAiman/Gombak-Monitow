# Gombak Monitor

Pulls online news about the Gombak parliamentary area (P.098) and its three DUN
(Sungai Tua, Gombak Setia, Hulu Kelang), classifies each story by DUN, PDM and
category with Claude Haiku, stores it, and sends a Telegram digest of what is new.

**Phase 1 covers online news only.** Your own team's activities (Telegram intake)
and social media come later, in a separate repo.

## How it works

1. `config.json` lists the DUN and PDM (with search queries) and the categories.
2. `main.py` runs about 36 Google News RSS searches, keeps stories from the last 7 days,
   and skips anything already in the database.
3. Claude Haiku decides which stories really happened inside the area, and tags
   DUN / PDM / category with a one-line factual summary.
4. New relevant stories go to Telegram, grouped by DUN. Each is marked as sent, so no repeats.
5. `--weekly` sends a summary: items per DUN and category, and PDM with no coverage.

Google News gives headlines and short snippets, not full articles, so classification
is mostly headline-based. Items Claude is unsure about are flagged with a warning sign.

## Deploy (new GitHub repo + Railway)

1. Create a new **private** GitHub repo (e.g. `gombak-monitor`) and push these files.
   Keep it separate from your GPS repo.
2. In Railway: **New Project > Deploy from GitHub repo**, choose the new repo.
3. Add a database: **New > Database > PostgreSQL**. In the app service's Variables,
   add `DATABASE_URL` referencing the Postgres service (`${{Postgres.DATABASE_URL}}`).
   Without this, dedupe state is lost on every deploy.
4. Create a **new Telegram bot** with @BotFather for Gombak (do not reuse the GPS bot),
   add it to your Gombak channel or group as admin, and get the chat id.
5. Add these Variables to the service:
   - `ANTHROPIC_API_KEY`
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_IDS` (comma-separated, e.g. `-1001234567890,123456789`)
6. Set the schedule: service **Settings > Cron Schedule**, for example `0 1 * * *`
   (Railway cron runs in UTC, so this is 09:00 Malaysia time). The script exits when
   finished, which is what cron services need. If Railway does not pick up the
   `Procfile`, set the start command to `python main.py`.
7. Optional weekly summary: duplicate the service, set its start command to
   `python main.py --weekly` and cron to `0 1 * * 1` (Mondays 09:00 MYT).

## Test locally first

```bash
pip install -r requirements.txt
cp .env.example .env        # fill in keys, then export them or use your own loader
python tests/test_offline.py        # no network or keys needed
python main.py --dry-run            # real fetch + classify, prints the digest
```

`--dry-run` still calls Claude (a few cents at most) and stores results locally
in `gombak.db`, but sends nothing to Telegram.

## Tuning

- **Too much noise:** edit the queries in `config.json`. Ambiguous place names use a
  `context` field, e.g. `"Taman Jasa" (Batu Caves OR Selayang OR Gombak)`.
- **PDM that share one search** (Sri Gombak 1-10, the Keramat AU blocks) have
  `"queries": []` on all but one entry. They are still tagged by the classifier.
- **Local council news:** `extra_queries` includes Majlis Perbandaran Selayang and MPAJ.
  Confirm which councils actually cover your DUN and adjust.
- **Cost and volume:** `max_new_per_run`, `batch_size`, `max_age_days` and
  `per_query_limit` are all in `config.json`.

## Notes

- Only links, headlines and short summaries are stored, not article text. Link back
  to the source when you republish anything.
- The digest is descriptive and neutral by design. Review it before sharing publicly.
- "No coverage" in the weekly summary means no news, not no activity.
