"""
AI Betting Engine - Localhost Web Dashboard Sunucusu (Flask)
"""
import os
import sys
import time
from flask import Flask, render_template, jsonify, request
import database
import config
import nesine_api
import gemini_engine

app = Flask(__name__, template_folder=os.path.join(os.path.dirname(__file__), "templates"))

# Veritabanını ve gerekli migration'ları başlat
database.init_db()

@app.route("/")
def index():
    """Ana dashboard sayfasını döner."""
    return render_template("index.html")

@app.route("/api/matches/unfinished")
def api_unfinished_matches():
    """Tamamlanmamış (match_completed = 0) maçları JSON olarak döner."""
    conn = database.get_connection()
    database.update_expired_matches(conn)
    limit = request.args.get("limit", default=250, type=int)
    matches = database.get_unfinished_matches(conn, limit=limit)
    conn.close()
    return jsonify({
        "success": True,
        "count": len(matches),
        "matches": matches
    })

@app.route("/api/matches/completed")
def api_completed_matches():
    """Tamamlanmış maçları döner. days=2 parametresi ile son 2 günde bitenler filtrelenebilir."""
    conn = database.get_connection()
    database.update_expired_matches(conn)
    days_param = request.args.get("days", default=None)
    days = int(days_param) if days_param and days_param.isdigit() else None
    limit = request.args.get("limit", default=250, type=int)
    matches = database.get_completed_matches(conn, days_limit=days, limit=limit)
    conn.close()
    return jsonify({
        "success": True,
        "count": len(matches),
        "days_filter": days,
        "matches": matches
    })

@app.route("/api/matches/<int:match_id>/details")
def api_match_details(match_id: int):
    """
    Maçın son maç istatistiklerini, H2H geçmişini, sakatlıkları ve tahminini döner.
    Eğer veritabanında H2H/Son maçlar yoksa anlık olarak çekmeye çalışır.
    """
    conn = database.get_connection()
    ctx = database.get_match_full_context(conn, match_id)
    if not ctx:
        conn.close()
        return jsonify({"success": False, "error": "Maç bulunamadı"}), 404

    # Eğer istatistikler henüz çekilmemişse önce Nesine, olmazsa Gemini Canlı Arama ile çek
    if not ctx.get("h2h") and not ctx.get("home_last"):
        m = ctx["match"]
        code = m["code"]
        home_team = m["home_team"]
        away_team = m["away_team"]
        
        # 1. Önce Nesine API'yi dene
        h2h = nesine_api.fetch_competition_history(code)
        last_m = nesine_api.fetch_last_matches(code)
        missing = nesine_api.fetch_missing_players(code)
        table = nesine_api.fetch_league_table(code)
        
        home_last = last_m.get("HomeTeamMatches") if isinstance(last_m, dict) else None
        away_last = last_m.get("AwayTeamMatches") if isinstance(last_m, dict) else None
        
        # 2. Eğer Nesine verisi boşsa (sunucu 500 hatası veriyorsa), Gemini Canlı Arama ile internetten çek
        if not h2h and not home_last:
            live = gemini_engine.fetch_live_team_stats(home_team, away_team)
            h2h = live.get("h2h") or []
            home_last = live.get("home_last") or []
            away_last = live.get("away_last") or []
            
        database.save_match_stats(conn, match_id, h2h, home_last, away_last, missing, table)
        conn.commit()
        ctx = database.get_match_full_context(conn, match_id)
        
    # Almanya yerel saat dönüşümünü ekle
    loc_d, loc_t = database.convert_tr_to_local_time(ctx["match"]["match_date"], ctx["match"]["match_time"])
    ctx["match"]["local_date"] = loc_d
    ctx["match"]["local_time"] = loc_t
    ctx["match"]["tr_date"] = ctx["match"]["match_date"]
    ctx["match"]["tr_time"] = ctx["match"]["match_time"]

    conn.close()
    return jsonify({
        "success": True,
        "data": ctx
    })

