"""
python_engine.py: Hibrit Makine Öğrenmesi (Scikit-Learn Random Forest + Poisson/xG) Tahmin Motoru

Bu modül Gemini'den %100 BAĞIMSIZDIR ve hiçbir dış API'ye (Google, OpenAI vb.) istek atmaz.
Mimarisi:
1. Temel Omurga (Backbone): Poisson / Dixon-Coles matematiksel gol beklentisi (xG) ve olasılık dağılımı.
2. Öznitelik Mühendisliği (Sıfır Sızıntı / Zero-Leakage Pre-Match Features):
   - Takımların maçtan ÖNCE oynadığı son 10 maçın istatistikleri (match_stats tablosundaki son maçlar).
   - Son maç verisi yoksa yedek: yalnızca o maçtan ÖNCE oynanmış bitmiş maçların ortalaması (_ResultsIndex).
   - Maçın kendi skoru kesinlikle öznitelik hesabına katılmaz.
   - Poisson beklenen goller (xG Ev, xG Dep, xG Farkı), MS / 3 Gol Alt/Üst / KG olasılıkları, form puanları (PPG)
3. Makine Öğrenmesi (Scikit-Learn Random Forest):
   - Bitmiş maçlar tarih sırasıyla Walk-Forward ile tahmin edilir: her dilim yalnızca kendinden ÖNCE oynanmış
     maçlarla eğitilir (gelecek, geçmişi tahmin etmez; canlı kullanımdaki koşulun aynısı). Eğitim için ayrılan
     ilk maçlar saf Poisson (xG) ile tahmin edilir.
   - Gelecek maçlar için tüm tamamlanmış geçmiş maçlardan öğrenen üretim modeli kullanılır.
4. Güven yüzdesi kalibredir: modelin ham olasılığıdır (ek puan / alt sınır yok). İY olasılıkları Poisson İY matrisinden gelir.
"""
import json
import math
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple
import config
import database
import rules

try:
    from sklearn.ensemble import RandomForestClassifier
    SKLEARN_AVAILABLE = True
except ImportError:
    SKLEARN_AVAILABLE = False

# Maksimum hesaplanacak gol sınırı (0-7 gol arası matris futbol maçlarının %99.8'ini kapsar)
MAX_GOALS_MATRIX = 7

# Random Forest için gereken en az bitmiş maç sayısı ve üretim modeli önbellek süresi (sn)
MIN_TRAINING_MATCHES = 15
MODEL_CACHE_SECONDS = 1800

# Walk-Forward: ilk maçların beşte biri (en az MIN_TRAINING_MATCHES) sadece eğitim içindir; kalanı en çok bu kadar dilime bölünür
WALK_FORWARD_WARMUP_RATIO = 5
WALK_FORWARD_BLOCKS = 10
WALK_FORWARD_MIN_BLOCK = 10

_FOOTBALL_COMPLETED_WHERE = "m.sport_type = 'FOOTBALL' AND r.ft_home_score IS NOT NULL AND r.ft_away_score IS NOT NULL"

# Eğitilmiş üretim modeli önbelleği (Hafıza içi hızlı tahmin için)
_model_cache: Dict[str, Any] = {"models": None, "trained_count": 0, "last_trained_at": 0.0}

def poisson_pmf(k: int, lamb: float) -> float:
    """Poisson olasılık kütle fonksiyonu: P(k; lamb) = (lamb^k * e^-lamb) / k!"""
    if lamb <= 0:
        return 1.0 if k == 0 else 0.0
    return (math.pow(lamb, k) * math.exp(-lamb)) / math.factorial(k)

def _confidence(probability: float) -> int:
    """Model olasılığını yüzdeye çevirir (kalibre: ek puan ve alt sınır yok)."""
    return int(max(1, min(99, round(probability * 100))))

def _sort_key(chrono_date: str, match_time: Optional[str]) -> str:
    return f"{chrono_date} {match_time or ''}"

