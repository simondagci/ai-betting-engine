import sqlite3
import json
import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional, List, Dict, Any, Tuple, Set
import config
import nesine_api

DB_FILE = config.DB_FILE_PATH

def get_connection(db_path: str = DB_FILE) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn

def convert_tr_to_local_time(
    match_date_str: str, 
    match_time_str: str, 
    src_tz_name: str = None, 
    dst_tz_name: str = None
) -> Tuple[str, str]:
    """
    Türkiye (TRT, UTC+3) kaynaklı tarih ve saati, kullanıcının bulunduğu yerel saate (örn: Almanya / Berlin) dönüştürür.
    """
    if not match_date_str:
        return match_date_str, match_time_str
        
    time_str = (match_time_str or "00:00").strip()
    date_str = match_date_str.strip()
    src_tz = src_tz_name or getattr(config, "SOURCE_TIMEZONE", "Europe/Istanbul")
    dst_tz = dst_tz_name or getattr(config, "DISPLAY_TIMEZONE", "Europe/Berlin")
    
    dt = None
    for d_fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        for t_fmt in ("%H:%M", "%H:%M:%S"):
            try:
                dt = datetime.strptime(f"{date_str} {time_str}", f"{d_fmt} {t_fmt}")
                break
            except ValueError:
                continue
        if dt:
            break
            
    if not dt:
        return match_date_str, match_time_str

    try:
        dt_src = dt.replace(tzinfo=ZoneInfo(src_tz))
        dt_dst = dt_src.astimezone(ZoneInfo(dst_tz))
        return dt_dst.strftime("%d.%m.%Y"), dt_dst.strftime("%H:%M")
    except Exception:
        return match_date_str, match_time_str

def is_match_past(match_date_str: str, match_time_str: str = "", delay_minutes: Optional[int] = None) -> bool:
    """
    Maçın başlama saatinden belirtilen dakika (varsayılan: 150 dk / 2.5 saat) geçtikten sonra True döner.
    Nesine maç saati Türkiye (UTC+3) bazlı olduğu için kıyaslama Türkiye saat dilimi baz alınarak yapılır.
    """
    if not match_date_str:
        return False
    if delay_minutes is None:
        delay_minutes = getattr(config, "MATCH_COMPLETION_DELAY_MINUTES", 150)
        
    date_parts = match_date_str.strip()
    time_parts = (match_time_str or "00:00").strip()
    
    # Desteklenen tarih ve saat formatları
    for d_fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y"):
        for t_fmt in ("%H:%M", "%H:%M:%S", ""):
            dt_str = f"{date_parts} {time_parts}".strip() if t_fmt else date_parts
            fmt = f"{d_fmt} {t_fmt}".strip() if t_fmt else d_fmt
            try:
                start_dt = datetime.strptime(dt_str, fmt)
                src_tz = ZoneInfo(getattr(config, "SOURCE_TIMEZONE", "Europe/Istanbul"))
                start_dt = start_dt.replace(tzinfo=src_tz)
                now_src = datetime.now(src_tz)
                completed_dt = start_dt + timedelta(minutes=delay_minutes)
                return now_src >= completed_dt
            except ValueError:
                continue
    return False

def init_db(db_path: str = DB_FILE) -> None:
    """Oluşturulacak olan ilişkisel SQLite veritabanı şemasını başlatır ve güncellemeleri uygular."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        
        # Takımlar Tablosu
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS teams (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            sport_type TEXT NOT NULL DEFAULT 'FOOTBALL'
        );
        """)
        
        # Ligler Tablosu (gemini_analysis: 1 ise Gemini analizi yapılır, 0 ise yapılmaz)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS leagues (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            sport_type TEXT NOT NULL DEFAULT 'FOOTBALL',
            gemini_analysis INTEGER NOT NULL DEFAULT 0
        );
        """)
        
        # Mevcut veritabanları için leagues.gemini_analysis sütunu kontrolü ve migration
        cursor.execute("PRAGMA table_info(leagues);")
        columns = [row["name"] for row in cursor.fetchall()]
        if "gemini_analysis" not in columns:
            cursor.execute("ALTER TABLE leagues ADD COLUMN gemini_analysis INTEGER NOT NULL DEFAULT 0;")
        
        # Maçlar Tablosu (match_completed: Maç tamamlandı/tarihi geçti mi? Default: 0)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL UNIQUE,
            home_team_id INTEGER NOT NULL,
            away_team_id INTEGER NOT NULL,
            league_id INTEGER,
            match_date TEXT NOT NULL,
            match_time TEXT,
            sport_type TEXT NOT NULL DEFAULT 'FOOTBALL',
            status TEXT NOT NULL DEFAULT 'PENDING',
            match_completed INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (home_team_id) REFERENCES teams (id),
            FOREIGN KEY (away_team_id) REFERENCES teams (id),
            FOREIGN KEY (league_id) REFERENCES leagues (id)
        );
        """)
        
        # Mevcut veritabanları için matches.match_completed sütunu kontrolü ve migration
        cursor.execute("PRAGMA table_info(matches);")
        match_cols = [row["name"] for row in cursor.fetchall()]
        if "match_completed" not in match_cols:
            cursor.execute("ALTER TABLE matches ADD COLUMN match_completed INTEGER NOT NULL DEFAULT 0;")
        
        # Oranlar Tablosu
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS odds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id INTEGER NOT NULL,
            market_type INTEGER,
            market_name TEXT,
            option_name TEXT NOT NULL,
            odds_value REAL NOT NULL,
            FOREIGN KEY (match_id) REFERENCES matches (id)
        );
        """)
        
        # Maç Detay ve İstatistik Verileri Tablosu
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS match_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id INTEGER NOT NULL UNIQUE,
            h2h_json TEXT,
            home_last_matches_json TEXT,
            away_last_matches_json TEXT,
            missing_players_json TEXT,
            league_table_json TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (match_id) REFERENCES matches (id)
        );
        """)
        
        # Gemini AI Tahminleri Tablosu
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id INTEGER NOT NULL,
            predicted_ht_result TEXT,
            predicted_ft_result TEXT,
            predicted_over_under_3 TEXT,
            predicted_kg TEXT,
            predicted_score TEXT,
            confidence_score INTEGER,
            ai_analysis_summary TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (match_id) REFERENCES matches (id)
        );
        """)
        
        # Maç Sonuçları Tablosu (Başarı Oranı Takibi İçin)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id INTEGER NOT NULL UNIQUE,
            ht_home_score INTEGER,
            ht_away_score INTEGER,
            ft_home_score INTEGER,
            ft_away_score INTEGER,
            is_over_3_goals INTEGER,
            is_kg_var INTEGER,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (match_id) REFERENCES matches (id)
        );
        """)
        conn.commit()

        # Tarihi geçmiş maçları topluca güncelle
        update_expired_matches(conn)

