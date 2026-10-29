import requests
import json
from typing import Dict, Any, List, Optional, Tuple
import config

DEFAULT_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Encoding": "gzip, deflate"
}

def _clean_nesine_data_json(raw_text: str) -> Dict[str, Any]:
    """Nesine istatistik API'lerinin döndürdüğü 'Data' string JSON'ını temizler ve dict objesine çevirir."""
    if not raw_text:
        return {}
    if "Beklenmedik Hata" in raw_text or "<!DOCTYPE html>" in raw_text:
        return {}
    if raw_text.startswith("\ufeff"):
        raw_text = raw_text[1:]
    
    try:
        outer = json.loads(raw_text)
        if isinstance(outer, dict) and "Data" in outer:
            data_str = outer["Data"]
            if isinstance(data_str, str):
                cleaned_str = data_str.replace('\\"', '"').replace('\\', '')
                if cleaned_str.startswith('"') and cleaned_str.endswith('"'):
                    cleaned_str = cleaned_str[1:-1]
                return json.loads(cleaned_str)
            elif isinstance(data_str, dict):
                return data_str
        return outer
    except Exception:
        return {}

def extract_odds_from_event(event: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Nesine bülten maç objesindeki (MA dizisi) piyasa ve oranları okunabilir isimlerle parse eder.
    """
    odds_list = []
    markets = event.get("MA", [])
    if not markets:
        return odds_list

    for market in markets:
        mt = market.get("MT")
        mtid = market.get("MTID")
        mst = market.get("MST")
        sov = market.get("SOV")
        oca = market.get("OCA", [])
        bm = market.get("BM")  # Ana bülten marketi mi?

        market_name = market.get("MN")
        
        # 1. Maç Sonucu (MS 1, MS 0, MS 2)
        if mtid == 1 or mst == 1:
            market_name = "Maç Sonucu"
            labels = {1: "MS 1", 2: "MS 0", 3: "MS 2"}
            for c in oca:
                odds_list.append({
                    "market_type": mt,
                    "market_name": market_name,
                    "option_name": labels.get(c.get("N"), f"MS {c.get('N')}"),
                    "odds_value": float(c.get("O", 0.0))
                })
        # 2. İlk Yarı Sonucu (İY 1, İY 0, İY 2)
        elif mtid == 7 or mst == 88:
            market_name = "İlk Yarı Sonucu"
            labels = {1: "İY 1", 2: "İY 0", 3: "İY 2"}
            for c in oca:
                odds_list.append({
                    "market_type": mt,
                    "market_name": market_name,
                    "option_name": labels.get(c.get("N"), f"İY {c.get('N')}"),
                    "odds_value": float(c.get("O", 0.0))
                })
        # 3. Karşılıklı Gol (KG VAR, KG YOK)
        elif mtid == 38 or mst == 89:
            market_name = "Karşılıklı Gol"
            labels = {1: "KG VAR", 2: "KG YOK"}
            for c in oca:
                odds_list.append({
                    "market_type": mt,
                    "market_name": market_name,
                    "option_name": labels.get(c.get("N"), f"KG Seçenek {c.get('N')}"),
                    "odds_value": float(c.get("O", 0.0))
                })
        # 4. Çifte Şans (1-X, 1-2, X-2)
        elif mtid == 3 or mst == 92:
            market_name = "Çifte Şans"
            labels = {1: "1-X", 2: "1-2", 3: "X-2"}
            for c in oca:
                odds_list.append({
                    "market_type": mt,
                    "market_name": market_name,
                    "option_name": labels.get(c.get("N"), f"ÇŞ {c.get('N')}"),
                    "odds_value": float(c.get("O", 0.0))
                })
        # 5. Ana Maç 2.5 Gol Alt/Üst Marketi
        elif mtid == 12 or (mst == 101 and sov == 2.5 and bm == 1):
            market_name = "2.5 Gol Alt/Üst"
            labels = {1: "2.5 Alt", 2: "2.5 Üst"}
            for c in oca:
                odds_list.append({
                    "market_type": mt,
                    "market_name": market_name,
                    "option_name": labels.get(c.get("N"), f"2.5 {c.get('N')}"),
                    "odds_value": float(c.get("O", 0.0))
                })
        # 6. Diğer Ana Maç Toplam Golleri (1.5, 3.5, 4.5 vb.)
        elif mtid in (11, 13, 155) or (mst == 101 and len(oca) == 2 and sov not in (0.5, 2.5)):
            market_name = f"{sov} Gol Alt/Üst"
            labels = {1: f"{sov} Alt", 2: f"{sov} Üst"}
            for c in oca:
                odds_list.append({
                    "market_type": mt,
                    "market_name": market_name,
                    "option_name": labels.get(c.get("N"), f"Seçenek {c.get('N')}"),
                    "odds_value": float(c.get("O", 0.0))
                })
        # 7. İlk Yarı Alt/Üst
        elif mtid in (256, 328, 29) or mst == 604:
            market_name = f"İY {sov} Gol Alt/Üst"
            labels = {1: f"İY {sov} Alt", 2: f"İY {sov} Üst"}
            for c in oca:
                odds_list.append({
                    "market_type": mt,
                    "market_name": market_name,
                    "option_name": labels.get(c.get("N"), f"Seçenek {c.get('N')}"),
                    "odds_value": float(c.get("O", 0.0))
                })
        # 8. Ev Sahibi Takım Alt/Üst
        elif mtid in (326, 212) or mst == 603:
            market_name = f"Ev Sahibi {sov} Gol Alt/Üst"
            labels = {1: f"Ev {sov} Alt", 2: f"Ev {sov} Üst"}
            for c in oca:
                odds_list.append({
                    "market_type": mt,
                    "market_name": market_name,
                    "option_name": labels.get(c.get("N"), f"Seçenek {c.get('N')}"),
                    "odds_value": float(c.get("O", 0.0))
                })
        # 9. Deplasman Takım Alt/Üst
        elif mtid in (15,) or (mst == 60 and sov != 1.5):
            market_name = f"Deplasman {sov} Gol Alt/Üst"
            labels = {1: f"Dep {sov} Alt", 2: f"Dep {sov} Üst"}
            for c in oca:
                odds_list.append({
                    "market_type": mt,
                    "market_name": market_name,
                    "option_name": labels.get(c.get("N"), f"Seçenek {c.get('N')}"),
                    "odds_value": float(c.get("O", 0.0))
                })
        # 10. Toplam Korner / Kart Alt/Üst
        elif mtid in (216, 218) or mst in (48, 49):
            category = "Korner" if (mtid == 216 or mst == 48) else "Kart"
            market_name = f"Toplam {category} {sov} Alt/Üst"
            labels = {1: f"{category} {sov} Alt", 2: f"{category} {sov} Üst"}
            for c in oca:
                odds_list.append({
                    "market_type": mt,
                    "market_name": market_name,
                    "option_name": labels.get(c.get("N"), f"Seçenek {c.get('N')}"),
                    "odds_value": float(c.get("O", 0.0))
                })
        # 11. Diğer 2 Seçenekli Özel Alt/Üst Marketleri
        elif sov is not None and len(oca) == 2:
            market_name = f"Özel {sov} Alt/Üst"
            labels = {1: f"{sov} Alt", 2: f"{sov} Üst"}
            for c in oca:
                odds_list.append({
                    "market_type": mt,
                    "market_name": market_name,
                    "option_name": labels.get(c.get("N"), f"Seçenek {c.get('N')}"),
                    "odds_value": float(c.get("O", 0.0))
                })
        # 6. Genel Fallback
        elif market_name:
            for c in oca:
                opt_name = c.get("ON") or f"Seçenek {c.get('N')}"
                odds_list.append({
                    "market_type": mt,
                    "market_name": market_name,
                    "option_name": opt_name,
                    "odds_value": float(c.get("O", 0.0))
                })

    return odds_list

def fetch_bulletin() -> List[Dict[str, Any]]:
    """
    Güncel İddaa bültenindeki tüm maçları çeker.
    Lig adlarını 'LA' sözlüğünden otomatik eşleştirip her maça 'league_name' olarak ekler.
    """
    url = getattr(config, "NESINE_BULLETIN_URL", "https://bulten.nesine.com/api/bulten/getprebultenfull")
    try:
        res = requests.get(url, headers=DEFAULT_HEADERS, timeout=15)
        if res.status_code == 200:
            data = res.json()
            sg = data.get("sg", {})
            events = sg.get("EA", [])
            leagues = {str(item.get("LID")): item.get("N") for item in sg.get("LA", [])}
            
            # Her maça lig ismini ve okunabilir alanları ata
            for event in events:
                lc = str(event.get("LC"))
                event["league_name"] = leagues.get(lc, event.get("ENN") or "Genel Lig")
            return events
    except Exception as e:
        print(f"Bülten çekilirken hata oluştu: {e}")
    return []

def fetch_last_matches(match_code: str) -> Dict[str, Any]:
    """Takımların son maç istatistiklerini çeker."""
    base_url = getattr(config, "NESINE_STATS_BASE_URL", "https://istatistik.nesine.com/api/v2")
    url = f"{base_url}/{match_code}/lastmatches"
    try:
        res = requests.get(url, headers=DEFAULT_HEADERS, timeout=10)
        if res.status_code == 200 and "Beklenmedik Hata" not in res.text:
            return _clean_nesine_data_json(res.text)
    except Exception:
        pass
    return {}

def fetch_competition_history(match_code: str) -> Dict[str, Any]:
    """Takımların aralarındaki H2H mücadele geçmişini çeker."""
    base_url = getattr(config, "NESINE_STATS_BASE_URL", "https://istatistik.nesine.com/api/v2")
    url = f"{base_url}/{match_code}/competitionhistory?filter=false"
    try:
        res = requests.get(url, headers=DEFAULT_HEADERS, timeout=10)
        if res.status_code == 200 and "Beklenmedik Hata" not in res.text:
            return _clean_nesine_data_json(res.text)
    except Exception:
        pass
    return {}

def fetch_missing_players(match_code: str) -> Dict[str, Any]:
    """Sakat ve cezalı oyuncuları çeker."""
    base_url = getattr(config, "NESINE_STATS_BASE_URL", "https://istatistik.nesine.com/api/v2")
    url = f"{base_url}/{match_code}/missingplayers"
    try:
        res = requests.get(url, headers=DEFAULT_HEADERS, timeout=10)
        if res.status_code == 200 and "Beklenmedik Hata" not in res.text:
            return _clean_nesine_data_json(res.text)
    except Exception:
        pass
    return {}

def fetch_league_table(match_code: str) -> Dict[str, Any]:
    """Lig puan durumunu çeker."""
    base_url = getattr(config, "NESINE_STATS_BASE_URL", "https://istatistik.nesine.com/api/v2")
    url = f"{base_url}/{match_code}/leaguetable"
    try:
        res = requests.get(url, headers=DEFAULT_HEADERS, timeout=10)
        if res.status_code == 200 and "Beklenmedik Hata" not in res.text:
            return _clean_nesine_data_json(res.text)
    except Exception:
        pass
    return {}

def fetch_match_results(date_str: str, is_basketball: bool = False) -> List[Dict[str, Any]]:
    """Biten maç sonuçlarını çeker. Format: YYYY-MM-DD"""
    bet_type = "2" if is_basketball else "1"
    base_url = getattr(config, "NESINE_LIVESCORE_URL", "https://ls.nesine.com/api/v1/LiveScore/GetIddaaResultObjectList")
    url = f"{base_url}?BetType={bet_type}&OnlyLive=0&Date={date_str}&League="
    try:
        res = requests.get(url, headers=DEFAULT_HEADERS, timeout=15)
        if res.status_code == 200:
            data = res.json()
            return data.get("ES", [])
    except Exception:
        pass
    return []
