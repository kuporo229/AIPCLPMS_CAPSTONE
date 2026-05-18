/* ═══════════════════════════════════════════
   Barba.js — Premium Page Transitions
   ═══════════════════════════════════════════ */
(function () {
  'use strict';
  if (typeof barba === 'undefined' || typeof anime === 'undefined') return;

  // ── Re-execute inline scripts after Barba swaps content ──
  // DOMParser creates non-executable script nodes.
  // This replaces each inline <script> with a fresh executable copy.
  function runContainerScripts() {
    var c = document.querySelector('[data-barba="container"]');
    if (!c) return;
    c.querySelectorAll('script').forEach(function (old) {
      if (old.src || old.dataset.barbaRan) return;
      try {
        var f = document.createElement('script');
        f.textContent = old.textContent;
        f.dataset.barbaRan = '1';
        old.parentNode.replaceChild(f, old);
      } catch (e) { console.warn('[PT] script error:', e); }
    });
  }

  // Observe wrapper for new container insertions and re-run scripts
  var wrapper = document.querySelector('[data-barba="wrapper"]');
  if (wrapper) {
    new MutationObserver(function () {
      runContainerScripts();
    }).observe(wrapper, { childList: true, subtree: true });
  }

  // ── Prevent flex collapse during transition ──
  function preventCollapse(on) {
    var w = document.querySelector('[data-barba="wrapper"]');
    if (!w) return;
    w.style.minHeight = on ? w.offsetHeight + 'px' : '';
  }

  // ── Init Barba ──
  barba.init({
    // Skip Barba for admin routes (complex JS, forms)
    prevent: function (obj) {
      var el = obj && obj.el ? obj.el : obj;
      if (el && el.getAttribute) {
        var href = el.getAttribute('href') || '';
        if (href.indexOf('/admin/') === 0) return true;
      }
      return false;
    },
    transitions: [{
      name: 'premium-slide',
      beforeLeave: function () {
        preventCollapse(true);
      },
      leave: function (data) {
        return new Promise(function (resolve) {
          anime({
            targets: data.current.container,
            opacity: [1, 0], translateX: [0, -40],
            easing: 'easeInOutCubic',
            duration: 300,
            complete: function () {
              data.current.container.style.display = 'none';
              resolve();
            }
          });
        });
      },
      beforeEnter: function (data) {
        data.next.container.style.opacity = '0';
      },
      enter: function (data) {
        return new Promise(function (resolve) {
          anime({
            targets: data.next.container,
            opacity: [0, 1], translateX: [40, 0],
            easing: 'easeOutCubic',
            duration: 350,
            complete: function () {
              data.next.container.style.opacity = '';
              data.next.container.style.transform = '';
              preventCollapse(false);
              resolve();
            }
          });
        });
      },
      afterEnter: function () {
        runContainerScripts();
        reinitPage();
      }
    }]
  });

  // ── Re-init page state ──
  function reinitPage() {
    var path = window.location.pathname;
    var best = { el: null, len: 0 };
    document.querySelectorAll('nav a[href]').forEach(function (link) {
      link.classList.remove('sidebar-active', 'bg-primary-50', 'dark:bg-primary-900/20', 'text-primary-600', 'dark:text-primary-400');
      var h = link.getAttribute('href');
      if (!h) return;
      if (h !== '/' && path.indexOf(h) === 0 && h.length > best.len) best = { el: link, len: h.length };
      if (h === '/' && (path === '/' || path === '/dashboard')) best = { el: link, len: 999 };
    });
    if (best.el) best.el.classList.add('sidebar-active', 'bg-primary-50', 'dark:bg-primary-900/20', 'text-primary-600', 'dark:text-primary-400');
    if (window.checkNotifications) setTimeout(window.checkNotifications, 100);
    if (window.initFlowbite) setTimeout(window.initFlowbite, 50);
    if (window.Alpine) document.querySelectorAll('[x-data]').forEach(function (el) { Alpine.initTree(el); });
    var toggle = document.getElementById('sidebar-toggle');
    var sidebar = document.getElementById('sidebar');
    if (toggle && sidebar) toggle.onclick = function () { sidebar.classList.toggle('-translate-x-full'); };
  }
})();
