"""
Gombak Monitor
--------------
Pulls online news about the Gombak parliamentary area (P.098) and its three
DUN, classifies each story by DUN / PDM / category with Claude Haiku, stores
it in a database (so nothing is sent twice), and sends a digest to Telegram.

Usage:
    python main.py              # pull, classify, send digest of new items
    python main.py --dry-run    # same, but print the digest instead of sending
    python main.py --weekly     # send a weekly coverage summary

Environment variables:
    ANTHROPIC_API_KEY     required
    TELEGRAM_BOT_TOKEN    required to send
    TELEGRAM_CHAT_IDS     comma-separated chat / channel ids
    DATABASE_URL          optional; Postgres URL (Railway). If unset, a local
                          SQLite file is used (fine for testing only, because
                          Railway's disk is wiped on every deploy)
    CLAUDE_MODEL          optional; defaults to Claude Haiku 4.5
"""

import argparse
import hashlib
import html
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
MYT = timezone(timedelta(hours=8))
MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")
USER_AGENT = "Mozilla/5.0 (compatible; GombakMonitor/1.0)"
GENERAL_LABEL = "Parlimen Gombak (umum)"


# --------------------------------------------------------------------------
# Config helpers
# --------------------------------------------------------------------------
def load_config():
    with open(os.path.join(HERE, "config.json"), "r", encoding="utf-8") as f:
        return json.load(f)


def norm_key(text):
    """Loose key for matching names (ignores case, spaces, dashes, symbols)."""
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def build_queries(cfg):
    """Unique list of Google News search queries built from the config."""
    seen, out = set(), []

    def add(q):
        k = q.lower().strip()
        if k and k not in seen:
            seen.add(k)
            out.append(q.strip())

    for q in cfg.get("extra_queries", []):
        add(q)
    for dun in cfg["duns"]:
        for q in dun.get("queries", []):
            add(q)
        for pdm in dun["pdm"]:
            if "queries" in pdm:
                for q in pdm["queries"]:
                    add(q)
            elif pdm.get("context"):
                add(f'"{pdm["name"]}" ({pdm["context"]})')
            else:
                add(f'"{pdm["name"]}"')
    return out


# --------------------------------------------------------------------------
# Storage (Postgres on Railway, SQLite locally)
# --------------------------------------------------------------------------
class Store:
    def __init__(self):
        self.url = os.environ.get("DATABASE_URL")
        if self.url:
            import psycopg2  # imported lazily so local runs don't need it

            self.pg = True
            self.conn = psycopg2.connect(self.url)
        else:
            self.pg = False
            path = os.environ.get("SQLITE_PATH", os.path.join(HERE, "gombak.db"))
            self.conn = sqlite3.connect(path)
        self._init_schema()

    def _q(self, sql):
        return sql.replace("?", "%s") if self.pg else sql

    def execute(self, sql, params=()):
        cur = self.conn.cursor()
        cur.execute(self._q(sql), params)
        return cur

    def _init_schema(self):
        pk = "SERIAL PRIMARY KEY" if self.pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
        self.execute(
            f"""CREATE TABLE IF NOT EXISTS items (
                id {pk},
                link TEXT UNIQUE,
                title_key TEXT,
                title TEXT,
                source TEXT,
                published TEXT,
                fetched_at TEXT,
                relevant INTEGER,
                dun TEXT,
                pdm TEXT,
                category TEXT,
                summary TEXT,
                confidence TEXT,
                sent INTEGER DEFAULT 0
            )"""
        )
        self.execute("CREATE INDEX IF NOT EXISTS idx_items_title_key ON items (title_key)")
        self.conn.commit()

    def known(self, link, title_key):
        cur = self.execute(
            "SELECT 1 FROM items WHERE link = ? OR title_key = ? LIMIT 1",
            (link, title_key),
        )
        return cur.fetchone() is not None

    def add(self, article, result):
        relevant = 1 if result["relevant"] else 0
        self.execute(
            """INSERT INTO items
               (link, title_key, title, source, published, fetched_at, relevant,
                dun, pdm, category, summary, confidence, sent)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT (link) DO NOTHING""",
            (
                article["link"],
                article["title_key"],
                article["title"],
                article["source"],
                article["published"],
                utcnow_iso(),
                relevant,
                result["dun"],
                result["pdm"],
                result["category"],
                result["summary"],
                result["confidence"],
                0 if relevant else 1,  # irrelevant items never need sending
            ),
        )

    def commit(self):
        self.conn.commit()

    def unsent(self):
        cur = self.execute(
            """SELECT id, title, source, link, published, dun, pdm, category, summary, confidence
               FROM items WHERE relevant = 1 AND sent = 0
               ORDER BY published DESC"""
        )
        cols = ["id", "title", "source", "link", "published", "dun", "pdm", "category", "summary", "confidence"]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def mark_sent(self, ids):
        if not ids:
            return
        marks = ",".join("?" for _ in ids)
        self.execute(f"UPDATE items SET sent = 1 WHERE id IN ({marks})", tuple(ids))
        self.commit()

    def since(self, iso):
        cur = self.execute(
            """SELECT dun, pdm, category FROM items
               WHERE relevant = 1 AND fetched_at >= ?""",
            (iso,),
        )
        return cur.fetchall()


