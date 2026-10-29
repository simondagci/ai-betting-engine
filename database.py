import sqlite3
import json
from contextlib import contextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional, List, Dict, Any, Tuple, Set, Iterator, Sequence
import config
import nesine_api
import rules

DB_FILE = config.DB_FILE_PATH

# ==============================================================================
# ORTAK SQL PARÇALARI
# ==============================================================================

# Maç + takım + lig bilgilerini getiren standart kolonlar ve JOIN'ler
_MATCH_COLUMNS = """
    m.id, m.code, ht.name as home_team, at.name as away_team,
    l.name as league_name, l.id as league_id, l.gemini_analysis as league_gemini_analysis,
    m.match_date, m.match_time, m.sport_type, m.status, m.match_completed
"""

_MATCH_JOINS = """
    FROM matches m
    JOIN teams ht ON m.home_team_id = ht.id
    JOIN teams at ON m.away_team_id = at.id
    LEFT JOIN leagues l ON m.league_id = l.id
"""

# Bir maç için birden fazla tahmin olabilir; listelerde sadece en son tahmin kullanılır (satır tekrarını önler)
_LATEST_PREDICTION_JOIN = """
    LEFT JOIN predictions p ON p.id = (SELECT MAX(id) FROM predictions WHERE match_id = m.id)
"""

_PREDICTION_COLUMNS = """
    p.predicted_ht_result, p.predicted_ft_result, p.predicted_over_under_3,
    p.predicted_kg, p.predicted_score, p.confidence_score, p.ai_analysis_summary
"""

# 'DD.MM.YYYY' formatındaki tarihleri kronolojik sıralama için 'YYYY-MM-DD' formatına çevirir
_CHRONOLOGICAL_DATE = """
    CASE
        WHEN m.match_date LIKE '__.__.____' THEN
            substr(m.match_date, 7, 4) || '-' || substr(m.match_date, 4, 2) || '-' || substr(m.match_date, 1, 2)
        ELSE m.match_date
    END
"""

# ==============================================================================
# BAĞLANTI YÖNETİMİ
# ==============================================================================

def get_connection(db_path: str = DB_FILE) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn

@contextmanager
def db_session(db_path: str = DB_FILE) -> Iterator[sqlite3.Connection]:
    """Bağlantıyı açar; başarıyla biterse commit, hata olursa rollback yapar ve her durumda kapatır."""
    conn = get_connection(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

# ==============================================================================
# TARİH / SAAT YARDIMCILARI
# ==============================================================================

_DATE_FORMATS = ("%d.%m.%Y", "%Y-%m-%d")
_TIME_FORMATS = ("%H:%M", "%H:%M:%S")

def _parse_match_datetime(
    match_date_str: str,
    match_time_str: str,
    date_formats: Sequence[str] = _DATE_FORMATS,
    allow_date_only: bool = False
) -> Optional[datetime]:
    """Bülten tarih/saat metnini (saat dilimi bilgisi olmadan) datetime nesnesine çevirir. Başarısız olursa None döner."""
    date_str = (match_date_str or "").strip()
    time_str = (match_time_str or "00:00").strip()

    for d_fmt in date_formats:
        for t_fmt in _TIME_FORMATS:
            try:
                return datetime.strptime(f"{date_str} {time_str}", f"{d_fmt} {t_fmt}")
            except ValueError:
                continue

    if allow_date_only:
        for d_fmt in date_formats:
            try:
                return datetime.strptime(date_str, d_fmt)
            except ValueError:
                continue
    return None

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

    dt = _parse_match_datetime(match_date_str, match_time_str)
    if not dt:
        return match_date_str, match_time_str

    try:
        dt_src = dt.replace(tzinfo=ZoneInfo(src_tz_name or config.SOURCE_TIMEZONE))
        dt_dst = dt_src.astimezone(ZoneInfo(dst_tz_name or config.DISPLAY_TIMEZONE))
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
        delay_minutes = config.MATCH_COMPLETION_DELAY_MINUTES

    start_dt = _parse_match_datetime(
        match_date_str, match_time_str,
        date_formats=_DATE_FORMATS + ("%d/%m/%Y",),
        allow_date_only=True
    )
    if not start_dt:
        return False

    src_tz = ZoneInfo(config.SOURCE_TIMEZONE)
    completed_dt = start_dt.replace(tzinfo=src_tz) + timedelta(minutes=delay_minutes)
    return datetime.now(src_tz) >= completed_dt

def _completion_state(match_date: str, match_time: str) -> Tuple[str, int]:
    """Yeni kaydedilecek bir maçın (status, match_completed) değerlerini 150 dakika kuralına göre döner."""
    if is_match_past(match_date, match_time):
        return "COMPLETED", 1
    return "PENDING", 0

def add_local_time_fields(match: Dict[str, Any]) -> Dict[str, Any]:
    """Maç sözlüğüne yerel saat (local_*) ve orijinal Türkiye saati (tr_*) alanlarını ekler."""
    loc_d, loc_t = convert_tr_to_local_time(match["match_date"], match["match_time"])
    match["local_date"] = loc_d
    match["local_time"] = loc_t
    match["tr_date"] = match["match_date"]
    match["tr_time"] = match["match_time"]
    return match

# ==============================================================================
# ŞEMA VE MIGRATION
# ==============================================================================

def _ensure_column(cursor: sqlite3.Cursor, table: str, column: str, ddl: str) -> None:
    """Mevcut veritabanlarında eksik olan sütunu ekler (basit migration)."""
    cursor.execute(f"PRAGMA table_info({table});")
    if column not in [row["name"] for row in cursor.fetchall()]:
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl};")

