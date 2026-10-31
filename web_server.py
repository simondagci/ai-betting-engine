"""
AI Betting Engine - Localhost Web Dashboard Sunucusu (Flask)
"""
import os
import time
from typing import Any, Dict, Optional, Tuple
from flask import Flask, render_template, jsonify, request
import database
import config
import coupon
import data_source
import gemini_engine
import match_data
import prediction_batch
import results_sync
import rules
import stats_sync

MAX_SCORE = 99
# Basketbol skorlarında üst sınır yoktur (sadece negatif olmayan tam sayı kontrolü)
MAX_SCORE_BY_SPORT = {"FOOTBALL": MAX_SCORE, "BASKETBALL": None}

app = Flask(__name__, template_folder=os.path.join(os.path.dirname(__file__), "templates"))

# Veritabanını ve gerekli migration'ları başlat
database.init_db()

def _match_not_found():
    return jsonify({"success": False, "error": "Maç bulunamadı"}), 404

def _gemini_disabled_response(conn) -> Optional[Tuple[Any, int]]:
    """Genel Gemini anahtarı kapalıysa 403 yanıtı döner; açıksa None. Gemini'ye istek atan her endpoint bunu kontrol eder."""
    if database.is_gemini_enabled(conn):
        return None
    return jsonify({
        "success": False,
        "code": "GEMINI_DISABLED",
        "error": "Gemini analizleri kapalı. Lig Yönetimi › Ayarlar sayfasından açabilirsiniz."
    }), 403

def _parse_score(data: Dict[str, Any], key: str, max_score: Optional[int] = MAX_SCORE) -> int:
    """Negatif olmayan tam sayı skoru doğrular; max_score None ise üst sınır uygulanmaz."""
    value = data.get(key)
    if isinstance(value, bool):
        raise ValueError(key)
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if not isinstance(value, int) or value < 0 or (max_score is not None and value > max_score):
        raise ValueError(key)
    return value

@app.route("/")
def index():
    """Ana dashboard sayfasını döner."""
    return render_template("index.html")

@app.route("/matches/<int:match_id>")
def match_detail_page(match_id: int):
    """Maç kayıt (detay) sayfasını döner. Veriler sayfa içinden /api/matches/<id>/details ile yüklenir."""
    return render_template("match_detail.html", match_id=match_id)

@app.route("/api/matches/unfinished")
def api_unfinished_matches():
    """Tamamlanmamış (match_completed = 0) maçları JSON olarak döner."""
    limit = request.args.get("limit", default=250, type=int)
    with database.db_session() as conn:
        database.update_expired_matches(conn)
        matches = database.get_unfinished_matches(conn, limit=limit)
    return jsonify({
        "success": True,
        "count": len(matches),
        "matches": matches
    })

@app.route("/api/matches/completed")
def api_completed_matches():
    """Tamamlanmış maçları döner. days=2 parametresi ile son 2 günde bitenler filtrelenebilir."""
    days_param = request.args.get("days", default=None)
    days = int(days_param) if days_param and days_param.isdigit() else None
    limit = request.args.get("limit", default=250, type=int)
    with database.db_session() as conn:
        database.update_expired_matches(conn)
        matches = database.get_completed_matches(conn, days_limit=days, limit=limit)
    return jsonify({
        "success": True,
        "count": len(matches),
        "days_filter": days,
        "matches": matches
    })

