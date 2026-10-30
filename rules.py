"""
İş Kuralları (Gemini.md §3) - Maç sonucu, Alt/Üst ve Karşılıklı Gol hesaplamalarının tek kaynağı.

Futbol: Alt/Üst sınırı sabit 3 goldür, KG uygulanır.
Basketbol: Alt/Üst ve KG yoktur; bunun yerine maç sonu toplam sayı tahmini yapılır.
"""
from typing import Any, Dict, List, Optional

FOOTBALL = "FOOTBALL"
BASKETBALL = "BASKETBALL"

# Futbol Alt / Üst sınırı: toplam gol 3'ten fazla (>= 4) ise ÜST, 3 veya daha az ise ALT
OVER_UNDER_GOAL_LIMIT = 3

def is_basketball(sport_type: Optional[str]) -> bool:
    return sport_type == BASKETBALL

def result_label(prefix: str, home_score: int, away_score: int) -> str:
    """Skora göre sonuç etiketini döner. prefix: 'MS' (Maç Sonu) veya 'İY' (İlk Yarı). Örn: 'MS 1', 'İY 0'."""
    if home_score > away_score:
        return f"{prefix} 1"
    if away_score > home_score:
        return f"{prefix} 2"
    return f"{prefix} 0"

def is_over_3_goals(home_score: int, away_score: int) -> bool:
    return (home_score + away_score) > OVER_UNDER_GOAL_LIMIT

def over_under_label(home_score: int, away_score: int) -> str:
    return "ÜST" if is_over_3_goals(home_score, away_score) else "ALT"

def is_kg_var(home_score: int, away_score: int) -> bool:
    return home_score > 0 and away_score > 0

def kg_label(home_score: int, away_score: int) -> str:
    return "KG VAR" if is_kg_var(home_score, away_score) else "KG YOK"

def is_result_prediction_correct(actual: str, predicted: str, prefix: str) -> bool:
    """
    'MS 1' / 'İY 0' gibi sonuç tahminlerini esnek eşleştirir.
    Gemini bazen 'MS1' veya 'MS - 1' gibi formatlar döndürdüğü için son karakter + prefix kontrolü de yapılır.
    """
    pred = (predicted or "").upper().strip()
    return actual in pred or (actual[-1] in pred and prefix in pred)

def is_label_prediction_correct(actual: str, predicted: str) -> bool:
    """'ÜST' / 'ALT' / 'KG VAR' / 'KG YOK' gibi etiket tahminlerini kontrol eder."""
    return actual in (predicted or "").upper().strip()

def is_score_prediction_correct(home_score: int, away_score: int, predicted_score: str) -> bool:
    return f"{home_score}-{away_score}" == (predicted_score or "").replace(" ", "").strip()

def normalize_selection(selection: Optional[str]) -> str:
    """'iy 1', 'İY1', ' KG  Var ' gibi yazımları karşılaştırılabilir hale getirir ('IY 1', 'KG VAR')."""
    text = (selection or "").upper()
    for turkish, ascii_char in (("İ", "I"), ("Ü", "U"), ("Ç", "C"), ("Ş", "S"), ("Ğ", "G"), ("Ö", "O")):
        text = text.replace(turkish, ascii_char)
    text = " ".join(text.split())
    for prefix in ("IY", "MS"):
        if text.startswith(prefix) and len(text) > len(prefix) and text[len(prefix)] != " ":
            text = f"{prefix} {text[len(prefix):]}"
    return text

# ⭐ Değerli tercih (value bet) adayları: market anahtarı -> (oran tablosundaki market adı, {bülten seçeneği: gösterim etiketi}, ad)
# Futbolda ALT / ÜST, 3 gol kuralı nedeniyle 3.5 baremine karşılık gelir (ÜST = 4+ gol).
BET_OPTIONS = {
    "ft": ("Maç Sonucu", {"MS 1": "MS 1", "MS 0": "MS 0", "MS 2": "MS 2"}, "Maç Sonu"),
    "ht": ("İlk Yarı Sonucu", {"İY 1": "İY 1", "İY 0": "İY 0", "İY 2": "İY 2"}, "İlk Yarı"),
    "over_under": ("3.5 Gol Alt/Üst", {"3.5 Alt": "ALT", "3.5 Üst": "ÜST"}, "3 Gol Alt/Üst"),
    "kg": ("Karşılıklı Gol", {"KG VAR": "KG VAR", "KG YOK": "KG YOK"}, "Karşılıklı Gol"),
    "dc": ("Çifte Şans", {"1-X": "1-X", "1-2": "1-2", "X-2": "X-2"}, "Çifte Şans"),
}
BASKETBALL_BET_MARKETS = ("ft", "ht")

