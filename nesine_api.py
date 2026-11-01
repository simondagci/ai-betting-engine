import requests
import json
from typing import Dict, Any, List, Optional, Tuple
import config
import rules

DEFAULT_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Encoding": "gzip, deflate"
}

STATS_TIMEOUT = 10
BULLETIN_TIMEOUT = 15

def _clean_nesine_data_json(raw_text: str) -> Dict[str, Any]:
    """Nesine istatistik API'lerinin döndürdüğü 'Data' string JSON'ını temizler ve dict objesine çevirir."""
    if not raw_text:
        return {}
    if "Beklenmedik Hata" in raw_text or "<!DOCTYPE html>" in raw_text:
        return {}
    if raw_text.startswith("﻿"):
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

# (market_name, {seçenek_no: etiket}, bilinmeyen seçenek no için etiket şablonu)
# labels None ise seçeneklerin kendi isimleri ('ON') kullanılır.
MarketSpec = Tuple[str, Optional[Dict[int, str]], Optional[str]]

def _over_under_labels(prefix: str) -> Dict[int, str]:
    return {1: f"{prefix} Alt", 2: f"{prefix} Üst"}

def _classify_market(market: Dict[str, Any]) -> Optional[MarketSpec]:
    """Bülten marketini (MTID / MST / SOV kodlarına göre) okunabilir market adı ve seçenek etiketlerine eşler."""
    mtid = market.get("MTID")
    mst = market.get("MST")
    sov = market.get("SOV")
    bm = market.get("BM")  # Ana bülten marketi mi?
    option_count = len(market.get("OCA") or [])

    # 1. Maç Sonucu (MS 1, MS 0, MS 2)
    if mtid == 1 or mst == 1:
        return "Maç Sonucu", {1: "MS 1", 2: "MS 0", 3: "MS 2"}, "MS {}"
    # 2. İlk Yarı Sonucu (İY 1, İY 0, İY 2)
    if mtid == 7 or mst == 88:
        return "İlk Yarı Sonucu", {1: "İY 1", 2: "İY 0", 3: "İY 2"}, "İY {}"
    # 3. Karşılıklı Gol (KG VAR, KG YOK)
    if mtid == 38 or mst == 89:
        return "Karşılıklı Gol", {1: "KG VAR", 2: "KG YOK"}, "KG Seçenek {}"
    # 4. Çifte Şans (1-X, 1-2, X-2)
    if mtid == 3 or mst == 92:
        return "Çifte Şans", {1: "1-X", 2: "1-2", 3: "X-2"}, "ÇŞ {}"
    # 5. Ana Maç 2.5 Gol Alt/Üst Marketi
    if mtid == 12 or (mst == 101 and sov == 2.5 and bm == 1):
        return "2.5 Gol Alt/Üst", _over_under_labels("2.5"), "2.5 {}"
    # 6. Diğer Ana Maç Toplam Golleri (1.5, 3.5, 4.5 vb.)
    if mtid in (11, 13, 155) or (mst == 101 and option_count == 2 and sov not in (0.5, 2.5)):
        return f"{sov} Gol Alt/Üst", _over_under_labels(f"{sov}"), "Seçenek {}"
    # 7. İlk Yarı Alt/Üst
    if mtid in (256, 328, 29) or mst == 604:
        return f"İY {sov} Gol Alt/Üst", _over_under_labels(f"İY {sov}"), "Seçenek {}"
    # 8. Ev Sahibi Takım Alt/Üst
    if mtid in (326, 212) or mst == 603:
        return f"Ev Sahibi {sov} Gol Alt/Üst", _over_under_labels(f"Ev {sov}"), "Seçenek {}"
    # 9. Deplasman Takım Alt/Üst
    if mtid == 15 or (mst == 60 and sov != 1.5):
        return f"Deplasman {sov} Gol Alt/Üst", _over_under_labels(f"Dep {sov}"), "Seçenek {}"
    # 10. Toplam Korner / Kart Alt/Üst
    if mtid in (216, 218) or mst in (48, 49):
        category = "Korner" if (mtid == 216 or mst == 48) else "Kart"
        return f"Toplam {category} {sov} Alt/Üst", _over_under_labels(f"{category} {sov}"), "Seçenek {}"
    # 11. Diğer 2 Seçenekli Özel Alt/Üst Marketleri
    if sov is not None and option_count == 2:
        return f"Özel {sov} Alt/Üst", _over_under_labels(f"{sov}"), "Seçenek {}"
    # 12. Genel Fallback: Bültendeki market adı ve seçenek isimleri olduğu gibi kullanılır
    if market.get("MN"):
        return market["MN"], None, None
    return None

# Basketbol market alt tipleri (MST): futbol kodlarıyla çakıştığı için ayrı sınıflandırılır
BASKETBALL_MATCH_RESULT_MST = 2
BASKETBALL_FIRST_HALF_RESULT_MST = 621
BASKETBALL_TOTAL_POINTS_MST = 114
BASKETBALL_TOTAL_POINTS_MARKET = rules.TOTAL_POINTS_MARKET

def _format_line(value: Any) -> str:
    """Barem değerini metne çevirir: 237.5 -> '237.5', 3.0 -> '3'."""
    return f"{value:g}" if isinstance(value, float) else str(value)