def utcnow_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


# --------------------------------------------------------------------------
# Step 1: fetch
# --------------------------------------------------------------------------
def clean_snippet(raw, title):
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = re.sub(r"\s+", " ", html.unescape(text)).strip()
    if not text or text.lower().startswith(title.lower()[:40]):
        return ""
    return text[:300]


def entry_time(entry):
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    if t:
        return datetime(*t[:6], tzinfo=timezone.utc)
    return None


def fetch_news(cfg):
    import feedparser  # imported here so offline tests don't need it

    gn = cfg["google_news"]
    max_age = timedelta(days=cfg["max_age_days"])
    cutoff = datetime.now(timezone.utc) - max_age
    articles = {}

    queries = build_queries(cfg)
    print(f"Mengambil berita: {len(queries)} carian")
    for q in queries:
        url = (
            "https://news.google.com/rss/search?q="
            + quote_plus(f"{q} when:{cfg['max_age_days']}d")
            + f"&hl={gn['hl']}&gl={gn['gl']}&ceid={gn['ceid']}"
        )
        try:
            resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
            resp.raise_for_status()
            feed = feedparser.parse(resp.content)
        except Exception as e:
            print(f"  ! gagal: {q} ({e})")
            continue

        for entry in feed.entries[: cfg["per_query_limit"]]:
            title = (entry.get("title") or "").strip()
            link = entry.get("link")
            if not title or not link:
                continue
            source = (entry.get("source") or {}).get("title", "") if isinstance(entry.get("source"), dict) else ""
            if source and title.endswith(f" - {source}"):
                title = title[: -len(f" - {source}")].strip()
            dt = entry_time(entry)
            if dt and dt < cutoff:
                continue
            key = hashlib.sha1(norm_key(title).encode("utf-8")).hexdigest()[:16]
            if link in articles or any(a["title_key"] == key for a in articles.values()):
                continue
            articles[link] = {
                "title": title,
                "link": link,
                "source": source or "Sumber",
                "published": dt.strftime("%Y-%m-%dT%H:%M:%S") if dt else "",
                "snippet": clean_snippet(entry.get("summary", ""), title),
                "title_key": key,
            }
        time.sleep(cfg.get("request_delay_seconds", 1.0))

    print(f"Jumpa {len(articles)} artikel unik")
    return list(articles.values())


# --------------------------------------------------------------------------
# Step 2: classify with Claude Haiku
# --------------------------------------------------------------------------
def areas_text(cfg):
    lines = []
    for d in cfg["duns"]:
        names = ", ".join(p["name"] for p in d["pdm"])
        extra = f" (juga dikenali: {', '.join(d['aliases'])})" if d.get("aliases") else ""
        lines.append(f"- DUN {d['name']}{extra}: {names}")
    return "\n".join(lines)


def categories_text(cfg):
    return "\n".join(f"- {c['key']}: {c['desc']}" for c in cfg["categories"])


def build_prompt(cfg, batch):
    items = []
    for i, a in enumerate(batch):
        line = f"[{i}] {a['title']} | {a['source']} | {a['published'][:10] or 'tarikh tidak diketahui'}"
        if a["snippet"]:
            line += f"\n{a['snippet']}"
        items.append(line)
    return f"""Anda menapis berita untuk pemantauan kawasan Parlimen Gombak (P.098), Selangor.

Kawasan (DUN dan Daerah Mengundi di dalamnya):
{areas_text(cfg)}

Kategori:
{categories_text(cfg)}

Tugas: untuk setiap artikel, tentukan sama ada peristiwa, isu atau acara itu berlaku di lokasi dalam kawasan di atas.
- Relevan: berita tentang acara, isu, projek atau insiden di lokasi dalam kawasan (termasuk acara besar seperti perayaan di Batu Caves).
- Tidak relevan: berita nasional atau tempat lain yang hanya menyebut nama tempat tanpa kaitan lokasi kejadian, atau nama yang sama tetapi di kawasan lain.
- Jika relevan tetapi lokasi tepat tidak jelas, letakkan "dun" dan "pdm" sebagai null.
- Ringkasan berdasarkan tajuk dan petikan yang diberi sahaja. Jangan reka butiran. Nada neutral, fakta sahaja, tanpa pendapat.

Artikel:
{chr(10).join(items)}

Kembalikan HANYA array JSON, satu objek bagi setiap artikel, dalam format:
[{{"id": 0, "relevant": true, "dun": "nama DUN atau null", "pdm": "nama Daerah Mengundi atau null", "category": "kunci kategori", "summary": "satu ayat BM, maksimum 25 perkataan", "confidence": "tinggi|sederhana|rendah"}}]"""