@app.route("/api/matches/<int:match_id>/details")
def api_match_details(match_id: int):
    """
    Maçın son maç istatistiklerini, H2H geçmişini, sakatlıkları, tahminlerini ve sonucunu döner.
    Veritabanında H2H/Son maçlar yoksa veri kaynağı (iddaa.com / Nesine) servisleri denenir.
    Gemini maç detayı / istatistik çekmek için hiçbir zaman kullanılmaz; sadece maç analizi (analyze) içindir.
    """
    with database.db_session() as conn:
        ctx = database.get_match_full_context(conn, match_id)
        if not ctx:
            return _match_not_found()

        if not ctx.get("h2h") and not ctx.get("home_last"):
            fetched = data_source.fetch_match_stats(ctx["match"]["code"], ctx["match"]["sport_type"])
            if any(fetched.values()):
                database.save_match_stats(conn, match_id, **match_data.merge_stats(ctx, fetched))
                conn.commit()
                ctx = database.get_match_full_context(conn, match_id)

        ctx["predictions"] = database.get_match_predictions(conn, match_id)
        ctx["result"] = database.get_match_result(conn, match_id)
        if ctx["result"]:
            for prediction in ctx["predictions"]:
                sport_type = ctx["match"]["sport_type"]
                prediction["evaluation"] = rules.evaluate_prediction(prediction, ctx["result"], sport_type)
        ctx["settings"] = {
            "data_source": data_source.source_name(),
            "gemini_enabled": database.is_gemini_enabled(conn),
            "require_league_approval": config.REQUIRE_LEAGUE_APPROVAL
        }

    # Almanya yerel saat dönüşümünü ekle
    database.add_local_time_fields(ctx["match"])
    return jsonify({
        "success": True,
        "data": ctx
    })

@app.route("/api/matches/<int:match_id>/sync_stats", methods=["POST"])
def api_sync_match_stats(match_id: int):
    """Veri kaynağının (iddaa.com / Nesine) istatistik servislerinden H2H, son maçlar ve puan durumunu çekip kaydeder."""
    with database.db_session() as conn:
        ctx = database.get_match_full_context(conn, match_id)
        if not ctx:
            return _match_not_found()

        fetched = data_source.fetch_match_stats(ctx["match"]["code"], ctx["match"]["sport_type"])
        if not any(fetched.values()):
            return jsonify({"success": False, "error": f"{data_source.source_name()} bu maç için istatistik verisi döndürmedi."}), 502

        # Sadece kaynağın dolu döndürdüğü alanları güncelle; boş alanlarda mevcut (örn. Gemini ile çekilmiş) veriyi koru
        database.save_match_stats(conn, match_id, **match_data.merge_stats(ctx, fetched))
    return jsonify({"success": True, "updated_fields": [key for key, value in fetched.items() if value]})

@app.route("/api/matches/<int:match_id>/result", methods=["POST"])
def api_save_match_result(match_id: int):
    """
    Biten maçın ilk yarı ve maç sonu skorunu kaydeder.
    Futbolda 3 gol ve KG durumu, basketbolda maç sonu toplam sayı döner.
    """
    data = request.get_json(silent=True) or {}
    with database.db_session() as conn:
        ctx = database.get_match_full_context(conn, match_id)
        if not ctx:
            return _match_not_found()
        sport_type = ctx["match"]["sport_type"]
        max_score = MAX_SCORE_BY_SPORT.get(sport_type, MAX_SCORE)
        try:
            ht_home, ht_away, ft_home, ft_away = (_parse_score(data, k, max_score) for k in ("ht_home", "ht_away", "ft_home", "ft_away"))
        except ValueError as e:
            limit = f"0-{max_score} arası" if max_score is not None else "0 veya daha büyük"
            return jsonify({"success": False, "error": f"Geçersiz skor değeri: {e} ({limit} tam sayı olmalı)"}), 400
        if ht_home > ft_home or ht_away > ft_away:
            return jsonify({"success": False, "error": "İlk yarı skoru maç sonu skorundan büyük olamaz."}), 400

        database.save_result(conn, match_id, ht_home, ht_away, ft_home, ft_away)

    if rules.is_basketball(sport_type):
        return jsonify({"success": True, "total_points": ft_home + ft_away})
    return jsonify({
        "success": True,
        "over_under": rules.over_under_label(ft_home, ft_away),
        "kg": rules.kg_label(ft_home, ft_away)
    })

