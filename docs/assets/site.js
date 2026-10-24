// Syntax highlighting for Nexo snippets, copy buttons and example tabs.
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

  var tabs = Array.prototype.slice.call(document.querySelectorAll('[role="tab"]'));
  function select(tab) {
    tabs.forEach(function (t) {
      var on = t === tab;
      t.setAttribute("aria-selected", on ? "true" : "false");
      t.tabIndex = on ? 0 : -1;
      document.getElementById(t.getAttribute("aria-controls")).hidden = !on;
    });
  }
  tabs.forEach(function (tab, i) {
    tab.addEventListener("click", function () { select(tab); });
    tab.addEventListener("keydown", function (e) {
      var d = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
      if (!d) return;
      var next = tabs[(i + d + tabs.length) % tabs.length];
      select(next);
      next.focus();
    });
  });
})();
