"""
İş Kuralları (Gemini.md §3) - Maç sonucu, 3 Gol Alt/Üst ve Karşılıklı Gol hesaplamalarının tek kaynağı.
"""
from typing import Any, Dict

# Alt / Üst sınırı: toplam gol 3'ten fazla (>= 4) ise ÜST, 3 veya daha az ise ALT
OVER_UNDER_GOAL_LIMIT = 3

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

def evaluate_prediction(prediction: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, bool]:
    """
    Bir tahmini gerçek maç sonucuyla karşılaştırır.
    prediction: predicted_* alanları, result: ht_/ft_ home/away skor alanları (predictions / results tabloları).
    """
    ht_h, ht_a = result["ht_home_score"], result["ht_away_score"]
    ft_h, ft_a = result["ft_home_score"], result["ft_away_score"]
    return {
        "ht": is_result_prediction_correct(result_label("İY", ht_h, ht_a), prediction.get("predicted_ht_result"), "İY"),
        "ft": is_result_prediction_correct(result_label("MS", ft_h, ft_a), prediction.get("predicted_ft_result"), "MS"),
        "over_under": is_label_prediction_correct(over_under_label(ft_h, ft_a), prediction.get("predicted_over_under_3")),
        "kg": is_label_prediction_correct(kg_label(ft_h, ft_a), prediction.get("predicted_kg")),
        "score": is_score_prediction_correct(ft_h, ft_a, prediction.get("predicted_score")),
    }