def update_expired_matches(conn: sqlite3.Connection) -> int:
    """Tarihi ve saati geçmiş olan henüz tamamlanmamış maçları topluca 'match_completed = 1' olarak günceller."""
    cursor = conn.cursor()
    cursor.execute("SELECT id, match_date, match_time, status FROM matches WHERE match_completed = 0")
    rows = cursor.fetchall()
    
    to_update = []
    for r in rows:
        if is_match_past(r["match_date"], r["match_time"]):
            new_status = 'COMPLETED' if r["status"] == 'PENDING' else r["status"]
            to_update.append((new_status, r["id"]))
            
    if to_update:
        cursor.executemany("UPDATE matches SET match_completed = 1, status = ? WHERE id = ?", to_update)
        conn.commit()
        
    return len(to_update)

def get_existing_match_codes(conn: sqlite3.Connection) -> Set[str]:
    """Veritabanında kayıtlı tüm maç kodlarını küme (set) olarak döner."""
    cursor = conn.cursor()
    cursor.execute("SELECT code FROM matches")
    return {str(row["code"]) for row in cursor.fetchall()}

def get_or_create_team(conn: sqlite3.Connection, name: str, sport_type: str = "FOOTBALL") -> int:
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM teams WHERE name = ?", (name,))
    row = cursor.fetchone()
    if row:
        return row["id"]
    cursor.execute("INSERT INTO teams (name, sport_type) VALUES (?, ?)", (name, sport_type))
    return cursor.lastrowid

def get_or_create_league(conn: sqlite3.Connection, name: str, sport_type: str = "FOOTBALL", gemini_analysis: int = 0) -> int:
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM leagues WHERE name = ?", (name,))
    row = cursor.fetchone()
    if row:
        return row["id"]
    cursor.execute("INSERT INTO leagues (name, sport_type, gemini_analysis) VALUES (?, ?, ?)", (name, sport_type, gemini_analysis))
    return cursor.lastrowid

def get_leagues(conn: sqlite3.Connection, query: str = "", only_enabled: bool = False) -> List[sqlite3.Row]:
    """Ligleri, maç sayıları ve Gemini analiz izin durumuyla birlikte listeler."""
    cursor = conn.cursor()
    sql = """
        SELECT l.id, l.name, l.sport_type, l.gemini_analysis, COUNT(m.id) as match_count
        FROM leagues l
        LEFT JOIN matches m ON l.id = m.league_id
        WHERE 1=1
    """
    params = []
    if query:
        sql += " AND l.name LIKE ?"
        params.append(f"%{query}%")
    if only_enabled:
        sql += " AND l.gemini_analysis = 1"
    
    sql += " GROUP BY l.id ORDER BY l.gemini_analysis DESC, match_count DESC, l.name ASC"
    cursor.execute(sql, params)
    return cursor.fetchall()

def get_leagues_admin(conn: sqlite3.Connection, query: str = "") -> List[Dict[str, Any]]:
    """Admin paneli için ligleri toplam maç ve bitmemiş maç sayılarıyla birlikte listeler."""
    cursor = conn.cursor()
    sql = """
        SELECT 
            l.id, l.name, l.sport_type, l.gemini_analysis,
            COUNT(m.id) as total_matches,
            SUM(CASE WHEN m.match_completed = 0 THEN 1 ELSE 0 END) as unfinished_matches,
            SUM(CASE WHEN m.status = 'ANALYZED' AND m.match_completed = 0 THEN 1 ELSE 0 END) as analyzed_matches
        FROM leagues l
        LEFT JOIN matches m ON l.id = m.league_id
        WHERE 1=1
    """
    params = []
    if query:
        sql += " AND l.name LIKE ?"
        params.append(f"%{query}%")
        
    sql += " GROUP BY l.id ORDER BY unfinished_matches DESC, total_matches DESC, l.name ASC"
    cursor.execute(sql, params)
    rows = cursor.fetchall()
    return [
        {
            "id": r["id"],
            "name": r["name"],
            "sport_type": r["sport_type"],
            "gemini_analysis": bool(r["gemini_analysis"]),
            "total_matches": r["total_matches"] or 0,
            "unfinished_matches": r["unfinished_matches"] or 0,
            "analyzed_matches": r["analyzed_matches"] or 0
        }
        for r in rows
    ]