def bet_candidates(odds: List[Dict[str, Any]], sport_type: str = FOOTBALL, min_odds: float = 0.0) -> List[Dict[str, Any]]:
    """
    Kayıtlı oranlardan ⭐ değerli tercih adaylarını çıkarır: [{"market", "selection", "market_label", "odds"}].
    Sadece oranı min_odds ve üstü olan seçenekler döner (basketbolda sadece MS ve İY).
    """
    markets = BASKETBALL_BET_MARKETS if is_basketball(sport_type) else tuple(BET_OPTIONS)
    by_market_option = {(o.get("market_name"), o.get("option_name")): o.get("odds_value") for o in odds}
    candidates = []
    for key in markets:
        market_name, options, market_label = BET_OPTIONS[key]
        for option, label in options.items():
            value = by_market_option.get((market_name, option))
            if value and value >= min_odds:
                candidates.append({"market": key, "selection": label, "market_label": market_label, "odds": value})
    return candidates

def selection_odds(odds: List[Dict[str, Any]], market: Optional[str], selection: Optional[str]) -> Optional[float]:
    """Bir ⭐ seçimin bültendeki oranı (eski tahminlerde kayıtlı oran yoksa gösterim için)."""
    if market not in BET_OPTIONS or not selection:
        return None
    market_name, options, _ = BET_OPTIONS[market]
    target = normalize_selection(selection)
    option = next((opt for opt, label in options.items() if normalize_selection(label) == target), None)
    return next((o.get("odds_value") for o in odds if o.get("market_name") == market_name and o.get("option_name") == option), None)

def selection_won(market: Optional[str], selection: Optional[str], result: Dict[str, Any]) -> Optional[bool]:
    """Bir seçimin (örn. 'ft' / 'MS 0', 'dc' / '1-X', 'over_under' / 'ÜST') maç sonucunda tutup tutmadığı; değerlendirilemezse None."""
    ft_h, ft_a = result.get("ft_home_score"), result.get("ft_away_score")
    ht_h, ht_a = result.get("ht_home_score"), result.get("ht_away_score")
    sel = normalize_selection(selection)
    if ft_h is None or ft_a is None or not sel or market not in BET_OPTIONS:
        return None
    if market == "ft":
        return sel == normalize_selection(result_label("MS", ft_h, ft_a))
    if market == "ht":
        if ht_h is None or ht_a is None:
            return None
        return sel == normalize_selection(result_label("İY", ht_h, ht_a))
    if market == "over_under":
        return ("UST" in sel) == is_over_3_goals(ft_h, ft_a)
    if market == "kg":
        return ("VAR" in sel) == is_kg_var(ft_h, ft_a)
    # Çifte şans: '1-X' deplasman kazanmazsa, 'X-2' ev sahibi kazanmazsa, '1-2' beraberlik olmazsa tutar
    outcome = {"MS 1": "1", "MS 0": "X", "MS 2": "2"}[result_label("MS", ft_h, ft_a)]
    return outcome in sel.split("-")

def evaluate_prediction(prediction: Dict[str, Any], result: Dict[str, Any],
                        sport_type: str = FOOTBALL) -> Dict[str, Optional[bool]]:
    """
    Bir tahmini gerçek maç sonucuyla karşılaştırır. Değerlendirilemeyen alanlar None döner
    (örn. ilk yarı skoru yoksa 'ht', basketbolda 'over_under' ve 'kg').
    Basketbolda 'total_diff': tahmini toplam sayı ile gerçek toplam arasındaki mutlak fark.
    'best_bet': Gemini'nin en güçlü tercihi (best_bet_market) tuttu mu; tercih yoksa None.
    prediction: predicted_* alanları, result: ht_/ft_ home/away skor alanları.
    """
    ht_h, ht_a = result.get("ht_home_score"), result.get("ht_away_score")
    ft_h, ft_a = result["ft_home_score"], result["ft_away_score"]

    ht = None
    if ht_h is not None and ht_a is not None:
        ht = is_result_prediction_correct(result_label("İY", ht_h, ht_a), prediction.get("predicted_ht_result"), "İY")

    total_diff = None
    if is_basketball(sport_type):
        over_under = kg = None
        if prediction.get("predicted_total_points") is not None:
            total_diff = abs((ft_h + ft_a) - prediction["predicted_total_points"])
    else:
        over_under = is_label_prediction_correct(over_under_label(ft_h, ft_a), prediction.get("predicted_over_under_3"))
        kg = is_label_prediction_correct(kg_label(ft_h, ft_a), prediction.get("predicted_kg"))

    evaluation = {
        "ht": ht,
        "ft": is_result_prediction_correct(result_label("MS", ft_h, ft_a), prediction.get("predicted_ft_result"), "MS"),
        "over_under": over_under,
        "kg": kg,
        "score": is_score_prediction_correct(ft_h, ft_a, prediction.get("predicted_score")),
        "total_diff": total_diff,
    }
    # ⭐ değerli tercih, tahminden farklı bir seçim olabilir (örn. tahmin MS 1, ⭐ KG VAR); seçimin kendisi değerlendirilir
    evaluation["best_bet"] = selection_won(prediction.get("best_bet_market"), prediction.get("best_bet_selection"), result)
    return evaluation
