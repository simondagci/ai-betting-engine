"""
Kupon motoru: Kalibre edilmiş olasılıklar ve çoklu model adayları (Gemini ⭐/2, Python ⭐/2) ile
yüksek tutma olasılığına sahip kombine kuponlar önerir.

Tasarım Esasları:
1. Kalibrasyon: Şişkin güven yüzdeleri yerine geçmiş maçlardaki gerçekleşen isabet oranları
   Empirical Bayes / Hiyerarşik daraltma (shrinkage) ile kalibre edilir.
2. Aday Kaynakları:
   - Gemini ⭐ (best_bet)
   - Gemini 2. tercih (alt_bet)
   - Python ⭐ (python_best_bet)
   - Python 2. tercih (python_alt_bet)
3. Maç İçi Seçim: Oranı min_odds şartını sağlayan adaylardan kalibre puanı en yüksek olan seçilir.
4. Kupon Metrikleri: Kuponun toplam oranı, tahmini tutma olasılığı (win_probability) ve beklenen değeri (EV).

Oran kuralları sabit değildir; Lig Yönetimi › Ayarlar'dan (yoksa .env MIN_MATCH_ODDS / MIN_COUPON_TOTAL_ODDS)
her kupon üretiminde okunur (database.get_coupon_odds_settings).
"""
import bisect
import itertools
import math
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo
import config
import database
import rules

STRATEGY_BANKO = "banko"
STRATEGY_VALUE = "value"
STRATEGY_SURPRISE = "surprise"
VALID_STRATEGIES = (STRATEGY_BANKO, STRATEGY_VALUE, STRATEGY_SURPRISE)

MAX_MATCH_COUNT = 10
MAX_MATCHES_PER_LEAGUE = 2
# Kombinasyon araması en yüksek puanlı adaylarla ve en fazla bu kadar kombinasyonla sınırlıdır (istek süresi sabit kalır)
POOL_LIMIT = 25
MAX_COMBINATIONS = 20000
# Lig isabeti bu kadar değerlendirilmiş tahminden azsa puanı etkilemez
MIN_LEAGUE_SAMPLES = 2

# Kalibrasyon tercihin oran bandına göre ayrı hesaplanır (düşük oranlı tercihler çok daha sık tutar)
ODDS_BAND_EDGES = (1.30, 1.55, 1.80)
ODDS_BANDS = ("<1.30", "1.30-1.55", "1.55-1.80", "1.80+")
SHRINKAGE_K = 8.0

# Aday kaynakları: (pick türü, kaynak, tahmindeki tercih anahtarı)
CANDIDATE_KINDS = (
    ("best_bet", "gemini", "best_bet"),
    ("alt_bet", "gemini", "alt_bet"),
    ("python_best_bet", "python", "best_bet"),
    ("python_alt_bet", "python", "alt_bet"),
)

PICK_LABELS = {
    "best_bet": "⭐ Gemini 1. tercih",
    "alt_bet": "Gemini 2. tercih",
    "python_best_bet": "⭐ Python 1. tercih",
    "python_alt_bet": "Python 2. tercih"
}

def _today_local() -> str:
    return datetime.now(ZoneInfo(config.DISPLAY_TIMEZONE)).strftime("%d.%m.%Y")

def get_odds_band(odds: float) -> str:
    return ODDS_BANDS[bisect.bisect_right(ODDS_BAND_EDGES, odds)]

def build_calibration(history: List[Dict[str, Any]], k: float = SHRINKAGE_K) -> Dict[str, Any]:
    """
    Geçmiş tercihlerden (database.get_pick_history) hiyerarşik Bayesyen kalibrasyon tablosu kurar:
    p = (kazanan + K * önsel) / (n + K)
    - level_2: (kaynak, spor, oran bandı); önsel: o gruptaki ortalama model güveni (güven yoksa gerçekleşen oran)
    - level_1: (kaynak, spor, market, seçim, oran bandı); önsel: level_2 tahmini
    """
    groups: Dict[Tuple, Dict[str, float]] = {}
    details: Dict[Tuple, Dict[str, float]] = {}
    for h in history:
        band = get_odds_band(float(h["odds"]))
        sport = h.get("sport_type") or rules.FOOTBALL
        won = int(bool(h.get("won")))
        group = groups.setdefault((h["source"], sport, band), {"wins": 0, "count": 0, "conf_sum": 0.0, "conf_count": 0})
        detail = details.setdefault((h["source"], sport, h["market"], rules.calibration_selection(h["market"], h["selection"]), band), {"wins": 0, "count": 0})
        group["wins"] += won
        group["count"] += 1
        detail["wins"] += won
        detail["count"] += 1
        if h.get("confidence") is not None:
            group["conf_sum"] += float(h["confidence"])
            group["conf_count"] += 1

    level_2 = {}
    for key, g in groups.items():
        prior = g["conf_sum"] / g["conf_count"] / 100.0 if g["conf_count"] else g["wins"] / g["count"]
        level_2[key] = {"prob": (g["wins"] + k * prior) / (g["count"] + k), "count": g["count"]}
    level_1 = {
        key: {"prob": (d["wins"] + k * level_2[(key[0], key[1], key[4])]["prob"]) / (d["count"] + k), "count": d["count"]}
        for key, d in details.items()
    }
    return {"level_1": level_1, "level_2": level_2}

