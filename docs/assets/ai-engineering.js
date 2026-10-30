/* Pure local review helpers. User text is never transmitted or persisted. */
(function () {
  'use strict';
  function readiness(rubric, values) {
    if (Object.keys(values).some(key=>!rubric.categories.some(c=>c.id===key))) throw Error('Unknown readiness category');
    const rows = rubric.categories.map(category => {
      const supplied = values[category.id] || {};
      let status = supplied.status || 'Unknown';
      if (!rubric.statuses.includes(status)) throw Error('Invalid readiness status');
      if (supplied.evidence !== undefined && typeof supplied.evidence !== 'string') throw Error('Evidence must be text');
      const evidence = (supplied.evidence || '').trim();
      if (['Passed', 'Not applicable'].includes(status) && !evidence) status = 'Unknown';
      return {...category, critical:category.critical || supplied.critical === true, status, evidence, risks: supplied.risks || 'Not assessed', remediation: supplied.remediation || 'Collect and review evidence'};
    });
    const applicable = rows.filter(row => row.status !== 'Not applicable');
    return {score: applicable.length ? Math.round(10000 * rows.filter(row => row.status === 'Passed').length / applicable.length) / 100 : null,
      blockers: rows.filter(row => row.critical && ['Failed','Unknown'].includes(row.status)).map(row => row.id), categories: rows, rule: rubric.scoring, certification: false};
  }
  function cost(input, output, inputRate, outputRate) {
    const values = [input,output,inputRate,outputRate];
    if (values.some(v => v === '' || v == null || !Number.isFinite(Number(v)) || Number(v) < 0)) return null;
    return (Number(input)*Number(inputRate)+Number(output)*Number(outputRate))/1e6;
  }
  function draftAdr(values) {
    const fields = ['context','decision','alternatives','consequences','cost_complexity','reconsider_when'];
    if (fields.some(key => !String(values[key] || '').trim())) throw Error('Complete all six ADR fields.');
    return '# Architecture decision\n\nStatus: Proposed (requires explicit user approval)\n\n'+fields.map(key=>'## '+key.replaceAll('_',' ')+'\n\n'+values[key]).join('\n\n')+'\n';
  }
  function onboarding(values) {
    const labels = ['Business goal and product knowledge','MVP and measurable success','Users, workload and growth','Data sensitivity, integrations and compliance','Monthly budget, infrastructure and operational limits'];
    const unknowns = labels.filter((_,i)=>!String(values[i] || '').trim());
    return 'AI Readiness Check — user-supplied facts, not verified requirements\n\n'+labels.map((label,i)=>label+': '+(String(values[i] || '').trim() || 'Unknown')).join('\n')+'\n\nAssumptions: none inferred.\nRecommendations: validate the smallest MVP; complete the Shield Security Assessment.\nUnknowns: '+(unknowns.join('; ') || 'Answers still require evidence and clarification of critical constraints.')+'\nBlockers: '+(unknowns.length ? 'Missing information; block consequential decisions until critical gaps are resolved.' : 'Consequential decisions still require explicit approval and verified critical constraints.')+'\nNext step: '+(unknowns.length ? 'Answer the missing questions above (maximum five per round).' : 'Review constraints, draft the simplest architecture and Proposed ADRs before implementation.')+'\n';
  }
  const api = {readiness,cost,draftAdr,onboarding};
  if (typeof module !== 'undefined') module.exports = api;
  if (typeof document === 'undefined') return;
  const main = document.querySelector('.ai-main');
  if (!main) return;
  document.getElementById('make-readiness').addEventListener('click',()=>{
    document.getElementById('project-report').value=onboarding([0,1,2,3,4].map(i=>document.getElementById('question-'+i).value));
    document.getElementById('project-report').focus();
  });
  document.getElementById('make-adr').addEventListener('click',()=>{
    const values={}; document.querySelectorAll('[data-adr]').forEach(el=>values[el.dataset.adr]=el.value);
    try {document.getElementById('adr-report').value=draftAdr(values);} catch(error) {document.getElementById('adr-report').value=error.message;}
  });
  document.getElementById('estimate-cost').addEventListener('click',()=>{
    const baseline=document.getElementById('baseline-tokens').value;
    const selected=document.getElementById('token-count').dataset.tokens;
    const output=document.getElementById('output-tokens').value;
    const inputRate=document.getElementById('input-rate').value;
    const outputRate=document.getElementById('output-rate').value;
    const a=cost(baseline,output,inputRate,outputRate), b=cost(selected,output,inputRate,outputRate);
    document.getElementById('cost-report').textContent=a===null || b===null ? 'Enter finite, nonnegative token counts and both prices.' : 'Estimated cost in your configured currency: baseline '+a.toFixed(6)+'; selected prompt '+b.toFixed(6)+'. Includes your output-token allowance; excludes fetched context, retries, task text and provider overhead. This is a calculation, not a measured model outcome.';
  });
  document.getElementById('example-search').addEventListener('input',event=>{
    const query=event.target.value.trim().toLowerCase();let visible=0;
    document.querySelectorAll('.verified-example').forEach(item=>{item.hidden=!item.dataset.search.toLowerCase().includes(query);if (!item.hidden) visible++;});
    document.getElementById('example-status').textContent=visible+' examples match.';
  });
  fetch(main.dataset.bundle+'/readiness.json').then(response=>{if(!response.ok) throw Error();return response.json();}).then(rubric=>{
    function adaptQuestions() {
      const questions=rubric.questions[document.getElementById('experience').value];
      questions.forEach((question,i)=>{document.querySelector('label[for="question-'+i+'"]').textContent=question;});
    }
    document.getElementById('experience').addEventListener('change',adaptQuestions);
    document.addEventListener('nexo-experience',adaptQuestions);
    adaptQuestions();
    const container=document.getElementById('readiness-categories');
    rubric.categories.forEach(category=>{
      const fieldset=document.createElement('fieldset');const legend=document.createElement('legend');legend.textContent=category.label+(category.critical?' (critical)':'');fieldset.append(legend);
      const label=document.createElement('label');label.textContent='Status ';const select=document.createElement('select');select.id='review-'+category.id;rubric.statuses.forEach(status=>{const option=document.createElement('option');option.value=status;option.textContent=status;select.append(option);});select.value='Unknown';label.append(select);fieldset.append(label);
      ['evidence','risks','remediation'].forEach(key=>{const label=document.createElement('label');label.textContent=key;const input=document.createElement('input');input.id='review-'+category.id+'-'+key;label.append(input);fieldset.append(label);});container.append(fieldset);
    });
    const button=document.getElementById('score-readiness');button.disabled=false;button.addEventListener('click',()=>{
      const values={};rubric.categories.forEach(c=>{values[c.id]={status:document.getElementById('review-'+c.id).value};['evidence','risks','remediation'].forEach(key=>values[c.id][key]=document.getElementById('review-'+c.id+'-'+key).value);});
      document.getElementById('production-report').value=JSON.stringify(readiness(rubric,values),null,2);
      document.getElementById('production-report').focus();
    });
  }).catch(()=>{document.getElementById('production-report').value='Readiness rubric unavailable. Use the versioned JSON and local readiness tool; no passing result inferred.';});
})();
