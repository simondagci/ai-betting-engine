"""
Toplu Gemini analizi: analize uygun tüm maçları (bitmemiş, analiz edilmemiş, son maçlar + H2H dolu, lig izni açık)
arka planda tek tek Gemini'ye gönderir ve tahminleri kaydeder. İlerleme get_status() ile ilerleme çubuğunda gösterilir.

Güvenlik: Gemini ayarı iş sırasında kapatılırsa iş durur; art arda MAX_CONSECUTIVE_ERRORS hata olursa
(örn. API kotası doldu) iş 'error' durumuyla durur, böylece boşuna istek atılmaz.
"""
from typing import Any, Dict, List, Optional
import config
import database
import gemini_engine
import match_data
from background_job import BackgroundJob, StopJob

MAX_CONSECUTIVE_ERRORS = 5

# GEMINI_PARALLEL_REQUESTS maç aynı anda Gemini'ye gönderilir (varsayılan 3)
_job = BackgroundJob("gemini-batch", outcomes=("analyzed", "skipped"), delay_seconds=config.BATCH_REQUEST_DELAY,
                     max_consecutive_errors=MAX_CONSECUTIVE_ERRORS, workers=config.GEMINI_PARALLEL_REQUESTS)

def eligible_matches(db_path: str = database.DB_FILE) -> List[Dict[str, Any]]:
    with database.db_session(db_path) as conn:
        return database.get_matches_ready_for_analysis(conn, require_league_approval=config.REQUIRE_LEAGUE_APPROVAL)

def _label(match: Dict[str, Any]) -> str:
    return f"{match['home_team']} - {match['away_team']}"

def analyze_one(match: Dict[str, Any], db_path: str = database.DB_FILE) -> str:
    """
    Tek maçı analiz eder: 'analyzed' (tahmin kaydedildi) veya 'skipped' (bu arada analiz edilmiş / verisi eksik / lig izni kapanmış).
    Gemini hataları fırlatılır (BackgroundJob hata sayar). Gemini ayarı kapatıldıysa StopJob ile iş durur.
    """
    with database.db_session(db_path) as conn:
        if not database.is_gemini_enabled(conn):
            raise StopJob("Gemini analizleri kapatıldığı için toplu analiz durduruldu.")
        if database.get_match_predictions(conn, match["id"]):
            return "skipped"
        ctx, missing = match_data.load_context_for_analysis(conn, match["id"])
        if ctx is None or missing:
            return "skipped"
        if config.REQUIRE_LEAGUE_APPROVAL and not ctx["match"]["league_gemini_analysis"]:
            return "skipped"

    # Uzun süren Gemini isteği sırasında veritabanı bağlantısı açık tutulmaz
    prediction = gemini_engine.generate_prediction(ctx, model_name=config.GEMINI_MODEL)
    with database.db_session(db_path) as conn:
        database.save_prediction(conn, match["id"], prediction)
    return "analyzed"

def start_background(db_path: str = database.DB_FILE) -> Dict[str, Any]:
    """Analize uygun tüm maçlar için işi başlatır. Zaten çalışıyorsa mevcut durumu döner ('started': False)."""
    if _job.is_running():
        return {**_job.status(), "started": False}
    return _job.start(eligible_matches(db_path), worker=lambda m: analyze_one(m, db_path), label=_label)

def get_status() -> Dict[str, Any]:
    return _job.status()

def cancel() -> Dict[str, Any]:
    return _job.cancel()

def wait(timeout: Optional[float] = None) -> None:
    _job.wait(timeout)