@app.route("/api/admin/sync_bulletin", methods=["POST"])
def api_sync_bulletin():
    """
    Veri kaynağından güncel bülteni çekip yeni maçları ve oranları veritabanına kaydeder (mevcut maçlar atlanır).
    Ardından istatistiği olmayan tüm bitmemiş maçlar için arka plan istatistik işi başlatılır (ilerleme: /api/admin/jobs/stats).
    """
    data = request.get_json(silent=True) or {}
    allowed_types = ("1", "2") if data.get("include_basketball") else ("1",)

    events = data_source.fetch_bulletin()
    if not events:
        return jsonify({"success": False, "error": "Bültenden maç verisi çekilemedi. İnternet bağlantısını kontrol edin."}), 502

    with database.db_session() as conn:
        expired_count = database.update_expired_matches(conn)
        sync_res = database.sync_bulletin_matches_and_odds(
            conn,
            events,
            allowed_types,
            default_league_gemini_analysis=config.DEFAULT_LEAGUE_GEMINI_ANALYSIS
        )
    return jsonify({
        "success": True,
        "source": data_source.source_name(),
        "total_events": len(events),
        "expired": expired_count,
        "stats_job": stats_sync.start_background(),
        **sync_res
    })

MAX_RESULT_DAYS = 14

@app.route("/api/admin/sync_results", methods=["POST"])
def api_sync_results():
    """
    Tamamlanmış ama skoru girilmemiş futbol ve basketbol maçlarının (son 'days' gün) sonuçlarını arka planda çeker
    (ilerleme: /api/admin/jobs/results). Skoru zaten girilmiş maçlar tekrar güncellenmez.
    """
    if not data_source.supports_results():
        return jsonify({"success": False, "error": f"{data_source.source_name()} otomatik sonuç çekmeyi desteklemiyor."}), 400
    data = request.get_json(silent=True) or {}
    days = min(max(int(data.get("days") or 7), 1), MAX_RESULT_DAYS)

    return jsonify({"success": True, "source": data_source.source_name(), "days": days,
                    "job": results_sync.start_background(days=days)})

@app.route("/api/admin/jobs/results")
def api_results_job_status():
    """Sonuç çekme işinin ilerleme durumunu döner."""
    return jsonify({"success": True, "job": results_sync.get_status()})

@app.route("/api/admin/jobs/results/cancel", methods=["POST"])
def api_cancel_results_job():
    """Çalışan sonuç çekme işini durdurur."""
    return jsonify({"success": True, "job": results_sync.cancel()})

@app.route("/api/admin/sync_stats", methods=["POST"])
def api_sync_stats_batch():
    """İstatistiği olmayan tüm bitmemiş maçlar için arka plan istatistik işini başlatır (zaten çalışıyorsa mevcut durumu döner)."""
    return jsonify({"success": True, "source": data_source.source_name(), "job": stats_sync.start_background()})

@app.route("/api/admin/jobs/stats")
def api_stats_job_status():
    """Arka plan istatistik işinin ilerlemesi (ilerleme çubuğu için)."""
    return jsonify({"success": True, "job": stats_sync.get_status()})

@app.route("/api/admin/jobs/stats/cancel", methods=["POST"])
def api_stats_job_cancel():
    """Arka plan istatistik işini, o an işlenen maç bittikten sonra durdurur."""
    return jsonify({"success": True, "job": stats_sync.cancel()})

@app.route("/api/admin/jobs/predictions")
def api_prediction_job_status():
    """Toplu Gemini analizinin ilerlemesi ve şu an analize uygun (bitmemiş, analiz edilmemiş, verisi tam) maç sayısı."""
    return jsonify({
        "success": True,
        "job": prediction_batch.get_status(),
        "eligible": len(prediction_batch.eligible_matches())
    })

