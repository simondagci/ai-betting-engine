"""
Kupon motoru: Gemini'nin analiz ettiği, henüz başlamamış maçlardan kombine kupon önerir.

Oran kuralları sabit değildir; Lig Yönetimi › Ayarlar'dan (yoksa .env MIN_MATCH_ODDS / MIN_COUPON_TOTAL_ODDS)
her kupon üretiminde okunur (database.get_coupon_odds_settings):
- Maç tercihi: ⭐ en güçlü tercih; oranı minimum maç oranının altındaysa 2. tercih; ikisi de altındaysa maç kupona girmez.
- Kuponun toplam oranı minimum kupon oranının altında olamaz.

Seçilen tercihin türüne göre (⭐ / 2. tercih) ligin geçmiş isabet oranı puana bonus / ceza olarak eklenir.
Stratejiler: 'banko' (yüksek olasılık, ılımlı oran), 'value' (beklenen değer = olasılık x oran), 'surprise' (yüksek oran).
"""
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

PICK_LABELS = {"best_bet": "⭐ En güçlü tercih", "alt_bet": "2. tercih"}

def _today_local() -> str:
    return datetime.now(ZoneInfo(config.DISPLAY_TIMEZONE)).strftime("%d.%m.%Y")

def pick_selection(match: Dict[str, Any], min_odds: float) -> Optional[Dict[str, Any]]:
    """Maçın kupon tercihi: oranı min_odds ve üstüyse ⭐, değilse 2. tercih; ikisi de uygun değilse None."""
    for kind in ("best_bet", "alt_bet"):
        odds, confidence = match.get(f"{kind}_odds"), match.get(f"{kind}_confidence")
        if match.get(f"{kind}_selection") and odds and confidence and odds >= min_odds:
            return {"kind": kind, "market": match[f"{kind}_market"], "selection": match[f"{kind}_selection"],
                    "odds": float(odds), "confidence": int(confidence)}
    return None

def _base_score(pick: Dict[str, Any], strategy: str) -> float:
    odds, confidence = pick["odds"], pick["confidence"]
    if strategy == STRATEGY_VALUE:
        return confidence / 100 * odds * 60          # beklenen değer 1.20 -> 72 puan
    if strategy == STRATEGY_SURPRISE:
        return confidence * 0.4 + min(odds, 4.5) * 20
    return confidence - max(0.0, (odds - 1.80) * 15)  # banko: 1.80 üstü oranlar riskli sayılır

def _league_weight(accuracy: Optional[float], samples: int) -> Tuple[float, str]:
    """Lig isabetine göre puan bonusu / cezası ve açıklaması."""
    if accuracy is None or samples < MIN_LEAGUE_SAMPLES:
        return 0.0, "Geçmiş veri yok (nötr)"
    for threshold, bonus, note in ((75, 20.0, "🏆 Yüksek lig başarısı"), (65, 12.0, "✓ Başarılı lig"), (50, 0.0, "Dengeli lig"),
                                   (40, -10.0, "Düşük lig isabeti")):
        if accuracy >= threshold:
            return bonus, f"{note} (%{accuracy:.0f})"
    return -25.0, f"⚠ Düşük lig başarısı (%{accuracy:.0f})"

def score_candidate(match: Dict[str, Any], pick: Dict[str, Any], league_stats: Dict[str, Any],
                    strategy: str) -> Dict[str, Any]:
    """Aday maçın kupon puanı: strateji puanı + seçilen tercih türünün lig isabetine göre ağırlık."""
    kind = pick["kind"]
    accuracy, samples = league_stats.get(f"{kind}_accuracy"), league_stats.get(f"{kind}_total", 0)
    bonus, note = _league_weight(accuracy, samples)
    return {"match": match, "pick": pick, "score": round(_base_score(pick, strategy) + bonus, 2),
            "league_accuracy": accuracy, "league_evaluated": samples, "league_note": note}

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
        "sport_type": m["sport_type"], "pick": pick["kind"], "pick_label": PICK_LABELS[pick["kind"]], "market": pick["market"],
        "selection": pick["selection"], "odds": pick["odds"], "confidence": pick["confidence"],
        "predicted_score": m.get("predicted_score"), "coupon_score": item["score"], "played": m["played"],
        "league_accuracy": item["league_accuracy"], "league_evaluated": item["league_evaluated"],
        "league_note": item["league_note"],
        "ai_reason": (m.get("best_bet_reason") if pick["kind"] == "best_bet" else None) or m.get("ai_analysis_summary"),
    }