def parse_json_array(text):
    """Extract a JSON array from a model reply; returns None on failure."""
    if not text:
        return None
    text = re.sub(r"```(?:json)?", "", text)
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list) or not all(isinstance(x, dict) for x in data):
        return None
    return data


def normalise_result(res, cfg):
    dun_by_key = {norm_key(d["name"]): d for d in cfg["duns"]}
    cat_keys = {c["key"] for c in cfg["categories"]}

    relevant = res.get("relevant") is True
    dun = pdm = None
    dun_obj = dun_by_key.get(norm_key(res.get("dun")))
    if dun_obj:
        dun = dun_obj["name"]
        pdm_by_key = {norm_key(p["name"]): p["name"] for p in dun_obj["pdm"]}
        pdm = pdm_by_key.get(norm_key(res.get("pdm")))
    category = res.get("category") if res.get("category") in cat_keys else "lain"
    confidence = res.get("confidence") if res.get("confidence") in ("tinggi", "sederhana", "rendah") else "sederhana"
    summary = re.sub(r"\s+", " ", str(res.get("summary") or "")).strip()[:300]
    return {
        "relevant": relevant,
        "dun": dun,
        "pdm": pdm,
        "category": category,
        "summary": summary,
        "confidence": confidence,
    }


def classify_batch(client, cfg, batch):
    """Returns {index: normalised result} or None if the model reply was unusable."""
    prompt = build_prompt(cfg, batch)
    for attempt in range(2):
        try:
            resp = client.messages.create(
                model=MODEL,
                max_tokens=2500,
                messages=[{"role": "user", "content": prompt}],
            )
            data = parse_json_array(resp.content[0].text)
        except Exception as e:
            print(f"  ! ralat Claude (cubaan {attempt + 1}): {e}")
            data = None
        if data is not None:
            results = {}
            for r in data:
                idx = r.get("id")
                if isinstance(idx, int) and 0 <= idx < len(batch):
                    results[idx] = normalise_result(r, cfg)
            return results
    return None


def classify_and_store(cfg, store, articles):
    import anthropic

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY tidak dijumpai")
        sys.exit(1)
    client = anthropic.Anthropic()

    size = cfg["batch_size"]
    stored = relevant = 0
    for i in range(0, len(articles), size):
        batch = articles[i : i + size]
        print(f"Menapis artikel {i + 1}-{i + len(batch)} / {len(articles)}")
        results = classify_batch(client, cfg, batch)
        if results is None:
            print("  ! kumpulan ini dilangkau; akan dicuba semula pada larian seterusnya")
            continue
        for idx, art in enumerate(batch):
            res = results.get(idx)
            if res is None:
                continue  # model skipped it; retry next run
            store.add(art, res)
            stored += 1
            relevant += 1 if res["relevant"] else 0
        store.commit()
    print(f"Disimpan {stored} artikel, {relevant} berkaitan")


# --------------------------------------------------------------------------
# Step 3: digest + Telegram
# --------------------------------------------------------------------------
def esc(text):
    return html.escape(text or "", quote=False)


