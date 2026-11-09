(function () {
  var meta = document.querySelector('meta[name="csrf-token"]');
  var csrf = meta ? meta.content : '';

  /* ---------- confirm destructive actions ---------- */
  document.addEventListener('submit', function (e) {
    var msg = e.target.getAttribute('data-confirm');
    if (msg && !window.confirm(msg)) e.preventDefault();
  });

  /* ---------- show the selected file name ---------- */
  function humanSize(bytes) {
    if (!bytes) return '0 B';
    var units = ['B', 'KB', 'MB', 'GB'];
    var i = Math.floor(Math.log(bytes) / Math.log(1024));
    i = Math.min(i, units.length - 1);
    return (bytes / Math.pow(1024, i)).toFixed(i ? 1 : 0) + ' ' + units[i];
  }

  document.querySelectorAll('[data-file-input]').forEach(function (input) {
    input.addEventListener('change', function () {
      var zone = input.previousElementSibling;
      var label = zone && zone.querySelector ? zone.querySelector('.upload-name') : null;
      if (!label) label = document.getElementById('upload-name');
      if (!label) return;
      if (input.files && input.files.length) {
        label.textContent = input.files[0].name + ' · ' + humanSize(input.files[0].size);
      } else {
        label.textContent = 'No file selected';
      }
    });
  });

  /* ---------- copy invite codes ---------- */
  document.querySelectorAll('[data-copy]').forEach(function (button) {
    button.addEventListener('click', function () {
      var text = button.getAttribute('data-copy') || '';
      var original = button.textContent;
      function copied() {
        button.textContent = 'Copied';
        window.setTimeout(function () { button.textContent = original; }, 1600);
      }
      function fallback() {
        var input = document.createElement('textarea');
        input.value = text;
        input.setAttribute('readonly', '');
        input.style.position = 'fixed';
        input.style.opacity = '0';
        document.body.appendChild(input);
        input.select();
        try { document.execCommand('copy'); copied(); } catch (err) { /* no-op */ }
        input.remove();
      }
      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(text).then(copied).catch(fallback);
      } else {
        fallback();
      }
    });
  });

  /* ---------- challenge countdowns ---------- */
  function remainingText(ms) {
    var total = Math.max(0, Math.floor(ms / 1000));
    var days = Math.floor(total / 86400);
    var hours = Math.floor((total % 86400) / 3600);
    var mins = Math.floor((total % 3600) / 60);
    var secs = total % 60;
    if (days) return days + 'd ' + hours + 'h';
    if (hours) return hours + 'h ' + mins + 'm';
    if (mins) return mins + 'm ' + secs + 's';
    return secs + 's';
  }

  function updateCountdowns() {
    document.querySelectorAll('[data-countdown-to]').forEach(function (node) {
      var target = Date.parse(node.getAttribute('data-countdown-to') || '');
      if (isNaN(target)) return;
      var left = target - Date.now();
      node.textContent = left > 0 ? remainingText(left) : 'now';
    });
  }
  window.setInterval(updateCountdowns, 1000);
  updateCountdowns();

  /* ---------- challenge browser filtering ---------- */
  var search = document.getElementById('challenge-search');
  var filterBar = document.getElementById('category-filter');
  var sections = Array.prototype.slice.call(document.querySelectorAll('.cat'));
  var emptyFilter = document.getElementById('filter-empty');
  var activeCat = 'all';

  function applyFilters() {
    if (!sections.length) return;
    var q = search ? search.value.trim().toLowerCase() : '';
    var anyVisible = false;

    sections.forEach(function (section) {
      var catMatches = activeCat === 'all' || section.dataset.category === activeCat;
      var cards = section.querySelectorAll('.card');
      var visibleInSection = 0;

      cards.forEach(function (card) {
        var desc = card.querySelector('.desc');
        var haystack = [
          card.dataset.title || '',
          card.dataset.category || '',
          desc ? desc.textContent : ''
        ].join(' ').toLowerCase();
        var show = catMatches && (!q || haystack.indexOf(q) !== -1);
        card.hidden = !show;
        if (show) visibleInSection++;
      });

      section.hidden = visibleInSection === 0;
      if (visibleInSection > 0) anyVisible = true;
    });

    if (emptyFilter) emptyFilter.hidden = anyVisible;
  }

  if (search) search.addEventListener('input', applyFilters);
  if (filterBar) {
    filterBar.addEventListener('click', function (e) {
      var chip = e.target.closest('.chip');
      if (!chip) return;
      filterBar.querySelectorAll('.chip').forEach(function (c) {
        c.classList.toggle('is-active', c === chip);
      });
      activeCat = chip.dataset.cat || 'all';
      applyFilters();
    });
  }

  /* ---------- Infinity Stones ---------- */
  var stoneEls = Array.prototype.slice.call(document.querySelectorAll('.stone[data-stone]'));
  var stoneCats = {};
  stoneEls.forEach(function (li) {
    var style = window.getComputedStyle(li);
    var color = style.getPropertyValue('--stone').trim();
    var rgb = style.getPropertyValue('--stone-rgb').trim();
    if (li.dataset.cat) stoneCats[li.dataset.cat] = { color: color, rgb: rgb };
  });

  function stoneToast(stone) {
    var toast = document.createElement('div');
    toast.className = 'stone-toast';
    toast.setAttribute('role', 'status');
    if (stone.color) toast.style.setProperty('--stone', stone.color);
    if (stone.rgb) toast.style.setProperty('--stone-rgb', stone.rgb);
    toast.textContent = stone.name + ' Stone collected.';
    document.body.appendChild(toast);
    window.setTimeout(function () { toast.remove(); }, 2900);
  }

  function updateStoneSection(stone) {
    var sections = document.querySelectorAll('.cat[data-category]');
    for (var i = 0; i < sections.length; i++) {
      if (sections[i].dataset.category !== stone.cat) continue;
      var badge = sections[i].querySelector('.cat-stone');
      if (badge) {
        var label = stone.solved + '/' + stone.total;
        if (stone.unlocked) {
          label += ' · Stone claimed';
        } else if (stone.total > stone.required) {
          label += ' · ' + Math.max(stone.required - stone.solved, 0) + ' to claim';
        }
        badge.textContent = label;
        badge.classList.toggle('is-earned', !!stone.unlocked);
      }
      break;
    }
  }

  function renderStones(payload) {
    if (!payload || !payload.stones) return;
    payload.stones.forEach(function (stone) {
      var li = document.getElementById('stone-' + stone.key);
      var wasEarned = li ? li.classList.contains('is-earned') : false;
      if (li) {
        var frac = li.querySelector('.stone-frac');
        if (frac) frac.textContent = stone.solved + '/' + stone.total;
        var bar = li.querySelector('.stone-bar i');
        if (bar) {
          var counted = Math.min(stone.solved, stone.required);
          bar.style.width = (stone.required ? Math.round((counted / stone.required) * 100) : 0) + '%';
        }
        li.title = stone.solved + ' of ' + stone.total + ' solved · ' +
          stone.required + ' needed';
        var state = li.querySelector('.stone-state');
        if (state) state.textContent = stone.unlocked ? 'Collected' : 'Locked';
        if (stone.unlocked) {
          li.classList.add('is-earned');
          if (!wasEarned) {
            li.classList.add('just-earned');
            window.setTimeout(function () { li.classList.remove('just-earned'); }, 1600);
            stoneToast(stone);
          }
        }
      }
      updateStoneSection(stone);
    });
    var count = document.getElementById('stones-count');
    if (count) count.textContent = String(payload.stones_collected);
    var panel = document.getElementById('stone-panel');
    if (panel && payload.stones_complete) panel.classList.add('is-complete');
    var snapZone = document.getElementById('snap-zone');
    if (snapZone && payload.stones_complete) snapZone.hidden = false;
  }

  /* ---------- the Snap ---------- */
  var snapBtn = document.getElementById('snap-btn');
  var snapOverlay = document.getElementById('snap-overlay');
  var snapUndo = document.getElementById('snap-undo');

  function closeSnap() {
    if (!snapOverlay) return;
    snapOverlay.hidden = true;
    document.body.classList.remove('snapped');
    if (snapBtn) snapBtn.focus({ preventScroll: true });
  }

  if (snapBtn && snapOverlay) {
    snapBtn.addEventListener('click', function () {
      snapOverlay.hidden = false;
      document.body.classList.add('snapped');
      if (snapUndo) snapUndo.focus({ preventScroll: true });
    });
  }
  if (snapUndo) snapUndo.addEventListener('click', closeSnap);
  if (snapOverlay) {
    snapOverlay.addEventListener('click', function (e) {
      if (e.target === snapOverlay) closeSnap();
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && !snapOverlay.hidden) closeSnap();
    });
  }

  /* ---------- challenge modal ---------- */
  var modal = document.getElementById('modal');
  if (!modal) return;

  var el = {
    title: document.getElementById('m-title'),
    cat: document.getElementById('m-cat'),
    meta: document.getElementById('m-meta'),
    status: document.getElementById('m-status'),
    schedule: document.getElementById('m-schedule'),
    desc: document.getElementById('m-desc'),
    hints: document.getElementById('m-hints'),
    hintList: document.getElementById('m-hint-list'),
    resources: document.getElementById('m-resources'),
    link: document.getElementById('m-link'),
    file: document.getElementById('m-file'),
    filename: document.getElementById('m-filename'),
    filemeta: document.getElementById('m-filemeta'),
    flag: document.getElementById('m-flag'),
    send: document.getElementById('m-send'),
    result: document.getElementById('m-result'),
    close: document.getElementById('m-close')
  };
  var current = null;

  function setResult(kind, text) {
    el.result.className = 'result ' + kind;
    el.result.textContent = text;
  }

  function populateHints(card) {
    if (!el.hints || !el.hintList) return;
    var hints = [];
    try {
      hints = JSON.parse(card.dataset.hints || '[]');
    } catch (err) {
      hints = [];
    }
    if (!Array.isArray(hints)) hints = [];
    hints = hints.map(function (hint) {
      return String(hint).trim();
    }).filter(function (hint) {
      return hint.length > 0;
    });

    el.hintList.textContent = '';
    hints.forEach(function (text, i) {
      var item = document.createElement('details');
      item.className = 'hint-item';
      var summary = document.createElement('summary');
      summary.textContent = 'Hint ' + (i + 1);
      var body = document.createElement('p');
      body.textContent = text;
      item.appendChild(summary);
      item.appendChild(body);
      el.hintList.appendChild(item);
    });
    el.hints.hidden = hints.length === 0;
  }

  function openCard(card) {
    current = card;
    var solved = card.dataset.solved === '1';
    var viewState = card.dataset.state || 'live';
    var actualState = card.dataset.scheduleState || viewState;
    var locked = viewState === 'scheduled' || viewState === 'ended';
    var teamRequired = card.dataset.teamRequired === '1';

    el.title.textContent = card.dataset.title;
    if (el.cat) {
      el.cat.textContent = card.dataset.category || 'Challenge';
      var stone = stoneCats[card.dataset.category || ''];
      if (stone) {
        el.cat.classList.add('stone-chip');
        if (stone.color) el.cat.style.setProperty('--stone', stone.color);
        if (stone.rgb) el.cat.style.setProperty('--stone-rgb', stone.rgb);
      }
    }
    el.meta.textContent = card.dataset.points + ' pts';
    el.desc.textContent = card.querySelector('.desc') ? card.querySelector('.desc').textContent : '';
    populateHints(card);
    if (el.status) el.status.hidden = !solved;

    if (el.schedule) {
      var scheduleText = '';
      if (actualState === 'scheduled') {
        scheduleText = 'Scheduled to start ' + (card.dataset.startLabel || 'soon') + '.';
      } else if (actualState === 'ended') {
        scheduleText = 'This challenge ended ' + (card.dataset.endLabel || '') + '.';
      } else if (card.dataset.end) {
        scheduleText = 'Live now · ends ' + (card.dataset.endLabel || '') + '.';
      }
      if (scheduleText && viewState === 'live' && actualState !== 'live') {
        scheduleText += ' Admin preview is allowed.';
      }
      el.schedule.textContent = scheduleText;
      el.schedule.hidden = !scheduleText;
    }

    var hasLink = !!card.dataset.url;
    var hasFile = !!card.dataset.file;
    var resources = el.resources;
    if (resources) resources.hidden = !hasLink && !hasFile;

    if (el.link) {
      if (hasLink) {
        el.link.href = card.dataset.url;
        el.link.hidden = false;
      } else {
        el.link.hidden = true;
        el.link.removeAttribute('href');
      }
    }

    if (el.file) {
      if (hasFile) {
        el.file.href = '/challenges/' + card.dataset.id + '/download';
        el.file.hidden = false;
        if (el.filename) el.filename.textContent = card.dataset.filename ? 'Download ' + card.dataset.filename : 'Download file';
      } else {
        el.file.hidden = true;
        el.file.removeAttribute('href');
      }
    }
    if (el.filemeta) {
      el.filemeta.hidden = !hasFile || !card.dataset.filesize;
      el.filemeta.textContent = card.dataset.filesize || '';
    }

    el.flag.value = '';
    el.flag.disabled = solved || locked || teamRequired;
    el.send.disabled = solved || locked || teamRequired;
    if (viewState === 'scheduled') {
      setResult('scheduled', 'This challenge has not started yet.');
    } else if (viewState === 'ended') {
      setResult('ended', 'This challenge has ended.');
    } else if (teamRequired) {
      setResult('team', 'Join or create a team from the Team page before submitting flags.');
    } else {
      setResult(solved ? 'already' : '', solved ? 'You already solved this challenge.' : '');
    }
    modal.showModal();
    if (!solved && !locked && !teamRequired) el.flag.focus();
  }

  document.querySelectorAll('.card').forEach(function (card) {
    card.addEventListener('click', function () { openCard(card); });
  });

  function submitFlag() {
    if (!current || el.send.disabled) return;
    el.send.disabled = true;
    el.send.textContent = 'Checking…';
    fetch('/challenges/' + current.dataset.id + '/submit', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf },
      body: JSON.stringify({ flag: el.flag.value })
    })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        setResult(d.status, d.message);
        renderStones(d);
        if (d.status === 'correct' || d.status === 'already') {
          current.dataset.solved = '1';
          current.classList.add('solved');
          if (el.status) el.status.hidden = false;

          var foot = current.querySelector('.card-foot') || current;
          if (!current.querySelector('.tick')) {
            var tick = document.createElement('span');
            tick.className = 'tick';
            tick.textContent = 'Solved';
            foot.appendChild(tick);
          }
          el.flag.disabled = true;
          if (d.points) {
            var score = document.getElementById('score');
            if (score) score.textContent = String(parseInt(score.textContent, 10) + d.points);
          }
        } else if (d.status === 'scheduled' || d.status === 'ended' || d.status === 'team') {
          el.send.disabled = true;
        } else {
          el.send.disabled = false;
        }
      })
      .catch(function () {
        setResult('error', 'Network error. Try again.');
        el.send.disabled = false;
      })
      .finally(function () {
        el.send.textContent = 'Submit';
      });
  }

  el.send.addEventListener('click', submitFlag);
  el.flag.addEventListener('keydown', function (e) {
    if (e.key === 'Enter') { e.preventDefault(); submitFlag(); }
  });
  el.close.addEventListener('click', function () { modal.close(); });
  modal.addEventListener('click', function (e) {
    if (e.target === modal) modal.close();
  });
})();