def init_db(db_path: str = DB_FILE) -> None:
    """Oluşturulacak olan ilişkisel SQLite veritabanı şemasını başlatır ve güncellemeleri uygular."""
    with db_session(db_path) as conn:
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
        _ensure_column(cursor, "leagues", "gemini_analysis", "INTEGER NOT NULL DEFAULT 0")

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
        _ensure_column(cursor, "matches", "match_completed", "INTEGER NOT NULL DEFAULT 0")

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

        # Panelden değiştirilebilen uygulama ayarları (.env varsayılanlarını geçersiz kılar)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)
        conn.commit()

        # Tarihi geçmiş maçları topluca güncelle
        update_expired_matches(conn)

def update_expired_matches(conn: sqlite3.Connection) -> int:
    """Tarihi ve saati geçmiş olan henüz tamamlanmamış maçları topluca 'match_completed = 1' olarak günceller."""
    cursor = conn.cursor()
    cursor.execute("SELECT id, match_date, match_time, status FROM matches WHERE match_completed = 0")

    to_update = [
        ("COMPLETED" if r["status"] == "PENDING" else r["status"], r["id"])
        for r in cursor.fetchall()
        if is_match_past(r["match_date"], r["match_time"])
    ]
    if to_update:
        cursor.executemany("UPDATE matches SET match_completed = 1, status = ? WHERE id = ?", to_update)
        conn.commit()

    return len(to_update)

# ==============================================================================
# AYARLAR
# ==============================================================================

SETTING_GEMINI_ENABLED = "gemini_analysis_enabled"