def _parse_last_matches_stats(matches_list: Optional[List[Dict[str, Any]]], team_name: str) -> Tuple[float, float, float, float, float]:
    """
    Maçtan önce kaydedilmiş son maçlar listesinden (pre-match data) takımın:
    (ortalama_atılan, ortalama_yenilen, ppg, galibiyet_oranı, beraberlik_oranı) değerlerini döner.
    Maçın kendi skoru kesinlikle bu listede yer almaz (sıfır sızıntı).
    """
    if not matches_list:
        return 1.35, 1.25, 1.30, 0.38, 0.28

    scored = []
    conceded = []
    pts = 0
    wins = draws = 0
    for m in matches_list[:10]:
        sc = m.get("score") or ""
        if "-" in sc:
            parts = sc.split("-")
            try:
                hg = int(parts[0].strip())
                ag = int(parts[1].strip())
                is_home = (m.get("home") == team_name)
                s = hg if is_home else ag
                c = ag if is_home else hg
                scored.append(s)
                conceded.append(c)
                if s > c:
                    pts += 3
                    wins += 1
                elif s == c:
                    pts += 1
                    draws += 1
            except ValueError:
                pass

    n = max(len(scored), 1)
    return (
        sum(scored) / n if scored else 1.35,
        sum(conceded) / n if conceded else 1.25,
        pts / n,
        wins / n,
        draws / n
    )

# ==============================================================================
# VERİ YÜKLEME (toplu sorgular: döngüde SQL yok)
# ==============================================================================

class _ResultsIndex:
    """Bitmiş maçların takım bazlı skor geçmişi; ortalamalar yalnızca verilen maçtan ÖNCE oynananlardan hesaplanır."""

    def __init__(self, rows: Sequence[Dict[str, Any]]):
        self._games: Dict[Tuple[int, bool], List[Tuple[str, Optional[int], int, int]]] = {}
        for r in rows:
            self._games.setdefault((r["home_team_id"], True), []).append(
                (r["sort_key"], r["league_id"], r["ft_home_score"], r["ft_away_score"]))
            self._games.setdefault((r["away_team_id"], False), []).append(
                (r["sort_key"], r["league_id"], r["ft_away_score"], r["ft_home_score"]))

    def averages(self, team_id: int, is_home: bool, league_id: Optional[int], before_key: str) -> Tuple[float, float, int]:
        """(ortalama_atılan, ortalama_yenilen, maç_sayısı); before_key'den önceki maçlar yoksa lig varsayılanları."""
        games = [g for g in self._games.get((team_id, is_home), [])
                 if g[0] < before_key and (not league_id or g[1] == league_id)]
        if not games:
            return 1.35, 1.20, 0
        count = len(games)
        return sum(g[2] for g in games) / count, sum(g[3] for g in games) / count, count

def _load_results_index(conn) -> _ResultsIndex:
    rows = conn.execute(f"""
        SELECT m.home_team_id, m.away_team_id, m.league_id, {database._CHRONOLOGICAL_DATE} AS chrono_date, m.match_time,
               r.ft_home_score, r.ft_away_score
        FROM results r
        JOIN matches m ON m.id = r.match_id
        WHERE r.ft_home_score IS NOT NULL AND r.ft_away_score IS NOT NULL
    """).fetchall()
    return _ResultsIndex([{**dict(r), "sort_key": _sort_key(r["chrono_date"], r["match_time"])} for r in rows])

def _load_json_list(text: Optional[str]) -> Optional[List[Dict[str, Any]]]:
    if not text:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, list) else None