def get_unfinished_matches_by_league(conn: sqlite3.Connection, league_id: int) -> List[sqlite3.Row]:
    """Belirtilen lige ait bitmemiş (match_completed = 0) maçları getirir."""
    cursor = conn.cursor()
    cursor.execute("""
        SELECT m.id, m.code, ht.name as home_team, at.name as away_team,
               l.name as league_name, m.match_date, m.match_time, m.sport_type, m.status
        FROM matches m
        JOIN teams ht ON m.home_team_id = ht.id
        JOIN teams at ON m.away_team_id = at.id
        JOIN leagues l ON m.league_id = l.id
        WHERE m.league_id = ? AND m.match_completed = 0
        ORDER BY m.match_date ASC, m.match_time ASC
    """, (league_id,))
    return cursor.fetchall()

def set_league_gemini_analysis(conn: sqlite3.Connection, league_id: int, enabled: bool) -> bool:
    """Belirtilen ligin Gemini AI analiz iznini günceller."""
    cursor = conn.cursor()
    cursor.execute("UPDATE leagues SET gemini_analysis = ? WHERE id = ?", (1 if enabled else 0, league_id))
    return cursor.rowcount > 0

def set_all_leagues_gemini_analysis(conn: sqlite3.Connection, enabled: bool) -> int:
    """Tüm liglerin Gemini analiz iznini topluca açar veya kapatır."""
    cursor = conn.cursor()
    cursor.execute("UPDATE leagues SET gemini_analysis = ?", (1 if enabled else 0,))
    return cursor.rowcount

def is_league_analysis_enabled(conn: sqlite3.Connection, league_id: int) -> bool:
    """Ligin Gemini analiz izninin açık olup olmadığını döner."""
    cursor = conn.cursor()
    cursor.execute("SELECT gemini_analysis FROM leagues WHERE id = ?", (league_id,))
    row = cursor.fetchone()
    return bool(row["gemini_analysis"]) if row else False

def match_exists(conn: sqlite3.Connection, code: str) -> bool:
    """Verilen kodlu maçın veritabanında kayıtlı olup olmadığını kontrol eder."""
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM matches WHERE code = ?", (code,))
    return cursor.fetchone() is not None

