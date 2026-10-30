"""
iddaa.com veri kaynağı (bülten, oranlar, istatistikler, maç sonuçları).

Nesine istatistik servisleri Türkiye dışından erişimi engellediği için varsayılan kaynak iddaa.com'dur.
Maç kodları ve lig ID'leri Nesine ile aynıdır (ikisi de merkezi iddaa bültenini kullanır).
Uygulamanın geri kalanı değişmeden çalışsın diye veriler mevcut formatlara dönüştürülür:
  - Bülten -> Nesine 'sg.EA' etkinlik formatı (nesine_api.extract_odds_from_event ile ayrıştırılır)
  - Son maçlar / H2H -> [{date, league, home, away, score, ht_score, result}] (detay sayfası ve Gemini prompt'u)
  - Puan durumu -> {"LeagueTable": [{Rank, TeamName, Played, Won, Draw, Lost, Points, ...}]}
Dokümantasyon: mdFiles/APIs/iddaa/README.md
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo
import requests
import config
import nesine_api

SOURCE_NAME = "iddaa.com"

DEFAULT_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.iddaa.com",
    "Referer": "https://www.iddaa.com/",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Encoding": "gzip, deflate"
}

BULLETIN_TIMEOUT = 30
STATS_TIMEOUT = 15
# Bir maçın 4 istatistik isteği (ev / deplasman son maçlar, H2H, puan durumu) aynı anda atılır.
# Siteyi yormamak için maçlar arasında bekleme stats_sync.MATCH_DELAY_SECONDS ile yapılır.
STATS_PARALLEL_REQUESTS = 4

# Bülten 'TYPE' kodu (Nesine ile aynı) -> iddaa spor ID'si ve istatistik yolu
SPORTS = {
    "1": {"sport_id": 1, "stats_path": "soccer"},
    "2": {"sport_id": 2, "stats_path": "basketball"},
}
SPORT_TYPE_CODES = {"FOOTBALL": "1", "BASKETBALL": "2"}

# live-results servisinde 's' (durum) değeri: 6 = maç normal süresinde tamamlandı
FINISHED_STATUS = 6

def _get_json(url: str, params: Optional[Dict[str, Any]] = None, timeout: int = STATS_TIMEOUT) -> Optional[Any]:
    """iddaa servislerini çağırır; {"isSuccess": true, "data": ...} yanıtında data'yı, aksi halde None döner."""
    try:
        res = requests.get(url, params=params, headers=DEFAULT_HEADERS, timeout=timeout)
        if res.status_code != 200:
            return None
        body = res.json()
    except (requests.RequestException, ValueError):
        return None
    if not isinstance(body, dict) or not body.get("isSuccess"):
        return None
    return body.get("data")

def _to_source_datetime(epoch_seconds: Optional[int]) -> Tuple[str, str]:
    """Epoch zamanını bülten kaynak saatine (Türkiye) göre ('DD.MM.YYYY', 'HH:MM') formatına çevirir (Nesine D/T ile aynı)."""
    if not epoch_seconds:
        return "", ""
    dt = datetime.fromtimestamp(epoch_seconds, ZoneInfo(config.SOURCE_TIMEZONE))
    return dt.strftime("%d.%m.%Y"), dt.strftime("%H:%M")

def _to_iso_date(epoch_seconds: Optional[int]) -> str:
    if not epoch_seconds:
        return ""
    return datetime.fromtimestamp(epoch_seconds, ZoneInfo(config.SOURCE_TIMEZONE)).strftime("%Y-%m-%d")

# ==============================================================================
# BÜLTEN
# ==============================================================================

def _fetch_competitions() -> Dict[int, str]:
    data = _get_json(f"{config.IDDAA_SPORTSBOOK_URL}/sportsbook/competitions", timeout=BULLETIN_TIMEOUT) or []
    return {c.get("i"): c.get("n") for c in data if isinstance(c, dict)}

def _fetch_market_names() -> Dict[str, str]:
    """'{market_type}_{sub_type}' -> market adı (örn. '2_821' -> '2. Yarı Karşılıklı Gol')."""
    data = _get_json(f"{config.IDDAA_SPORTSBOOK_URL}/sportsbook/get_market_config", timeout=BULLETIN_TIMEOUT) or {}
    markets = data.get("m") if isinstance(data, dict) else None
    return {key: m.get("n") for key, m in (markets or {}).items() if isinstance(m, dict)}

def _parse_sov(value: Any) -> Optional[float]:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None

