/* The Cosy Nook — reading affordances. No deps. */
(function () {
  var root = document.documentElement;
  var THEMES = ["light", "sepia", "dark"];
  var SIZES = ["s", "m", "l", "xl"];

  function get(k, f) {
    try { return localStorage.getItem(k) || f; } catch (e) { return f; }
  }
  function set(k, v) {
    try { localStorage.setItem(k, v); } catch (e) {}
  }

  // Theme + size -----------------------------------------------------------
  var theme = get("nook:theme", "light");
  if (THEMES.indexOf(theme) < 0) theme = "light";
  var size = get("nook:size", "m");
  if (SIZES.indexOf(size) < 0) size = "m";
  root.setAttribute("data-theme", theme);
  root.setAttribute("data-size", size);

  var themeName = document.getElementById("themeName");
  function paintTheme() {
    if (themeName) themeName.textContent = theme[0].toUpperCase() + theme.slice(1);
  }
  paintTheme();

  var themeBtn = document.getElementById("themeBtn");
  if (themeBtn) themeBtn.addEventListener("click", function () {
    theme = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length];
    root.setAttribute("data-theme", theme);
    set("nook:theme", theme);
    paintTheme();
  });
  var up = document.getElementById("sizeUp");
  var down = document.getElementById("sizeDown");
  function bump(d) {
    var i = SIZES.indexOf(size) + d;
    i = Math.max(0, Math.min(SIZES.length - 1, i));
    size = SIZES[i];
    root.setAttribute("data-size", size);
    set("nook:size", size);
  }
  if (up) up.addEventListener("click", function () { bump(1); });
  if (down) down.addEventListener("click", function () { bump(-1); });

  // Progress + remember place ------------------------------------------------
  var bar = document.getElementById("progressBar");
  var key = null;
  if (/chapter|function|opening/.test(location.pathname)) key = "nook:pos:" + location.pathname;
  function onScroll() {
    var h = document.documentElement;
    var max = h.scrollHeight - h.clientHeight;
    var p = max > 0 ? (h.scrollTop / max) : 0;
    if (bar) bar.style.width = (p * 100).toFixed(1) + "%";
    if (key && (window.scrollY > 120)) set(key, String(window.scrollY));
  }
  window.addEventListener("scroll", onScroll, { passive: true });
  onScroll();
  if (key) {
    var y = parseFloat(get(key, "0"));
    if (y > 200) {
      var pill = document.createElement("button");
      pill.className = "btn ghost";
      pill.type = "button";
      pill.textContent = "Pick up where you left off";
      pill.style.cssText = "position:fixed;right:16px;bottom:16px;z-index:45;box-shadow:0 10px 30px rgba(0,0,0,.25)";
      pill.addEventListener("click", function () {
        window.scrollTo({ top: y, behavior: "smooth" });
        pill.remove();
      });
      document.body.appendChild(pill);
      setTimeout(function () { if (pill.parentNode) pill.parentNode.removeChild(pill); }, 12000);
    }
    set("nook:last", location.pathname + "|||" + document.title);
  }

  // Continue button on the library home --------------------------------------
  var cont = document.getElementById("continueBtn");
  if (cont) {
    var last = get("nook:last", "");
    if (last && last.indexOf("|||") > 0) {
      var parts = last.split("|||");
      cont.hidden = false;
      cont.textContent = "Continue: " + parts[1].split("·")[0].trim();
      cont.addEventListener("click", function () { location.href = parts[0]; });
    }
  }

  // TOC toggle on small screens ------------------------------------------------
  var tocToggle = document.getElementById("tocToggle");
  if (tocToggle) tocToggle.addEventListener("click", function () {
    var box = document.querySelector(".toc-box");
    if (box) {
      box.open = !box.open;
      box.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
  });

  // Keyboard: left/right chapter hop -------------------------------------------
  document.addEventListener("keydown", function (e) {
    if (/INPUT|TEXTAREA/.test(document.activeElement && document.activeElement.tagName || "")) return;
    if (e.key === "ArrowLeft" && document.body.dataset.prev) location.href = document.body.dataset.prev;
    if (e.key === "ArrowRight" && document.body.dataset.next) location.href = document.body.dataset.next;
  });

  // Search ----------------------------------------------------------------------
  var btn = document.getElementById("searchBtn");
  var drawer = document.getElementById("searchDrawer");
  var input = document.getElementById("searchInput");
  var out = document.getElementById("searchOut");
  var data = null;
  var prefix = (function () {
    // assets live at <prefix>assets/...; search.json lives at <prefix>search.json
    var s = document.querySelector('link[rel="stylesheet"]');
    var href = s ? s.getAttribute("href") : "assets/style.css";
    return href.replace(/assets\/style\.css.*$/, "");
  })();
  function toggleSearch(force) {
    if (!drawer) return;
    var show = typeof force === "boolean" ? force : drawer.hidden;
    drawer.hidden = !show;
    if (show && input) input.focus();
  }
  if (btn) btn.addEventListener("click", function () { toggleSearch(); });
  document.addEventListener("keydown", function (e) {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); toggleSearch(true); }
    if (e.key === "Escape" && drawer && !drawer.hidden) toggleSearch(false);
  });
  function loadIndex(cb) {
    if (data) return cb(data);
    fetch(prefix + "search.json").then(function (r) { return r.json(); }).then(function (j) {
      data = j; cb(j);
    }).catch(function () { cb([]); });
  }
  var t = null;
  if (input) input.addEventListener("input", function () {
    clearTimeout(t);
    t = setTimeout(function () {
      var q = input.value.trim().toLowerCase();
      if (!q || q.length < 2) { out.innerHTML = ""; return; }
      loadIndex(function (idx) {
        var hits = idx.filter(function (e) {
          return (e.series + " " + e.story + " " + e.chapter + " " + e.excerpt).toLowerCase().indexOf(q) >= 0;
        }).slice(0, 12);
        out.innerHTML = hits.length ? hits.map(function (e) {
          return '<a href="' + prefix + e.url + '"><strong>' + esc(e.story) + " — " + esc(e.chapter) +
            '</strong><small>' + esc(e.series) + " · " + esc(e.excerpt.slice(0, 90)) + "</small></a>";
        }).join("") : '<p class="meta">Nothing on this shelf matches — try a name or a place.</p>';
      });
    }, 140);
  });
  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
})();
