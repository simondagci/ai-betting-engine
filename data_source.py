"""
Aktif veri kaynağı (config.DATA_SOURCE): bülten, istatistik ve maç sonuçları buradan çağrılır.
app.py ve web_server.py nesine_api / iddaa_api modüllerini doğrudan değil, bu modül üzerinden kullanır.
"""
from typing import Any, Dict, List, Optional
import config
import iddaa_api
import nesine_api

def _is_nesine() -> bool:
    return config.DATA_SOURCE == "nesine"

def source_name() -> str:
    return "Nesine" if _is_nesine() else iddaa_api.SOURCE_NAME

def fetch_bulletin() -> List[Dict[str, Any]]:
    """Bülten etkinliklerini Nesine 'sg.EA' formatında döner (database.sync_bulletin_matches_and_odds girdisi)."""
    return nesine_api.fetch_bulletin() if _is_nesine() else iddaa_api.fetch_bulletin()

def fetch_match_stats(match_code: str, sport_type: str = "FOOTBALL") -> Dict[str, Any]:
    """Dönen anahtarlar database.save_match_stats parametreleriyle aynıdır."""
    if _is_nesine():
        return nesine_api.fetch_match_stats(match_code)
    return iddaa_api.fetch_match_stats(match_code, sport_type)

def supports_results() -> bool:
    """Otomatik maç sonucu çekme sadece iddaa.com kaynağında desteklenir."""
    return not _is_nesine()

def fetch_match_results(date_str: str, is_basketball: bool = False) -> Optional[List[Dict[str, Any]]]:
    """Normal süresinde biten maçların skorları; kaynak desteklemiyorsa None."""
    if not supports_results():
        return None
    return iddaa_api.fetch_match_results(date_str, is_basketball)
