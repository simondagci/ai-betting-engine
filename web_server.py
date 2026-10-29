"""
AI Betting Engine - Localhost Web Dashboard Sunucusu (Flask)
"""
import os
import time
from typing import Any, Dict, Optional, Tuple
from flask import Flask, render_template, jsonify, request
import database
import config
import nesine_api
import gemini_engine
import rules

MAX_SCORE = 99

app = Flask(__name__, template_folder=os.path.join(os.path.dirname(__file__), "templates"))

# Veritabanını ve gerekli migration'ları başlat
database.init_db()

def _match_not_found():
    return jsonify({"success": False, "error": "Maç bulunamadı"}), 404

def _fetch_live_stats(match: Dict[str, Any]) -> Dict[str, Any]:
    """Gemini Google Search ile H2H ve son maç verilerini internetten çeker (save_match_stats parametre formatında)."""
    live = gemini_engine.fetch_live_team_stats(match["home_team"], match["away_team"])
    return {
        "h2h_data": live.get("h2h") or [],
        "home_last": live.get("home_last") or [],
        "away_last": live.get("away_last") or [],
        "missing": None,
        "league_table": None
    }

def _gemini_disabled_response(conn) -> Optional[Tuple[Any, int]]:
    """Genel Gemini anahtarı kapalıysa 403 yanıtı döner; açıksa None. Gemini'ye istek atan her endpoint bunu kontrol eder."""
    if database.is_gemini_enabled(conn):
        return None
    return jsonify({
        "success": False,
        "code": "GEMINI_DISABLED",
        "error": "Gemini analizleri kapalı. Lig Yönetimi › Ayarlar sayfasından açabilirsiniz."
    }), 403

def _merge_stats(ctx: Dict[str, Any], fetched: Dict[str, Any]) -> Dict[str, Any]:
    """Yeni çekilen istatistiklerden sadece dolu olanları kullanır; boş alanlarda mevcut kayıtlı veriyi korur."""
    existing = {
        "h2h_data": ctx["h2h"],
        "home_last": ctx["home_last"],
        "away_last": ctx["away_last"],
        "missing": ctx["missing"],
        "league_table": ctx["league_table"]
    }
    return {key: fetched.get(key) or existing[key] for key in existing}