def generate_coupon(match_count: int = 3, today_only: bool = True, strategy: str = STRATEGY_BANKO,
                    sport_type: Optional[str] = None, exclude_played: bool = False,
                    db_path: str = database.DB_FILE) -> Dict[str, Any]:
    """
    Ayarlardaki oran kurallarına uyan, puanı en yüksek kuponu üretir. sport_type None ise tüm sporlar.
    exclude_played: kayıtlı (oynanan) bir kuponda yer alan maçlar tekrar önerilmez.
    """
    strategy = strategy if strategy in VALID_STRATEGIES else STRATEGY_BANKO
    match_count = max(1, min(match_count, MAX_MATCH_COUNT))

    with database.db_session(db_path) as conn:
        odds_rules = database.get_coupon_odds_settings(conn)
        league_stats = database.get_league_prediction_stats(conn)
        matches = database.get_coupon_candidates(conn)
    min_odds, min_total_odds = odds_rules["min_match_odds"], odds_rules["min_coupon_odds"]

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
        pick = pick_selection(m, min_odds)
        if pick:
            scored.append(score_candidate(m, pick, league_stats.get(m["league_id"], {}), strategy))
    if len(scored) < match_count:
        return _failure(f"Oranı en az {min_odds:.2f} olan tercihi bulunan yeterli maç yok ({len(scored)}/{match_count}). "
                        "Maç sayısını düşürün veya daha fazla maç analiz edin.", len(matches), odds_rules)

    scored.sort(key=lambda c: c["score"], reverse=True)
    selected = select_combination(scored, match_count, min_total_odds)
    if not selected:
        return _failure(f"{len(scored)} uygun maçtan toplam oranı en az {min_total_odds:.2f} olan {match_count} maçlık kupon "
                        "oluşturulamadı. Maç sayısını artırın veya stratejiyi değiştirin.", len(matches), odds_rules)

    formatted = [_format_match(item) for item in selected]
    return {
        "success": True,
        "strategy": strategy,
        "today_only": today_only,
        "exclude_played": exclude_played,
        "odds_rules": odds_rules,
        "match_count": len(formatted),
        "total_odds": round(_total_odds(tuple(selected)), 2),
        "average_confidence": round(sum(m["confidence"] for m in formatted) / len(formatted), 1),
        "candidates_count": len(matches),
        "matches": formatted,
    }

def save_played_coupon(strategy: Optional[str], items: List[Dict[str, Any]], db_path: str = database.DB_FILE) -> int:
    """
    Oynanan kuponu kaydeder. items: [{"id": maç id, "pick": "best_bet" | "alt_bet"}].
    Tercih, market ve oran istemciden alınmaz; maçın son tahmininden okunur (kupon üretildiği haliyle).
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
            predictions = database.get_match_predictions(conn, int(item["id"]))
            bet = predictions[0].get(kind) if predictions else None
            if not bet or not bet.get("odds"):
                raise ValueError(f"{item['id']} numaralı maçın {PICK_LABELS[kind]} kaydı bulunamadı.")
            rows.append({"match_id": int(item["id"]), "pick": kind, "market": bet["market"], "selection": bet["selection"],
                         "odds": float(bet["odds"]), "confidence": bet.get("confidence")})
        strategy = strategy if strategy in VALID_STRATEGIES else None
        return database.save_coupon(conn, strategy, rows)

def summary_prompt(coupon: Dict[str, Any]) -> str:
    """Kupon yorumu için Gemini prompt'u (sadece kuponun kendi verisiyle; Google Search kullanılmaz)."""
    lines = "\n".join(
        f"- {m.get('home_team')} - {m.get('away_team')} ({m.get('league_name')}, "
        f"{'basketbol' if rules.is_basketball(m.get('sport_type')) else 'futbol'}): "
        f"Tercih {m.get('selection')} | Oran {m.get('odds')} | Gemini olasılığı %{m.get('confidence')}"
        for m in coupon.get("matches", [])
    )
    return f"""Sen uzman bir spor veri analisti ve bahis stratejistisin. Aşağıdaki kombine kuponu incele ve
2-3 cümlelik net, Türkçe bir kupon özeti ve risk değerlendirmesi yaz (en riskli maçı belirt).

KUPONDAKİ MAÇLAR:
{lines}
Toplam oran: {coupon.get('total_odds')}
Strateji: {coupon.get('strategy')}
"""