@app.route("/api/admin/jobs/predictions/start", methods=["POST"])
def api_prediction_job_start():
    """Analize uygun tüm maçlar için arka planda Gemini analizini başlatır (zaten çalışıyorsa mevcut durumu döner)."""
    with database.db_session() as conn:
        disabled = _gemini_disabled_response(conn)
        if disabled:
            return disabled
    return jsonify({"success": True, "job": prediction_batch.start_background()})

@app.route("/api/admin/jobs/predictions/cancel", methods=["POST"])
def api_prediction_job_cancel():
    """Toplu analizi, o an analiz edilen maç bittikten sonra durdurur."""
    return jsonify({"success": True, "job": prediction_batch.cancel()})

# ==================== KUPON MOTORU ====================
@app.route("/api/coupon/generate", methods=["POST"])
def api_generate_coupon():
    """
    Ayarlardaki oran kurallarına (min maç oranı, min kupon oranı) uyan kupon üretir.
    {"match_count": 3, "today_only": true, "strategy": "banko|value|surprise", "sport_type": "FOOTBALL|BASKETBALL|null",
     "exclude_played": false}  -> exclude_played: oynanan kuponlardaki maçlar tekrar önerilmez
    """
    data = request.get_json(silent=True) or {}
    try:
        match_count = int(data.get("match_count") or 3)
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "Maç sayısı sayı olmalıdır."}), 400
    sport_type = data.get("sport_type")
    return jsonify(coupon.generate_coupon(
        match_count=match_count,
        today_only=bool(data.get("today_only", True)),
        strategy=str(data.get("strategy") or coupon.STRATEGY_BANKO).lower(),
        sport_type=sport_type if sport_type in (rules.FOOTBALL, rules.BASKETBALL) else None,
        exclude_played=bool(data.get("exclude_played", False)),
    ))

@app.route("/api/coupons", methods=["GET"])
def api_list_coupons():
    """Kaydedilen kuponlar; her tercih maç sonucuna göre tuttu / yattı / bekliyor olarak değerlendirilir."""
    with database.db_session() as conn:
        return jsonify({"success": True, **database.get_coupons(conn)})

@app.route("/api/coupons", methods=["POST"])
def api_save_coupon():
    """Oynanan kuponu kaydeder: {"strategy": "banko", "matches": [{"id": 12, "pick": "best_bet"}, ...]}."""
    data = request.get_json(silent=True) or {}
    try:
        coupon_id = coupon.save_played_coupon(data.get("strategy"), data.get("matches") or [])
    except (TypeError, ValueError, KeyError) as e:
        return jsonify({"success": False, "error": str(e) or "Geçersiz kupon."}), 400
    return jsonify({"success": True, "coupon_id": coupon_id})

@app.route("/api/coupons/<int:coupon_id>", methods=["DELETE"])
def api_delete_coupon(coupon_id: int):
    with database.db_session() as conn:
        if not database.delete_coupon(conn, coupon_id):
            return jsonify({"success": False, "error": "Kupon bulunamadı."}), 404
    return jsonify({"success": True})

@app.route("/api/coupon/ai_summary", methods=["POST"])
def api_coupon_ai_summary():
    """Üretilen kupon için Gemini'den kısa değerlendirme alır (sadece butona basınca)."""
    data = request.get_json(silent=True) or {}
    if not data.get("matches"):
        return jsonify({"success": False, "error": "Kuponda maç bulunamadı."}), 400
    with database.db_session() as conn:
        disabled = _gemini_disabled_response(conn)
        if disabled:
            return disabled
    try:
        response = gemini_engine.get_gemini_client().models.generate_content(
            model=config.GEMINI_MODEL, contents=coupon.summary_prompt(data))
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    return jsonify({"success": True, "summary": (response.text or "").strip() or "Değerlendirme üretilemedi."})