def get_setting(conn: sqlite3.Connection, key: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT value, updated_at FROM settings WHERE key = ?", (key,)).fetchone()

def set_setting(conn: sqlite3.Connection, key: str, value: Optional[str]) -> None:
    """Ayarı kaydeder. value None ise ayar silinir ve .env varsayılanına dönülür."""
    if value is None:
        conn.execute("DELETE FROM settings WHERE key = ?", (key,))
        return
    conn.execute("""
        INSERT INTO settings (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
    """, (key, value))

def get_gemini_setting(conn: sqlite3.Connection) -> Dict[str, Any]:
    """
    Genel Gemini analiz anahtarının etkin değerini döner.
    Panelden kaydedilmiş değer varsa o, yoksa .env'deki ENABLE_GEMINI_ANALYSIS kullanılır.
    """
    row = get_setting(conn, SETTING_GEMINI_ENABLED)
    return {
        "enabled": row["value"] == "1" if row else config.ENABLE_GEMINI_ANALYSIS,
        "source": "panel" if row else "env",
        "env_default": config.ENABLE_GEMINI_ANALYSIS,
        "updated_at": row["updated_at"] if row else None
    }

def is_gemini_enabled(conn: sqlite3.Connection) -> bool:
    return get_gemini_setting(conn)["enabled"]

def set_gemini_enabled(conn: sqlite3.Connection, enabled: Optional[bool]) -> None:
    """True/False panel değerini kaydeder; None .env varsayılanına döndürür."""
    set_setting(conn, SETTING_GEMINI_ENABLED, None if enabled is None else ("1" if enabled else "0"))

# ==============================================================================
# TAKIM / LİG
# ==============================================================================

def get_existing_match_codes(conn: sqlite3.Connection) -> Set[str]:
    """Veritabanında kayıtlı tüm maç kodlarını küme (set) olarak döner."""
    cursor = conn.cursor()
    cursor.execute("SELECT code FROM matches")
    return {str(row["code"]) for row in cursor.fetchall()}

def _name_to_id_map(cursor: sqlite3.Cursor, table: str, key_column: str = "name") -> Dict[str, int]:
    cursor.execute(f"SELECT id, {key_column} FROM {table}")
    return {str(row[key_column]): row["id"] for row in cursor.fetchall()}

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
    return conn.execute(sql, params).fetchall()

def get_leagues_admin(conn: sqlite3.Connection, query: str = "") -> List[Dict[str, Any]]:
    """Admin paneli için ligleri toplam maç ve bitmemiş maç sayılarıyla birlikte listeler."""
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
        for r in conn.execute(sql, params).fetchall()
    ]

def set_league_gemini_analysis(conn: sqlite3.Connection, league_id: int, enabled: bool) -> bool:
    """Belirtilen ligin Gemini AI analiz iznini günceller."""
    cursor = conn.execute("UPDATE leagues SET gemini_analysis = ? WHERE id = ?", (1 if enabled else 0, league_id))
    return cursor.rowcount > 0

def set_all_leagues_gemini_analysis(conn: sqlite3.Connection, enabled: bool) -> int:
    """Tüm liglerin Gemini analiz iznini topluca açar veya kapatır."""
    return conn.execute("UPDATE leagues SET gemini_analysis = ?", (1 if enabled else 0,)).rowcount

def is_league_analysis_enabled(conn: sqlite3.Connection, league_id: int) -> bool:
    """Ligin Gemini analiz izninin açık olup olmadığını döner."""
    row = conn.execute("SELECT gemini_analysis FROM leagues WHERE id = ?", (league_id,)).fetchone()
    return bool(row["gemini_analysis"]) if row else False

# ==============================================================================
# KAYIT (WRITE) İŞLEMLERİ
# ==============================================================================

_INSERT_MATCH_SQL = """
    INSERT INTO matches (code, home_team_id, away_team_id, league_id, match_date, match_time, sport_type, status, match_completed)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_INSERT_ODDS_SQL = """
    INSERT INTO odds (match_id, market_type, market_name, option_name, odds_value)
    VALUES (?, ?, ?, ?, ?)