def save_match(conn: sqlite3.Connection, code: str, home_team: str, away_team: str, 
               league_name: str, match_date: str, match_time: str, sport_type: str = "FOOTBALL",
               default_league_gemini_analysis: int = 0) -> int:
    """Tekil maç kaydı yapar (Var ise doğrudan id döner)."""
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM matches WHERE code = ?", (code,))
    row = cursor.fetchone()
    if row:
        return row["id"]

    home_id = get_or_create_team(conn, home_team, sport_type)
    away_id = get_or_create_team(conn, away_team, sport_type)
    league_id = get_or_create_league(conn, league_name, sport_type, default_league_gemini_analysis) if league_name else None
    
    is_past = is_match_past(match_date, match_time)
    match_completed = 1 if is_past else 0
    status = 'COMPLETED' if is_past else 'PENDING'
    
    cursor.execute("""
        INSERT INTO matches (code, home_team_id, away_team_id, league_id, match_date, match_time, sport_type, status, match_completed)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (code, home_id, away_id, league_id, match_date, match_time, sport_type, status, match_completed))
    return cursor.lastrowid

def save_odds(conn: sqlite3.Connection, match_id: int, odds_list: List[Dict[str, Any]]) -> None:
    """Belirtilen maça ait oranları toplu (executemany) olarak kaydeder."""
    cursor = conn.cursor()
    cursor.execute("DELETE FROM odds WHERE match_id = ?", (match_id,))
    if odds_list:
        params = [
            (match_id, o.get("market_type"), o.get("market_name"), o.get("option_name"), o.get("odds_value"))
            for o in odds_list
        ]
        cursor.executemany("""
            INSERT INTO odds (match_id, market_type, market_name, option_name, odds_value)
            VALUES (?, ?, ?, ?, ?)
        """, params)

def sync_bulletin_matches_and_odds(
    conn: sqlite3.Connection, 
    events: List[Dict[str, Any]], 
    allowed_types: Tuple[str, ...],
    default_league_gemini_analysis: int = 0
) -> Dict[str, int]:
    """
    Bülten etkinliklerini hafıza içi önbellek ve toplu SQL (executemany) ile yüksek performansla senkronize eder.
    DÖNGÜ İÇERİSİNDE HİÇBİR TEKİL SQL SORGUSU ÇALIŞTIRILMAZ.
    """
    cursor = conn.cursor()
    
    # 1. Mevcut verileri tek seferde hafıza içi sözlüklere (in-memory cache) al
    cursor.execute("SELECT id, name FROM teams")
    teams_cache = {row["name"]: row["id"] for row in cursor.fetchall()}
    
    cursor.execute("SELECT id, name FROM leagues")
    leagues_cache = {row["name"]: row["id"] for row in cursor.fetchall()}
    
    cursor.execute("SELECT id, code FROM matches")
    matches_cache = {str(row["code"]): row["id"] for row in cursor.fetchall()}
    
    # 2. Bülten etkinliklerini filtrele ve yeni verileri belirle
    new_events = []
    needed_teams = set()
    needed_leagues = set()
    skipped_count = 0
    
    for event in events:
        sport_type_val = str(event.get("TYPE", ""))
        if sport_type_val not in allowed_types:
            continue
        code = str(event.get("C", ""))
        if not code or code == "None":
            continue
        # Zaten veritabanında varsa atla (tekrar almaya gerek yok)
        if code in matches_cache:
            skipped_count += 1
            continue
        
        sport_type = "FOOTBALL" if sport_type_val == "1" else "BASKETBALL"
        home_team = event.get("HN") or event.get("ENN") or "Ev Sahibi"
        away_team = event.get("AN") or "Deplasman"
        league_name = event.get("league_name") or event.get("ENN") or "Genel Lig"
        
        new_events.append({
            "code": code,
            "home_team": home_team,
            "away_team": away_team,
            "league_name": league_name,
            "match_date": event.get("D", ""),
            "match_time": event.get("T", ""),
            "sport_type": sport_type,
            "raw_event": event
        })
        
        if home_team not in teams_cache:
            needed_teams.add((home_team, sport_type))
        if away_team not in teams_cache:
            needed_teams.add((away_team, sport_type))
        if league_name not in leagues_cache:
            needed_leagues.add((league_name, sport_type))
            
    # 3. Yeni takımları topluca (executemany) ekle
    if needed_teams:
        cursor.executemany("INSERT OR IGNORE INTO teams (name, sport_type) VALUES (?, ?)", list(needed_teams))
        cursor.execute("SELECT id, name FROM teams")
        teams_cache = {row["name"]: row["id"] for row in cursor.fetchall()}
        
    # 4. Yeni ligleri topluca (executemany) ekle
    if needed_leagues:
        leagues_params = [(name, st, default_league_gemini_analysis) for name, st in needed_leagues]
        cursor.executemany("INSERT OR IGNORE INTO leagues (name, sport_type, gemini_analysis) VALUES (?, ?, ?)", leagues_params)
        cursor.execute("SELECT id, name FROM leagues")
        leagues_cache = {row["name"]: row["id"] for row in cursor.fetchall()}
        
    # 5. Yeni maçları hazırla ve topluca (executemany) ekle
    new_matches_params = []
    football_count = 0
    basketball_count = 0
    
    for item in new_events:
        h_id = teams_cache[item["home_team"]]
        a_id = teams_cache[item["away_team"]]
        l_id = leagues_cache.get(item["league_name"])
        is_past = is_match_past(item["match_date"], item["match_time"])
        match_completed = 1 if is_past else 0
        status = 'COMPLETED' if is_past else 'PENDING'
        
        new_matches_params.append((
            item["code"], h_id, a_id, l_id,
            item["match_date"], item["match_time"],
            item["sport_type"], status, match_completed
        ))
        if item["sport_type"] == "FOOTBALL":
            football_count += 1
        else:
            basketball_count += 1
            
    if new_matches_params:
        cursor.executemany("""
            INSERT INTO matches (code, home_team_id, away_team_id, league_id, match_date, match_time, sport_type, status, match_completed)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, new_matches_params)
        
        # Eklenen maçların ID'lerini hafızadaki sözlüğe güncelle
        cursor.execute("SELECT id, code FROM matches")
        matches_cache = {str(row["code"]): row["id"] for row in cursor.fetchall()}
        
    # 6. Tüm yeni maçların oranlarını tek bir toplu listede topla ve executemany ile ekle
    all_odds_params = []
    for item in new_events:
        match_id = matches_cache.get(item["code"])
        if not match_id:
            continue
        odds = nesine_api.extract_odds_from_event(item["raw_event"])
        for o in odds:
            all_odds_params.append((
                match_id, o.get("market_type"), o.get("market_name"), o.get("option_name"), o.get("odds_value")
            ))
            
    if all_odds_params:
        cursor.executemany("""
            INSERT INTO odds (match_id, market_type, market_name, option_name, odds_value)
            VALUES (?, ?, ?, ?, ?)
        """, all_odds_params)
        
    conn.commit()
    
    return {
        "new_football": football_count,
        "new_basketball": basketball_count,
        "skipped": skipped_count,
        "odds_total": len(all_odds_params)
    }

