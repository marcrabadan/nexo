/* Static, version-pinned context. No model calls, analytics or account. */
(function () {
  "use strict";
  async function copyPrompt(textarea, status, button, clipboard) {
    const text = textarea.value;
    button.disabled = true;
    try {
      if (!clipboard || !clipboard.writeText) throw new Error("Clipboard unavailable");
      await clipboard.writeText(text);
      status.textContent = "Prompt copied. Append your task in your assistant.";
    } catch (_) {
      textarea.focus();
      textarea.select();
      status.textContent = "Clipboard unavailable. Prompt selected; press Ctrl+C or Command+C to copy.";
    } finally {
      button.disabled = false;
    }
  }
  if (typeof module !== "undefined") module.exports = { copyPrompt };
  if (typeof document === "undefined") return;
  const main = document.querySelector(".ai-main");
  if (!main) return;
  const textarea = document.getElementById("prompt");
  const status = document.getElementById("copy-status");
  const button = document.getElementById("copy-prompt");
  button.addEventListener("click", () => copyPrompt(textarea, status, button, navigator.clipboard));
  const profile = document.getElementById("profile");
  const experience = document.getElementById("experience");
  const versions = document.getElementById("context-version");
  let sequence = 0;
  async function load(bundle, expectedRevision) {
    const request = ++sequence;
    const controls = [profile,experience,document.getElementById("mode"),document.getElementById("access")];
    controls.forEach(control=>{control.disabled=true;});
    try {
      const response=await fetch(bundle+"/prompts.json");
      if (!response.ok) throw Error("Context unavailable");
      const data=await response.json();
      if (request!==sequence) return;
      if (data.revision!==expectedRevision) throw Error("Context revision mismatch");
      profile.querySelectorAll('option').forEach(option=>{option.disabled=!Object.keys(data.prompts).some(key=>key.startsWith(option.value+'-'));});
      if (profile.selectedOptions[0].disabled) profile.value='developer';
      const hasExperience=Object.keys(data.prompts).some(key=>key.endsWith('-expert'));
      if (!hasExperience) experience.value='developer';
      function update() {
        const key=profile.value+'-'+document.querySelector('input[name="mode"]:checked').value+'-'+document.querySelector('input[name="access"]:checked').value+(experience.value==='developer'?'':'-'+experience.value);
        const prompt=data.prompts[key];
        if (!prompt) {status.textContent='This profile/experience is unavailable in the selected version.';return;}
        textarea.value=prompt.text;
        const count=document.getElementById('token-count');count.textContent=prompt.tokens+' reference tokens';count.dataset.tokens=prompt.tokens;
        document.getElementById('download-prompt').href=prompt.download;
        document.getElementById('version-status').textContent='Selected prompt revision '+data.revision+'. Examples and review tools below use the current page revision.';
        document.dispatchEvent(new CustomEvent('nexo-experience'));
        status.textContent=prompt.label+' / '+experience.value+' ready.';
      }
      controls.forEach(control=>{control.onchange=update;control.disabled=false;});
      experience.disabled=!hasExperience;
      profile.onchange=()=>{if(profile.value==='beginner'&&hasExperience) experience.value='beginner';update();};
      update();
    } catch (_) {
      if (request!==sequence) return;
      status.textContent="Profile switching unavailable. The last loaded prompt remains ready to copy; use the versioned downloads below for other formats.";
    }
  }
  load(main.dataset.bundle,main.dataset.revision);
  fetch('ai-guide/versions.json').then(response=>{if(!response.ok) throw Error();return response.json();}).then(data=>{
    const original=versions.options[0];
    data.versions.filter(v=>v.revision!==main.dataset.revision).forEach(version=>{
      const option=document.createElement('option');option.value=version.bundle;option.textContent='Archived — '+version.revision.slice(0,20);option.dataset.revision=version.revision;versions.append(option);
    });
    original.dataset.revision=main.dataset.revision;
    versions.disabled=false;
    versions.addEventListener('change',()=>load(versions.value,versions.selectedOptions[0].dataset.revision));
  }).catch(()=>{document.getElementById('version-status').textContent='Version list unavailable; current pinned context remains usable.';});

})();