@app.route("/api/matches/<int:match_id>/analyze", methods=["POST"])
def api_analyze_single_match(match_id: int):
    """
    Tekil bir maç için anlık Gemini analizi yapar.
    Son maçlar / H2H verisi yoksa (veri kaynağından bir kez daha denenir) Gemini'ye gönderilmez: 409 (MISSING_STATS).
    REQUIRE_LEAGUE_APPROVAL açıksa ve ligin Gemini izni kapalıysa 409 (LEAGUE_DISABLED) döner;
    istek gövdesinde {"enable_league": true} gönderilirse önce lig izni açılır, sonra analiz yapılır.
    """
    data = request.get_json(silent=True) or {}
    with database.db_session() as conn:
        if not database.get_match_full_context(conn, match_id):
            return _match_not_found()
        disabled = _gemini_disabled_response(conn)
        if disabled:
            return disabled

        ctx, missing = match_data.load_context_for_analysis(conn, match_id)
        if missing:
            return jsonify({
                "success": False,
                "code": "MISSING_STATS",
                "missing": missing,
                "error": match_data.missing_stats_message(missing)
            }), 409

        match = ctx["match"]
        if config.REQUIRE_LEAGUE_APPROVAL and match["league_id"] and not match["league_gemini_analysis"]:
            if not data.get("enable_league"):
                return jsonify({
                    "success": False,
                    "code": "LEAGUE_DISABLED",
                    "league_id": match["league_id"],
                    "league_name": match["league_name"],
                    "error": f"'{match['league_name']}' ligi için Gemini analiz izni kapalı."
                }), 409
            database.set_league_gemini_analysis(conn, match["league_id"], True)
            conn.commit()

        try:
            prediction = gemini_engine.generate_prediction(ctx, model_name=config.GEMINI_MODEL)
            database.save_prediction(conn, match_id, prediction)
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500
    return jsonify({
        "success": True,
        "prediction": prediction
    })

@app.route("/api/admin/leagues")
def api_admin_leagues():
    """Admin sekmesi için tüm ligleri, toplam ve bitmemiş maç sayılarıyla listeler."""
    q = request.args.get("q", default="")
    with database.db_session() as conn:
        leagues = database.get_leagues_admin(conn, query=q)
    return jsonify({
        "success": True,
        "count": len(leagues),
        "leagues": leagues
    })

@app.route("/api/admin/leagues/<int:league_id>/toggle", methods=["POST"])
def api_toggle_league(league_id: int):
    """Ligin Gemini analiz iznini (Açık/Kapalı) günceller."""
    data = request.get_json(silent=True) or {}
    with database.db_session() as conn:
        # Eğer enabled açıkça belirtildiyse onu kullan, yoksa tersine çevir
        if "enabled" in data:
            new_val = bool(data["enabled"])
        else:
            new_val = not database.is_league_analysis_enabled(conn, league_id)
        success = database.set_league_gemini_analysis(conn, league_id, new_val)
    return jsonify({
        "success": success,
        "league_id": league_id,
        "enabled": new_val
    })

@app.route("/api/admin/leagues/toggle_all", methods=["POST"])
def api_toggle_all_leagues():
    """Tüm liglerin Gemini analiz iznini topluca açar veya kapatır."""
    data = request.get_json(silent=True) or {}
    enabled = bool(data.get("enabled", True))
    with database.db_session() as conn:
        count = database.set_all_leagues_gemini_analysis(conn, enabled)
    return jsonify({
        "success": True,
        "updated_count": count,
        "enabled": enabled
    })

