// ================================
// Picklist JS (Fixed & Optimized)
// ================================

let draggedItem = null;
let saveTimeout = null;
// lastTimestamp is initialized from the server in the template
const INACTIVITY_TIMEOUT = 3000; // 3 seconds before auto-save
const FETCH_UPDATE_INTERVAL = 5000; // Fetch updates every 5 seconds
let isFetching = false; // To prevent overlapping fetch requests

// ----------------
// Utility Functions
// ----------------

// Get CSRF token (for Django POST requests)
function getCookie(name) {
    let cookieValue = null;
    if (document.cookie && document.cookie !== '') {
        const cookies = document.cookie.split(';');
        for (let i = 0; i < cookies.length; i++) {
            const cookie = cookies[i].trim();
            if (cookie.substring(0, name.length + 1) === (name + '=')) {
                cookieValue = decodeURIComponent(cookie.substring(name.length + 1));
                break;
            }
        }
    }
    return cookieValue;
}

// Trigger haptic feedback if supported
function triggerHapticFeedback() {
    if (navigator.vibrate) {
        navigator.vibrate(50);
    }
}

// Sanitize team IDs for HTML
function sanitizeId(team) {
    return team.replace(/\s+/g, '_').replace(/[^\w-]/g, '');
}

// ----------------
// Save Functions
// ----------------

// Temporary save (frequent updates)
function saveTemporary() {
    const lists = ['no_pick', '1st_pick', '2nd_pick', '3rd_pick', 'dnp', '4th_pick'];
    // Last line of defence before anything reaches the server. Skips the drag
    // placeholder (it has no id, which would otherwise save as an empty entry)
    // and drops any team already recorded in an earlier column, so a duplicate
    // introduced by a UI glitch can never be persisted.
    const saved = Object.create(null);
    const data = lists.map(listId =>
        Array.from(document.getElementById(listId)?.children || [])
            .map(item => item.id)
            .filter(id => {
                if (!id || saved[id]) { return false; }
                saved[id] = true;
                return true;
            })
    );

    fetch(`/strategy/picklist/submit/?comp=${encodeURIComponent(comp_code)}`, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': getCookie('csrftoken')
        },
        body: JSON.stringify(data)
    })
    .then(response => response.json())
    .then(response => {
        lastTimestamp = response.timestamp;
    })
    .catch(err => console.error("Error saving temporary:", err));
}

// Save to database (manual or auto-save)
function saveToDB() {
    const saveButton = document.querySelector('.save-button');
    const statusDiv = document.querySelector('.save-status');
    const statusMessage = document.querySelector('.status-message');
    saveButton.disabled = true;

    const lists = ['no_pick', '1st_pick', '2nd_pick', '3rd_pick', 'dnp', '4th_pick'];
    const data = lists.map(listId => 
        Array.from(document.getElementById(listId)?.children || [])
            .map(item => item.id)
    );

    fetch(`/strategy/picklist/submit/?comp=${encodeURIComponent(comp_code)}&save_to_db=true`, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': getCookie('csrftoken')
        },
        body: JSON.stringify(data)
    })
    .then(response => {
        if (!response.ok) throw new Error('Network response was not ok');
        return response.json();
    })
    .then(data => {
        triggerHapticFeedback();
        statusDiv.style.display = 'block';
        statusDiv.classList.add('success');
        statusDiv.classList.remove('error');
        statusMessage.textContent = 'Sent to database!';
        saveButton.classList.add('success');

        setTimeout(() => {
            statusDiv.style.display = 'none';
            saveButton.classList.remove('success');
            saveButton.disabled = false;
        }, 3000);
    })
    .catch(error => {
        console.error('Error saving to database:', error);
        statusDiv.style.display = 'block';
        statusDiv.classList.add('error');
        statusDiv.classList.remove('success');
        statusMessage.textContent = 'Error saving to database. Please try again.';
        saveButton.disabled = false;

        setTimeout(() => {
            statusDiv.style.display = 'none';
        }, 5000);
    });
}

// Reset inactivity timer for auto-save
function resetInactivityTimer() {
    if (saveTimeout) clearTimeout(saveTimeout);
    saveTimeout = setTimeout(() => {
        saveToDB();
    }, INACTIVITY_TIMEOUT);
}

// ----------------
// Drag & Drop Functions
// ----------------

// Create team element for a list
function createTeamElement(team) {
    const safeId   = sanitizeId(String(team));
    const info     = (window.TEAM_INFO || {})[parseInt(team, 10)] ||
                     (window.TEAM_INFO || {})[String(team)] || {};
    const nickname = (info.nickname || '').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    const logoUrl  = info.logo_url || '';

    const logoHtml = logoUrl
        ? `<img src="${logoUrl.replace(/"/g, '&quot;')}" alt="" class="team-logo" width="40" height="40"
               onerror="this.parentNode.replaceChild(Object.assign(document.createElement('div'),{className:'team-logo-placeholder'}),this)">`
        : `<div class="team-logo-placeholder"></div>`;

    const statsHtml = (window.buildStatChips ? window.buildStatChips(info) : '');

    return `
        <li draggable="true"
            ondragstart="onDragStart(event)"
            class="picklist_teams"
            ondrop="onDrop(event)"
            ondragover="onDragOver(event)"
            id="${safeId}"
            data-team="${safeId}">
            <div class="team-card-inner">
                ${logoHtml}
                <div class="team-text">
                    <button class="team-number-btn" onclick="openTeamModal(event,${parseInt(team,10)})" draggable="false">${team}</button>
                    <span class="team-nickname">${nickname}</span>
                    <span class="team-stats js-stats">${statsHtml}</span>
                </div>
            </div>
            <input type="checkbox" onchange="chosen(event)">
        </li>
    `;
}

