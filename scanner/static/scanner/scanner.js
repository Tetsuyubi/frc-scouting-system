import QrScanner from "/static/scanner/qr-scanner.min.js";

const video       = document.getElementById('qr-video');
const camQrResult = document.getElementById('cam-qr-result');
const scanFeedback = document.getElementById('scan-feedback');
const cameraSelect = document.getElementById('camera-select');
const historyList  = document.getElementById('scan-history-list');
const historyEmpty = document.getElementById('scan-history-empty');

let prevResult = "";
let prevResultTimer = null;
const MAX_HISTORY = 10;

// ── CSRF ──────────────────────────────────────────────
function getCookie(name) {
    let value = null;
    if (document.cookie) {
        document.cookie.split(';').forEach(c => {
            c = c.trim();
            if (c.startsWith(name + '='))
                value = decodeURIComponent(c.substring(name.length + 1));
        });
    }
    return value;
}

// ── History ───────────────────────────────────────────
function addToHistory(scouterName, teamNum, matchNum, status) {
    if (!historyList) return;

    const empty = document.getElementById('scan-history-empty');
    if (empty) empty.style.display = 'none';

    const row = document.createElement('div');
    row.className = 'history-row history-' + status;

    const now = new Date();
    const time = now.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });

    const icon = status === 'success' ? '✓' : '✕';

    const iconSpan = document.createElement('span');
    iconSpan.className = 'h-icon';
    iconSpan.textContent = icon;

    const detailSpan = document.createElement('span');
    detailSpan.className = scouterName ? 'h-scouter' : 'h-scouter muted';
    detailSpan.textContent = scouterName || 'Unknown scouter';

    const bodySpan = document.createElement('span');
    bodySpan.className = 'h-body';
    bodySpan.appendChild(detailSpan);
    if (teamNum) bodySpan.appendChild(document.createTextNode(` · Team ${teamNum}`));
    if (matchNum) bodySpan.appendChild(document.createTextNode(` · Match ${matchNum}`));

    const timeSpan = document.createElement('span');
    timeSpan.className = 'h-time';
    timeSpan.textContent = time;

    row.appendChild(iconSpan);
    row.appendChild(bodySpan);
    row.appendChild(timeSpan);

    // Animate in
    row.style.opacity = '0';
    row.style.transform = 'translateX(10px)';
    historyList.prepend(row);
    requestAnimationFrame(() => requestAnimationFrame(() => {
        row.style.transition = 'opacity 0.28s ease, transform 0.28s ease';
        row.style.opacity    = '1';
        row.style.transform  = 'none';
    }));

    // Cap at MAX_HISTORY
    while (historyList.children.length > MAX_HISTORY) {
        historyList.removeChild(historyList.lastChild);
    }
}

// ── Offline outbox ────────────────────────────────────
// Venue wi-fi drops constantly, so a scan that cannot reach the server is
// queued locally and retried instead of being lost. Validation failures are
// NOT queued: that data is bad and would retry forever.
var OUTBOX_KEY = 'scanner_outbox_v1';
var OUTBOX_MAX = 500;
var flushing   = false;

// ── Online / offline mode ─────────────────────────────
// Online: every scan is sent as soon as it is read (and queued if that fails).
// Offline: scans go straight into the outbox with no network attempt, for
// stands with no usable wi-fi. Switching back to online uploads the queue.
var MODE_KEY = 'scanner_mode';

function scanMode() {
    try { return localStorage.getItem(MODE_KEY) === 'offline' ? 'offline' : 'online'; }
    catch (e) { return 'online'; }
}

function setScanMode(mode) {
    try { localStorage.setItem(MODE_KEY, mode); } catch (e) {}
    syncModeUI();
    if (mode === 'online') {
        flushOutbox();
    } else {
        setFeedback('queued', 'Offline mode: scans are saved on this device');
    }
}

