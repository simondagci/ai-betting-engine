// AI Betting Engine - Ortak arayüz yardımcıları (liste ve detay sayfaları tarafından kullanılır)

// Nesine / Gemini kaynaklı tüm metinler innerHTML'e basılmadan önce kaçışlanır
function esc(value) {
    return String(value ?? '').replace(/[&<>"']/g, c => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[c]));
}

async function api(url, options = {}) {
    const res = await fetch(url, options);
    let json;
    try {
        json = await res.json();
    } catch (_) {
        throw new Error(`Sunucu yanıtı okunamadı (HTTP ${res.status})`);
    }
    if (!res.ok || json.success === false) {
        const err = new Error(json.error || json.message || `HTTP ${res.status}`);
        err.code = json.code;      // örn. GEMINI_DISABLED, LEAGUE_DISABLED
        err.data = json;
        throw err;
    }
    return json;
}

const GEMINI_DISABLED_HINT = 'Gemini analizleri kapalı. Lig Yönetimi › Ayarlar sayfasından açabilirsiniz.';
const MISSING_STATS_HINT = 'Son maçlar / H2H verisi yok; bu maç Gemini\'ye gönderilmez.';

// Gemini'ye gönderilmesi için gereken istatistikler (sunucudaki match_data.REQUIRED_SECTIONS ile aynı)
function missingStatSections(ctx) {
    const required = [['home_last', 'Ev sahibinin son maçları'], ['away_last', 'Deplasmanın son maçları'], ['h2h', 'Aralarındaki maçlar (H2H)']];
    return required.filter(([key]) => !(Array.isArray(ctx[key]) ? ctx[key].length : ctx[key])).map(([, label]) => label);
}

function postJson(url, body) {
    return api(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body || {})
    });
}

// ==================== TOAST ====================
function showToast(message, type = 'success') {
    let container = document.getElementById('toastContainer');
    if (!container) {
        container = document.createElement('div');
        container.id = 'toastContainer';
        container.className = 'toast-container';
        container.setAttribute('role', 'status');
        container.setAttribute('aria-live', 'polite');
        document.body.appendChild(container);
    }
    const icon = { success: '✓', error: '⚠', info: 'ℹ' }[type] || '';
    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;
    toast.innerHTML = `<span>${icon}</span><span>${esc(message)}</span>`;
    container.appendChild(toast);
    setTimeout(() => toast.remove(), type === 'error' ? 6000 : 3500);
}

// Uzun süren işlemlerde butonu kilitleyip spinner gösterir; işlem bitince eski haline döndürür
async function withBusyButton(btn, busyLabel, task) {
    const original = btn ? btn.innerHTML : '';
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = `<span class="spinner"></span> ${esc(busyLabel)}`;
    }
    try {
        return await task();
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = original;
        }
    }
}

// ==================== MODAL ====================
// onConfirm hata fırlatırsa modal açık kalır ve hata mesajı modal içinde gösterilir.
// onCancel: modal onaylanmadan kapatılırsa (İptal, ×, Esc, dışarı tıklama) çağrılır.
function openModal({ title, bodyHtml, confirmLabel = 'Kaydet', cancelLabel = 'İptal', onConfirm, onOpen, onCancel }) {
    let confirmed = false;
    const backdrop = document.createElement('div');
    backdrop.className = 'modal-backdrop';
    backdrop.innerHTML = `
        <div class="modal" role="dialog" aria-modal="true" aria-labelledby="modalTitle">
            <div class="modal-header">
                <h2 id="modalTitle">${esc(title)}</h2>
                <button class="modal-close" type="button" aria-label="Kapat">&times;</button>
            </div>
            <form class="modal-form">
                <div class="modal-body">
                    ${bodyHtml}
                    <div class="modal-error" hidden></div>
                </div>
                <div class="modal-footer">
                    <button class="btn" type="button" data-action="cancel">${esc(cancelLabel)}</button>
                    <button class="btn btn-brand" type="submit">${esc(confirmLabel)}</button>
                </div>
            </form>
        </div>`;
    document.body.appendChild(backdrop);

    const close = () => {
        document.removeEventListener('keydown', onKeydown);
        backdrop.remove();
        if (!confirmed && onCancel) onCancel();
    };
    const onKeydown = e => { if (e.key === 'Escape') close(); };
    document.addEventListener('keydown', onKeydown);

    backdrop.addEventListener('click', e => { if (e.target === backdrop) close(); });
    backdrop.querySelector('.modal-close').addEventListener('click', close);
    backdrop.querySelector('[data-action="cancel"]').addEventListener('click', close);

    const form = backdrop.querySelector('.modal-form');
    const errorBox = backdrop.querySelector('.modal-error');
    const submitBtn = form.querySelector('button[type="submit"]');

    form.addEventListener('submit', async e => {
        e.preventDefault();
        errorBox.hidden = true;
        try {
            await withBusyButton(submitBtn, 'Lütfen bekleyin...', () => onConfirm(form));
            confirmed = true;
            close();
        } catch (err) {
            errorBox.textContent = err.message;
            errorBox.hidden = false;
        }
    });

    if (onOpen) onOpen(form);
    const firstInput = form.querySelector('input, select, textarea');
    (firstInput || submitBtn).focus();
}

function confirmDialog({ title, message, confirmLabel = 'Onayla', onConfirm }) {
    openModal({ title, bodyHtml: `<p>${esc(message)}</p>`, confirmLabel, onConfirm });
}

