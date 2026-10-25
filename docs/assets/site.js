// Syntax highlighting for Nexo snippets, copy buttons, tabs, theme and menu.
(function () {
  "use strict";

  var KEYWORDS = new Set((
    "if elif else for in while repeat match pass fn return type trait is pub use var self weak " +
    "true false none and or not as try unsafe expect ensure parallel on emit when every at lifecycle " +
    "test assert fails where map pick group by sum count avg sort desc take each it spawn select shared " +
    "ai agent models profile server"
  ).split(" "));

  var TOKEN = /(#[^\n]*)|("(?:[^"\\\n]|\\.)*")|(\d[\d_]*(?:\.\d+)?(?:%| ?(?:EUR|USD|GBP)\b)?)|([A-Za-z_][A-Za-z0-9_]*[!?]?)|(\|>|->|\?\?|[!?](?=[\s):\],]|$))/g;

  function esc(s) {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  function span(cls, s) {
    return '<span class="tok-' + cls + '">' + esc(s) + "</span>";
  }

  // A string literal with {interpolation} parts; braces inside the
  // interpolation may contain nested strings such as {row["region"]}.
  function str(s) {
    var out = "", i = 0, start = 0;
    while (i < s.length) {
      if (s[i] === "{") {
        var depth = 1, j = i + 1;
        while (j < s.length && depth > 0) {
          if (s[j] === "{") depth++;
          else if (s[j] === "}") depth--;
          j++;
        }
        out += span("s", s.slice(start, i)) + span("i", s.slice(i, j));
        i = start = j;
      } else {
        i++;
      }
    }
    return out + span("s", s.slice(start));
  }

  function highlight(src) {
    var out = "", last = 0, m;
    // Interpolated strings can hold quotes ({row["region"]}), so strings are
    // matched by hand before the regular expression gets to them.
    TOKEN.lastIndex = 0;
    while ((m = TOKEN.exec(src))) {
      var start = m.index, text = m[0];
      if (m[2] !== undefined) {
        var end = start + 1, depth = 0;
        while (end < src.length && src[end] !== "\n") {
          var c = src[end];
          if (c === "{") depth++;
          else if (c === "}") depth--;
          else if (c === "\\") end++;
          else if (c === '"' && depth === 0) break;
          end++;
        }
        text = src.slice(start, end + 1);
        TOKEN.lastIndex = end + 1;
      }
      out += esc(src.slice(last, start));
      if (m[1] !== undefined) out += span("c", text);
      else if (m[2] !== undefined) out += str(text);
      else if (m[3] !== undefined) out += span("n", text);
      else if (m[4] !== undefined) {
        var word = text.replace(/[!?]$/, "");
        if (KEYWORDS.has(word) && word === text) out += span("k", text);
        else if (/^[A-Z]/.test(word)) out += span("t", text);
        else out += esc(text);
      } else out += span("o", text);
      last = start + text.length;
    }
    return out + esc(src.slice(last));
  }

  document.querySelectorAll("pre code.nexo").forEach(function (el) {
    el.innerHTML = highlight(el.textContent);
  });

  document.querySelectorAll(".copy").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var code = btn.closest(".code").querySelector("pre code");
      if (!navigator.clipboard) return;
      navigator.clipboard.writeText(code.textContent).then(function () {
        btn.textContent = "Copied";
        setTimeout(function () { btn.textContent = "Copy"; }, 1500);
      }).catch(function () {});
    });
  });

  // Each tablist switches its own panels; arrows move between its tabs.
  document.querySelectorAll('[role="tablist"]').forEach(function (list) {
    var tabs = Array.prototype.slice.call(list.querySelectorAll('[role="tab"]'));
    var vertical = list.getAttribute("aria-orientation") === "vertical";
    function select(tab) {
      tabs.forEach(function (t) {
        var on = t === tab;
        t.setAttribute("aria-selected", on ? "true" : "false");
        t.tabIndex = on ? 0 : -1;
        document.getElementById(t.getAttribute("aria-controls")).hidden = !on;
      });
      tab.scrollIntoView({ block: "nearest", inline: "nearest" });
    }
    tabs.forEach(function (tab, i) {
      tab.addEventListener("click", function () { select(tab); });
      tab.addEventListener("keydown", function (e) {
        var k = e.key, d = 0;
        if (k === "ArrowRight" || (vertical && k === "ArrowDown")) d = 1;
        else if (k === "ArrowLeft" || (vertical && k === "ArrowUp")) d = -1;
        if (!d) return;
        e.preventDefault();
        var next = tabs[(i + d + tabs.length) % tabs.length];
        select(next);
        next.focus();
      });
    });
  });

  // Theme: follows the system until the button picks one, then remembers it.
  var root = document.documentElement;
  var theme = document.getElementById("theme");
  if (theme) {
    theme.addEventListener("click", function () {
      var dark = root.dataset.theme
        ? root.dataset.theme === "dark"
        : window.matchMedia("(prefers-color-scheme: dark)").matches;
      root.dataset.theme = dark ? "light" : "dark";
      try { localStorage.setItem("nexo-theme", root.dataset.theme); } catch (e) {}
    });
  }

  // Menu on narrow screens.
  var menu = document.getElementById("menu"), nav = document.getElementById("nav");
  if (menu && nav) {
    function setOpen(open) {
      nav.classList.toggle("open", open);
      menu.setAttribute("aria-expanded", open ? "true" : "false");
    }
    menu.addEventListener("click", function () { setOpen(!nav.classList.contains("open")); });
    nav.addEventListener("click", function (e) { if (e.target.closest("a")) setOpen(false); });
    document.addEventListener("keydown", function (e) { if (e.key === "Escape") setOpen(false); });
  }
})();