def _market_name(template: Optional[str], sov: Any) -> Optional[str]:
    """Market adı şablonundaki '{0}' / '{h}' yer tutucularını market değeriyle (sov) doldurur, örn. 'Alt/Üst {0}' -> 'Alt/Üst 2.5'."""
    if not template:
        return None
    value = "" if sov in (None, "") else str(sov)
    return template.replace("{0}", value).replace("{h}", value).strip()

def _to_nesine_market(market: Dict[str, Any], market_names: Dict[str, str]) -> Dict[str, Any]:
    """iddaa market objesini Nesine 'MA' formatına çevirir (t=MT, st=MST, sov=SOV, o=OCA)."""
    mt, mst = market.get("t"), market.get("st")
    return {
        "MT": mt,
        "MST": mst,
        "SOV": _parse_sov(market.get("sov")),
        # Nesine ana maç toplam gol marketini (MST 101) BM=1 ile işaretler; iddaa'da her MST 101 ana maç marketidir
        "BM": 1 if mst == 101 else None,
        "MN": _market_name(market_names.get(f"{mt}_{mst}"), market.get("sov")),
        # 'wodd' online oran, 'odd' bayi oranıdır; Nesine'nin gösterdiği (ve DB'deki mevcut) oranlar online orandır
        "OCA": [{"N": o.get("no"), "O": o.get("wodd") or o.get("odd"), "ON": o.get("n")} for o in market.get("o") or []]
    }

def _to_nesine_event(event: Dict[str, Any], type_code: str, competitions: Dict[int, str],
                     market_names: Dict[str, str]) -> Dict[str, Any]:
    match_date, match_time = _to_source_datetime(event.get("d"))
    league_id = event.get("ci")
    return {
        "C": event.get("i"),
        "TYPE": type_code,
        "HN": event.get("hn"),
        "AN": event.get("an"),
        "D": match_date,
        "T": match_time,
        "LC": league_id,
        "BRID": event.get("bri"),
        "league_name": competitions.get(league_id) or "Genel Lig",
        "league_external_id": league_id,
        "MA": [_to_nesine_market(m, market_names) for m in event.get("m") or []]
    }

def fetch_bulletin() -> List[Dict[str, Any]]:
    """Futbol ve basketbol bültenini çeker ve Nesine etkinlik formatında döner (lig adı 'league_name' alanında)."""
    competitions = _fetch_competitions()
    market_names = _fetch_market_names()
    events: List[Dict[str, Any]] = []
    for type_code, sport in SPORTS.items():
        data = _get_json(
            f"{config.IDDAA_SPORTSBOOK_URL}/sportsbook/events",
            params={"st": sport["sport_id"], "type": 0, "version": 0},
            timeout=BULLETIN_TIMEOUT
        )
        if not data:
            print(f"iddaa.com bülteni çekilemedi (spor: {sport['stats_path']})")
            continue
        events.extend(
            _to_nesine_event(e, type_code, competitions, market_names)
            for e in data.get("events") or []
            # "Haftalık Bahisler" gibi özel etkinlikler (örn. "LaLiga - 11. Hafta (10 Maç)") gerçek maç değildir:
            # BetRadar ID'leri yoktur ve istatistik servisi EventNotFound döner. Gerçek maçların hepsinde 'bri' doludur.
            if is_real_match(e)
        )
    return events

def is_real_match(event: Dict[str, Any]) -> bool:
    """Bülten etkinliği gerçek bir maç mı (özel / haftalık bahis etkinliği değil mi)?"""
    return bool(event.get("bri"))

# ==============================================================================
# İSTATİSTİKLER
# ==============================================================================

def _score_text(home: Dict[str, Any], away: Dict[str, Any], key: str) -> str:
    h, a = home.get(key), away.get(key)
    return f"{h}-{a}" if h is not None and a is not None else ""

def _format_history(matches: Optional[List[Dict[str, Any]]]) -> Optional[List[Dict[str, Any]]]:
    """iddaa geçmiş maç listesini detay sayfası / Gemini prompt formatına çevirir. Boş listede None döner."""
    if not matches:
        return None
    rows = []
    for m in matches:
        home, away = m.get("h") or {}, m.get("a") or {}
        row = {
            "date": _to_iso_date(m.get("t")),
            "league": (m.get("l") or {}).get("n", ""),
            "home": home.get("n", ""),
            "away": away.get("n", ""),
            "score": _score_text(home, away, "rs"),
            "ht_score": _score_text(home, away, "fhs"),
        }
        # Son maç listelerinde 'mr' ilgili takımın bakış açısından sonuçtur (G / B / M)
        if m.get("mr"):
            row["result"] = m["mr"]
        rows.append(row)
    return rows

