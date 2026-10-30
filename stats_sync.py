"""
Toplu istatistik senkronizasyonu: istatistiği olmayan tüm bitmemiş maçların son maçlarını, H2H'sini ve puan durumunu çeker.

- Web: start_background() işi ayrı bir thread'de başlatır, get_status() ilerlemeyi (ilerleme çubuğu için) döner.
- Terminal: run() aynı işlemi on_progress geri çağrısıyla senkron çalıştırır.
Aynı anda tek iş çalışır; iş yarıda kalırsa (iptal / sunucu yeniden başlatma) tekrar başlatıldığında
sadece hâlâ istatistiği olmayan maçlar işlenir.
"""
import threading
import time
from typing import Any, Callable, Dict, List, Optional
import database
import match_data
from background_job import BackgroundJob

# Maçlar arasında bekleme (saniye): iddaa.com'a saniyede en fazla ~4 maçlık istek gider
MATCH_DELAY_SECONDS = 0.25

_job = BackgroundJob("stats-sync", outcomes=("updated", "empty"), delay_seconds=MATCH_DELAY_SECONDS)

def pending_matches(db_path: str = database.DB_FILE) -> List[Dict[str, Any]]:
    """İstatistiği olmayan bitmemiş maçlar (en yakın tarihliden başlayarak)."""
    with database.db_session(db_path) as conn:
        return database.get_matches_without_stats(conn, limit=1_000_000)

def _label(match: Dict[str, Any]) -> str:
    return f"{match['home_team']} - {match['away_team']}"

def sync_one(match: Dict[str, Any], db_path: str = database.DB_FILE) -> str:
    """Tek maçın istatistiklerini çeker ve kaydeder. 'updated' (veri kaydedildi) veya 'empty' (kaynak veri döndürmedi) döner."""
    with database.db_session(db_path) as conn:
        ctx = database.get_match_full_context(conn, match["id"])
        if ctx is None:
            return "empty"
        if ctx["h2h"] or ctx["home_last"] or ctx["away_last"]:
            return "updated"  # bu arada detay sayfasından çekilmiş; tekrar istek atma
        return "updated" if match_data.fetch_and_save_stats(conn, ctx) else "empty"

def run(matches: List[Dict[str, Any]], db_path: str = database.DB_FILE,
        on_progress: Optional[Callable[[Dict[str, Any]], None]] = None,
        cancel_event: Optional[threading.Event] = None) -> Dict[str, int]:
    """
    Terminal için senkron çalıştırma. Her maç ayrı kaydedilir; iş yarıda kesilse bile çekilen veriler kalır.
    on_progress her maçtan sonra {"processed", "updated", "empty", "errors", "current"} ile çağrılır.
    """
    counts = {"processed": 0, "updated": 0, "empty": 0, "errors": 0}
    for match in matches:
        if cancel_event is not None and cancel_event.is_set():
            break
        try:
            counts[sync_one(match, db_path)] += 1
        except Exception:
            counts["errors"] += 1
        counts["processed"] += 1
        if on_progress:
            on_progress({**counts, "current": _label(match)})
        time.sleep(MATCH_DELAY_SECONDS)
    return counts

def start_background(db_path: str = database.DB_FILE) -> Dict[str, Any]:
    """İşi arka planda başlatır. Zaten çalışıyorsa yeni iş başlatmaz ve mevcut durumu döner ('started': False)."""
    if _job.is_running():
        return {**_job.status(), "started": False}
    return _job.start(pending_matches(db_path), worker=lambda m: sync_one(m, db_path), label=_label)

def get_status() -> Dict[str, Any]:
    """İlerleme çubuğu için iş durumu: status (idle / running / done / cancelled / error), sayılar, yüzde ve tahmini kalan süre."""
    return _job.status()

def cancel() -> Dict[str, Any]:
    """Çalışan işi mevcut maç bittikten sonra durdurur."""
    return _job.cancel()

def wait(timeout: Optional[float] = None) -> None:
    """Testler için: arka plan işinin bitmesini bekler."""
    _job.wait(timeout)
