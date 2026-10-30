"""
İş Kuralları (Gemini.md §3) - Maç sonucu, Alt/Üst ve Karşılıklı Gol hesaplamalarının tek kaynağı.

Futbol: Alt/Üst sınırı sabit 3 goldür, KG uygulanır.
Basketbol: Alt/Üst ve KG yoktur; bunun yerine maç sonu toplam sayı tahmini yapılır.
"""
from typing import Any, Dict, Optional

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

# Gemini'nin "en güçlü tercih" (⭐) olarak seçebileceği marketler: anahtar -> (tahmin alanı, gösterim adı)
BEST_BET_MARKETS = {
    "ft": ("predicted_ft_result", "Maç Sonu"),
    "ht": ("predicted_ht_result", "İlk Yarı"),
    "over_under": ("predicted_over_under_3", "3 Gol Alt/Üst"),
    "kg": ("predicted_kg", "Karşılıklı Gol"),
}
BASKETBALL_BEST_BET_MARKETS = ("ft", "ht")

def best_bet_market_key(market: Optional[str], sport_type: str = FOOTBALL) -> Optional[str]:
    """Gemini'nin yazdığı market adını ('MS', 'İlk Yarı', 'ALT/ÜST', 'KG' ...) anahtara çevirir; sporda geçersizse None."""
    text = normalize_selection(market)
    if text.startswith("KG") or "KARSILIKLI" in text:
        key = "kg"
    elif "ALT" in text or "UST" in text:
        key = "over_under"
    elif text.startswith("IY") or text.startswith("ILK"):
        key = "ht"
    elif text.startswith("MS") or text.startswith("MAC"):
        key = "ft"
    else:
        return None
    if is_basketball(sport_type) and key not in BASKETBALL_BEST_BET_MARKETS:
        return None
    return key

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
    evaluation["best_bet"] = evaluation.get(prediction.get("best_bet_market")) if prediction.get("best_bet_market") in BEST_BET_MARKETS else None
    return evaluation
