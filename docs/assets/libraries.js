(() => {
  function revealGuide() {
    let id;
    try { id = decodeURIComponent(location.hash.slice(1)); } catch (_) { return; }
    const target = document.getElementById(id);
    if (!target) return;
    let changed = false;
    for (let parent = target; parent; parent = parent.parentElement) {
      if (parent.tagName === "DETAILS" && !parent.open) { parent.open = true; changed = true; }
    }
    if (changed) target.scrollIntoView();
  }
  revealGuide();
  window.addEventListener("hashchange", revealGuide);
})();