def save_match_stats(conn: sqlite3.Connection, match_id: int, h2h_data: Any, home_last: Any, 
                     away_last: Any, missing: Any, league_table: Any) -> None:
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO match_stats (match_id, h2h_json, home_last_matches_json, away_last_matches_json, missing_players_json, league_table_json)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(match_id) DO UPDATE SET
            h2h_json = excluded.h2h_json,
            home_last_matches_json = excluded.home_last_matches_json,
            away_last_matches_json = excluded.away_last_matches_json,
            missing_players_json = excluded.missing_players_json,
            league_table_json = excluded.league_table_json,
            updated_at = CURRENT_TIMESTAMP
    """, (
        match_id,
        json.dumps(h2h_data, ensure_ascii=False) if h2h_data else None,
        json.dumps(home_last, ensure_ascii=False) if home_last else None,
        json.dumps(away_last, ensure_ascii=False) if away_last else None,
        json.dumps(missing, ensure_ascii=False) if missing else None,
        json.dumps(league_table, ensure_ascii=False) if league_table else None
    ))

def save_prediction(conn: sqlite3.Connection, match_id: int, prediction_data: Dict[str, Any]) -> int:
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO predictions (match_id, predicted_ht_result, predicted_ft_result, predicted_over_under_3, predicted_kg, predicted_score, confidence_score, ai_analysis_summary)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        match_id,
        prediction_data.get("predicted_ht_result"),
        prediction_data.get("predicted_ft_result"),
        prediction_data.get("predicted_over_under_3"),
        prediction_data.get("predicted_kg"),
        prediction_data.get("predicted_score"),
        prediction_data.get("confidence_score"),
        prediction_data.get("analysis_summary")
    ))
    cursor.execute("UPDATE matches SET status = 'ANALYZED' WHERE id = ? AND status != 'COMPLETED'", (match_id,))
    return cursor.lastrowid

def save_result(conn: sqlite3.Connection, match_id: int, ht_home: int, ht_away: int, ft_home: int, ft_away: int) -> None:
    total_goals = ft_home + ft_away
    is_over_3 = 1 if total_goals > 3 else 0
    is_kg_var = 1 if (ft_home > 0 and ft_away > 0) else 0
    
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO results (match_id, ht_home_score, ht_away_score, ft_home_score, ft_away_score, is_over_3_goals, is_kg_var)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(match_id) DO UPDATE SET
            ht_home_score = excluded.ht_home_score,
            ht_away_score = excluded.ht_away_score,
            ft_home_score = excluded.ft_home_score,
            ft_away_score = excluded.ft_away_score,
            is_over_3_goals = excluded.is_over_3_goals,
            is_kg_var = excluded.is_kg_var,
            updated_at = CURRENT_TIMESTAMP
    """, (match_id, ht_home, ht_away, ft_home, ft_away, is_over_3, is_kg_var))
    
    cursor.execute("UPDATE matches SET status = 'COMPLETED', match_completed = 1 WHERE id = ?", (match_id,))

def get_match_by_code(conn: sqlite3.Connection, code: str) -> Optional[sqlite3.Row]:
    cursor = conn.cursor()
    cursor.execute("""
        SELECT m.id, m.code, ht.name as home_team, at.name as away_team, 
               l.name as league_name, l.id as league_id, l.gemini_analysis as league_gemini_analysis,
               m.match_date, m.match_time, m.sport_type, m.status, m.match_completed
        FROM matches m
        JOIN teams ht ON m.home_team_id = ht.id
        JOIN teams at ON m.away_team_id = at.id
        LEFT JOIN leagues l ON m.league_id = l.id
        WHERE m.code = ?
    """, (code,))
    return cursor.fetchone()

def get_pending_matches(conn: sqlite3.Connection, limit: int = 100, only_gemini_enabled: bool = False) -> List[sqlite3.Row]:
    """Bekleyen ve henüz tarihi geçmemiş (match_completed = 0) maçları getirir."""
    cursor = conn.cursor()
    sql = """
        SELECT m.id, m.code, ht.name as home_team, at.name as away_team, 
               l.name as league_name, l.id as league_id, l.gemini_analysis as league_gemini_analysis,
               m.match_date, m.match_time, m.sport_type, m.status, m.match_completed
        FROM matches m
        JOIN teams ht ON m.home_team_id = ht.id
        JOIN teams at ON m.away_team_id = at.id
        LEFT JOIN leagues l ON m.league_id = l.id
        WHERE m.status = 'PENDING' AND m.match_completed = 0
    """
    if only_gemini_enabled:
        sql += " AND l.gemini_analysis = 1"
    
    sql += """
        ORDER BY 
            CASE 
                WHEN m.match_date LIKE '__.__.____' THEN
                    substr(m.match_date, 7, 4) || '-' || substr(m.match_date, 4, 2) || '-' || substr(m.match_date, 1, 2)
                ELSE m.match_date
            END ASC, 
            m.match_time ASC LIMIT ?
    """
    cursor.execute(sql, (limit,))
    return cursor.fetchall()

def get_unfinished_matches(conn: sqlite3.Connection, limit: int = 250) -> List[Dict[str, Any]]:
    """
    Henüz tamamlanmamış (match_completed = 0) tüm maçları, tahminleri ve oranları ile birlikte getirir.
    Kronolojik olarak en yakın tarihteki maçlar en üstte yer alır.
    Saatler kullanıcının bulunduğu yerel saate (örn: Almanya / Berlin) dönüştürülür.
    """
    cursor = conn.cursor()
    cursor.execute("""
        SELECT 
            m.id, m.code, ht.name as home_team, at.name as away_team, 
            l.name as league_name, l.id as league_id, l.gemini_analysis as league_gemini_analysis,
            m.match_date, m.match_time, m.sport_type, m.status, m.match_completed,
            p.predicted_ht_result, p.predicted_ft_result, p.predicted_over_under_3,
            p.predicted_kg, p.predicted_score, p.confidence_score, p.ai_analysis_summary
        FROM matches m
        JOIN teams ht ON m.home_team_id = ht.id
        JOIN teams at ON m.away_team_id = at.id
        LEFT JOIN leagues l ON m.league_id = l.id
        LEFT JOIN predictions p ON m.id = p.match_id
        WHERE m.match_completed = 0
        ORDER BY 
            CASE 
                WHEN m.match_date LIKE '__.__.____' THEN
                    substr(m.match_date, 7, 4) || '-' || substr(m.match_date, 4, 2) || '-' || substr(m.match_date, 1, 2)
                ELSE m.match_date
            END ASC, 
            m.match_time ASC
        LIMIT ?
    """, (limit,))
    matches = [dict(row) for row in cursor.fetchall()]
    
    if not matches:
        return []
        
    match_ids = [m["id"] for m in matches]
    placeholders = ",".join("?" for _ in match_ids)
    
    # Oranları tek seferde çekip hafızada eşleştir (Zero SQL in loop)
    cursor.execute(f"""
        SELECT match_id, market_name, option_name, odds_value
        FROM odds
        WHERE match_id IN ({placeholders})
    """, match_ids)
    
    odds_by_match: Dict[int, List[Dict[str, Any]]] = {}
    for r in cursor.fetchall():
        mid = r["match_id"]
        if mid not in odds_by_match:
            odds_by_match[mid] = []
        odds_by_match[mid].append({
            "market_name": r["market_name"],
            "option_name": r["option_name"],
            "odds_value": r["odds_value"]
        })
        
    for m in matches:
        # Türkiye saatini kullanıcının yerel saatine (örn: Almanya / Berlin) dönüştür
        loc_d, loc_t = convert_tr_to_local_time(m["match_date"], m["match_time"])
        m["local_date"] = loc_d
        m["local_time"] = loc_t
        m["tr_date"] = m["match_date"]
        m["tr_time"] = m["match_time"]
        m["odds"] = odds_by_match.get(m["id"], [])
        
    return matches