@app.route("/api/admin/leagues/<int:league_id>/analyze", methods=["POST"])
def api_analyze_league_matches(league_id: int):
    """
    Belirtilen ligdeki tüm bitmemiş ve henüz analiz edilmemiş maçları Gemini AI ile analiz eder.
    Son maçlar / H2H verisi olmayan maçlar Gemini'ye gönderilmez, 'skipped' listesinde raporlanır.
    Ayrıca ligin analiz iznini de otomatik olarak aktif eder.
    """
    analyzed_results = []
    errors = []
    skipped = []

    with database.db_session() as conn:
        disabled = _gemini_disabled_response(conn)
        if disabled:
            return disabled

        # 1. Ligin analiz iznini aktif et
        database.set_league_gemini_analysis(conn, league_id, True)
        conn.commit()

        # 2. Ligin bitmemiş maçlarını bul
        matches = database.get_unfinished_matches_by_league(conn, league_id)
        if not matches:
            return jsonify({
                "success": True,
                "message": "Bu ligde analiz edilecek bitmemiş maç bulunamadı.",
                "analyzed_count": 0,
                "results": []
            })

        for m in matches:
            # Zaten analiz edildiyse atla
            if m["status"] == "ANALYZED":
                continue

            match_id = m["id"]
            ctx, missing = match_data.load_context_for_analysis(conn, match_id)
            if not ctx:
                continue
            if missing:
                skipped.append({"code": m["code"], "teams": f"{m['home_team']} vs {m['away_team']}", "missing": missing})
                continue

            try:
                pred = gemini_engine.generate_prediction(ctx, model_name=config.GEMINI_MODEL)
                database.save_prediction(conn, match_id, pred)
                # Uzun süren toplu analizde her tahmini anında kalıcı hale getir
                conn.commit()
                analyzed_results.append({
                    "match_id": match_id,
                    "code": m["code"],
                    "teams": f"{m['home_team']} vs {m['away_team']}",
                    "score": pred.get("predicted_score"),
                    "confidence": pred.get("confidence_score")
                })
                time.sleep(config.BATCH_REQUEST_DELAY)
            except Exception as e:
                errors.append(f"{m['code']}: {str(e)}")

    return jsonify({
        "success": True,
        "league_id": league_id,
        "analyzed_count": len(analyzed_results),
        "skipped_count": len(skipped),
        "skipped": skipped,
        "results": analyzed_results,
        "errors": errors
    })

@app.route("/api/leagues/accuracy")
def api_get_leagues_accuracy():
    """Liglere göre Tercih 1 (⭐) ve 2. Tercih (alt_bet) tutma oranlarını ve özet metrikleri döner."""
    with database.db_session() as conn:
        leagues = database.get_leagues_admin(conn)
        accuracy_overall = database.calculate_prediction_accuracy(conn)

    # Değerlendirilen maç sayısı > 0 olan ligler
    evaluated_leagues = [l for l in leagues if l.get("evaluated", 0) > 0]
    evaluated_leagues.sort(
        key=lambda x: (
            x["best_bet_accuracy"] if x["best_bet_accuracy"] is not None else -1,
            x.get("best_bet_total", 0)
        ),
        reverse=True
    )

    top_league = evaluated_leagues[0] if evaluated_leagues else None

    return jsonify({
        "success": True,
        "summary": {
            "total_evaluated_matches": accuracy_overall["total_evaluated"],
            "best_bet_correct": accuracy_overall["best_bet_correct"],
            "best_bet_total": accuracy_overall["best_bet_total"],
            "best_bet_accuracy": accuracy_overall["best_bet_accuracy"],
            "alt_bet_correct": accuracy_overall["alt_bet_correct"],
            "alt_bet_total": accuracy_overall["alt_bet_total"],
            "alt_bet_accuracy": accuracy_overall["alt_bet_accuracy"],
            "evaluated_leagues_count": len(evaluated_leagues),
            "total_leagues_count": len(leagues),
            "top_league": {
                "name": top_league["name"],
                "accuracy": top_league["best_bet_accuracy"],
                "total": top_league["best_bet_total"],
                "correct": top_league["best_bet_correct"]
            } if top_league else None
        },
        "leagues": leagues
    })