"""

def _odds_params(match_id: int, odds_list: List[Dict[str, Any]]) -> List[Tuple]:
    return [
        (match_id, o.get("market_type"), o.get("market_name"), o.get("option_name"), o.get("odds_value"))
        for o in odds_list
    ]

def match_exists(conn: sqlite3.Connection, code: str) -> bool:
    """Verilen kodlu maçın veritabanında kayıtlı olup olmadığını kontrol eder."""
    return conn.execute("SELECT 1 FROM matches WHERE code = ?", (code,)).fetchone() is not None

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
    status, match_completed = _completion_state(match_date, match_time)

    cursor.execute(_INSERT_MATCH_SQL, (code, home_id, away_id, league_id, match_date, match_time, sport_type, status, match_completed))
    return cursor.lastrowid

def save_odds(conn: sqlite3.Connection, match_id: int, odds_list: List[Dict[str, Any]]) -> None:
    """Belirtilen maça ait oranları toplu (executemany) olarak kaydeder."""
    cursor = conn.cursor()
    cursor.execute("DELETE FROM odds WHERE match_id = ?", (match_id,))
    if odds_list:
        cursor.executemany(_INSERT_ODDS_SQL, _odds_params(match_id, odds_list))

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
    teams_cache = _name_to_id_map(cursor, "teams")
    leagues_cache = _name_to_id_map(cursor, "leagues")
    existing_codes = set(_name_to_id_map(cursor, "matches", key_column="code"))

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
        if code in existing_codes:
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

        for team in (home_team, away_team):
            if team not in teams_cache:
                needed_teams.add((team, sport_type))
        if league_name not in leagues_cache:
            needed_leagues.add((league_name, sport_type))

    # 3. Yeni takımları topluca (executemany) ekle
    if needed_teams:
        cursor.executemany("INSERT OR IGNORE INTO teams (name, sport_type) VALUES (?, ?)", list(needed_teams))
        teams_cache = _name_to_id_map(cursor, "teams")

    # 4. Yeni ligleri topluca (executemany) ekle
    if needed_leagues:
        leagues_params = [(name, st, default_league_gemini_analysis) for name, st in needed_leagues]
        cursor.executemany("INSERT OR IGNORE INTO leagues (name, sport_type, gemini_analysis) VALUES (?, ?, ?)", leagues_params)
        leagues_cache = _name_to_id_map(cursor, "leagues")

    # 5. Yeni maçları hazırla ve topluca (executemany) ekle
    new_matches_params = []
    for item in new_events:
        status, match_completed = _completion_state(item["match_date"], item["match_time"])
        new_matches_params.append((
            item["code"], teams_cache[item["home_team"]], teams_cache[item["away_team"]],
            leagues_cache.get(item["league_name"]),
            item["match_date"], item["match_time"],
            item["sport_type"], status, match_completed
        ))

    matches_cache: Dict[str, int] = {}
    if new_matches_params:
        cursor.executemany(_INSERT_MATCH_SQL, new_matches_params)
        # Eklenen maçların ID'lerini hafızadaki sözlüğe al
        matches_cache = _name_to_id_map(cursor, "matches", key_column="code")

    # 6. Tüm yeni maçların oranlarını tek bir toplu listede topla ve executemany ile ekle
    all_odds_params = []
    for item in new_events:
        match_id = matches_cache.get(item["code"])
        if match_id:
            all_odds_params.extend(_odds_params(match_id, nesine_api.extract_odds_from_event(item["raw_event"])))

    if all_odds_params:
        cursor.executemany(_INSERT_ODDS_SQL, all_odds_params)

    conn.commit()

    football_count = sum(1 for item in new_events if item["sport_type"] == "FOOTBALL")
    return {
        "new_football": football_count,
        "new_basketball": len(new_events) - football_count,
        "skipped": skipped_count,
        "odds_total": len(all_odds_params)
    }

def _dump_json(value: Any) -> Optional[str]:
    return json.dumps(value, ensure_ascii=False) if value else None

def save_match_stats(conn: sqlite3.Connection, match_id: int, h2h_data: Any, home_last: Any,
                     away_last: Any, missing: Any, league_table: Any) -> None:
    conn.execute("""
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
        _dump_json(h2h_data),
        _dump_json(home_last),
        _dump_json(away_last),
        _dump_json(missing),
        _dump_json(league_table)
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
    prediction_id = cursor.lastrowid
    cursor.execute("UPDATE matches SET status = 'ANALYZED' WHERE id = ? AND status != 'COMPLETED'", (match_id,))
    return prediction_id

def save_result(conn: sqlite3.Connection, match_id: int, ht_home: int, ht_away: int, ft_home: int, ft_away: int) -> None:
    is_over_3 = 1 if rules.is_over_3_goals(ft_home, ft_away) else 0
    is_kg_var = 1 if rules.is_kg_var(ft_home, ft_away) else 0

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

# ==============================================================================
# SORGU (READ) İŞLEMLERİ
# ==============================================================================