def get_completed_matches(
    conn: sqlite3.Connection, 
    days_limit: Optional[int] = None, 
    limit: int = 250
) -> List[Dict[str, Any]]:
    """
    Tamamlanmış (match_completed = 1 veya status = 'COMPLETED') maçları sonuçları ve tahminleriyle birlikte döner.
    days_limit verilirse (örn: 2) son 2 gün içinde oynanmış maçları filtreler.
    En son biten maç en üstte olacak şekilde kronolojik (DESC) sıralanır.
    """
    cursor = conn.cursor()
    cursor.execute("""
        SELECT 
            m.id, m.code, ht.name as home_team, at.name as away_team, 
            l.name as league_name, l.id as league_id, l.gemini_analysis as league_gemini_analysis,
            m.match_date, m.match_time, m.sport_type, m.status, m.match_completed,
            p.predicted_ht_result, p.predicted_ft_result, p.predicted_over_under_3,
            p.predicted_kg, p.predicted_score, p.confidence_score, p.ai_analysis_summary,
            r.ht_home_score, r.ht_away_score, r.ft_home_score, r.ft_away_score,
            r.is_over_3_goals, r.is_kg_var
        FROM matches m
        JOIN teams ht ON m.home_team_id = ht.id
        JOIN teams at ON m.away_team_id = at.id
        LEFT JOIN leagues l ON m.league_id = l.id
        LEFT JOIN predictions p ON m.id = p.match_id
        LEFT JOIN results r ON m.id = r.match_id
        WHERE m.match_completed = 1 OR m.status = 'COMPLETED'
        ORDER BY 
            CASE 
                WHEN m.match_date LIKE '__.__.____' THEN
                    substr(m.match_date, 7, 4) || '-' || substr(m.match_date, 4, 2) || '-' || substr(m.match_date, 1, 2)
                ELSE m.match_date
            END DESC, 
            m.match_time DESC
        LIMIT ?
    """, (limit,))
    matches = [dict(row) for row in cursor.fetchall()]
    
    if not matches:
        return []
        
    match_ids = [m["id"] for m in matches]
    placeholders = ",".join("?" for _ in match_ids)
    
    # Oranları tek seferde çekip hafızada eşleştir (Zero SQL in loop)
    cursor.execute(f"""
        SELECT match_id, market_name, option_name, odds_value
        FROM odds
        WHERE match_id IN ({placeholders})
    """, match_ids)
    
    odds_by_match: Dict[int, List[Dict[str, Any]]] = {}
    for r in cursor.fetchall():
        mid = r["match_id"]
        if mid not in odds_by_match:
            odds_by_match[mid] = []
        odds_by_match[mid].append({
            "market_name": r["market_name"],
            "option_name": r["option_name"],
            "odds_value": r["odds_value"]
        })
        
    src_tz = ZoneInfo(getattr(config, "SOURCE_TIMEZONE", "Europe/Istanbul"))
    now_src = datetime.now(src_tz)
    filtered_matches = []
    
    for m in matches:
        loc_d, loc_t = convert_tr_to_local_time(m["match_date"], m["match_time"])
        m["local_date"] = loc_d
        m["local_time"] = loc_t
        m["tr_date"] = m["match_date"]
        m["tr_time"] = m["match_time"]
        m["odds"] = odds_by_match.get(m["id"], [])
        
        # Son X gün filtresi kontrolü (days_limit)
        if days_limit is not None:
            m_dt = None
            for d_fmt in ("%d.%m.%Y", "%Y-%m-%d"):
                for t_fmt in ("%H:%M", "%H:%M:%S"):
                    try:
                        m_dt = datetime.strptime(f"{m['match_date']} {m['match_time'] or '00:00'}", f"{d_fmt} {t_fmt}")
                        break
                    except ValueError:
                        continue
                if m_dt:
                    break
            
            if m_dt:
                m_dt = m_dt.replace(tzinfo=src_tz)
                cutoff = now_src - timedelta(days=days_limit)
                if m_dt < cutoff:
                    continue  # X günden eski maçları atla
                    
        # Skor ve tahmin doğruluğu değerlendirmesi (eğer hem sonuç hem tahmin varsa)
        if m.get("ft_home_score") is not None and m.get("predicted_ft_result"):
            ft_h = m["ft_home_score"]
            ft_a = m["ft_away_score"]
            actual_ft = "MS 1" if ft_h > ft_a else ("MS 2" if ft_a > ft_h else "MS 0")
            pred_ft = (m["predicted_ft_result"] or "").upper().strip()
            m["is_prediction_correct"] = bool(actual_ft in pred_ft or (actual_ft[-1] in pred_ft and "MS" in pred_ft))
        else:
            m["is_prediction_correct"] = None
            
        filtered_matches.append(m)
        
    return filtered_matches

