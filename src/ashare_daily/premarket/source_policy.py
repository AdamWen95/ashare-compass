"""Declared-role coverage; disabled sources and bounded gaps remain visible."""
from .contracts import aware


def coverage_summary(registry: dict, collection: dict, *, required_roles: list[str]) -> dict:
    health = {r["source_id"]: r for r in collection.get("source_health", [])}
    roles = {}
    for role in required_roles:
        sources = [s for s in registry.get("sources", []) if role in s.get("research_roles", [])]
        active = [s for s in sources if s["registration"].get("enabled") is True]
        ready, gaps = [], []
        for source in active:
            key = source["registration"]["source_id"]
            record = health.get(key, {})
            ambiguous = any(r.get("source_id") == key and r.get("reason") in {
                "after_cutoff_or_date_precision_ambiguous", "first_seen_after_cutoff"}
                for r in collection.get("rejected", []))
            if (record.get("status") in {"ok", "empty_bounded_window"} and not record.get("failures")
                    and not record.get("semantic_review_required")
                    and not record.get("index_limit_reached") and not record.get("article_limit_reached")
                    and not collection.get("runtime_limit_reached") and not ambiguous):
                ready.append(key)
            else:
                gaps.append(key)
        status = "not_enabled" if not active else "ready" if ready and not gaps else "partial" if ready else "unavailable"
        roles[role] = {"status": status, "ready_sources": ready, "incomplete_sources": gaps}
    source_ids = {key for r in roles.values() for key in r["ready_sources"]}
    observed = [r.get("requested_at", r["fetched_at"]) for r in collection.get("requests", []) if r.get("source_id") in source_ids
                and r.get("role") in {"index", "article"} and r.get("status") == "ok" and r.get("fetched_at")]
    observed.extend(r["covered_until_at"] for r in health.values()
                    if r["source_id"] in source_ids and r.get("covered_until_at"))
    return {"ready": bool(roles) and all(r["status"] == "ready" for r in roles.values()), "roles": roles,
            "covered_until_at": min(observed, key=aware) if observed else None,
            "request_count": collection.get("request_count", 0), "cutoff_at": collection.get("news_cutoff"),
            "finished_at": collection.get("finished_at"),
            "source_health": list(health.values()),
            "notice": "状态仅覆盖已登记来源及实际采集窗口，不代表全部市场消息"}
