# Architectural decisions to revisit

One-line decisions taken under deadline pressure that deserve a second look once
real usage data accumulates. Append-only. Date each entry.

---

## 2026-05-04 — Per-ticker thesis: standalone script, not a brief.py mode

**Decision:** `build_thesis.py` lives as a sibling script alongside `brief.py`, not as a 5th mode (`brief.py thesis`).

**Reasoning at the time:**
- brief.py modes (`morning`/`quick`/`hourly`/`status`) all vary on the same axis: how much data to re-fetch. Adding `thesis` would mix axes — output shape vs freshness scope — and make the mode list incoherent.
- Different consumer. brief.py emits a digest the LLM reads to make decisions. build_thesis.py emits per-ticker prose that publish_site.sh enriches into staging.json. Different layer of the pipeline.
- Duplicate-fetch risk handled by reading brief.py's cached digest (`state/cache/morning_digest_<date>.json`) where available.

**What would flip the decision:**
- If 3+ similar prose-generation needs accumulate (e.g. wheel commentary, recommendation auto-text, headline generation), the shared rule-application + templating layer probably belongs inside brief.py rather than across N siblings.
- If build_thesis.py ends up duplicating > 30% of brief.py's data-fetching code despite the cache fallback.
- If ordering between brief.py and build_thesis.py becomes tricky (e.g. publish_site.sh has to coordinate stale-data races).

**Trigger to revisit:** when adding the second LLM-replacement script (e.g. `build_recommendations.py` or `build_headline.py`). At that point, the right move may be to extract a shared `_analyzer.py` module that both brief.py and the prose-generation siblings consume, OR to consolidate everything into brief.py with mode flags.

**Owner:** trader skill team.