def search_matches(conn: sqlite3.Connection, query: str = "", status: str = "", sport_type: str = "", 
                   only_active: bool = False, limit: int = 50) -> List[sqlite3.Row]:
    cursor = conn.cursor()
    sql = """
        SELECT m.id, m.code, ht.name as home_team, at.name as away_team, 
               l.name as league_name, l.id as league_id, l.gemini_analysis as league_gemini_analysis,
               m.match_date, m.match_time, m.sport_type, m.status, m.match_completed
        FROM matches m
        JOIN teams ht ON m.home_team_id = ht.id
        JOIN teams at ON m.away_team_id = at.id
        LEFT JOIN leagues l ON m.league_id = l.id
        WHERE 1=1
    """
    params = []
    if query:
        sql += " AND (m.code LIKE ? OR ht.name LIKE ? OR at.name LIKE ? OR l.name LIKE ?)"
        q_param = f"%{query}%"
        params.extend([q_param, q_param, q_param, q_param])
    if status:
        sql += " AND m.status = ?"
        params.append(status)
    if sport_type:
        sql += " AND m.sport_type = ?"
        params.append(sport_type)
    if only_active:
        sql += " AND m.match_completed = 0"
    
    sql += " ORDER BY m.match_date DESC, m.match_time DESC LIMIT ?"
    params.append(limit)
    cursor.execute(sql, params)
    return cursor.fetchall()

def get_match_full_context(conn: sqlite3.Connection, match_id: int) -> Optional[Dict[str, Any]]:
    cursor = conn.cursor()
    cursor.execute("""
        SELECT m.id, m.code, ht.name as home_team, at.name as away_team, 
               l.name as league_name, l.id as league_id, l.gemini_analysis as league_gemini_analysis,
               m.match_date, m.match_time, m.sport_type, m.status, m.match_completed
        FROM matches m
        JOIN teams ht ON m.home_team_id = ht.id
        JOIN teams at ON m.away_team_id = at.id
        LEFT JOIN leagues l ON m.league_id = l.id
        WHERE m.id = ?
    """, (match_id,))
    match = cursor.fetchone()
    if not match:
        return None
    
    cursor.execute("SELECT market_name, option_name, odds_value FROM odds WHERE match_id = ?", (match_id,))
    odds = [dict(row) for row in cursor.fetchall()]
    
    cursor.execute("SELECT * FROM match_stats WHERE match_id = ?", (match_id,))
    stats_row = cursor.fetchone()
    stats = dict(stats_row) if stats_row else {}
    
    return {
        "match": dict(match),
        "odds": odds,
        "h2h": json.loads(stats.get("h2h_json")) if stats.get("h2h_json") else None,
        "home_last": json.loads(stats.get("home_last_matches_json")) if stats.get("home_last_matches_json") else None,
        "away_last": json.loads(stats.get("away_last_matches_json")) if stats.get("away_last_matches_json") else None,
        "missing": json.loads(stats.get("missing_players_json")) if stats.get("missing_players_json") else None,
        "league_table": json.loads(stats.get("league_table_json")) if stats.get("league_table_json") else None
    }

def get_predictions(conn: sqlite3.Connection, min_confidence: int = 0, limit: int = 50) -> List[Dict[str, Any]]:
    cursor = conn.cursor()
    cursor.execute("""
        SELECT p.id, p.match_id, m.code, ht.name as home_team, at.name as away_team, l.name as league_name,
               m.match_date, m.match_time, m.status, m.match_completed,
               p.predicted_ht_result, p.predicted_ft_result, p.predicted_over_under_3, p.predicted_kg,
               p.predicted_score, p.confidence_score, p.ai_analysis_summary, p.created_at,
               r.ft_home_score, r.ft_away_score, r.ht_home_score, r.ht_away_score
        FROM predictions p
        JOIN matches m ON p.match_id = m.id
        JOIN teams ht ON m.home_team_id = ht.id
        JOIN teams at ON m.away_team_id = at.id
        LEFT JOIN leagues l ON m.league_id = l.id
        LEFT JOIN results r ON m.id = r.match_id
        WHERE p.confidence_score >= ?
        ORDER BY p.created_at DESC
        LIMIT ?
    """, (min_confidence, limit))
    return [dict(row) for row in cursor.fetchall()]

