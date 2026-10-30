"""
Toplu maç sonuçları işi: tamamlanmış ama skoru girilmemiş futbol ve basketbol maçlarının skorlarını veri kaynağından
(iddaa.com) arka planda çeker. İlerleme get_status() ile ilerleme çubuğunda gösterilir.

Skoru zaten kayıtlı maçlar listeye alınmaz ve iş sürerken elle girilen skorların üzerine yazılmaz ('skipped').
Sadece normal süresinde biten maçlar yazılır (futbolda ilk yarı skoru da bilinmeli); diğerleri 'missing' sayılır.
"""
from typing import Any, Dict, List, Optional, Tuple
import database
import data_source
import rules
from background_job import BackgroundJob

# Sonuçlar tarih + spor bazında tek istekle gelir: {(tarih, basketbol mu): {maç kodu: sonuç}}
ResultsCache = Dict[Tuple[str, bool], Dict[str, Dict[str, Any]]]

# Tarih önbelleği worker'lar arasında paylaşıldığı için tek worker
_job = BackgroundJob("results-sync", outcomes=("updated", "missing", "skipped"))

def pending_matches(db_path: str = database.DB_FILE, days: int = 7) -> List[Dict[str, Any]]:
    """Son 'days' gün içinde tamamlanmış ama skoru girilmemiş futbol ve basketbol maçları."""
    with database.db_session(db_path) as conn:
        database.update_expired_matches(conn)
        return [
            {**m, "sport_type": sport}
            for sport in (rules.FOOTBALL, rules.BASKETBALL)
            for m in database.get_matches_awaiting_results(conn, sport_type=sport, days=days)
        ]

def _label(match: Dict[str, Any]) -> str:
    return f"{match['home_team']} - {match['away_team']}"

def _fetch_result(match: Dict[str, Any], cache: ResultsCache) -> Optional[Dict[str, Any]]:
    key = (match["result_date"], rules.is_basketball(match["sport_type"]))
    if key not in cache:
        cache[key] = {str(r["code"]): r for r in data_source.fetch_match_results(key[0], is_basketball=key[1]) or []}
    return cache[key].get(str(match["code"]))

def sync_one(match: Dict[str, Any], cache: ResultsCache, db_path: str = database.DB_FILE) -> str:
    """Tek maçın sonucunu kaydeder: 'updated', 'missing' (skor henüz yok / eksik) veya 'skipped' (bu arada skor girilmiş)."""
    res = _fetch_result(match, cache)
    if not res or res["ft_home"] is None or res["ft_away"] is None:
        return "missing"
    # Futbolda İY tahmini değerlendirildiği için ilk yarı skoru zorunlu; basketbolda yoksa boş bırakılır
    if not rules.is_basketball(match["sport_type"]) and (res["ht_home"] is None or res["ht_away"] is None):
        return "missing"

    with database.db_session(db_path) as conn:
        if database.get_match_result(conn, match["id"]):
            return "skipped"
        database.save_result(conn, match["id"], res["ht_home"], res["ht_away"], res["ft_home"], res["ft_away"])
    return "updated"

def start_background(days: int = 7, db_path: str = database.DB_FILE) -> Dict[str, Any]:
    """Sonuç işini başlatır. Zaten çalışıyorsa mevcut durumu döner ('started': False)."""
    if _job.is_running():
        return {**_job.status(), "started": False}
    cache: ResultsCache = {}
    return _job.start(pending_matches(db_path, days), worker=lambda m: sync_one(m, cache, db_path), label=_label)

def get_status() -> Dict[str, Any]:
    return _job.status()

def cancel() -> Dict[str, Any]:
    return _job.cancel()

def wait(timeout: Optional[float] = None) -> None:
    _job.wait(timeout)