def calibrated_probability(calibration: Optional[Dict[str, Any]], source: str, sport: str, market: str, selection: str,
                           odds: float, stated_confidence: float) -> Tuple[float, int]:
    """
    Tercihin kalibre tutma olasılığı (%) ve dayandığı örnek sayısı. Önce ayrıntılı (market + seçim), yoksa grup
    (kaynak + oran bandı) tahmini; geçmiş yoksa modelin kendi güven yüzdesi (örnek sayısı 0).
    total_points için seçim sınırsız yönle ('ALT'/'ÜST') normalize edilir (rules.calibration_selection).
    """
    band = get_odds_band(odds)
    for level, key in (("level_1", (source, sport, market, rules.calibration_selection(market, selection), band)), ("level_2", (source, sport, band))):
        entry = (calibration or {}).get(level, {}).get(key)
        if entry:
            return round(entry["prob"] * 100.0, 1), entry["count"]
    return round(float(stated_confidence), 1), 0

def _base_score(calibrated_prob: float, odds: float, strategy: str) -> float:
    """Stratejiye göre temel maç puanı (kalibre olasılık %): banko = olasılık − yüksek oran cezası, value = olasılık x oran, surprise = yüksek oran."""
    if strategy == STRATEGY_VALUE:
        return (calibrated_prob / 100.0) * odds * 60.0
    if strategy == STRATEGY_SURPRISE:
        return calibrated_prob * 0.4 + min(odds, 4.5) * 20.0
    return calibrated_prob - max(0.0, (odds - 1.80) * 15.0)

def _league_weight(accuracy: Optional[float], samples: int) -> Tuple[float, str]:
    """Lig isabetine göre puan bonusu / cezası ve açıklaması."""
    if accuracy is None or samples < MIN_LEAGUE_SAMPLES:
        return 0.0, "Geçmiş veri yok (nötr)"
    for threshold, bonus, note in ((75, 20.0, "🏆 Yüksek lig başarısı"), (65, 12.0, "✓ Başarılı lig"), (50, 0.0, "Dengeli lig"),
                                   (40, -10.0, "Düşük lig isabeti")):
        if accuracy >= threshold:
            return bonus, f"{note} (%{accuracy:.0f})"
    return -25.0, f"⚠ Düşük lig başarısı (%{accuracy:.0f})"