// Handle drag start
function onDragStart(ev) {
    // Walk up to the draggable <li> in case the event fires on an inner element
    let target = ev.target;
    while (target && !target.classList.contains('picklist_teams')) {
        target = target.parentNode;
    }
    if (!target) return;
    draggedItem = target;
    ev.dataTransfer.setData("team_number_id", target.id);
    ev.dataTransfer.effectAllowed = "move";
    draggedItem.classList.add('being-dragged');
    resetInactivityTimer();
}

// Handle drag over
function onDragOver(ev) {
    ev.preventDefault();
    const list = getListElement(ev.target);
    if (!list || !draggedItem) return;

    const items = Array.from(list.children).filter(i => i !== draggedItem);
    if (items.length === 0) {
        list.appendChild(draggedItem);
        return;
    }

    const mouseY = ev.clientY;
    let closestItem = null;
    let closestDistance = Infinity;

    items.forEach(item => {
        const rect = item.getBoundingClientRect();
        const distance = Math.abs(mouseY - (rect.top + rect.height / 2));
        if (distance < closestDistance) {
            closestDistance = distance;
            closestItem = item;
        }
    });

    if (closestItem) {
        const rect = closestItem.getBoundingClientRect();
        const beforeOrAfter = mouseY < rect.top + rect.height / 2;
        list.insertBefore(draggedItem, beforeOrAfter ? closestItem : closestItem.nextSibling);
    }

    resetInactivityTimer();
}

// Handle drop
function onDrop(ev) {
    ev.preventDefault();
    const list = getListElement(ev.target);
    if (!list) return;

    const droppedItem = document.getElementById(ev.dataTransfer.getData("team_number_id"));
    if (droppedItem && draggedItem) {
        if (!list.contains(droppedItem)) list.appendChild(droppedItem);
        draggedItem.classList.remove('being-dragged');
        draggedItem = null;
        saveTemporary();
    }
    resetInactivityTimer();
}

// Get parent list element
function getListElement(element) {
    if (!element) return null;
    let el = element;
    while (el) {
        if (el.classList && el.classList.contains('lists')) return el;
        el = el.parentNode;
    }
    return null;
}

// ----------------
// Checkbox Function
// ----------------
function chosen(ev) {
    const listItem = ev.target.parentNode;
    const noPick = document.getElementById("no_pick");

    if (ev.target.checked) {
        noPick.appendChild(listItem);
        ev.target.checked = false;

        const strike = listItem.querySelector("s");
        if (strike) {
            const normal = document.createElement("p");
            normal.innerHTML = strike.innerHTML;
            strike.replaceWith(normal);
        }
    }
    saveTemporary();
    resetInactivityTimer();
}

// ----------------
// Fetch Updates with Throttling
// ----------------
function fetchUpdates() {
    if (isFetching) return;
    isFetching = true;

    fetch(`/strategy/picklist/submit/?comp=${encodeURIComponent(comp_code)}&timestamp=${lastTimestamp}&t=${Date.now()}`, {
        headers: { 'Cache-Control': 'no-cache', 'Pragma': 'no-cache' }
    })
    .then(res => res.json())
    .then(response => {
        if (response.status === 'updated') {
            lastTimestamp = response.timestamp;
            updateLists(response.data);
        } else if (response.status === 'no_change') {
            lastTimestamp = response.timestamp;
        }
    })
    .catch(err => console.error('Error fetching updates:', err))
    .finally(() => isFetching = false);
}

// Update lists with new data
function updateLists(data) {
    const lists = ['no_pick', '1st_pick', '2nd_pick', '3rd_pick', 'dnp', '4th_pick'];

    lists.forEach((listId, index) => {
        const list = document.getElementById(listId);
        if (!list) return;

        const currentTeams = Array.from(list.children).map(item => item.id);
        const newTeams = data[index] || [];

        if (JSON.stringify(currentTeams) !== JSON.stringify(newTeams) && !isDragBusy()) {
            list.innerHTML = newTeams.map(team => createTeamElement(team)).join('');
        }
    });

    dedupeLists();
}

// True while a card is detached from its list: either being dragged or still
// animating into place. Rebuilding the DOM during either duplicates or strands
// the card.
function isDragBusy() {
    return !!draggedItem || !!(window.__picklistDragBusy);
}

// Safety net. A team must appear exactly once across the whole board, so if
// anything ever puts a second copy in, drop it rather than let it reach the
// saved picklist (duplicate ids also break getElementById lookups).
function dedupeLists() {
    var seen = Object.create(null);
    ['no_pick', '1st_pick', '2nd_pick', '3rd_pick', 'dnp', '4th_pick'].forEach(function (listId) {
        var list = document.getElementById(listId);
        if (!list) return;
        Array.prototype.slice.call(list.children).forEach(function (li) {
            if (!li.id || li.classList.contains('picklist-placeholder')) return;
            if (seen[li.id]) { li.parentNode.removeChild(li); }
            else { seen[li.id] = true; }
        });
    });
}