@app.route("/api/stats")
def api_stats():
    """Genel sistem ve veritabanı istatistiklerini döner."""
    with database.db_session() as conn:
        database.update_expired_matches(conn)
        stats = database.get_db_stats(conn)
        stats["completed_2days"] = len(database.get_completed_matches(conn, days_limit=2, limit=100000))
        gemini_enabled = database.is_gemini_enabled(conn)
    return jsonify({
        "success": True,
        "stats": stats,
        "config": {
            "data_source": data_source.source_name(),
            "gemini_analysis_enabled": gemini_enabled,
            "require_league_approval": config.REQUIRE_LEAGUE_APPROVAL,
            "model": config.GEMINI_MODEL
        }
    })

@app.route("/api/admin/settings")
def api_get_settings():
    """Ayarlar sayfası için panelden değiştirilebilen ve salt okunur ayarları döner."""
    with database.db_session() as conn:
        gemini = database.get_gemini_setting(conn)
        coupon_odds = database.get_coupon_odds_settings(conn)
        stats = database.get_db_stats(conn)
    return jsonify({
        "success": True,
        "gemini": gemini,
        "coupon_odds": coupon_odds,
        "readonly": {
            "data_source": data_source.source_name(),
            "model": config.GEMINI_MODEL,
            "require_league_approval": config.REQUIRE_LEAGUE_APPROVAL,
            "default_league_gemini_analysis": bool(config.DEFAULT_LEAGUE_GEMINI_ANALYSIS),
            "min_confidence_for_coupon": config.MIN_CONFIDENCE_FOR_COUPON,
            "batch_request_delay": config.BATCH_REQUEST_DELAY,
            "api_key_configured": bool(config.GEMINI_API_KEY),
            "enabled_leagues": stats["enabled_leagues"],
            "leagues": stats["leagues"]
        }
    })

@app.route("/api/admin/settings/gemini", methods=["POST"])
def api_set_gemini_setting():
    """
    Genel Gemini analiz anahtarını günceller.
    {"enabled": true|false} panel değerini kaydeder; {"enabled": null} .env varsayılanına döndürür.
    """
    data = request.get_json(silent=True) or {}
    if "enabled" not in data or not (data["enabled"] is None or isinstance(data["enabled"], bool)):
        return jsonify({"success": False, "error": "'enabled' alanı true, false veya null olmalı."}), 400

    with database.db_session() as conn:
        database.set_gemini_enabled(conn, data["enabled"])
        gemini = database.get_gemini_setting(conn)
    return jsonify({"success": True, "gemini": gemini})

@app.route("/api/admin/settings/coupon_odds", methods=["POST"])
def api_set_coupon_odds_settings():
    """
    Kupon oran kurallarını kaydeder: {"min_match_odds": 1.30, "min_coupon_odds": 2.00}.
    Bir değer null gönderilirse o kural .env varsayılanına döner.
    """
    data = request.get_json(silent=True) or {}
    values = {}
    for key in ("min_match_odds", "min_coupon_odds"):
        try:
            values[key] = None if data.get(key) is None else float(data[key])
        except (TypeError, ValueError):
            return jsonify({"success": False, "error": f"'{key}' sayı olmalıdır."}), 400
    with database.db_session() as conn:
        database.set_coupon_odds_settings(conn, values["min_match_odds"], values["min_coupon_odds"])
        odds = database.get_coupon_odds_settings(conn)
    return jsonify({"success": True, "coupon_odds": odds})

def start_server(host: str = None, port: int = None, debug: bool = False):
    """Flask web sunucusunu başlatır."""
    h = host or config.WEB_HOST
    p = port or config.WEB_PORT
    print(f"\n🚀 AI Betting Engine Web Dashboard Başlatılıyor...")
    print(f"👉 Tarayıcınızdan açın: http://{h}:{p}")
    print(f"🛑 Sunucuyu durdurmak için CTRL+C tuşlarına basabilirsiniz.\n")
    app.run(host=h, port=p, debug=debug)

if __name__ == "__main__":
    start_server(debug=True)