@app.route("/api/matches/<int:match_id>/refresh_stats", methods=["POST"])
def api_refresh_match_stats(match_id: int):
    """Google Search ile maçın H2H ve son maç verilerini canlı olarak internetten çeker ve veritabanına kaydeder."""
    conn = database.get_connection()
    ctx = database.get_match_full_context(conn, match_id)
    if not ctx:
        conn.close()
        return jsonify({"success": False, "error": "Maç bulunamadı"}), 404
        
    m = ctx["match"]
    live = gemini_engine.fetch_live_team_stats(m["home_team"], m["away_team"])
    h2h = live.get("h2h") or []
    home_last = live.get("home_last") or []
    away_last = live.get("away_last") or []
    
    database.save_match_stats(conn, match_id, h2h, home_last, away_last, None, None)
    conn.commit()
    ctx = database.get_match_full_context(conn, match_id)
    conn.close()
    return jsonify({
        "success": True,
        "data": ctx
    })

@app.route("/api/matches/<int:match_id>/analyze", methods=["POST"])
def api_analyze_single_match(match_id: int):
    """Tekil bir maç için anlık Gemini 3.8 Flash analizi yapar."""
    conn = database.get_connection()
    ctx = database.get_match_full_context(conn, match_id)
    if not ctx:
        conn.close()
        return jsonify({"success": False, "error": "Maç bulunamadı"}), 404

    try:
        prediction = gemini_engine.generate_prediction(ctx, model_name=config.GEMINI_MODEL)
        database.save_prediction(conn, match_id, prediction)
        conn.commit()
        conn.close()
        return jsonify({
            "success": True,
            "prediction": prediction
        })
    except Exception as e:
        conn.close()
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/admin/leagues")
def api_admin_leagues():
    """Admin sekmesi için tüm ligleri, toplam ve bitmemiş maç sayılarıyla listeler."""
    conn = database.get_connection()
    q = request.args.get("q", default="")
    leagues = database.get_leagues_admin(conn, query=q)
    conn.close()
    return jsonify({
        "success": True,
        "count": len(leagues),
        "leagues": leagues
    })

@app.route("/api/admin/leagues/<int:league_id>/toggle", methods=["POST"])
def api_toggle_league(league_id: int):
    """Ligin Gemini analiz iznini (Açık/Kapalı) günceller."""
    data = request.get_json(silent=True) or {}
    conn = database.get_connection()
    
    # Eğer enabled açıkça belirtildiyse onu kullan, yoksa tersine çevir
    if "enabled" in data:
        new_val = bool(data["enabled"])
    else:
        current = database.is_league_analysis_enabled(conn, league_id)
        new_val = not current
        
    success = database.set_league_gemini_analysis(conn, league_id, new_val)
    conn.commit()
    conn.close()
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
    conn = database.get_connection()
    count = database.set_all_leagues_gemini_analysis(conn, enabled)
    conn.commit()
    conn.close()
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
    conn = database.get_connection()
    
    # 1. Ligin analiz iznini aktif et
    database.set_league_gemini_analysis(conn, league_id, True)
    conn.commit()
    
    # 2. Ligin bitmemiş maçlarını bul
    matches = database.get_unfinished_matches_by_league(conn, league_id)
    if not matches:
        conn.close()
        return jsonify({
            "success": True,
            "message": "Bu ligde analiz edilecek bitmemiş maç bulunamadı.",
            "analyzed_count": 0,
            "results": []
        })
        
    analyzed_results = []
    errors = []
    
    for m in matches:
        match_id = m["id"]
        # Zaten analiz edildiyse atla
        if m["status"] == "ANALYZED":
            continue
            
        ctx = database.get_match_full_context(conn, match_id)
        if not ctx:
            continue
            
        try:
            pred = gemini_engine.generate_prediction(ctx, model_name=config.GEMINI_MODEL)
            database.save_prediction(conn, match_id, pred)
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
            
    conn.close()
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
    conn = database.get_connection()
    database.update_expired_matches(conn)
    stats = database.get_db_stats(conn)
    stats["completed_2days"] = len(database.get_completed_matches(conn, days_limit=2))
    conn.close()
    return jsonify({
        "success": True,
        "stats": stats,
        "config": {
            "gemini_analysis_enabled": config.ENABLE_GEMINI_ANALYSIS,
            "require_league_approval": config.REQUIRE_LEAGUE_APPROVAL,
            "model": config.GEMINI_MODEL
        }
    })

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