// ----------------
// Initialize
// ----------------
document.addEventListener('DOMContentLoaded', function() {
    fetchUpdates();
    setInterval(fetchUpdates, FETCH_UPDATE_INTERVAL);
    resetInactivityTimer();
});


/* =========================================================================
   Pointer-based drag and drop
   -------------------------------------------------------------------------
   The board previously relied on the HTML5 drag-and-drop API, which never
   fires on touchscreens - so on the tablets strategy actually uses at
   competition, no team could be moved at all. It also had no `dragend`
   handler, so a cancelled drag left a card stuck in its dragging state.

   Pointer Events cover mouse, touch and pen with one code path, so this
   replaces the native API rather than sitting alongside it (two drag systems
   competing for the same element fight each other).
   ========================================================================= */
(function () {
    var LIST_IDS   = ['no_pick', '1st_pick', '2nd_pick', '3rd_pick', 'dnp', '4th_pick'];
    var THRESHOLD  = 6;     // px before a press becomes a drag, so taps still click
    var EDGE       = 60;    // px from viewport edge that triggers auto-scroll
    var EDGE_SPEED = 12;

    var card = null, placeholder = null, dragging = false, pointerId = null;
    var startX = 0, startY = 0, grabX = 0, grabY = 0, scrollTimer = null;
    var lastX = 0, tilt = 0, hoverList = null, settling = false;

    // Touch has to serve two gestures on the same surface: swiping the board
    // sideways between columns, and picking a card up. A press that stays
    // put for HOLD_MS becomes a drag; anything that moves first is a scroll
    // and is handed back to the browser. Mouse and pen keep the old
    // move-a-few-pixels behaviour, where there is no such ambiguity.
    var HOLD_MS = 200, HOLD_SLOP = 10;
    var isTouch = false, armed = false, holdTimer = null;

    function buzz(ms) {
        if (navigator.vibrate) { try { navigator.vibrate(ms); } catch (e) {} }
    }

    // FLIP: record where every card currently sits, let the DOM change, then
    // animate each one from its old box to its new one. Without this the
    // siblings jump instantly when the placeholder moves, which is the single
    // biggest thing that makes a sortable list feel cheap.
    function snapshot() {
        var boxes = new Map();
        lists().forEach(function (l) {
            Array.prototype.forEach.call(l.children, function (el) {
                if (el !== card) { boxes.set(el, el.getBoundingClientRect()); }
            });
        });
        return boxes;
    }

    function playFlip(boxes) {
        boxes.forEach(function (oldBox, el) {
            if (!el.parentNode) return;
            var nb = el.getBoundingClientRect();
            var dx = oldBox.left - nb.left;
            var dy = oldBox.top - nb.top;
            if (!dx && !dy) return;
            el.style.transition = 'none';
            el.style.transform = 'translate(' + dx + 'px,' + dy + 'px)';
            // Force a style flush so the browser treats the next change as a
            // transition rather than collapsing both into one frame.
            void el.offsetHeight;
            el.style.transition = 'transform 0.18s cubic-bezier(0.22,1,0.36,1)';
            el.style.transform = '';
        });
    }

    function lists() {
        return LIST_IDS.map(function (id) { return document.getElementById(id); })
                       .filter(Boolean);
    }

    // Native drag would otherwise start on desktop and conflict with ours.
    function disableNativeDrag(root) {
        (root || document).querySelectorAll('.picklist_teams[draggable]').forEach(function (li) {
            li.setAttribute('draggable', 'false');
        });
    }

    function cardFrom(target) {
        var el = target;
        while (el && el !== document.body) {
            if (el.classList && el.classList.contains('picklist_teams')) return el;
            el = el.parentNode;
        }
        return null;
    }

    function onPointerDown(ev) {
        if (settling) return;                                       // let the last card land
        if (ev.button !== undefined && ev.button !== 0) return;      // left button only
        // Let the team-number button and the checkbox behave normally.
        if (ev.target.closest('button, input, select, a, textarea')) return;

        var li = cardFrom(ev.target);
        if (!li) return;

        card = li;
        pointerId = ev.pointerId;
        startX = ev.clientX;
        startY = ev.clientY;
        var rect = li.getBoundingClientRect();
        grabX = ev.clientX - rect.left;
        grabY = ev.clientY - rect.top;

        isTouch = (ev.pointerType === 'touch');
        armed = !isTouch;                 // mouse and pen are ready at once
        clearTimeout(holdTimer);
        if (isTouch) {
            holdTimer = setTimeout(function () {
                if (!card || dragging) return;
                armed = true;
                beginDrag();              // lift it under the finger straight away
            }, HOLD_MS);
        }
    }

    // The press turned out to be a scroll. Let go of the card entirely so the
    // browser keeps the gesture.
    function abandonPress() {
        clearTimeout(holdTimer);
        card = null;
        pointerId = null;
        armed = false;
    }

    function beginDrag() {
        dragging = true;
        var rect = card.getBoundingClientRect();

        placeholder = document.createElement('li');
        placeholder.className = 'picklist-placeholder';
        placeholder.style.height = rect.height + 'px';
        card.parentNode.insertBefore(placeholder, card);

        card.classList.add('being-dragged');
        card.style.width    = rect.width + 'px';
        card.style.position = 'fixed';
        card.style.left     = rect.left + 'px';
        card.style.top      = rect.top + 'px';
        card.style.zIndex   = '9500';
        card.style.pointerEvents = 'none';
        document.body.appendChild(card);
        document.body.style.userSelect = 'none';
        document.body.classList.add('is-dragging');
        window.__picklistDragBusy = true;
        lastX = rect.left;
        buzz(12);   // a short tick confirms the card was picked up
    }

    function moveCard(x, y) {
        var left = x - grabX;
        // Lean into the direction of travel. Eased toward the target so the
        // tilt settles instead of jittering with every pointer sample.
        var target = Math.max(-7, Math.min(7, (left - lastX) * 0.9));
        tilt += (target - tilt) * 0.25;
        lastX = left;
        card.style.left = left + 'px';
        card.style.top  = (y - grabY) + 'px';
        card.style.transform = 'scale(1.03) rotate(' + tilt.toFixed(2) + 'deg)';
    }

    // Place the placeholder in whichever list is under the pointer, at the
    // position matching the pointer's vertical offset.
    function reposition(x, y) {
        // Called from the auto-scroll timer as well as from pointermove, so
        // it can fire in the gap after a drop has released these.
        if (!card || !placeholder) return;
        var under = document.elementFromPoint(x, y);
        if (!under) return;

        var list = under.closest ? under.closest('.lists') : null;
        if (!list) {
            var host = cardFrom(under);
            list = host ? host.parentNode : null;
        }
        if (!list || LIST_IDS.indexOf(list.id) === -1) return;

        if (hoverList !== list) {
            if (hoverList) { hoverList.classList.remove('drag-over'); }
            list.classList.add('drag-over');
            hoverList = list;
            buzz(6);   // crossing into a different column is worth feeling
        }

        var siblings = Array.prototype.slice.call(list.children)
            .filter(function (n) { return n !== placeholder && n !== card; });

        var before = null;
        for (var i = 0; i < siblings.length; i++) {
            var r = siblings[i].getBoundingClientRect();
            if (y < r.top + r.height / 2) { before = siblings[i]; break; }
        }

        // No-op guard: without it every pointer sample would restart the FLIP
        // animation and the siblings would never finish moving.
        if (placeholder.parentNode === list && placeholder.nextSibling === before) return;

        var boxes = snapshot();
        list.insertBefore(placeholder, before);
        playFlip(boxes);
    }

    function board() { return document.querySelector('.picklist-board'); }

    // Auto-scroll while a card is held near an edge. Vertical scrolls the
    // page as before; horizontal scrolls the board, which is what makes it
    // possible to carry a card from "No Pick" across to "Third Pick" on a
    // phone, where only one column is on screen at a time.
    function autoScroll(x, y) {
        clearInterval(scrollTimer);

        var vdir = 0;
        if (y < EDGE) vdir = -1;
        else if (y > window.innerHeight - EDGE) vdir = 1;

        // A column has its own scrollbar once it is taller than its cap, so
        // near its top or bottom edge scroll the column rather than the page
        // - otherwise a long list cannot be reordered while holding a card.
        var col = hoverList;
        var colScrolls = col && col.scrollHeight > col.clientHeight + 2;
        var cdir = 0;
        if (colScrolls) {
            var cb = col.getBoundingClientRect();
            if (y < cb.top + EDGE) cdir = -1;
            else if (y > cb.bottom - EDGE) cdir = 1;
            if (cdir) vdir = 0;           // the column takes priority
        }

        var hdir = 0;
        var strip = board();
        var scrollable = strip && strip.scrollWidth > strip.clientWidth + 2;
        if (scrollable) {
            var box = strip.getBoundingClientRect();
            if (x < box.left + EDGE) hdir = -1;
            else if (x > box.right - EDGE) hdir = 1;
        }

        if (!vdir && !hdir && !cdir) return;
        scrollTimer = setInterval(function () {
            if (vdir) { window.scrollBy(0, vdir * EDGE_SPEED); }
            if (cdir && col) {
                col.scrollTop += cdir * EDGE_SPEED;
                reposition(x, y);
            }
            if (hdir && strip) {
                strip.scrollLeft += hdir * EDGE_SPEED;
                // The pointer has not moved, but a different column is now
                // underneath it, so the placeholder has to be re-placed.
                reposition(x, y);
            }
        }, 16);
    }

    function onPointerMove(ev) {
        if (!card || ev.pointerId !== pointerId) return;

        if (!dragging) {
            if (isTouch && !armed) {
                // Moved before the hold completed, so this is a swipe.
                if (Math.abs(ev.clientX - startX) > HOLD_SLOP ||
                    Math.abs(ev.clientY - startY) > HOLD_SLOP) {
                    abandonPress();
                }
                return;                   // no preventDefault: let it scroll
            }
            if (Math.abs(ev.clientX - startX) < THRESHOLD &&
                Math.abs(ev.clientY - startY) < THRESHOLD) return;
            beginDrag();
        }
        ev.preventDefault();
        moveCard(ev.clientX, ev.clientY);
        reposition(ev.clientX, ev.clientY);
        autoScroll(ev.clientX, ev.clientY);
    }

    // Drop the card into its slot by gliding it there, rather than teleporting.
    function finish(cancelled) {
        clearInterval(scrollTimer);
        document.body.style.userSelect = '';
        document.body.classList.remove('is-dragging');
        if (hoverList) { hoverList.classList.remove('drag-over'); hoverList = null; }

        if (!dragging || !card || !placeholder) {
            card = null; placeholder = null; dragging = false; pointerId = null;
            window.__picklistDragBusy = false;
            return;
        }

        var movingCard = card, gap = placeholder;
        var target = gap.parentNode;                 // destination list, for recovery
        var toBox  = gap.getBoundingClientRect();
        var from   = movingCard.getBoundingClientRect();

        // Release local state now so a new drag can start even mid-settle.
        card = null; placeholder = null; dragging = false; pointerId = null;
        settling = true;

        function land() {
            settling = false;
            var host = gap.parentNode;
            if (host) {
                host.insertBefore(movingCard, gap);
                host.removeChild(gap);
            } else {
                // Something replaced the list contents mid-drag. Put the card
                // back in the column it was headed for; if that is gone too,
                // fall back to the pool so it can never be lost.
                var fallback = (target && target.isConnected ? target : null) ||
                               document.getElementById('no_pick');
                if (fallback) { fallback.appendChild(movingCard); }
                else { movingCard.parentNode && movingCard.parentNode.removeChild(movingCard); }
            }
            movingCard.classList.remove('being-dragged');
            ['width', 'position', 'left', 'top', 'zIndex', 'pointerEvents', 'transform', 'transition']
                .forEach(function (prop) { movingCard.style[prop] = ''; });
            window.__picklistDragBusy = false;
            if (typeof dedupeLists === 'function') { dedupeLists(); }
            if (!cancelled && typeof saveTemporary === 'function') { saveTemporary(); }
            if (typeof resetInactivityTimer === 'function') { resetInactivityTimer(); }
        }

        // Already there: skip the animation rather than play a 0px one.
        if (Math.abs(from.left - toBox.left) < 1 && Math.abs(from.top - toBox.top) < 1) {
            movingCard.style.transform = '';
            land();
            return;
        }

        movingCard.style.transition =
            'left 0.19s cubic-bezier(0.22,1,0.36,1), top 0.19s cubic-bezier(0.22,1,0.36,1), transform 0.19s ease-out';
        movingCard.style.left = toBox.left + 'px';
        movingCard.style.top  = toBox.top + 'px';
        movingCard.style.transform = 'scale(1) rotate(0deg)';
        buzz(cancelled ? 4 : 10);

        var done = false;
        function once() { if (done) return; done = true; land(); }
        movingCard.addEventListener('transitionend', once, { once: true });
        setTimeout(once, 260);   // guarantee landing if the transition is dropped
    }

    function onPointerUp(ev) {
        clearTimeout(holdTimer);
        if (!card || ev.pointerId !== pointerId) return;
        finish(false);
    }

    // A cancelled pointer (ESC, browser gesture, lost capture) must still
    // restore the card - the old code had no equivalent and left it stuck.
    function onPointerCancel(ev) {
        clearTimeout(holdTimer);
        if (!card || ev.pointerId !== pointerId) return;
        finish(true);
    }

    function init() {
        disableNativeDrag();
        document.addEventListener('pointerdown', onPointerDown, { passive: true });
        document.addEventListener('pointermove', onPointerMove, { passive: false });
        document.addEventListener('pointerup', onPointerUp);
        document.addEventListener('pointercancel', onPointerCancel);
        window.addEventListener('blur', function () { if (card) finish(true); });

        // Cards added later (createTeamElement) must also lose native drag.
        lists().forEach(function (l) {
            new MutationObserver(function () { disableNativeDrag(l); })
                .observe(l, { childList: true });
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else { init(); }
})();


/* =========================================================================
   Star, strike, attribute filters, column sort and undo
   -------------------------------------------------------------------------
   Star and strike are stored server-side rather than on the DOM node. The
   board rebuilds itself from the server every few seconds (updateLists), so
   anything held only in the markup would silently vanish mid-draft - exactly
   when it matters most. The flags ride along on the existing poll response,
   so a star set on one tablet shows up on every other one.
   ========================================================================= */
(function () {
    var LIST_IDS = ['no_pick', '1st_pick', '2nd_pick', '3rd_pick', 'dnp', '4th_pick'];

    function flags() {
        if (!window.PICKLIST_FLAGS) { window.PICKLIST_FLAGS = { starred: [], struck: [] }; }
        var f = window.PICKLIST_FLAGS;
        if (!Array.isArray(f.starred)) { f.starred = []; }
        if (!Array.isArray(f.struck)) { f.struck = []; }
        return f;
    }

    function has(flag, team) { return flags()[flag].indexOf(String(team)) !== -1; }

    function lists() {
        return LIST_IDS.map(function (id) { return document.getElementById(id); }).filter(Boolean);
    }

    function cards() {
        return Array.prototype.slice.call(document.querySelectorAll('.picklist_teams'));
    }

    function teamOf(li) { return String(li.dataset.team || li.id || ''); }

    // ---- Rendering -------------------------------------------------------

    var STAR_SVG = '<svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"><path d="M12 2.5l2.9 5.9 6.6.9-4.8 4.6 1.2 6.5L12 17.3 6.1 20.4l1.2-6.5L2.5 9.3l6.6-.9z"/></svg>';
    var STRIKE_SVG = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><path d="M4 12h16"/></svg>';

    // Buttons are injected rather than written into the template because the
    // same card markup is produced in two places (the server render and
    // createTeamElement); injecting keeps them from drifting apart.
    function decorate(li) {
        if (!li || li.querySelector('.card-flags')) { return; }
        var team = teamOf(li);
        if (!team) { return; }

        var wrap = document.createElement('div');
        wrap.className = 'card-flags';

        var star = document.createElement('button');
        star.type = 'button';
        star.className = 'flag-btn flag-star';
        star.title = 'Star - float this team to the top of its column';
        star.setAttribute('aria-label', 'Star team ' + team);
        star.innerHTML = STAR_SVG;
        star.onclick = function (ev) { ev.stopPropagation(); toggle('starred', team); };

        var strike = document.createElement('button');
        strike.type = 'button';
        strike.className = 'flag-btn flag-strike';
        strike.title = 'Strike out - this team has already been picked';
        strike.setAttribute('aria-label', 'Strike out team ' + team);
        strike.innerHTML = STRIKE_SVG;
        strike.onclick = function (ev) { ev.stopPropagation(); toggle('struck', team); };

        wrap.appendChild(star);
        wrap.appendChild(strike);

        // Sits alongside the existing checkbox rather than replacing it.
        var box = li.querySelector('input[type="checkbox"]');
        if (box) { li.insertBefore(wrap, box); } else { li.appendChild(wrap); }
    }

    function paint(li) {
        var team = teamOf(li);
        var starred = has('starred', team);
        var struck = has('struck', team);
        li.classList.toggle('is-starred', starred);
        li.classList.toggle('is-struck', struck);
        var star = li.querySelector('.flag-star');
        var strike = li.querySelector('.flag-strike');
        if (star) { star.classList.toggle('is-on', starred); }
        if (strike) { strike.classList.toggle('is-on', struck); }
    }

    function refresh() {
        cards().forEach(function (li) { decorate(li); paint(li); });
        var moved = floatStarred();
        applyFilters();
        updateUndoButton();
        // Persist the reorder, but never mid-drag: saving then would capture a
        // board with the dragged card detached from its column.
        if (moved && !restoring && typeof isDragBusy === 'function' && !isDragBusy()) {
            if (typeof saveTemporary === 'function') { saveTemporary(); }
        }
    }
    window.refreshPicklistFlags = refresh;

    // ---- Persistence -----------------------------------------------------

    function toggle(flag, team) {
        var list = flags()[flag];
        var on = list.indexOf(String(team)) === -1;

        // Update locally first so the board reacts immediately; the poll
        // reconciles if the server disagrees.
        if (on) {
            list.push(String(team));
        } else {
            flags()[flag] = list.filter(function (t) { return t !== String(team); });
        }
        refresh();

        fetch('/strategy/picklist/flags/?comp=' + encodeURIComponent(comp_code), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCookie('csrftoken') },
            body: JSON.stringify({ flag: flag, team: String(team), on: on })
        })
        .then(function (r) { return r.json(); })
        .then(function (res) {
            if (res && res.flags) { window.PICKLIST_FLAGS = res.flags; refresh(); }
        })
        .catch(function (err) { console.error('Error saving flag:', err); });

        if (navigator.vibrate) { try { navigator.vibrate(8); } catch (e) {} }
    }

    // ---- Starred to the top ---------------------------------------------

    // Starred teams rise to the top of whichever column they are in, so the
    // shortlist reads off the top of "no pick" without dragging anything.
    function floatStarred() {
        var moved = false;
        lists().forEach(function (list) {
            var items = Array.prototype.slice.call(list.children)
                .filter(function (n) { return n.classList && n.classList.contains('picklist_teams'); });
            var starred = items.filter(function (li) { return has('starred', teamOf(li)); });
            if (!starred.length) { return; }

            // Already sitting at the top in order? Leave the DOM alone, so the
            // saved order and the board agree and the poll stops rebuilding.
            var settled = starred.every(function (li, i) { return items[i] === li; });
            if (settled) { return; }

            // Reversed so the original relative order survives once each is
            // prepended in turn.
            starred.slice().reverse().forEach(function (li) {
                list.insertBefore(li, list.firstChild);
            });
            moved = true;
        });
        return moved;
    }

    // ---- Attribute filters ----------------------------------------------

    var active = {};   // {pit_key: [selected values]}

    function infoFor(team) {
        var I = window.TEAM_INFO || {};
        return I[parseInt(team, 10)] || I[String(team)] || {};
    }

    function matchesFilters(team) {
        var pit = infoFor(team).pit || {};
        for (var key in active) {
            if (!active[key] || !active[key].length) { continue; }
            var value = pit[key];
            if (value === undefined || value === null) { return false; }
            // multiselect answers arrive as arrays, select answers as strings.
            var owned = Array.isArray(value) ? value.map(String) : [String(value)];
            var hit = active[key].some(function (want) { return owned.indexOf(String(want)) !== -1; });
            if (!hit) { return false; }
        }
        return true;
    }

    function applyFilters() {
        var any = Object.keys(active).some(function (k) { return active[k] && active[k].length; });
        cards().forEach(function (li) {
            li.classList.toggle('filtered-out', any && !matchesFilters(teamOf(li)));
        });
        var count = 0;
        Object.keys(active).forEach(function (k) { count += (active[k] || []).length; });
        var badge = document.getElementById('filter-active-count');
        if (badge) { badge.hidden = count === 0; badge.textContent = count; }
        if (typeof window.updateColumnCounts === 'function') { window.updateColumnCounts(); }
    }

    window.togglePicklistFilters = function () {
        var panel = document.getElementById('picklist-filters');
        var btn = document.getElementById('filter-toggle');
        if (!panel) { return; }
        panel.hidden = !panel.hidden;
        if (btn) { btn.classList.toggle('is-on', !panel.hidden); }
    };

    window.clearPicklistFilters = function () {
        active = {};
        document.querySelectorAll('.filter-chip.is-on').forEach(function (c) {
            c.classList.remove('is-on');
        });
        applyFilters();
    };

    function bindFilterChips() {
        document.querySelectorAll('.filter-chip').forEach(function (chip) {
            chip.addEventListener('click', function () {
                var key = chip.dataset.key;
                var value = chip.dataset.value;
                active[key] = active[key] || [];
                var at = active[key].indexOf(value);
                if (at === -1) {
                    active[key].push(value);
                    chip.classList.add('is-on');
                } else {
                    active[key].splice(at, 1);
                    chip.classList.remove('is-on');
                }
                applyFilters();
            });
        });
    }

    // ---- Sort every column ----------------------------------------------

    // Sorting rewrites DOM order, which is what saveTemporary reads, so a sort
    // is a real reordering of the picklist rather than a display-only trick.
    window.sortAllColumns = function (mode) {
        if (!mode) { return; }
        lists().forEach(function (list) { sortList(list, mode); });
        floatStarred();
        if (typeof saveTemporary === 'function') { saveTemporary(); }
        if (typeof resetInactivityTimer === 'function') { resetInactivityTimer(); }
    };

    function sortList(list, mode) {
        var items = Array.prototype.slice.call(list.children)
            .filter(function (n) { return n.classList && n.classList.contains('picklist_teams'); });

        items.sort(function (a, b) {
            var ia = infoFor(teamOf(a));
            var ib = infoFor(teamOf(b));
            var na = parseInt(teamOf(a), 10);
            var nb = parseInt(teamOf(b), 10);
            if (mode === 'number') { return na - nb; }
            if (mode === 'rank') { return (ia.rank || 1e9) - (ib.rank || 1e9) || na - nb; }
            if (mode === 'driver') { return (ib.driver || -1) - (ia.driver || -1) || na - nb; }
            if (mode === 'matches') { return (ib.matches || 0) - (ia.matches || 0) || na - nb; }
            // Anything else is a config metric key: highest first.
            var va = (ia.metrics || {})[mode] || 0;
            var vb = (ib.metrics || {})[mode] || 0;
            return vb - va || na - nb;
        });

        items.forEach(function (li) { list.appendChild(li); });
    }

    // Metric options come from the game config, so a new season's metrics
    // appear in the dropdown without a code change.
    function fillSortOptions() {
        var sel = document.getElementById('board-sort');
        if (!sel || !window.PICKLIST_SORTS) { return; }
        window.PICKLIST_SORTS.forEach(function (m) {
            var o = document.createElement('option');
            o.value = m.key;
            o.textContent = m.label;
            sel.appendChild(o);
        });
    }

    // ---- Undo ------------------------------------------------------------

    var history = [];
    var MAX_HISTORY = 30;
    var restoring = false;

    function snapshotBoard() {
        return LIST_IDS.map(function (id) {
            var l = document.getElementById(id);
            if (!l) { return []; }
            return Array.prototype.slice.call(l.children)
                .filter(function (n) { return n.id; })
                .map(function (n) { return n.id; });
        });
    }

    function pushHistory() {
        if (restoring) { return; }
        var snap = snapshotBoard();
        var last = history[history.length - 1];
        if (last && JSON.stringify(last) === JSON.stringify(snap)) { return; }
        history.push(snap);
        if (history.length > MAX_HISTORY) { history.shift(); }
        updateUndoButton();
    }

    function updateUndoButton() {
        var btn = document.getElementById('undo-btn');
        if (btn) { btn.disabled = history.length < 2; }
    }

    window.picklistUndo = function () {
        if (history.length < 2) { return; }
        history.pop();                       // discard the current state
        var target = history[history.length - 1];
        restoring = true;
        try {
            LIST_IDS.forEach(function (id, i) {
                var list = document.getElementById(id);
                if (!list) { return; }
                (target[i] || []).forEach(function (teamId) {
                    var li = document.getElementById(teamId);
                    if (li) { list.appendChild(li); }
                });
            });
            if (typeof dedupeLists === 'function') { dedupeLists(); }
            refresh();
            if (typeof saveTemporary === 'function') { saveTemporary(); }
            if (typeof resetInactivityTimer === 'function') { resetInactivityTimer(); }
        } finally {
            restoring = false;
        }
        updateUndoButton();
        if (navigator.vibrate) { try { navigator.vibrate(14); } catch (e) {} }
    };

    document.addEventListener('keydown', function (ev) {
        if ((ev.ctrlKey || ev.metaKey) && (ev.key === 'z' || ev.key === 'Z')) {
            var tag = (ev.target.tagName || '').toLowerCase();
            if (tag === 'input' || tag === 'textarea') { return; }   // let text undo work
            ev.preventDefault();
            window.picklistUndo();
        }
    });

    // ---- Wiring ----------------------------------------------------------

    function init() {
        fillSortOptions();
        bindFilterChips();
        refresh();
        pushHistory();

        // Re-decorate whenever the board is rebuilt by the poll or a drag.
        lists().forEach(function (l) {
            new MutationObserver(function () {
                cards().forEach(function (li) { decorate(li); paint(li); });
                applyFilters();
                pushHistory();
            }).observe(l, { childList: true });
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else { init(); }
})();


/* =========================================================================
   Extra pick columns (3rd, 4th) and the phone column tabs
   -------------------------------------------------------------------------
   3rd and 4th pick are off until switched on in the toolbar, and the choice
   is remembered on this device. A column that holds teams is always shown: the
   board is rebuilt from the server every few seconds, so hiding one that
   another tablet just filled would make those teams disappear.

   On a phone the board scrolls sideways one column at a time; the tab strip
   jumps straight to a column and shows which one is in view.
   ========================================================================= */
(function () {
    var OPTIONAL = ['3rd_pick', '4th_pick'];
    var TABS = [
        { id: 'no_pick',  label: 'No Pick' },
        { id: '1st_pick', label: '1st' },
        { id: '2nd_pick', label: '2nd' },
        { id: '3rd_pick', label: '3rd' },
        { id: '4th_pick', label: '4th' },
        { id: 'dnp',      label: 'DNP' }
    ];
    var board, tabs, queued = false;

    function wrapOf(id) { return document.getElementById('colwrap-' + id) || (document.getElementById(id) || {}).parentNode; }
    function listOf(id) { return document.getElementById(id); }
    function teamCount(id) {
        var l = listOf(id);
        return l ? l.querySelectorAll('.picklist_teams:not(.picklist-placeholder)').length : 0;
    }
    function key(id) { return 'picklistShow_' + id; }

    function applyVisibility() {
        OPTIONAL.forEach(function (id) {
            var wrap = wrapOf(id), box = document.getElementById('toggle-' + id);
            if (!wrap || !box) return;
            var has = teamCount(id) > 0;
            if (has) { box.checked = true; }
            box.disabled = has;
            box.title = has ? 'Move the teams out of this column to hide it' : '';
            wrap.hidden = !box.checked;
        });
        buildTabs();
    }

    function visibleTabs() {
        return TABS.filter(function (t) {
            var w = wrapOf(t.id);
            return w && !w.hidden;
        });
    }

    function buildTabs() {
        if (!tabs) return;
        var list = visibleTabs();
        tabs.innerHTML = '';
        list.forEach(function (t) {
            var b = document.createElement('button');
            b.type = 'button';
            b.className = 'board-tab';
            b.dataset.target = t.id;
            b.innerHTML = t.label + ' <span class="tab-count">' + teamCount(t.id) + '</span>';
            b.addEventListener('click', function () { goTo(t.id); });
            tabs.appendChild(b);
        });
        markCurrent();
    }

    function goTo(id) {
        var wrap = wrapOf(id);
        if (!wrap || !board) return;
        var left = wrap.getBoundingClientRect().left - board.getBoundingClientRect().left + board.scrollLeft;
        board.scrollTo({ left: left, behavior: 'smooth' });
    }

    function markCurrent() {
        if (!tabs || !board) return;
        var best = null, bestDist = Infinity;
        var origin = board.getBoundingClientRect().left;
        visibleTabs().forEach(function (t) {
            var d = Math.abs(wrapOf(t.id).getBoundingClientRect().left - origin);
            if (d < bestDist) { bestDist = d; best = t.id; }
        });
        Array.prototype.forEach.call(tabs.children, function (b) {
            var on = b.dataset.target === best;
            if (on) { b.setAttribute('aria-current', 'true'); } else { b.removeAttribute('aria-current'); }
            if (on && tabs.scrollWidth > tabs.clientWidth) {
                tabs.scrollTo({ left: b.offsetLeft - 16, behavior: 'smooth' });
            }
        });
    }

    function schedule() {
        if (queued) return;
        queued = true;
        requestAnimationFrame(function () { queued = false; applyVisibility(); });
    }

    function init() {
        board = document.getElementById('picklist-board');
        tabs = document.getElementById('board-tabs');
        if (!board) return;

        OPTIONAL.forEach(function (id) {
            var box = document.getElementById('toggle-' + id);
            if (!box) return;
            try { box.checked = localStorage.getItem(key(id)) === '1'; } catch (e) { /* default off */ }
            box.addEventListener('change', function () {
                try { localStorage.setItem(key(id), box.checked ? '1' : '0'); } catch (e) { /* still applies */ }
                applyVisibility();
                if (box.checked) { goTo(id); }
            });
        });

        board.addEventListener('scroll', function () {
            if (!queued) { queued = true; requestAnimationFrame(function () { queued = false; markCurrent(); }); }
        }, { passive: true });

        /* Counts and contents change as cards move and as the server refreshes the board. */
        new MutationObserver(schedule).observe(board, { childList: true, subtree: true });
        applyVisibility();
    }

    if (document.readyState === 'loading') { document.addEventListener('DOMContentLoaded', init); }
    else { init(); }
})();
