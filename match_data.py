"""
Maç istatistiklerinin (son maçlar, H2H, puan durumu) tamamlanması ve Gemini analizine uygunluk kontrolü.

Kural: Ev sahibinin ve deplasmanın son maçları ile aralarındaki maçlar (H2H) yoksa maç Gemini'ye GÖNDERİLMEZ.
web_server.py ve app.py analizden önce load_context_for_analysis() ile bu kuralı uygular.
"""
import sqlite3
from typing import Any, Dict, List, Optional, Tuple
import data_source
import database

# Gemini'ye gönderilmesi için dolu olması gereken istatistikler: (match_context anahtarı, kullanıcıya gösterilen ad)
REQUIRED_SECTIONS = (
    ("home_last", "Ev sahibinin son maçları"),
    ("away_last", "Deplasmanın son maçları"),
    ("h2h", "Aralarındaki maçlar (H2H)"),
)

def missing_sections(ctx: Dict[str, Any]) -> List[str]:
    """Analiz için eksik olan istatistiklerin adlarını döner; boş liste = Gemini'ye gönderilebilir."""
    return [label for key, label in REQUIRED_SECTIONS if not ctx.get(key)]

def merge_stats(ctx: Dict[str, Any], fetched: Dict[str, Any]) -> Dict[str, Any]:
    """Yeni çekilen istatistiklerden sadece dolu olanları kullanır; boş alanlarda mevcut kayıtlı veriyi korur."""
    existing = {
        "h2h_data": ctx["h2h"],
        "home_last": ctx["home_last"],
        "away_last": ctx["away_last"],
        "missing": ctx["missing"],
        "league_table": ctx["league_table"]
    }
    return {key: fetched.get(key) or existing[key] for key in existing}

def fetch_and_save_stats(conn: sqlite3.Connection, ctx: Dict[str, Any]) -> bool:
    """Maçın istatistiklerini veri kaynağından çekip kaydeder. Kaynak hiç veri döndürmezse False döner (hiçbir şey yazılmaz)."""
    match = ctx["match"]
    fetched = data_source.fetch_match_stats(match["code"], match["sport_type"])
    if not any(fetched.values()):
        return False
    database.save_match_stats(conn, match["id"], **merge_stats(ctx, fetched))
    return True

def load_context_for_analysis(conn: sqlite3.Connection, match_id: int) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """
    Gemini analizi için maç bağlamını yükler. Son maçlar / H2H eksikse veri kaynağından bir kez çekmeyi dener.
    Dönen ikinci değer hâlâ eksik olan istatistiklerdir; boş değilse maç Gemini'ye gönderilmemelidir.
    """
    ctx = database.get_match_full_context(conn, match_id)
    if not ctx:
        return None, []
    missing = missing_sections(ctx)
    if missing and fetch_and_save_stats(conn, ctx):
        conn.commit()
        ctx = database.get_match_full_context(conn, match_id)
        missing = missing_sections(ctx)
    return ctx, missing

def missing_stats_message(missing: List[str]) -> str:
    return "Son maçlar / H2H verisi eksik olduğu için maç Gemini'ye gönderilmedi. Eksik: " + ", ".join(missing)