function syncModeUI() {
    var mode = scanMode();
    document.querySelectorAll('[data-scan-mode]').forEach(function (b) {
        var on = b.getAttribute('data-scan-mode') === mode;
        b.classList.toggle('is-active', on);
        b.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
    var page = document.querySelector('.scanner-page');
    if (page) page.classList.toggle('is-offline-mode', mode === 'offline');
    var hint = document.getElementById('scan-mode-hint');
    if (hint) {
        hint.textContent = mode === 'offline'
            ? 'Scans are stored on this device. Switch to Online or tap Upload when you have wi-fi.'
            : (navigator.onLine ? 'Scans upload as soon as they are read.'
                                : 'No connection: scans will queue and upload when wi-fi returns.');
    }
}

document.querySelectorAll('[data-scan-mode]').forEach(function (b) {
    b.addEventListener('click', function () { setScanMode(b.getAttribute('data-scan-mode')); });
});
window.addEventListener('online', syncModeUI);
window.addEventListener('offline', syncModeUI);
syncModeUI();

function outboxRead() {
    try { return JSON.parse(localStorage.getItem(OUTBOX_KEY)) || []; }
    catch (e) { return []; }
}

function outboxWrite(items) {
    try { localStorage.setItem(OUTBOX_KEY, JSON.stringify(items.slice(-OUTBOX_MAX))); }
    catch (e) { /* private mode or quota: queueing is best-effort */ }
}

function outboxAdd(entry) {
    var items = outboxRead();
    items.push(entry);
    outboxWrite(items);
    updateOutboxBadge();
}

function updateOutboxBadge() {
    var el = document.getElementById('outbox-badge');
    var up = document.getElementById('outbox-upload');
    var n = outboxRead().length;
    if (el) {
        el.hidden = !n;
        el.textContent = n + ' scan' + (n === 1 ? '' : 's') + ' saved on this device';
    }
    if (up) {
        up.hidden = !n;
        up.disabled = flushing;
        up.textContent = flushing ? 'Uploading…' : 'Upload ' + n + ' now';
    }
}

// Resolves on success. Rejects with err.retryable set when the scan should be
// kept for a later attempt.
function sendScan(qrData) {
    return fetch("/scanner/", {
        method: 'POST',
        credentials: 'include',
        mode: 'same-origin',
        headers: {
            'Accept': 'application/json',
            'Content-Type': 'application/json',
            'X-Requested-With': 'XMLHttpRequest',
            'X-CSRFToken': getCookie('csrftoken'),
        },
        body: JSON.stringify({ qr_data: qrData })
    })
    .catch(function (networkErr) {
        // fetch only rejects when the request never completed.
        var e = new Error('Offline — queued');
        e.retryable = true;
        throw e;
    })
    .then(function (response) {
        return response.text().then(function (text) {
            var json;
            try { json = JSON.parse(text); } catch (_) {
                var pe = new Error(response.status === 401
                    ? 'Not authenticated — please log in'
                    : 'Server error (' + response.status + ')');
                // 5xx is transient; 401 means a human must act.
                pe.retryable = response.status >= 500;
                throw pe;
            }
            if (!response.ok) {
                var se = new Error(json.error || ('Server error (' + response.status + ')'));
                se.retryable = response.status >= 500;
                throw se;
            }
            return json;
        });
    });
}

// ── Server POST ───────────────────────────────────────
function post_data_to_server(data, scouterName, teamNum, matchNum) {
    if (scanMode() === 'offline') {
        outboxAdd({
            qr_data: data, scout: scouterName,
            team: teamNum, match: matchNum, ts: Date.now()
        });
        setFeedback('queued', 'Saved offline — ' + (scouterName || 'Unknown'));
        vibrateOnSuccess(150);
        addToHistory(scouterName, teamNum, matchNum, 'queued');
        return;
    }

    setFeedback('sending', 'Sending…');

    sendScan(data)
        .then(function () {
            setFeedback('success', 'Saved — ' + (scouterName || 'Unknown'));
            vibrateOnSuccess(150);
            addToHistory(scouterName, teamNum, matchNum, 'success');
            flushOutbox();
        })
        .catch(function (error) {
            if (error.retryable) {
                outboxAdd({
                    qr_data: data, scout: scouterName,
                    team: teamNum, match: matchNum, ts: Date.now()
                });
                setFeedback('queued', 'Queued — will send when back online');
                vibrateOnSuccess(80);
                addToHistory(scouterName, teamNum, matchNum, 'queued');
            } else {
                setFeedback('error', 'Error: ' + error.message);
                addToHistory(scouterName, teamNum, matchNum, 'error');
            }
        });
}

// Drain the queue oldest-first. Stops at the first retryable failure so the
// order is preserved and we do not hammer a server that is still unreachable.
function flushOutbox() {
    if (flushing) return Promise.resolve();
    var items = outboxRead();
    if (!items.length) return Promise.resolve();

    flushing = true;
    updateOutboxBadge();
    var remaining = items.slice();

    function step() {
        if (!remaining.length) return Promise.resolve();
        var entry = remaining[0];
        return sendScan(entry.qr_data)
            .then(function () {
                remaining.shift();
                outboxWrite(remaining);
                updateOutboxBadge();
                addToHistory(entry.scout, entry.team, entry.match, 'success');
                return step();
            })
            .catch(function (err) {
                if (err.retryable) return Promise.resolve();   // try again later
                // Permanently bad: drop it so it cannot block the queue forever.
                remaining.shift();
                outboxWrite(remaining);
                updateOutboxBadge();
                addToHistory(entry.scout, entry.team, entry.match, 'error');
                return step();
            });
    }

    return step().finally(function () {
        flushing = false;
        var left = outboxRead().length;
        if (!left) { setFeedback('success', 'All queued scans sent'); }
        updateOutboxBadge();
    });
}

// Automatic uploads only happen in online mode; offline mode waits for the
// Upload button or a switch back to online.
function autoFlush() { if (scanMode() === 'online' && navigator.onLine) flushOutbox(); }
window.addEventListener('online', autoFlush);
window.addEventListener('load', function () { updateOutboxBadge(); autoFlush(); });
setInterval(autoFlush, 30000);

var uploadBtn = document.getElementById('outbox-upload');
if (uploadBtn) uploadBtn.addEventListener('click', function () { flushOutbox(); });

// Let the page open without wi-fi next time (see /scanner/sw.js).
if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/scanner/sw.js', { scope: '/scanner/' }).catch(function () {});
}

// ── Feedback indicator ────────────────────────────────
function setFeedback(state, text) {
    if (!scanFeedback) return;
    scanFeedback.textContent = text;
    scanFeedback.className   = 'scan-feedback-text feedback-' + state;
}
// ── Vibration feedback (mobile only) ──────────────
function vibrateOnSuccess(time) {
    if (typeof navigator !== 'undefined' && typeof navigator.vibrate === 'function') {
        navigator.vibrate(time);
    }
}
// ── QR outline canvas ─────────────────────────────────
// Tracking state — persists across frames
let _outlineActive  = false;  // true after first successful scan
let _targetPts      = null;   // raw video-space points, updated every detection
let _currentPts     = null;   // smoothed drawn points (lerped toward _targetPts)
let _outlineAlpha   = 0;
let _lastDetectTime = 0;
let _outlineAnimId  = null;

const _IDLE_MS      = 150;   // ms without a detection before fading out
const _LERP_POS     = 0.25;  // position smoothing per frame (~60fps)
const _LERP_IN      = 0.15;  // alpha fade-in speed per frame
const _LERP_OUT     = 0.08;  // alpha fade-out speed per frame (slower)

function _outlineLoop(now) {
    const canvas = document.getElementById('qr-outline-canvas');
    const video  = document.getElementById('qr-video');
    if (!canvas || !video) { _outlineAnimId = null; return; }

    const detected = _outlineActive && _targetPts && (now - _lastDetectTime) < _IDLE_MS;

    // Lerp alpha toward target
    const alphaTarget = detected ? 1 : 0;
    _outlineAlpha += (alphaTarget - _outlineAlpha) * (detected ? _LERP_IN : _LERP_OUT);

    if (_outlineAlpha < 0.01 && !detected) {
        // Fully faded — stop loop and reset
        canvas.getContext('2d').clearRect(0, 0, canvas.width, canvas.height);
        _outlineActive = false;
        _currentPts    = null;
        _outlineAlpha  = 0;
        _outlineAnimId = null;
        return;
    }

    // Sync canvas size
    const rect = canvas.getBoundingClientRect();
    if (canvas.width !== rect.width || canvas.height !== rect.height) {
        canvas.width  = rect.width;
        canvas.height = rect.height;
    }

    // Scale raw video-space target points to canvas space
    const scale   = Math.min(rect.width / video.videoWidth, rect.height / video.videoHeight);
    const offsetX = (rect.width  - video.videoWidth  * scale) / 2;
    const offsetY = (rect.height - video.videoHeight * scale) / 2;
    const scaled  = _targetPts.map(p => ({
        x: p.x * scale + offsetX,
        y: p.y * scale + offsetY,
    }));

    // Init or lerp current points toward target
    if (!_currentPts) {
        _currentPts = scaled;
    } else {
        _currentPts = _currentPts.map((p, i) => ({
            x: p.x + (scaled[i].x - p.x) * _LERP_POS,
            y: p.y + (scaled[i].y - p.y) * _LERP_POS,
        }));
    }

    // Gentle pulse while detected
    const pulse     = detected ? 0.225 * Math.sin((now / 1000) * Math.PI * 2 * 0.5) : 0;
    const drawAlpha = Math.max(0, Math.min(1, _outlineAlpha + pulse));

    const ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.globalAlpha = drawAlpha;
    ctx.strokeStyle = '#fbbf24';
    ctx.lineWidth   = 3;
    ctx.setLineDash([12, 6]);
    ctx.lineCap     = 'round';
    ctx.lineJoin    = 'round';
    ctx.beginPath();
    ctx.moveTo(_currentPts[0].x, _currentPts[0].y);
    for (let i = 1; i < _currentPts.length; i++) ctx.lineTo(_currentPts[i].x, _currentPts[i].y);
    ctx.closePath();
    ctx.stroke();
    ctx.globalAlpha = 1;

    _outlineAnimId = requestAnimationFrame(_outlineLoop);
}

// Called on every successful scan — starts tracking
function showQrOutline(cornerPoints) {
    if (!cornerPoints || cornerPoints.length < 4) return;
    _outlineActive  = true;
    _targetPts      = cornerPoints;
    _lastDetectTime = performance.now();
    if (!_outlineAnimId) _outlineAnimId = requestAnimationFrame(_outlineLoop);
}

// Called on every duplicate detection — keeps position live
function updateOutlineTarget(cornerPoints) {
    if (!_outlineActive || !cornerPoints || cornerPoints.length < 4) return;
    _targetPts      = cornerPoints;
    _lastDetectTime = performance.now();
}

// ── Scan handler ──────────────────────────────────────
function setResult(label, result) {
    const data = result.data;

    // Always update tracking position, even for duplicate scans
    updateOutlineTarget(result.cornerPoints);

    if (data === prevResult) return;
    prevResult = data;

    // Allow re-scanning the same code after 2.5 s
    clearTimeout(prevResultTimer);
    prevResultTimer = setTimeout(() => { prevResult = ''; }, 2500);

    showQrOutline(result.cornerPoints);

    // Parse JSON payload
    let scouterName = null, teamNum = null, matchNum = null;
    try {
        const parsed = JSON.parse(data);
        scouterName = parsed.name  || parsed.scouter_name || null;
        teamNum     = parsed.team  || parsed.teamNumber   || parsed.team_number  || null;
        matchNum    = parsed.match || parsed.matchNumber  || parsed.match_number || null;
    } catch (_) { /* plain string QR — not JSON */ }

    // Flash the result — display name, teamNumber, and matchNumber
    let dataText = data;
    const parts = [];
    if (scouterName) parts.push(scouterName);
    if (teamNum) parts.push(`Team ${teamNum}`);
    if (matchNum) parts.push(`Match ${matchNum}`);
    if (parts.length > 0) dataText = parts.join(' · ');

    
    label.textContent = dataText;
    label.classList.add('flash');
    clearTimeout(label._flashTimer);
    label._flashTimer = setTimeout(() => label.classList.remove('flash'), 500);

    post_data_to_server(data, scouterName, teamNum, matchNum);
}

// ── Scanner init ──────────────────────────────────────
const scanner = new QrScanner(video, result => setResult(camQrResult, result), {
    highlightScanRegion: false,
    highlightCodeOutline: false,
    // Scan the full frame — prevents the library from zooming the video into a centre sub-region
    calculateScanRegion: (v) => ({
        x: 0, y: 0,
        width:  v.videoWidth  || 640,
        height: v.videoHeight || 480,
        downScaleFactor: 1,
    }),
});

scanner.start().then(() => {
    // ── Prevent library from zooming the video via inline styles ──
    // The library sets video.style.width / height / transform / zoom when it
    // updates its overlay. We observe and immediately clear those.
    new MutationObserver(() => {
        if (video.style.transform && video.style.transform !== 'none') {
            video.style.transform = 'none';
        }
        if (video.style.zoom && video.style.zoom !== '1') {
            video.style.zoom = '1';
        }
        // Library sometimes shrinks the video to 0 when "hiding" it
        if (video.style.width  || video.style.height) {
            video.style.removeProperty('width');
            video.style.removeProperty('height');
        }
    }).observe(video, { attributes: true, attributeFilter: ['style'] });

    // ── Lock hardware (optical) zoom to 1× if the browser supports it ──
    const stream = video.srcObject;
    if (stream) {
        const track = stream.getVideoTracks()[0];
        if (track) {
            const caps = track.getCapabilities ? track.getCapabilities() : {};
            if (caps.zoom) {
                track.applyConstraints({ advanced: [{ zoom: 1 }] }).catch(() => {});
            }
        }
    }

    // ── Populate camera dropdown ──
    QrScanner.listCameras(true).then(cameras => {
        cameraSelect.innerHTML = '';
        cameras.forEach(camera => {
            const opt   = document.createElement('option');
            opt.value   = camera.id;
            opt.text    = camera.label || camera.id;
            cameraSelect.appendChild(opt);
        });
        if (cameras.length <= 1) {
            cameraSelect.closest('.camera-row').style.display = 'none';
        }
    });
});

// Camera switch
cameraSelect.addEventListener('change', () => {
    scanner.setCamera(cameraSelect.value);
});

// Controls
document.getElementById('start-button').addEventListener('click', () => scanner.start());
document.getElementById('stop-button').addEventListener('click', () => scanner.stop());