def _load_contexts(conn, where: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
    """
    Maç bağlamları (takımlar, tarih, son maç listeleri, varsa sonuç) tek sorguda ve KRONOLOJİK sırayla.
    where: 'm', 'ht', 'at', 'l', 'ms', 'r' takma adlarını kullanabilir.
    """
    rows = conn.execute(f"""
        SELECT m.id, m.home_team_id, m.away_team_id, m.league_id, m.sport_type,
               ht.name AS home_name, at.name AS away_name,
               {database._CHRONOLOGICAL_DATE} AS chrono_date, m.match_time,
               ms.home_last_matches_json, ms.away_last_matches_json,
               r.ft_home_score, r.ft_away_score, r.is_over_3_goals, r.is_kg_var
        FROM matches m
        JOIN teams ht ON m.home_team_id = ht.id
        JOIN teams at ON m.away_team_id = at.id
        LEFT JOIN leagues l ON l.id = m.league_id
        LEFT JOIN match_stats ms ON ms.match_id = m.id
        LEFT JOIN results r ON r.match_id = m.id
        WHERE {where}
        ORDER BY chrono_date ASC, m.match_time ASC, m.id ASC
    """, list(params)).fetchall()
    contexts = []
    for row in rows:
        ctx = dict(row)
        ctx["sort_key"] = _sort_key(ctx["chrono_date"], ctx["match_time"])
        ctx["home_last"] = _load_json_list(ctx.pop("home_last_matches_json"))
        ctx["away_last"] = _load_json_list(ctx.pop("away_last_matches_json"))
        contexts.append(ctx)
    return contexts

# ==============================================================================
# ÖZNİTELİKLER VE POISSON
# ==============================================================================

def _team_inputs(ctx: Dict[str, Any], index: _ResultsIndex) -> Tuple[float, float, float, float, float, float]:
    """(ev_atılan, ev_yenilen, ev_ppg, dep_atılan, dep_yenilen, dep_ppg): önce maç öncesi son maçlar, yoksa maçtan önceki sonuçlar."""
    if ctx["home_last"] is not None and ctx["away_last"] is not None:
        h_s, h_c, h_ppg, _, _ = _parse_last_matches_stats(ctx["home_last"], ctx["home_name"])
        a_s, a_c, a_ppg, _, _ = _parse_last_matches_stats(ctx["away_last"], ctx["away_name"])
        return h_s, h_c, h_ppg, a_s, a_c, a_ppg
    h_s, h_c, _ = index.averages(ctx["home_team_id"], True, ctx["league_id"], ctx["sort_key"])
    a_s, a_c, _ = index.averages(ctx["away_team_id"], False, ctx["league_id"], ctx["sort_key"])
    return h_s, h_c, 1.30, a_s, a_c, 1.20

def calculate_expected_goals(h_scored: float, h_conceded: float, a_scored: float, a_conceded: float) -> Tuple[float, float]:
    """Dixon-Coles / Poisson esinli beklenen gol (xG): Ev Hücum * Dep Savunma Zaafiyeti / Lig Ortalaması."""
    lambda_home = max(0.3, min((h_scored * a_conceded) / 1.25, 4.5))
    mu_away = max(0.2, min((a_scored * h_conceded) / 1.45, 4.0))
    return round(lambda_home, 2), round(mu_away, 2)

def calculate_football_probabilities(lambda_home: float, mu_away: float) -> Dict[str, Any]:
    """
    Poisson dağılımı ile 8x8 olasılık matrisi oluşturur.
    MS 1/0/2, İY 1/0/2, 3 Gol Alt/Üst, KG Var/Yok ve en olası skorları hesaplar.
    """
    matrix: Dict[Tuple[int, int], float] = {}
    prob_home = 0.0
    prob_draw = 0.0
    prob_away = 0.0
    prob_over_3 = 0.0
    prob_kg = 0.0

    best_score = "1-1"
    best_score_prob = -1.0

    best_scores_by_outcome = {
        "MS 1": ("1-0", -1.0),
        "MS 0": ("1-1", -1.0),
        "MS 2": ("0-1", -1.0)
    }

    for i in range(MAX_GOALS_MATRIX + 1):
        p_i = poisson_pmf(i, lambda_home)
        for j in range(MAX_GOALS_MATRIX + 1):
            p_j = poisson_pmf(j, mu_away)
            prob = p_i * p_j
            matrix[(i, j)] = prob

            if prob > best_score_prob:
                best_score_prob = prob
                best_score = f"{i}-{j}"

            outcome = "MS 1" if i > j else ("MS 0" if i == j else "MS 2")
            if prob > best_scores_by_outcome[outcome][1]:
                best_scores_by_outcome[outcome] = (f"{i}-{j}", prob)

            if i > j:
                prob_home += prob
            elif i == j:
                prob_draw += prob
            else:
                prob_away += prob

            if (i + j) >= 4:
                prob_over_3 += prob

            if i >= 1 and j >= 1:
                prob_kg += prob

    # İlk Yarı Beklenen Golleri (Kabaca tam maçın %45'i)
    lambda_ht = lambda_home * 0.45
    mu_ht = mu_away * 0.45

    prob_ht_1 = 0.0
    prob_ht_0 = 0.0
    prob_ht_2 = 0.0
    best_ht_score = "0-0"
    best_ht_score_prob = -1.0

    best_ht_by_outcome = {
        "İY 1": ("1-0", -1.0),
        "İY 0": ("0-0", -1.0),
        "İY 2": ("0-1", -1.0)
    }

    for i in range(5):
        p_i = poisson_pmf(i, lambda_ht)
        for j in range(5):
            p_j = poisson_pmf(j, mu_ht)
            prob = p_i * p_j
            if prob > best_ht_score_prob:
                best_ht_score_prob = prob
                best_ht_score = f"{i}-{j}"

            ht_outcome = "İY 1" if i > j else ("İY 0" if i == j else "İY 2")
            if prob > best_ht_by_outcome[ht_outcome][1]:
                best_ht_by_outcome[ht_outcome] = (f"{i}-{j}", prob)

            if i > j:
                prob_ht_1 += prob
            elif i == j:
                prob_ht_0 += prob
            else:
                prob_ht_2 += prob

    if prob_home >= prob_draw and prob_home >= prob_away:
        predicted_ft = "MS 1"
    elif prob_away >= prob_home and prob_away >= prob_draw:
        predicted_ft = "MS 2"
    else:
        predicted_ft = "MS 0"

    if prob_ht_1 >= prob_ht_0 and prob_ht_1 >= prob_ht_2:
        predicted_ht = "İY 1"
    elif prob_ht_2 >= prob_ht_1 and prob_ht_2 >= prob_ht_0:
        predicted_ht = "İY 2"
    else:
        predicted_ht = "İY 0"

    predicted_ou = "ÜST" if prob_over_3 >= 0.50 else "ALT"
    predicted_kg = "KG VAR" if prob_kg >= 0.50 else "KG YOK"

    consistent_score = best_scores_by_outcome[predicted_ft][0]
    consistent_ht_score = best_ht_by_outcome[predicted_ht][0]

    return {
        "model_type": "poisson_xg",
        "expected_home_goals": int(round(lambda_home)),
        "expected_away_goals": int(round(mu_away)),
        "raw_lambda_home": lambda_home,
        "raw_mu_away": mu_away,
        "home_win_prob": round(prob_home, 3),
        "draw_prob": round(prob_draw, 3),
        "away_win_prob": round(prob_away, 3),
        "over_3_prob": round(prob_over_3, 3),
        "kg_prob": round(prob_kg, 3),
        "predicted_ft_result": predicted_ft,
        "predicted_score": consistent_score,
        "predicted_ht_result": predicted_ht,
        "predicted_ht_score": consistent_ht_score,
        "ht_home_prob": round(prob_ht_1, 3),
        "ht_draw_prob": round(prob_ht_0, 3),
        "ht_away_prob": round(prob_ht_2, 3),
        "best_scores_by_outcome": {k: v[0] for k, v in best_scores_by_outcome.items()},
        "predicted_over_under_3": predicted_ou,
        "predicted_kg": predicted_kg,
        "confidence_score": _confidence(max(prob_home, prob_draw, prob_away))
    }

def extract_features(ctx: Dict[str, Any], index: _ResultsIndex) -> Tuple[List[float], Dict[str, Any]]:
    """
    Maçtan önce mevcut olan istatistiklerden öznitelik vektörü üretir (veritabanına gitmez).
    Sıfır sızıntı garantisi: Maçın kendi skoru ve sonrasındaki maçlar asla öznitelik içine giremez.
    """
    h_s, h_c, h_ppg, a_s, a_c, a_ppg = _team_inputs(ctx, index)
    lambda_h, mu_a = calculate_expected_goals(h_s, h_c, a_s, a_c)
    poisson_res = calculate_football_probabilities(lambda_h, mu_a)
    feats = [
        lambda_h,
        mu_a,
        lambda_h - mu_a,
        poisson_res["home_win_prob"],
        poisson_res["draw_prob"],
        poisson_res["away_win_prob"],
        poisson_res["over_3_prob"],
        poisson_res["kg_prob"],
        round(h_s, 2),
        round(h_c, 2),
        round(h_ppg, 2),
        round(a_s, 2),
        round(a_c, 2),
        round(a_ppg, 2),
        round(h_ppg - a_ppg, 2)
    ]
    return feats, poisson_res

def _labels(ctx: Dict[str, Any]) -> Tuple[str, str, str]:
    """Bitmiş maçın (MS, 3 gol Alt/Üst, KG) etiketleri."""
    ft_home, ft_away = ctx["ft_home_score"], ctx["ft_away_score"]
    ft = "MS 1" if ft_home > ft_away else ("MS 2" if ft_away > ft_home else "MS 0")
    return ft, "ÜST" if ctx["is_over_3_goals"] == 1 else "ALT", "KG VAR" if ctx["is_kg_var"] == 1 else "KG YOK"

def _fit_models(X: List[List[float]], labels: List[Tuple[str, str, str]]) -> Dict[str, Any]:
    """MS, Alt/Üst ve KG için Random Forest sınıflandırıcıları eğitir."""
    models = {}
    for position, key in enumerate(("ft", "ou", "kg")):
        models[key] = RandomForestClassifier(n_estimators=100, max_depth=4, min_samples_split=5, random_state=42)\
            .fit(X, [label[position] for label in labels])
    return models

def _train_rf_production_models(conn) -> bool:
    """
    Tamamlanmış tüm futbol maçlarından Random Forest üretim modelini eğitir (gelecek maçlar için).
    Bitmiş maçlar bu modelle değil walk-forward ile tahmin edilir.
    """
    global _model_cache
    if not SKLEARN_AVAILABLE:
        return False

    count = conn.execute(f"""
        SELECT COUNT(*) FROM matches m JOIN results r ON r.match_id = m.id WHERE {_FOOTBALL_COMPLETED_WHERE}
    """).fetchone()[0]
    if count < MIN_TRAINING_MATCHES:
        return False
    if _model_cache["models"] is not None and _model_cache["trained_count"] == count \
            and (time.time() - _model_cache["last_trained_at"]) < MODEL_CACHE_SECONDS:
        return True

    index = _load_results_index(conn)
    contexts = _load_contexts(conn, _FOOTBALL_COMPLETED_WHERE)
    X = [extract_features(ctx, index)[0] for ctx in contexts]
    try:
        _model_cache = {"models": _fit_models(X, [_labels(ctx) for ctx in contexts]),
                        "trained_count": count, "last_trained_at": time.time()}
        return True
    except ValueError:
        return False

def _hybrid_prediction(models: Dict[str, Any], feats: List[float], poisson_res: Dict[str, Any]) -> Dict[str, Any]:
    """Random Forest olasılıklarını Poisson çıktısıyla birleştirip tahmin sözlüğü üretir (güven = ham olasılık)."""
    def probabilities(key: str) -> Dict[str, float]:
        model = models[key]
        return {cls: float(p) for cls, p in zip(model.classes_, model.predict_proba([feats])[0])}

    ft, ou, kg = probabilities("ft"), probabilities("ou"), probabilities("kg")
    prob_home = ft.get("MS 1", 0.0)
    prob_draw = ft.get("MS 0", 0.0)
    prob_away = ft.get("MS 2", 0.0)
    pred_ft = max(("MS 1", prob_home), ("MS 0", prob_draw), ("MS 2", prob_away), key=lambda x: x[1])[0]
    prob_over = ou.get("ÜST", 0.0)
    prob_kg = kg.get("KG VAR", 0.0)
    return {
        **{k: poisson_res[k] for k in ("expected_home_goals", "expected_away_goals", "predicted_ht_result", "predicted_ht_score",
                                       "ht_home_prob", "ht_draw_prob", "ht_away_prob")},
        "model_type": "hybrid_rf_poisson",
        "home_win_prob": round(prob_home, 3),
        "draw_prob": round(prob_draw, 3),
        "away_win_prob": round(prob_away, 3),
        "over_3_prob": round(prob_over, 3),
        "kg_prob": round(prob_kg, 3),
        "predicted_ft_result": pred_ft,
        "predicted_score": poisson_res["best_scores_by_outcome"].get(pred_ft, poisson_res["predicted_score"]),
        "predicted_over_under_3": "ÜST" if prob_over >= 0.5 else "ALT",
        "predicted_kg": "KG VAR" if prob_kg >= 0.5 else "KG YOK",
        "confidence_score": _confidence(max(prob_home, prob_draw, prob_away))
    }

def calculate_hybrid_ml_prediction(conn, ctx: Dict[str, Any], index: Optional[_ResultsIndex] = None) -> Dict[str, Any]:
    """Üretim modeli (veya model yoksa saf Poisson) ile bir maç bağlamının tahminini üretir."""
    index = index or _load_results_index(conn)
    feats, poisson_res = extract_features(ctx, index)
    if not _train_rf_production_models(conn) or _model_cache["models"] is None:
        return poisson_res
    return _hybrid_prediction(_model_cache["models"], feats, poisson_res)

def determine_python_best_and_alt_bet(pred_data: Dict[str, Any], odds: List[Dict[str, Any]],
                                      sport_type: str = rules.FOOTBALL) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """
    Python modelinin olasılık ve xG değerlerini bülten oranlarıyla eşleştirerek
    1. Tercih (⭐ best_bet) ve gerekiyorsa 2. Tercih (alt_bet) üretir.
    """
    if not odds:
        return None, None

    all_candidates = rules.bet_candidates(odds, sport_type=sport_type, min_odds=0.0)
    if not all_candidates:
        return None, None

    p_h = pred_data.get("home_win_prob") or 0.33
    p_d = pred_data.get("draw_prob") or 0.33
    p_a = pred_data.get("away_win_prob") or 0.33
    p_ou = pred_data.get("over_3_prob") if pred_data.get("over_3_prob") is not None else 0.50
    p_kg = pred_data.get("kg_prob") if pred_data.get("kg_prob") is not None else 0.50

    prob_map = {
        ("ft", "MS 1"): p_h,
        ("ft", "MS 0"): p_d,
        ("ft", "MS 2"): p_a,
        ("over_under", "ÜST"): p_ou,
        ("over_under", "ALT"): 1.0 - p_ou,
        ("kg", "KG VAR"): p_kg,
        ("kg", "KG YOK"): 1.0 - p_kg,
        ("dc", "1-X"): min(0.98, p_h + p_d),
        ("dc", "1-2"): min(0.98, p_h + p_a),
        ("dc", "X-2"): min(0.98, p_d + p_a),
    }
    # İlk yarı olasılıkları yalnızca Poisson İY matrisinden gelir; yoksa (basketbol) İY adayı değerlendirilmez
    if pred_data.get("ht_home_prob") is not None:
        prob_map[("ht", "İY 1")] = pred_data["ht_home_prob"]
        prob_map[("ht", "İY 0")] = pred_data["ht_draw_prob"]
        prob_map[("ht", "İY 2")] = pred_data["ht_away_prob"]

    scored_candidates = []
    for c in all_candidates:
        prob = prob_map.get((c["market"], c["selection"]))
        if prob is None:
            continue
        conf = _confidence(prob)
        scored_candidates.append({
            **c,
            "confidence": conf,
            "prob": prob,
            "ev": prob * c["odds"]
        })

    best_pool = [c for c in scored_candidates if c["market"] in rules.BEST_BET_MARKETS] or scored_candidates
    if not best_pool:
        return None, None

    best_pool.sort(key=lambda x: (x["confidence"], x["ev"]), reverse=True)
    top_best = best_pool[0]

    exp_h = int(round(pred_data.get('expected_home_goals', 0)))
    exp_a = int(round(pred_data.get('expected_away_goals', 0)))
    goal_text = f"Beklenen Sayı: {exp_h} - {exp_a}" if rules.is_basketball(sport_type) else f"Beklenen Gol: {exp_h} - {exp_a}"
    best_bet = {
        "market": top_best["market"],
        "selection": top_best["selection"],
        "odds": top_best["odds"],
        "confidence": top_best["confidence"],
        "reason": f"Python Modeli: %{top_best['confidence']} model güveni ({goal_text})."
    }

    alt_bet = None
    if top_best["odds"] < config.MIN_BEST_BET_ODDS:
        alt_pool = [c for c in scored_candidates if c["odds"] >= config.MIN_BEST_BET_ODDS]
        if alt_pool:
            alt_pool.sort(key=lambda x: (x["confidence"], x["ev"]), reverse=True)
            top_alt = alt_pool[0]
            alt_bet = {
                "market": top_alt["market"],
                "selection": top_alt["selection"],
                "odds": top_alt["odds"],
                "confidence": top_alt["confidence"]
            }

    return best_bet, alt_bet

def calculate_basketball_probabilities(ctx: Dict[str, Any], index: _ResultsIndex) -> Dict[str, Any]:
    """Basketbol için (maçtan önceki) sayı ortalamaları üzerinden beklenen skor ve galip tahmini."""
    h_scored, _, _ = index.averages(ctx["home_team_id"], True, ctx["league_id"], ctx["sort_key"])
    a_scored, _, _ = index.averages(ctx["away_team_id"], False, ctx["league_id"], ctx["sort_key"])

    h_pts = h_scored if h_scored > 50 else 82.0
    a_pts = a_scored if a_scored > 50 else 78.0

    exp_home = int(round(h_pts + 3.0))
    exp_away = int(round(a_pts))

    exp_ht_home = int(round(exp_home * 0.49))
    exp_ht_away = int(round(exp_away * 0.49))

    diff = exp_home - exp_away
    prob_home = 0.50 + (diff * 0.025)
    prob_home = max(0.20, min(prob_home, 0.85))

    return {
        "model_type": "basketball_linear_pts",
        "expected_home_goals": float(exp_home),
        "expected_away_goals": float(exp_away),
        "home_win_prob": round(prob_home, 3),
        "draw_prob": 0.0,
        "away_win_prob": round(1.0 - prob_home, 3),
        "over_3_prob": None,
        "kg_prob": None,
        "predicted_ft_result": "MS 1" if exp_home >= exp_away else "MS 2",
        "predicted_score": f"{exp_home}-{exp_away}",
        "predicted_ht_result": "İY 1" if exp_ht_home > exp_ht_away else ("İY 2" if exp_ht_away > exp_ht_home else "İY 0"),
        "predicted_ht_score": f"{exp_ht_home}-{exp_ht_away}",
        "predicted_over_under_3": None,
        "predicted_kg": None,
        "confidence_score": int(max(prob_home, 1.0 - prob_home) * 100)
    }

def is_match_eligible_for_prediction(conn, match_id: int) -> Tuple[bool, Optional[str]]:
    """
    Maçın tahminleme (Gemini & Python) için uygunluğunu kontrol eder:
    Kural: Ev sahibi son maçları, deplasman son maçları ve aralarındaki H2H maçları dolu olmalıdır.
    Ayrıca lig analize açık olmalıdır.
    """
    row = conn.execute("""
        SELECT m.id, m.league_id, l.name as league_name, l.gemini_analysis,
               ms.home_last_matches_json, ms.away_last_matches_json, ms.h2h_json
        FROM matches m
        LEFT JOIN leagues l ON l.id = m.league_id
        LEFT JOIN match_stats ms ON ms.match_id = m.id
        WHERE m.id = ?
    """, (match_id,)).fetchone()

    if not row:
        return False, "Maç bulunamadı."

    if not row["home_last_matches_json"]:
        return False, "Ev sahibinin son maçları eksik."
    if not row["away_last_matches_json"]:
        return False, "Deplasmanın son maçları eksik."
    if not row["h2h_json"]:
        return False, "Aralarındaki maçlar (H2H) eksik."

    if config.REQUIRE_LEAGUE_APPROVAL and row["league_id"] and not row["gemini_analysis"]:
        return False, f"'{row['league_name']}' ligi için analiz izni kapalı."

    return True, None

def generate_python_prediction(conn, match_id: int, check_eligibility: bool = True) -> Optional[Dict[str, Any]]:
    """
    Belirtilen maç için Gemini'den bağımsız yerel Python (Hibrit RF + Poisson) tahminini üretir ve
    'python_predictions' tablosuna kaydeder.
    Gemini ile aynı uygunluk kriterleri (Ev son maçlar, Dep son maçlar, H2H, Lig izni) sağlanmalıdır.
    """
    if check_eligibility:
        eligible, _ = is_match_eligible_for_prediction(conn, match_id)
        if not eligible:
            return None

    contexts = _load_contexts(conn, "m.id = ?", (match_id,))
    if not contexts:
        return None
    ctx = contexts[0]
    index = _load_results_index(conn)
    sport_type = ctx["sport_type"] or rules.FOOTBALL
    if rules.is_basketball(sport_type):
        pred_data = calculate_basketball_probabilities(ctx, index)
    else:
        pred_data = calculate_hybrid_ml_prediction(conn, ctx, index)

    odds_rows = conn.execute("SELECT market_name, option_name, odds_value FROM odds WHERE match_id = ?", (match_id,)).fetchall()
    odds = [dict(r) for r in odds_rows]
    best_bet, alt_bet = determine_python_best_and_alt_bet(pred_data, odds, sport_type=sport_type)
    pred_data["best_bet"] = best_bet
    pred_data["alt_bet"] = alt_bet

    database.save_python_prediction(conn, match_id, pred_data)
    pred_data["match_id"] = match_id
    return pred_data

def _save_football_prediction(conn, match_id: int, pred_data: Dict[str, Any], odds: List[Dict[str, Any]]) -> None:
    pred_data["best_bet"], pred_data["alt_bet"] = determine_python_best_and_alt_bet(pred_data, odds, sport_type=rules.FOOTBALL)
    database.save_python_prediction(conn, match_id, pred_data)

def recalculate_completed_matches_walk_forward(conn) -> int:
    """
    Tüm bitmiş futbol maçlarının tahminlerini Walk-Forward ile sıfırdan hesaplar: maçlar tarih sırasına dizilir ve
    her dilim YALNIZCA kendinden önce oynanmış maçlarla eğitilen modelle tahmin edilir (canlı kullanımdaki koşul).
    Eğitim için ayrılan ilk maçlar (warm-up) saf Poisson (xG) ile tahmin edilir.
    Yalnızca Gemini koşullarını sağlayan (Ev/Dep son maçlar, H2H ve Lig izni olan) maçlar değerlendirilir.
    """
    if not SKLEARN_AVAILABLE:
        return 0

    contexts = _load_contexts(conn, f"""{_FOOTBALL_COMPLETED_WHERE}
        AND ms.home_last_matches_json IS NOT NULL AND ms.away_last_matches_json IS NOT NULL AND ms.h2h_json IS NOT NULL
        AND (l.gemini_analysis = 1 OR l.id IS NULL)""")
    if len(contexts) < MIN_TRAINING_MATCHES:
        return 0

    index = _load_results_index(conn)
    odds_by_match = database._get_odds_by_match(conn, [c["id"] for c in contexts])
    features = [extract_features(ctx, index) for ctx in contexts]
    labels = [_labels(ctx) for ctx in contexts]

    warmup = max(MIN_TRAINING_MATCHES, len(contexts) // WALK_FORWARD_WARMUP_RATIO)
    for i in range(warmup):
        _save_football_prediction(conn, contexts[i]["id"], dict(features[i][1]), odds_by_match.get(contexts[i]["id"], []))

    remaining = len(contexts) - warmup
    block_size = max(WALK_FORWARD_MIN_BLOCK, -(-remaining // WALK_FORWARD_BLOCKS)) if remaining > 0 else 0
    for start in range(warmup, len(contexts), block_size or 1):
        models = _fit_models([f[0] for f in features[:start]], labels[:start])
        for i in range(start, min(start + block_size, len(contexts))):
            feats, poisson_res = features[i]
            _save_football_prediction(conn, contexts[i]["id"], _hybrid_prediction(models, feats, poisson_res),
                                      odds_by_match.get(contexts[i]["id"], []))
    return len(contexts)

def sync_python_predictions_for_gemini_matches(conn) -> int:
    """
    Tüm maçlar için yerel Python tahminlerini senkronize eder:
    - Bitmiş maçları sıfır sızıntılı Walk-Forward ile dürüstçe hesaplar.
    - Bitmemiş maçları üretim modeliyle tahmin eder.
    """
    # 1. Önce bitmiş maçları walk-forward ile dürüstçe hesapla
    oof_count = recalculate_completed_matches_walk_forward(conn)

    # 2. Bitmemiş maçları üretim modeliyle hesapla
    unfinished = database.get_matches_for_python_sync(conn)
    unf_count = 0
    for m in unfinished:
        generate_python_prediction(conn, m["id"])
        unf_count += 1

    return oof_count + unf_count