// Gemini tahmini ister. Ligin Gemini izni kapalıysa (LEAGUE_DISABLED) kullanıcıya ligi açıp analiz etmeyi önerir.
// Dönen değer: API yanıtı, kullanıcı vazgeçerse null. Diğer hatalar (GEMINI_DISABLED vb.) fırlatılır.
async function requestPrediction(matchId) {
    const url = `/api/matches/${matchId}/analyze`;
    try {
        return await postJson(url);
    } catch (err) {
        if (err.code !== 'LEAGUE_DISABLED') throw err;
        return new Promise(resolve => {
            openModal({
                title: 'Lig İzni Gerekli',
                bodyHtml: `<p>"${esc(err.data.league_name)}" ligi için Gemini analiz izni kapalı.</p>
                           <p class="muted" style="margin-top:8px;">Lig iznini açarsanız bu ligin diğer maçları da toplu analizlere (terminal menüsü) dahil olur.</p>`,
                confirmLabel: 'Ligi Aç ve Analiz Et',
                // Modal hemen kapanır; analiz isteğinin ilerlemesi çağıran butonda gösterilir
                onConfirm: () => { resolve(postJson(url, { enable_league: true })); },
                onCancel: () => resolve(null)
            });
        });
    }
}

// ==================== FORMATLAYICILAR ====================
function isBasketball(sportType) {
    return sportType === 'BASKETBALL';
}

// Liste görünümündeki ana oranlar. Basketbolda beraberlik (MS 0), 2.5 Gol Üst ve KG yoktur.
function pickMainOdds(odds = [], sportType = 'FOOTBALL') {
    const find = pred => odds.find(pred)?.odds_value ?? null;
    const basketball = isBasketball(sportType);
    return {
        ms1: find(o => o.market_name === 'Maç Sonucu' && o.option_name === 'MS 1'),
        ms0: basketball ? null : find(o => o.market_name === 'Maç Sonucu' && o.option_name === 'MS 0'),
        ms2: find(o => o.market_name === 'Maç Sonucu' && o.option_name === 'MS 2'),
        u25: basketball ? null : find(o => o.market_name === '2.5 Gol Alt/Üst' && o.option_name.includes('Üst')),
        kg: basketball ? null : find(o => o.market_name === 'Karşılıklı Gol' && o.option_name.includes('VAR'))
    };
}

// Tahmin özetinin ikinci satırı: futbol 'İY 1 (1-0) · ALT · KG YOK', basketbol 'İY 1 (42-38) · Toplam 217'
function predictionSubline(p, sportType) {
    if (!p) return '';
    const htText = p.predicted_ht_score
        ? `${p.predicted_ht_result || '—'} (${p.predicted_ht_score})`
        : (p.predicted_ht_result || '—');
    if (isBasketball(sportType)) {
        const total = p.predicted_total_points !== null && p.predicted_total_points !== undefined ? ` · Toplam ${p.predicted_total_points}` : '';
        return `${htText}${total}`;
    }
    return `${htText} · ${p.predicted_over_under_3 || '—'} · ${p.predicted_kg || '—'}`;
}

function formatOdd(value) {
    return value === null || value === undefined ? '<span class="muted">—</span>' : Number(value).toFixed(2);
}

function isCompleted(m) {
    return m.match_completed === 1 || m.status === 'COMPLETED';
}

function statusBadge(m) {
    if (isCompleted(m)) return '<span class="badge badge-brand">Tamamlandı</span>';
    if (m.status === 'ANALYZED' || m.predicted_score) return '<span class="badge badge-success">Analiz Edildi</span>';
    return '<span class="badge">Bekliyor</span>';
}

function confidenceBadge(score) {
    if (score === null || score === undefined) return '<span class="muted">—</span>';
    const cls = score >= 75 ? 'badge-success' : (score >= 60 ? 'badge-warning' : 'badge-error');
    return `<span class="badge ${cls}">%${esc(score)}</span>`;
}

function sportLabel(sportType) {
    return sportType === 'BASKETBALL' ? '🏀 Basketbol' : '⚽ Futbol';
}

function hasScore(m) {
    return m.ft_home_score !== null && m.ft_home_score !== undefined;
}

// Gemini (date/home/away/score) ve Nesine (Score[] / HomeTeam{...}) formatlarındaki geçmiş maç kayıtlarını normalize eder
function normalizeHistoryItem(item) {
    const teamName = team => (team && typeof team === 'object') ? (team.NesineNameLong || team.Name) : team;
    let score = item.score || item.FT || item.Score || '';
    if (Array.isArray(score)) {
        const ft = score.find(x => x.Type === 'FT' || x.Type === 'Current');
        score = ft ? `${ft.HomeTeamScore}-${ft.AwayTeamScore}` : '';
    }
    return {
        date: item.date || item.D || item.Date || '',
        league: item.league || item.LN || item.LeagueName || '',
        home: item.home || item.HN || teamName(item.HomeTeam) || '',
        away: item.away || item.AN || teamName(item.AwayTeam) || '',
        score: typeof score === 'string' || typeof score === 'number' ? String(score) : '',
        htScore: item.ht_score || '',
        // iddaa.com son maç listelerinde ilgili takımın bakış açısından sonuç (G / B / M)
        result: ['G', 'B', 'M'].includes(item.result) ? item.result : null
    };
}