def _parse_score(data: Dict[str, Any], key: str) -> int:
    value = data.get(key)
    if isinstance(value, bool):
        raise ValueError(key)
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if not isinstance(value, int) or not 0 <= value <= MAX_SCORE:
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
    Veritabanında H2H/Son maçlar yoksa sadece ücretsiz Nesine servisleri denenir.
    Gemini (Google Search) sayfa açılışında ASLA çağrılmaz; sadece 'İnternetten Tara' butonuyla (refresh_stats) çalışır.
    """
    with database.db_session() as conn:
        ctx = database.get_match_full_context(conn, match_id)
        if not ctx:
            return _match_not_found()

        if not ctx.get("h2h") and not ctx.get("home_last"):
            fetched = nesine_api.fetch_match_stats(ctx["match"]["code"])
            if any(fetched.values()):
                database.save_match_stats(conn, match_id, **_merge_stats(ctx, fetched))
                conn.commit()
                ctx = database.get_match_full_context(conn, match_id)

        ctx["predictions"] = database.get_match_predictions(conn, match_id)
        ctx["result"] = database.get_match_result(conn, match_id)
        if ctx["result"]:
            for prediction in ctx["predictions"]:
                prediction["evaluation"] = rules.evaluate_prediction(prediction, ctx["result"])
        ctx["settings"] = {
            "gemini_enabled": database.is_gemini_enabled(conn),
            "require_league_approval": config.REQUIRE_LEAGUE_APPROVAL
        }

    # Almanya yerel saat dönüşümünü ekle
    database.add_local_time_fields(ctx["match"])
    return jsonify({
        "success": True,
        "data": ctx
    })

@app.route("/api/matches/<int:match_id>/refresh_stats", methods=["POST"])
def api_refresh_match_stats(match_id: int):
    """Google Search ile maçın H2H ve son maç verilerini canlı olarak internetten çeker ve veritabanına kaydeder."""
    with database.db_session() as conn:
        ctx = database.get_match_full_context(conn, match_id)
        if not ctx:
            return _match_not_found()
        disabled = _gemini_disabled_response(conn)
        if disabled:
            return disabled

        # Gemini sadece H2H / son maçları döner; Nesine'den gelmiş eksik oyuncu ve puan durumu korunur
        database.save_match_stats(conn, match_id, **_merge_stats(ctx, _fetch_live_stats(ctx["match"])))
        conn.commit()
        ctx = database.get_match_full_context(conn, match_id)
    return jsonify({
        "success": True,
        "data": ctx
    })

@app.route("/api/matches/<int:match_id>/sync_stats", methods=["POST"])
def api_sync_match_stats(match_id: int):
    """Nesine istatistik servislerinden (H2H, son maçlar, eksikler, puan durumu) veriyi çekip kaydeder."""
    with database.db_session() as conn:
        ctx = database.get_match_full_context(conn, match_id)
        if not ctx:
            return _match_not_found()

        fetched = nesine_api.fetch_match_stats(ctx["match"]["code"])
        if not any(fetched.values()):
            return jsonify({"success": False, "error": "Nesine bu maç için istatistik verisi döndürmedi."}), 502

        # Sadece Nesine'nin dolu döndürdüğü alanları güncelle; boş alanlarda mevcut (örn. Gemini ile çekilmiş) veriyi koru
        database.save_match_stats(conn, match_id, **_merge_stats(ctx, fetched))
    return jsonify({"success": True, "updated_fields": [key for key, value in fetched.items() if value]})

@app.route("/api/matches/<int:match_id>/result", methods=["POST"])
def api_save_match_result(match_id: int):
    """Biten maçın ilk yarı ve maç sonu skorunu kaydeder (3 gol ve KG durumu otomatik hesaplanır)."""
    data = request.get_json(silent=True) or {}
    try:
        ht_home, ht_away, ft_home, ft_away = (_parse_score(data, k) for k in ("ht_home", "ht_away", "ft_home", "ft_away"))
    except ValueError as e:
        return jsonify({"success": False, "error": f"Geçersiz skor değeri: {e} (0-{MAX_SCORE} arası tam sayı olmalı)"}), 400
    if ht_home > ft_home or ht_away > ft_away:
        return jsonify({"success": False, "error": "İlk yarı skoru maç sonu skorundan büyük olamaz."}), 400

    with database.db_session() as conn:
        if not database.get_match_full_context(conn, match_id):
            return _match_not_found()
        database.save_result(conn, match_id, ht_home, ht_away, ft_home, ft_away)

    return jsonify({
        "success": True,
        "over_under": rules.over_under_label(ft_home, ft_away),
        "kg": rules.kg_label(ft_home, ft_away)
    })

@app.route("/api/admin/sync_bulletin", methods=["POST"])
def api_sync_bulletin():
    """Güncel Nesine bültenini çekip yeni maçları ve oranları veritabanına kaydeder (mevcut maçlar atlanır)."""
    data = request.get_json(silent=True) or {}
    allowed_types = ("1", "2") if data.get("include_basketball") else ("1",)

    events = nesine_api.fetch_bulletin()
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
        "total_events": len(events),
        "expired": expired_count,
        **sync_res
    })

@app.route("/api/matches/<int:match_id>/analyze", methods=["POST"])
def api_analyze_single_match(match_id: int):
    """
    Tekil bir maç için anlık Gemini analizi yapar.
    REQUIRE_LEAGUE_APPROVAL açıksa ve ligin Gemini izni kapalıysa 409 (LEAGUE_DISABLED) döner;
    istek gövdesinde {"enable_league": true} gönderilirse önce lig izni açılır, sonra analiz yapılır.
    """
    data = request.get_json(silent=True) or {}
    with database.db_session() as conn:
        ctx = database.get_match_full_context(conn, match_id)
        if not ctx:
            return _match_not_found()
        disabled = _gemini_disabled_response(conn)
        if disabled:
            return disabled

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
    Ayrıca ligin analiz iznini de otomatik olarak aktif eder.
    """
    analyzed_results = []
    errors = []

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
            ctx = database.get_match_full_context(conn, match_id)
            if not ctx:
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
        "results": analyzed_results,
        "errors": errors
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
        stats = database.get_db_stats(conn)
    return jsonify({
        "success": True,
        "gemini": gemini,
        "readonly": {
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