def _get_odds_by_match(conn: sqlite3.Connection, match_ids: List[int]) -> Dict[int, List[Dict[str, Any]]]:
    """Birden fazla maçın oranlarını tek sorguda çekip match_id'ye göre gruplar (Zero SQL in loop)."""
    odds_by_match: Dict[int, List[Dict[str, Any]]] = {}
    if not match_ids:
        return odds_by_match

    placeholders = ",".join("?" for _ in match_ids)
    rows = conn.execute(f"""
        SELECT match_id, market_name, option_name, odds_value
        FROM odds
        WHERE match_id IN ({placeholders})
    """, match_ids).fetchall()

    for r in rows:
        odds_by_match.setdefault(r["match_id"], []).append({
            "market_name": r["market_name"],
            "option_name": r["option_name"],
            "odds_value": r["odds_value"]
        })
    return odds_by_match

def _enrich_matches(conn: sqlite3.Connection, rows: List[sqlite3.Row]) -> List[Dict[str, Any]]:
    """Maç satırlarını sözlüğe çevirir; oranları ve yerel saat alanlarını ekler."""
    matches = [dict(row) for row in rows]
    odds_by_match = _get_odds_by_match(conn, [m["id"] for m in matches])
    for m in matches:
        add_local_time_fields(m)
        m["odds"] = odds_by_match.get(m["id"], [])
    return matches

def get_match_by_code(conn: sqlite3.Connection, code: str) -> Optional[sqlite3.Row]:
    return conn.execute(f"SELECT {_MATCH_COLUMNS} {_MATCH_JOINS} WHERE m.code = ?", (code,)).fetchone()

def get_unfinished_matches_by_league(conn: sqlite3.Connection, league_id: int) -> List[sqlite3.Row]:
    """Belirtilen lige ait bitmemiş (match_completed = 0) maçları kronolojik sırayla getirir."""
    return conn.execute(f"""
        SELECT {_MATCH_COLUMNS} {_MATCH_JOINS}
        WHERE m.league_id = ? AND m.match_completed = 0
        ORDER BY {_CHRONOLOGICAL_DATE} ASC, m.match_time ASC
    """, (league_id,)).fetchall()

def get_pending_matches(conn: sqlite3.Connection, limit: int = 100, only_gemini_enabled: bool = False) -> List[sqlite3.Row]:
    """Bekleyen ve henüz tarihi geçmemiş (match_completed = 0) maçları getirir."""
    sql = f"SELECT {_MATCH_COLUMNS} {_MATCH_JOINS} WHERE m.status = 'PENDING' AND m.match_completed = 0"
    if only_gemini_enabled:
        sql += " AND l.gemini_analysis = 1"
    sql += f" ORDER BY {_CHRONOLOGICAL_DATE} ASC, m.match_time ASC LIMIT ?"
    return conn.execute(sql, (limit,)).fetchall()

def get_unfinished_matches(conn: sqlite3.Connection, limit: int = 250) -> List[Dict[str, Any]]:
    """
    Henüz tamamlanmamış (match_completed = 0) tüm maçları, tahminleri ve oranları ile birlikte getirir.
    Kronolojik olarak en yakın tarihteki maçlar en üstte yer alır.
    Saatler kullanıcının bulunduğu yerel saate (örn: Almanya / Berlin) dönüştürülür.
    """
    rows = conn.execute(f"""
        SELECT {_MATCH_COLUMNS}, {_PREDICTION_COLUMNS}
        {_MATCH_JOINS}
        {_LATEST_PREDICTION_JOIN}
        WHERE m.match_completed = 0
        ORDER BY {_CHRONOLOGICAL_DATE} ASC, m.match_time ASC
        LIMIT ?
    """, (limit,)).fetchall()
    return _enrich_matches(conn, rows)

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
    rows = conn.execute(f"""
        SELECT {_MATCH_COLUMNS}, {_PREDICTION_COLUMNS},
            r.ht_home_score, r.ht_away_score, r.ft_home_score, r.ft_away_score,
            r.is_over_3_goals, r.is_kg_var
        {_MATCH_JOINS}
        {_LATEST_PREDICTION_JOIN}
        LEFT JOIN results r ON m.id = r.match_id
        WHERE m.match_completed = 1 OR m.status = 'COMPLETED'
        ORDER BY {_CHRONOLOGICAL_DATE} DESC, m.match_time DESC
        LIMIT ?
    """, (limit,)).fetchall()

    src_tz = ZoneInfo(config.SOURCE_TIMEZONE)
    cutoff = datetime.now(src_tz) - timedelta(days=days_limit) if days_limit is not None else None
    filtered_matches = []

    for m in _enrich_matches(conn, rows):
        # Son X gün filtresi kontrolü (days_limit)
        if cutoff is not None:
            m_dt = _parse_match_datetime(m["match_date"], m["match_time"])
            if m_dt and m_dt.replace(tzinfo=src_tz) < cutoff:
                continue  # X günden eski maçları atla

        # Skor ve tahmin doğruluğu değerlendirmesi (eğer hem sonuç hem tahmin varsa)
        if m.get("ft_home_score") is not None and m.get("predicted_ft_result"):
            actual_ft = rules.result_label("MS", m["ft_home_score"], m["ft_away_score"])
            m["is_prediction_correct"] = rules.is_result_prediction_correct(actual_ft, m["predicted_ft_result"], "MS")
        else:
            m["is_prediction_correct"] = None

        filtered_matches.append(m)

    return filtered_matches