def _classify_basketball_market(market: Dict[str, Any]) -> Optional[MarketSpec]:
    """
    Basketbol marketleri: MS (uzatmalar dahil, beraberlik yok), İlk Yarı Sonucu ve ana Toplam Sayı Alt/Üst baremi
    standart isimlerle; diğerleri bültendeki market / seçenek isimleriyle (örn. 'Handikaplı Maç Sonucu -2.5') döner.
    """
    mst, sov = market.get("MST"), market.get("SOV")
    if mst == BASKETBALL_MATCH_RESULT_MST:
        return "Maç Sonucu", {1: "MS 1", 2: "MS 2"}, "MS {}"
    if mst == BASKETBALL_FIRST_HALF_RESULT_MST:
        return "İlk Yarı Sonucu", {1: "İY 1", 2: "İY 0", 3: "İY 2"}, "İY {}"
    if mst == BASKETBALL_TOTAL_POINTS_MST and sov is not None:
        line = _format_line(sov)
        return BASKETBALL_TOTAL_POINTS_MARKET, {1: f"{line} Alt", 2: f"{line} Üst"}, "Seçenek {}"
    if market.get("MN"):
        return market["MN"], None, None
    return None

def extract_odds_from_event(event: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Nesine bülten maç objesindeki (MA dizisi) piyasa ve oranları okunabilir isimlerle parse eder.
    Basketbol etkinlikleri (TYPE == '2') kendi market kodlarıyla ayrı sınıflandırılır.
    """
    classify = _classify_basketball_market if str(event.get("TYPE")) == "2" else _classify_market
    odds_list = []
    for market in event.get("MA") or []:
        spec = classify(market)
        if spec is None:
            continue
        market_name, labels, fallback = spec

        for c in market.get("OCA") or []:
            n = c.get("N")
            if labels is None:
                option_name = c.get("ON") or f"Seçenek {n}"
            else:
                option_name = labels.get(n, fallback.format(n))
            odds_list.append({
                "market_type": market.get("MT"),
                "market_name": market_name,
                "option_name": option_name,
                "odds_value": float(c.get("O", 0.0))
            })

    return odds_list

def fetch_bulletin() -> List[Dict[str, Any]]:
    """
    Güncel İddaa bültenindeki tüm maçları çeker.
    Lig adlarını 'LA' sözlüğünden otomatik eşleştirip her maça 'league_name' olarak ekler.
    """
    try:
        res = requests.get(config.NESINE_BULLETIN_URL, headers=DEFAULT_HEADERS, timeout=BULLETIN_TIMEOUT)
        if res.status_code == 200:
            sg = res.json().get("sg", {})
            events = sg.get("EA", [])
            leagues = {str(item.get("LID")): item.get("N") for item in sg.get("LA", [])}

            # Her maça lig ismini ve lig ID'sini (iddaa.com ile aynı) ata
            for event in events:
                event["league_name"] = leagues.get(str(event.get("LC")), event.get("ENN") or "Genel Lig")
                event["league_external_id"] = event.get("LC")
            return events
    except Exception as e:
        print(f"Bülten çekilirken hata oluştu: {e}")
    return []

def _fetch_stats(match_code: str, endpoint: str) -> Dict[str, Any]:
    """istatistik.nesine.com servislerinden birini çağırır. Hata / boş yanıt durumunda {} döner."""
    url = f"{config.NESINE_STATS_BASE_URL}/{match_code}/{endpoint}"
    try:
        res = requests.get(url, headers=DEFAULT_HEADERS, timeout=STATS_TIMEOUT)
        if res.status_code == 200 and "Beklenmedik Hata" not in res.text:
            return _clean_nesine_data_json(res.text)
    except requests.RequestException:
        pass
    return {}

def fetch_last_matches(match_code: str) -> Dict[str, Any]:
    """Takımların son maç istatistiklerini çeker."""
    return _fetch_stats(match_code, "lastmatches")

def fetch_competition_history(match_code: str) -> Dict[str, Any]:
    """Takımların aralarındaki H2H mücadele geçmişini çeker."""
    return _fetch_stats(match_code, "competitionhistory?filter=false")

def fetch_missing_players(match_code: str) -> Dict[str, Any]:
    """Sakat ve cezalı oyuncuları çeker."""
    return _fetch_stats(match_code, "missingplayers")

def fetch_league_table(match_code: str) -> Dict[str, Any]:
    """Lig puan durumunu çeker."""
    return _fetch_stats(match_code, "leaguetable")

def fetch_match_stats(match_code: str) -> Dict[str, Any]:
    """
    Bir maç için tüm istatistik servislerini (H2H, son maçlar, eksik oyuncular, puan durumu) çağırır.
    Dönen anahtarlar database.save_match_stats parametreleriyle aynıdır.
    """
    last_matches = fetch_last_matches(match_code)
    return {
        "h2h_data": fetch_competition_history(match_code),
        "home_last": last_matches.get("HomeTeamMatches") if isinstance(last_matches, dict) else None,
        "away_last": last_matches.get("AwayTeamMatches") if isinstance(last_matches, dict) else None,
        "missing": fetch_missing_players(match_code),
        "league_table": fetch_league_table(match_code)
    }

def fetch_match_results(date_str: str, is_basketball: bool = False) -> List[Dict[str, Any]]:
    """Biten maç sonuçlarını çeker. Format: YYYY-MM-DD"""
    params = {"BetType": "2" if is_basketball else "1", "OnlyLive": "0", "Date": date_str, "League": ""}
    try:
        res = requests.get(config.NESINE_LIVESCORE_URL, params=params, headers=DEFAULT_HEADERS, timeout=BULLETIN_TIMEOUT)
        if res.status_code == 200:
            return res.json().get("ES", [])
    except Exception:
        pass
    return []