def calculate_prediction_accuracy(conn: sqlite3.Connection) -> Dict[str, Any]:
    """
    Hem tahmini (predictions) hem de maç sonucu (results) girilmiş olan maçların doğruluk ve başarı oranını hesaplar.
    """
    cursor = conn.cursor()
    cursor.execute("""
        SELECT 
            p.predicted_ht_result, p.predicted_ft_result, p.predicted_over_under_3, p.predicted_kg, p.predicted_score,
            r.ht_home_score, r.ht_away_score, r.ft_home_score, r.ft_away_score, r.is_over_3_goals, r.is_kg_var
        FROM predictions p
        JOIN results r ON p.match_id = r.match_id
    """)
    rows = cursor.fetchall()
    
    total = len(rows)
    if total == 0:
        return {
            "total_evaluated": 0,
            "ht_accuracy": 0.0,
            "ft_accuracy": 0.0,
            "over_under_3_accuracy": 0.0,
            "kg_accuracy": 0.0,
            "score_accuracy": 0.0,
            "overall_accuracy": 0.0
        }
    
    ht_correct = 0
    ft_correct = 0
    ou_correct = 0
    kg_correct = 0
    score_correct = 0

    for r in rows:
        ht_h = r["ht_home_score"]
        ht_a = r["ht_away_score"]
        ft_h = r["ft_home_score"]
        ft_a = r["ft_away_score"]
        
        # 1. İlk Yarı Doğrulaması (İY 1, İY 0, İY 2)
        actual_ht = "İY 1" if ht_h > ht_a else ("İY 2" if ht_a > ht_h else "İY 0")
        pred_ht = (r["predicted_ht_result"] or "").upper().strip()
        if actual_ht in pred_ht or (actual_ht[-1] in pred_ht and "İY" in pred_ht):
            ht_correct += 1
            
        # 2. Maç Sonu Doğrulaması (MS 1, MS 0, MS 2)
        actual_ft = "MS 1" if ft_h > ft_a else ("MS 2" if ft_a > ft_h else "MS 0")
        pred_ft = (r["predicted_ft_result"] or "").upper().strip()
        if actual_ft in pred_ft or (actual_ft[-1] in pred_ft and "MS" in pred_ft):
            ft_correct += 1
            
        # 3. 3 Gol Alt / Üst Doğrulaması (ÜST: Gol >= 4, ALT: Gol <= 3)
        actual_ou = "ÜST" if (ft_h + ft_a) > 3 else "ALT"
        pred_ou = (r["predicted_over_under_3"] or "").upper().strip()
        if actual_ou in pred_ou:
            ou_correct += 1
            
        # 4. Karşılıklı Gol Doğrulaması (KG VAR, KG YOK)
        actual_kg = "KG VAR" if (ft_h > 0 and ft_a > 0) else "KG YOK"
        pred_kg = (r["predicted_kg"] or "").upper().strip()
        if actual_kg in pred_kg:
            kg_correct += 1
            
        # 5. Skor Doğrulaması
        actual_score = f"{ft_h}-{ft_a}"
        pred_score = (r["predicted_score"] or "").replace(" ", "").strip()
        if actual_score == pred_score:
            score_correct += 1

    overall_score = (ht_correct + ft_correct + ou_correct + kg_correct) / (total * 4) * 100

    return {
        "total_evaluated": total,
        "ht_correct": ht_correct,
        "ht_accuracy": round((ht_correct / total) * 100, 1),
        "ft_correct": ft_correct,
        "ft_accuracy": round((ft_correct / total) * 100, 1),
        "ou_correct": ou_correct,
        "over_under_3_accuracy": round((ou_correct / total) * 100, 1),
        "kg_correct": kg_correct,
        "kg_accuracy": round((kg_correct / total) * 100, 1),
        "score_correct": score_correct,
        "score_accuracy": round((score_correct / total) * 100, 1),
        "overall_accuracy": round(overall_score, 1)
    }

def get_high_confidence_predictions(conn: sqlite3.Connection, min_confidence: int = 70, limit: int = 3) -> List[Dict[str, Any]]:
    """Kupon oluşturmak için tarihi henüz geçmemiş ve en yüksek güven oranına sahip analizleri çeker."""
    cursor = conn.cursor()
    cursor.execute("""
        SELECT p.id, p.match_id, m.code, ht.name as home_team, at.name as away_team, l.name as league_name,
               m.match_date, m.match_time,
               p.predicted_ht_result, p.predicted_ft_result, p.predicted_over_under_3, p.predicted_kg,
               p.predicted_score, p.confidence_score, p.ai_analysis_summary
        FROM predictions p
        JOIN matches m ON p.match_id = m.id
        JOIN teams ht ON m.home_team_id = ht.id
        JOIN teams at ON m.away_team_id = at.id
        LEFT JOIN leagues l ON m.league_id = l.id
        WHERE p.confidence_score >= ? AND m.match_completed = 0 AND m.status != 'COMPLETED'
        ORDER BY p.confidence_score DESC, p.created_at DESC
        LIMIT ?
    """, (min_confidence, limit))
    return [dict(row) for row in cursor.fetchall()]

def get_db_stats(conn: sqlite3.Connection) -> Dict[str, int]:
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM teams")
    teams_cnt = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM leagues")
    leagues_cnt = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM leagues WHERE gemini_analysis = 1")
    enabled_leagues_cnt = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM matches")
    matches_cnt = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM matches WHERE status='PENDING' AND match_completed=0")
    pending_cnt = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM matches WHERE status='ANALYZED' AND match_completed=0")
    analyzed_cnt = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM matches WHERE match_completed=1")
    completed_cnt = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM predictions")
    predictions_cnt = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM results")
    results_cnt = cursor.fetchone()[0]
    
    return {
        "teams": teams_cnt,
        "leagues": leagues_cnt,
        "enabled_leagues": enabled_leagues_cnt,
        "matches": matches_cnt,
        "pending": pending_cnt,
        "analyzed": analyzed_cnt,
        "completed": completed_cnt,
        "predictions": predictions_cnt,
        "results": results_cnt
    }

def clear_db(db_path: str = DB_FILE) -> None:
    """Veritabanındaki tüm tabloları temizler."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM results")
        cursor.execute("DELETE FROM predictions")
        cursor.execute("DELETE FROM match_stats")
        cursor.execute("DELETE FROM odds")
        cursor.execute("DELETE FROM matches")
        cursor.execute("DELETE FROM leagues")
        cursor.execute("DELETE FROM teams")
        conn.commit()