def search_matches(conn: sqlite3.Connection, query: str = "", status: str = "", sport_type: str = "",
                   only_active: bool = False, limit: int = 50) -> List[sqlite3.Row]:
    sql = f"SELECT {_MATCH_COLUMNS} {_MATCH_JOINS} WHERE 1=1"
    params: List[Any] = []
    if query:
        sql += " AND (m.code LIKE ? OR ht.name LIKE ? OR at.name LIKE ? OR l.name LIKE ?)"
        params.extend([f"%{query}%"] * 4)
    if status:
        sql += " AND m.status = ?"
        params.append(status)
    if sport_type:
        sql += " AND m.sport_type = ?"
        params.append(sport_type)
    if only_active:
        sql += " AND m.match_completed = 0"

    sql += f" ORDER BY {_CHRONOLOGICAL_DATE} DESC, m.match_time DESC LIMIT ?"
    params.append(limit)
    return conn.execute(sql, params).fetchall()

def _load_json(stats: Dict[str, Any], column: str) -> Any:
    return json.loads(stats[column]) if stats.get(column) else None

def get_match_full_context(conn: sqlite3.Connection, match_id: int) -> Optional[Dict[str, Any]]:
    match = conn.execute(f"SELECT {_MATCH_COLUMNS} {_MATCH_JOINS} WHERE m.id = ?", (match_id,)).fetchone()
    if not match:
        return None

    odds = [dict(row) for row in conn.execute(
        "SELECT market_name, option_name, odds_value FROM odds WHERE match_id = ?", (match_id,)
    ).fetchall()]

    stats_row = conn.execute("SELECT * FROM match_stats WHERE match_id = ?", (match_id,)).fetchone()
    stats = dict(stats_row) if stats_row else {}

    return {
        "match": dict(match),
        "odds": odds,
        "h2h": _load_json(stats, "h2h_json"),
        "home_last": _load_json(stats, "home_last_matches_json"),
        "away_last": _load_json(stats, "away_last_matches_json"),
        "missing": _load_json(stats, "missing_players_json"),
        "league_table": _load_json(stats, "league_table_json")
    }

def get_match_predictions(conn: sqlite3.Connection, match_id: int) -> List[Dict[str, Any]]:
    """Bir maça ait tüm Gemini tahminlerini en yeniden eskiye doğru döner."""
    rows = conn.execute(f"""
        SELECT p.id, p.created_at, {_PREDICTION_COLUMNS}
        FROM predictions p
        WHERE p.match_id = ?
        ORDER BY p.id DESC
    """, (match_id,)).fetchall()
    return [dict(row) for row in rows]

def get_match_result(conn: sqlite3.Connection, match_id: int) -> Optional[Dict[str, Any]]:
    row = conn.execute("""
        SELECT ht_home_score, ht_away_score, ft_home_score, ft_away_score, is_over_3_goals, is_kg_var, updated_at
        FROM results
        WHERE match_id = ?
    """, (match_id,)).fetchone()
    return dict(row) if row else None