def build_digest(rows, cfg):
    """Returns a list of Telegram-sized HTML messages."""
    labels = {c["key"]: c["label"] for c in cfg["categories"]}
    dun_order = [d["name"] for d in cfg["duns"]] + [None]

    groups = {}
    for r in rows:
        groups.setdefault(r["dun"], []).append(r)

    now = datetime.now(MYT).strftime("%d/%m/%Y %H:%M")
    header = f"\U0001F4CD <b>Pantau Gombak</b> \u2014 {now}\n{len(rows)} item baharu"
    limit = 3800
    messages, current = [], header

    def push(line, section):
        nonlocal current
        if len(current) + len(line) + 1 > limit:
            messages.append(current)
            current = f"<i>(sambungan)</i>\n{section}" if section and line != section else ""
        current = (current + "\n" + line) if current else line

    for dun in dun_order:
        items = groups.get(dun)
        if not items:
            continue
        section = f"\n<b>\u2501\u2501 {esc(dun or GENERAL_LABEL)} ({len(items)}) \u2501\u2501</b>"
        push(section, section)
        for r in items:
            where = f" \u00b7 {esc(r['pdm'])}" if r["pdm"] else ""
            flag = " \u26A0\uFE0F" if r["confidence"] == "rendah" else ""
            summary = esc(r["summary"] or r["title"])
            link = html.escape(r["link"], quote=True)
            line = (
                f"\u2022 <b>{esc(labels.get(r['category'], 'Lain-lain'))}</b>{where}{flag}\n"
                f"  {summary} (<a href=\"{link}\">{esc(r['source'])}</a>)"
            )
            push(line, section)
    if current:
        messages.append(current)
    return messages


def send_telegram(messages):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_ids = [c.strip() for c in os.environ.get("TELEGRAM_CHAT_IDS", "").split(",") if c.strip()]
    if not token or not chat_ids:
        print("TELEGRAM_BOT_TOKEN atau TELEGRAM_CHAT_IDS tidak dijumpai")
        return False

    delivered = False
    for chat_id in chat_ids:
        for msg in messages:
            resp = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": msg,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=30,
            )
            if resp.status_code == 200:
                delivered = True
            else:
                print(f"  ! Telegram gagal untuk {chat_id}: {resp.text[:200]}")
            time.sleep(0.5)
    return delivered


def weekly_summary(cfg, store):
    since = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S")
    rows = store.since(since)
    labels = {c["key"]: c["label"] for c in cfg["categories"]}

    lines = [f"\U0001F4CA <b>Ringkasan mingguan Gombak</b> \u2014 {datetime.now(MYT):%d/%m/%Y}", f"{len(rows)} item berkaitan dalam 7 hari"]
    for dun in cfg["duns"]:
        dun_rows = [r for r in rows if r[0] == dun["name"]]
        lines.append(f"\n<b>{esc(dun['name'])}</b>: {len(dun_rows)} item")
        by_cat = {}
        for _, _, cat in dun_rows:
            by_cat[cat] = by_cat.get(cat, 0) + 1
        if by_cat:
            lines.append("  " + ", ".join(f"{esc(labels.get(k, k))} {v}" for k, v in sorted(by_cat.items(), key=lambda x: -x[1])))
        seen = {r[1] for r in dun_rows if r[1]}
        quiet = [p["name"] for p in dun["pdm"] if p["name"] not in seen]
        if quiet:
            lines.append(f"  <i>Tiada liputan berita:</i> {esc(', '.join(quiet))}")
    general = [r for r in rows if r[0] is None]
    if general:
        lines.append(f"\n<b>{GENERAL_LABEL}</b>: {len(general)} item")
    lines.append("\n<i>Nota: tiada liputan berita tidak bermakna tiada aktiviti. Ini hanya apa yang dilaporkan dalam talian.</i>")
    return ["\n".join(lines)]


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Gombak Monitor")
    parser.add_argument("--dry-run", action="store_true", help="print the digest instead of sending it")
    parser.add_argument("--weekly", action="store_true", help="send the weekly coverage summary")
    args = parser.parse_args()

    cfg = load_config()
    store = Store()
    print(f"Gombak Monitor \u2014 {datetime.now(MYT):%d/%m/%Y %H:%M} MYT")

    if args.weekly:
        messages = weekly_summary(cfg, store)
        if args.dry_run:
            print("\n\n".join(messages))
        else:
            send_telegram(messages)
        return

    articles = fetch_news(cfg)
    new = [a for a in articles if not store.known(a["link"], a["title_key"])]
    new = new[: cfg["max_new_per_run"]]
    print(f"{len(new)} artikel baharu untuk ditapis")
    if new:
        classify_and_store(cfg, store, new)

    rows = store.unsent()
    if not rows and not cfg.get("send_empty_digest"):
        print("Tiada item berkaitan yang baharu.")
        return

    messages = build_digest(rows, cfg)
    if args.dry_run:
        print("\n\n".join(messages))
        return
    if send_telegram(messages):
        store.mark_sent([r["id"] for r in rows])
        print(f"Digest dihantar ({len(rows)} item).")
    else:
        print("Digest tidak dihantar; item kekal untuk larian seterusnya.")


if __name__ == "__main__":
    main()
