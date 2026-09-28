"""Offline checks that need no network or API keys. Run: python tests/test_offline.py"""
import os, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.environ["SQLITE_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ.pop("DATABASE_URL", None)

import main

cfg = main.load_config()

# queries
qs = main.build_queries(cfg)
assert len(qs) == len(set(q.lower() for q in qs)), "duplicate queries"
print(f"OK build_queries: {len(qs)} queries")

# JSON parsing
assert main.parse_json_array('```json\n[{"id":0}]\n```') == [{"id": 0}]
assert main.parse_json_array("no json here") is None
assert main.parse_json_array('[{"id":0},') is None
print("OK parse_json_array")

# normalise (dash/case tolerant, bad values fall back safely)
r = main.normalise_result({"relevant": True, "dun": "hulu kelang", "pdm": "taman melawati", "category": "??", "confidence": "x", "summary": " a  b "}, cfg)
assert r["dun"] == "Hulu Kelang" and r["pdm"] == "Taman Melawati" and r["category"] == "lain" and r["summary"] == "a b"
r = main.normalise_result({"relevant": True, "dun": "Sungai Tua", "pdm": "Sri Gombak 2-7"}, cfg)
assert r["pdm"] is None  # PDM belongs to a different DUN
r = main.normalise_result({"relevant": True, "dun": "Gombak Setia", "pdm": "Sri Gombak 2-7"}, cfg)
assert r["pdm"] == "Sri Gombak 2\u20137"
print("OK normalise_result")

# store + dedupe + digest
s = main.Store()
art = {"title": "Banjir kilat di Taman Melawati", "link": "https://x/1", "source": "Bernama", "published": "2026-09-28T01:00:00", "snippet": "", "title_key": "abc"}
res = {"relevant": True, "dun": "Hulu Kelang", "pdm": "Taman Melawati", "category": "insiden", "summary": "Banjir kilat <b>&</b> jalan ditutup.", "confidence": "rendah"}
s.add(art, res); s.commit()
assert s.known("https://x/1", "zzz") and s.known("other", "abc") and not s.known("other", "zzz")
rows = s.unsent()
assert len(rows) == 1
msgs = main.build_digest(rows, cfg)
assert "&lt;b&gt;" in msgs[0] and "Hulu Kelang" in msgs[0]
s.mark_sent([rows[0]["id"]])
assert s.unsent() == []
print("OK store / dedupe / digest")

# long digest is split under Telegram's limit
many = [dict(rows[0], id=i, summary="x" * 200, link=f"https://x/{i}") for i in range(80)]
parts = main.build_digest(many, cfg)
assert len(parts) > 1 and all(len(p) <= 4096 for p in parts)
print(f"OK chunking: {len(parts)} messages")

print(main.weekly_summary(cfg, s)[0][:300])
print("\nAll offline checks passed.")