def get_predictions(conn: sqlite3.Connection, min_confidence: int = 0, limit: int = 50) -> List[Dict[str, Any]]:
    rows = conn.execute("""
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
    """, (min_confidence, limit)).fetchall()
    return [dict(row) for row in rows]

def calculate_prediction_accuracy(conn: sqlite3.Connection) -> Dict[str, Any]:
    """
    Hem tahmini (predictions) hem de maç sonucu (results) girilmiş olan maçların doğruluk ve başarı oranını hesaplar.
    """
    rows = conn.execute("""
        SELECT
            p.predicted_ht_result, p.predicted_ft_result, p.predicted_over_under_3, p.predicted_kg, p.predicted_score,
            r.ht_home_score, r.ht_away_score, r.ft_home_score, r.ft_away_score, r.is_over_3_goals, r.is_kg_var
        FROM predictions p
        JOIN results r ON p.match_id = r.match_id
    """).fetchall()

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

    ht_correct = ft_correct = ou_correct = kg_correct = score_correct = 0

    for r in rows:
        row = dict(r)
        # İY / MS / 3 Gol Alt-Üst (ÜST: Gol >= 4) / KG / Tam Skor doğrulaması
        evaluation = rules.evaluate_prediction(row, row)
        ht_correct += evaluation["ht"]
        ft_correct += evaluation["ft"]
        ou_correct += evaluation["over_under"]
        kg_correct += evaluation["kg"]
        score_correct += evaluation["score"]

    def pct(correct: int) -> float:
        return round((correct / total) * 100, 1)

    overall_score = (ht_correct + ft_correct + ou_correct + kg_correct) / (total * 4) * 100

    return {
        "total_evaluated": total,
        "ht_correct": ht_correct,
        "ht_accuracy": pct(ht_correct),
        "ft_correct": ft_correct,
        "ft_accuracy": pct(ft_correct),
        "ou_correct": ou_correct,
        "over_under_3_accuracy": pct(ou_correct),
        "kg_correct": kg_correct,
        "kg_accuracy": pct(kg_correct),
        "score_correct": score_correct,
        "score_accuracy": pct(score_correct),
        "overall_accuracy": round(overall_score, 1)
    }

def get_high_confidence_predictions(conn: sqlite3.Connection, min_confidence: int = 70, limit: int = 3) -> List[Dict[str, Any]]:
    """Kupon oluşturmak için tarihi henüz geçmemiş ve en yüksek güven oranına sahip analizleri çeker."""
    rows = conn.execute("""
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
    """, (min_confidence, limit)).fetchall()
    return [dict(row) for row in rows]

def get_db_stats(conn: sqlite3.Connection) -> Dict[str, int]:
    row = conn.execute("""
        SELECT
            (SELECT COUNT(*) FROM teams) AS teams,
            (SELECT COUNT(*) FROM leagues) AS leagues,
            (SELECT COUNT(*) FROM leagues WHERE gemini_analysis = 1) AS enabled_leagues,
            (SELECT COUNT(*) FROM matches) AS matches,
            (SELECT COUNT(*) FROM matches WHERE status = 'PENDING' AND match_completed = 0) AS pending,
            (SELECT COUNT(*) FROM matches WHERE status = 'ANALYZED' AND match_completed = 0) AS analyzed,
            (SELECT COUNT(*) FROM matches WHERE match_completed = 1) AS completed,
            (SELECT COUNT(*) FROM predictions) AS predictions,
            (SELECT COUNT(*) FROM results) AS results
    """).fetchone()
    return dict(row)

def clear_db(db_path: str = DB_FILE) -> None:
    """Veritabanındaki tüm veri tablolarını temizler (settings tablosundaki panel ayarları korunur)."""
    with db_session(db_path) as conn:
        # Foreign key bağımlılık sırasına göre (çocuk tablolardan ebeveynlere)
        for table in ("results", "predictions", "match_stats", "odds", "matches", "leagues", "teams"):
            conn.execute(f"DELETE FROM {table}")