def _format_standings(data: Optional[Dict[str, Any]], team_names: Tuple[str, ...]) -> Optional[Dict[str, Any]]:
    """Puan durumunu Nesine 'LeagueTable' formatına çevirir. Birden fazla grup varsa takımların bulunduğu grup seçilir."""
    groups = (data or {}).get("g") or []
    if not groups:
        return None
    group = next((g for g in groups if any(t.get("n") in team_names for t in g.get("t") or [])), groups[0])
    table = [
        {
            "Rank": t.get("r"),
            "TeamName": t.get("n"),
            "Played": t.get("p"),
            "Won": t.get("w"),
            "Draw": t.get("d"),
            "Lost": t.get("l"),
            "GoalsFor": t.get("gf", t.get("pf")),
            "GoalsAgainst": t.get("ga", t.get("pa")),
            "GoalDiff": t.get("gd", t.get("pd")),
            # Basketbol puan tablosunda puan yerine galibiyet sayısı kullanılır
            "Points": t.get("pt", t.get("w")),
            "Form": t.get("lfive"),
        }
        for t in group.get("t") or []
    ]
    if not table:
        return None
    return {"LeagueTable": table, "Group": group.get("n"), "Season": ((data or {}).get("ss") or {}).get("n")}

def fetch_match_stats(match_code: str, sport_type: str = "FOOTBALL") -> Dict[str, Any]:
    """
    Bir maç için son maçları (ev / deplasman), H2H geçmişini ve puan durumunu çeker.
    Dönen anahtarlar database.save_match_stats parametreleriyle aynıdır.
    iddaa.com sakat / cezalı oyuncu servisi sunmadığı için 'missing' her zaman None'dır.
    """
    sport = SPORTS[SPORT_TYPE_CODES.get(sport_type, "1")]
    base = f"{config.IDDAA_STATS_URL}/statistics/{sport['stats_path']}"

    def recent(history_type: int) -> Optional[Dict[str, Any]]:
        params = {"matchHistoryType": history_type}
        if sport["stats_path"] == "soccer":
            params["isOnlyCurrentTournament"] = "false"
        return _get_json(f"{base}/recent-matches/{match_code}", params=params)

    standings_params = {"isLive": "false"}
    if sport["stats_path"] == "soccer":
        standings_params["standingType"] = 0

    with ThreadPoolExecutor(max_workers=STATS_PARALLEL_REQUESTS) as pool:
        home_f, away_f, h2h_f = (pool.submit(recent, history_type) for history_type in (0, 1, 2))
        standings_f = pool.submit(_get_json, f"{base}/standings/{match_code}", standings_params)
        home_data, away_data, h2h_data, standings = home_f.result(), away_f.result(), h2h_f.result(), standings_f.result()

    team_names = tuple(
        (d.get(side) or {}).get("n") for d in (home_data, away_data) if d for side in ("h", "a")
    )
    return {
        "h2h_data": _format_history((h2h_data or {}).get("m")),
        "home_last": _format_history((home_data or {}).get("m")),
        "away_last": _format_history((away_data or {}).get("m")),
        "missing": None,
        "league_table": _format_standings(standings, team_names)
    }

# ==============================================================================
# MAÇ SONUÇLARI
# ==============================================================================

def fetch_match_results(date_str: str, is_basketball: bool = False) -> List[Dict[str, Any]]:
    """
    Verilen günde (YYYY-MM-DD, Türkiye saatine göre) normal süresinde tamamlanmış maçların skorlarını döner:
    [{"code", "home_team", "away_team", "ht_home", "ht_away", "ft_home", "ft_away"}]
    Uzatma / penaltı / ertelenme gibi durumlar (s != 6) atlanır; bu maçların skoru elle girilmelidir.
    """
    data = _get_json(
        f"{config.IDDAA_STATS_URL}/live-results",
        params={"sportId": 2 if is_basketball else 1, "date": date_str}
    )
    if not data:
        return []

    scores = data.get("sc") or {}
    results = []
    for event in data.get("events") or []:
        score = scores.get(str(event.get("eventId")))
        if not score or score.get("s") != FINISHED_STATUS:
            continue
        home, away = score.get("ht") or {}, score.get("at") or {}
        if home.get("r") is None or away.get("r") is None:
            continue
        results.append({
            "code": str(event.get("eventId")),
            "home_team": event.get("homeTeam"),
            "away_team": event.get("awayTeam"),
            "ht_home": home.get("ht"),
            "ht_away": away.get("ht"),
            "ft_home": home.get("r"),
            "ft_away": away.get("r"),
        })
    return results