def _match_candidates(match: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Maçın dört tercih kaynağını (Gemini ⭐/2, Python ⭐/2) ortak biçime çevirir; seçimi, oranı veya güveni olmayanlar atlanır."""
    python_prediction = match.get("python_prediction") or {}
    candidates = []
    for kind, source, bet_key in CANDIDATE_KINDS:
        if source == "python":
            bet = python_prediction.get(bet_key) or {}
        else:
            bet = {field: match.get(f"{bet_key}_{field}") for field in ("market", "selection", "odds", "confidence", "reason")}
            bet["reason"] = bet["reason"] or match.get("ai_analysis_summary")
        if bet.get("selection") and bet.get("odds") and bet.get("confidence"):
            candidates.append({"kind": kind, "source": source, "bet_key": bet_key, "market": bet["market"],
                               "selection": bet["selection"], "odds": float(bet["odds"]),
                               "confidence": float(bet["confidence"]), "ai_reason": bet.get("reason")})
    return candidates

def pick_candidates(match: Dict[str, Any], min_odds: float, calibration: Optional[Dict[str, Any]],
                    league_stats: Dict[str, Dict[int, Any]], strategy: str) -> List[Dict[str, Any]]:
    """
    Maçın oranı min_odds ve üstü adaylarını kalibre olasılık + lig isabeti ağırlığıyla puanlayıp en iyiden başlayarak döner.
    league_stats: {"gemini": {lig id: istatistik}, "python": {...}}
    """
    sport = match.get("sport_type") or rules.FOOTBALL
    scored = []
    for c in _match_candidates(match):
        if c["odds"] < min_odds:
            continue
        c["calibrated_probability"], c["calibration_samples"] = calibrated_probability(
            calibration, c["source"], sport, c["market"], c["selection"], c["odds"], c["confidence"])
        stats = league_stats.get(c["source"], {}).get(match.get("league_id"), {})
        c["league_accuracy"] = stats.get(f"{c['bet_key']}_accuracy")
        c["league_evaluated"] = stats.get(f"{c['bet_key']}_total", 0)
        bonus, c["league_note"] = _league_weight(c["league_accuracy"], c["league_evaluated"])
        c["score"] = round(_base_score(c["calibrated_probability"], c["odds"], strategy) + bonus, 2)
        scored.append(c)
    return sorted(scored, key=lambda c: c["score"], reverse=True)

def _total_odds(combo: Tuple[Dict[str, Any], ...]) -> float:
    return math.prod(c["pick"]["odds"] for c in combo)

def select_combination(scored: List[Dict[str, Any]], match_count: int, min_total_odds: float,
                       max_per_league: int = MAX_MATCHES_PER_LEAGUE) -> Optional[List[Dict[str, Any]]]:
    """
    Toplam oranı >= min_total_odds olan ve toplam puanı en yüksek kombinasyon (scored puana göre azalan sıralı olmalı).
    Lig başına en fazla max_per_league maç olan kombinasyon tercih edilir; yoksa lig sınırı esnetilir.
    """
    pool_size = min(len(scored), POOL_LIMIT)
    while pool_size > match_count and math.comb(pool_size, match_count) > MAX_COMBINATIONS:
        pool_size -= 1
    best: Dict[bool, Tuple[float, Optional[Tuple[Dict[str, Any], ...]]]] = {True: (-math.inf, None), False: (-math.inf, None)}
    for combo in itertools.combinations(scored[:pool_size], match_count):
        if _total_odds(combo) < min_total_odds:
            continue
        leagues = [c["match"]["league_id"] for c in combo]
        diverse = max(leagues.count(lid) for lid in leagues) <= max_per_league
        score = sum(c["score"] for c in combo)
        if score > best[diverse][0]:
            best[diverse] = (score, combo)
    combo = best[True][1] or best[False][1]
    return list(combo) if combo else None

def _failure(message: str, candidates_count: int, odds_rules: Dict[str, Any]) -> Dict[str, Any]:
    return {"success": False, "message": message, "candidates_count": candidates_count, "odds_rules": odds_rules, "matches": []}

def _format_match(item: Dict[str, Any]) -> Dict[str, Any]:
    m, pick = item["match"], item["pick"]
    return {
        "id": m["id"], "code": m["code"], "home_team": m["home_team"], "away_team": m["away_team"],
        "league_name": m["league_name"], "local_date": m["local_date"], "local_time": m["local_time"],
        "sport_type": m["sport_type"], "pick": pick["kind"], "pick_label": PICK_LABELS[pick["kind"]], "source": pick["source"],
        "market": pick["market"], "selection": pick["selection"], "odds": pick["odds"], "confidence": pick["confidence"],
        "calibrated_probability": pick["calibrated_probability"], "calibration_samples": pick["calibration_samples"],
        "predicted_score": m.get("predicted_score"), "coupon_score": pick["score"], "played": m["played"],
        "league_accuracy": pick["league_accuracy"], "league_evaluated": pick["league_evaluated"],
        "league_note": pick["league_note"], "ai_reason": pick["ai_reason"],
    }

def generate_coupon(match_count: int = 3, today_only: bool = True, strategy: str = STRATEGY_BANKO,
                    sport_type: Optional[str] = None, exclude_played: bool = False,
                    db_path: str = database.DB_FILE) -> Dict[str, Any]:
    """
    Ayarlardaki oran kurallarına uyan, kalibre edilmiş puanı en yüksek kuponu üretir.
    Gemini ⭐/2 ve Python ⭐/2 adayları arasından en değerli olanları seçer.
    """
    strategy = strategy if strategy in VALID_STRATEGIES else STRATEGY_BANKO
    match_count = max(1, min(match_count, MAX_MATCH_COUNT))

    with database.db_session(db_path) as conn:
        odds_rules = database.get_coupon_odds_settings(conn)
        league_stats = {"gemini": database.get_league_prediction_stats(conn),
                        "python": database.get_league_python_prediction_stats(conn)}
        history = database.get_pick_history(conn)
        matches = database.get_coupon_candidates(conn)

    min_odds, min_total_odds = odds_rules["min_match_odds"], odds_rules["min_coupon_odds"]
    calibration = build_calibration(history) if history else None

    if sport_type:
        matches = [m for m in matches if m["sport_type"] == sport_type]
    if today_only:
        today = _today_local()
        matches = [m for m in matches if m["local_date"] == today]
    if exclude_played:
        matches = [m for m in matches if not m["played"]]
    if not matches:
        return _failure("Kriterlere uygun, analiz edilmiş ve henüz başlamamış maç bulunamadı.", 0, odds_rules)

    scored = []
    for m in matches:
        candidates = pick_candidates(m, min_odds, calibration, league_stats, strategy)
        if candidates:
            scored.append({"match": m, "pick": candidates[0], "score": candidates[0]["score"]})

    if len(scored) < match_count:
        return _failure(f"Oranı en az {min_odds:.2f} olan tercihi bulunan yeterli maç yok ({len(scored)}/{match_count}). "
                        "Maç sayısını düşürün veya daha fazla maç analiz edin.", len(matches), odds_rules)

    scored.sort(key=lambda c: c["score"], reverse=True)
    selected = select_combination(scored, match_count, min_total_odds)
    if not selected:
        return _failure(f"{len(scored)} uygun maçtan toplam oranı en az {min_total_odds:.2f} olan {match_count} maçlık kupon "
                        "oluşturulamadı. Maç sayısını artırın veya stratejiyi değiştirin.", len(matches), odds_rules)

    formatted = [_format_match(item) for item in selected]
    total_odds = round(_total_odds(tuple(selected)), 2)
    win_probability = round(math.prod(m["calibrated_probability"] / 100.0 for m in formatted) * 100.0, 1)

    return {
        "success": True,
        "strategy": strategy,
        "today_only": today_only,
        "exclude_played": exclude_played,
        "odds_rules": odds_rules,
        "match_count": len(formatted),
        "total_odds": total_odds,
        "win_probability": win_probability,
        "expected_value": round(win_probability / 100.0 * total_odds, 2),
        "average_confidence": round(sum(m["confidence"] for m in formatted) / len(formatted), 1),
        "candidates_count": len(matches),
        "matches": formatted,
    }

def save_played_coupon(strategy: Optional[str], items: List[Dict[str, Any]], db_path: str = database.DB_FILE) -> int:
    """
    Oynanan kuponu kaydeder. items: [{"id": maç id, "pick": "best_bet" | "alt_bet" | "python_best_bet" | "python_alt_bet"}].
    Tercih, market ve oran istemciden alınmaz; maçın tahmininden okunur (kupon üretildiği haliyle).
    Geçersiz maç / tercih varsa ValueError.
    """
    if not items:
        raise ValueError("Kuponda maç yok.")
    match_ids = [item.get("id") for item in items]
    if len(set(match_ids)) != len(match_ids):
        raise ValueError("Kuponda aynı maç birden fazla kez var.")
    with database.db_session(db_path) as conn:
        rows = []
        for item in items:
            kind = item.get("pick")
            if kind not in PICK_LABELS:
                raise ValueError(f"Geçersiz tercih türü: {kind}")
            mid = int(item["id"])
            if kind in ("python_best_bet", "python_alt_bet"):
                py_pred = database.get_python_prediction(conn, mid)
                sub_key = "best_bet" if kind == "python_best_bet" else "alt_bet"
                bet = py_pred.get(sub_key) if py_pred else None
            else:
                predictions = database.get_match_predictions(conn, mid)
                bet = predictions[0].get(kind) if predictions else None

            if not bet or not bet.get("odds"):
                raise ValueError(f"{item['id']} numaralı maçın {PICK_LABELS[kind]} kaydı bulunamadı.")
            rows.append({
                "match_id": mid,
                "pick": kind,
                "market": bet["market"],
                "selection": bet["selection"],
                "odds": float(bet["odds"]),
                "confidence": bet.get("confidence")
            })
        strategy = strategy if strategy in VALID_STRATEGIES else None
        return database.save_coupon(conn, strategy, rows)

def summary_prompt(coupon: Dict[str, Any]) -> str:
    """Kupon yorumu için Gemini prompt'u (sadece kuponun kendi verisiyle; Google Search kullanılmaz)."""
    lines = "\n".join(
        f"- {m.get('home_team')} - {m.get('away_team')} ({m.get('league_name')}, "
        f"{'basketbol' if rules.is_basketball(m.get('sport_type')) else 'futbol'}): "
        f"Tercih {m.get('selection')} | Oran {m.get('odds')} | Kalibre Olasılık %{m.get('calibrated_probability', m.get('confidence'))} "
        f"(Kaynak: {m.get('source', 'gemini')})"
        for m in coupon.get("matches", [])
    )
    return f"""Sen uzman bir spor veri analisti ve bahis stratejistisin. Aşağıdaki kombine kuponu incele ve
2-3 cümlelik net, Türkçe bir kupon özeti ve risk değerlendirmesi yaz (en riskli maçı belirt).

KUPONDAKİ MAÇLAR:
{lines}
Toplam oran: {coupon.get('total_odds')}
Tahmini kupon tutma olasılığı: %{coupon.get('win_probability', '—')}
Strateji: {coupon.get('strategy')}
"""
