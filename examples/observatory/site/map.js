/* The page of the internet observatory: it asks its own server (site.nexo)
   for the survey of the domain in the address and draws it. */
function render(WORLD, CABLES, LANDINGS, DATA) {
  const NS = "http://www.w3.org/2000/svg", STRONG = 20;
  const $ = id => document.getElementById(id);
  function el(tag, attrs, ...kids) {
    const svg = ["svg", "path", "circle", "g", "text", "line", "rect", "title"].includes(tag);
    const e = svg ? document.createElementNS(NS, tag) : document.createElement(tag);
    /* a style goes through the object, not the attribute: the server's
       Content-Security-Policy lets no inline style in */
    for (const k in attrs || {}) { if (k === "style") e.style.cssText = attrs[k]; else e.setAttribute(k, attrs[k]); }
    for (const kid of kids) if (kid != null) e.append(kid);
    return e;
  }
  const X = lon => (lon + 180) / 360 * 1000, Y = lat => (90 - lat) / 180 * 500;
  function line(points) {
    let d = "", prev = null;
    for (const p of points) {
      const jump = prev && Math.abs(p[0] - prev[0]) > 180;
      d += (prev && !jump ? "L" : "M") + X(p[0]).toFixed(1) + " " + Y(p[1]).toFixed(1);
      prev = p;
    }
    return d;
  }
  const plain = s => (s || "").toLowerCase().normalize("NFD").replace(/[^a-z ]/g, "").trim();
  const ALIAS = { "united states": "united states of america", "czech republic": "czechia", "bosnia and herzegovina": "bosnia and herz",
    "dominican republic": "dominican rep", "democratic republic of the congo": "dem rep congo", "central african republic": "central african rep",
    "south sudan": "s sudan", "equatorial guinea": "eq guinea", "ivory coast": "cote divoire", "western sahara": "w sahara",
    "solomon islands": "solomon is", "falkland islands": "falkland is", "north macedonia": "macedonia", "swaziland": "eswatini" };
  const SIGNAL = { bgp: "routes announced (BGP)", gtr: "Google traffic", "ping-slash24": "active probes", "merit-nt": "telescope traffic" };
  const signal = s => SIGNAL[s] || s;

  /* ---- the world, from TopoJSON ---- */
  function rings(topo) {
    const [sx, sy] = topo.transform.scale, [tx, ty] = topo.transform.translate;
    const arcs = topo.arcs.map(a => { let x = 0, y = 0; return a.map(p => [(x += p[0]) * sx + tx, (y += p[1]) * sy + ty]); });
    const ring = idx => idx.flatMap(i => i < 0 ? arcs[~i].slice().reverse() : arcs[i]);
    return topo.objects.countries.geometries.map(g => ({
      name: g.properties.name,
      rings: (g.type === "Polygon" ? [g.arcs] : g.arcs).flat().map(ring)
    }));
  }

  const map = $("map"), tip = $("tip");
  function hint(node, title, detail) {
    node.addEventListener("pointerenter", () => {
      tip.replaceChildren(el("b", {}, title), detail ? el("small", {}, detail) : null);
      tip.hidden = false;
    });
    node.addEventListener("pointermove", e => {
      const box = $("chart").getBoundingClientRect();
      tip.style.left = Math.min(e.clientX - box.left + 12, box.width - 290 < 0 ? 8 : box.width - 290) + "px";
      tip.style.top = (e.clientY - box.top + 14) + "px";
    });
    node.addEventListener("pointerleave", () => { tip.hidden = true; });
  }

  /* a cable fault shows in routes and probes, so only those signals mark a cable */
  const strong = (DATA.outages || []).filter(o => o.fall >= STRONG && o.signal !== "gtr");
  const worst = {};
  for (const o of DATA.outages || []) { const k = plain(o.country); if (!(k in worst) || worst[k].fall < o.fall) worst[k] = o; }

  /* land */
  const gLand = el("g"), named = new Set();
  for (const c of rings(WORLD)) {
    const key = plain(c.name), hit = Object.values(worst).find(o => (ALIAS[plain(o.country)] || plain(o.country)) === key);
    const p = el("path", { class: "land", d: c.rings.map(r => line(r) + "Z").join("") });
    if (hit) { named.add(plain(hit.country)); p.style.fill = "color-mix(in srgb, var(--bad) " + Math.min(75, 12 + hit.fall) + "%, var(--land))"; }
    hint(p, c.name, hit ? signal(hit.signal) + " down " + hit.fall + "%" : "no outage signal");
    gLand.append(p);
  }
  map.append(gLand);

  /* cables: one that lands in a country with a strong outage signal is marked */
  const landings = (LANDINGS && LANDINGS.features) || [];
  const country = f => plain(f.properties.name.split(",").pop());
  const hot = landings.filter(f => strong.some(o => plain(o.country) === country(f)));
  const gCable = el("g"), cableList = []; let marked = 0;
  for (const f of (CABLES && CABLES.features) || []) {
    const lines = f.geometry.type === "LineString" ? [f.geometry.coordinates] : f.geometry.coordinates;
    const ends = lines.flatMap(l => [l[0], l[l.length - 1]]);
    const land = hot.find(h => ends.some(e => Math.abs(e[0] - h.geometry.coordinates[0]) < .3 && Math.abs(e[1] - h.geometry.coordinates[1]) < .3));
    const p = el("path", { class: "cable" + (land ? " hit" : ""), d: lines.map(line).join("") });
    if (land) marked++;
    cableList.push({ name: f.properties.name, ends, node: p });
    hint(p, f.properties.name, land ? "lands at " + land.properties.name + ", where an outage signal is open" : "submarine cable, no outage signal at its landings");
    gCable.append(p);
  }
  map.append(gCable);

  /* outage rings for places too small for the map */
  const gRing = el("g");
  for (const o of strong) {
    if (named.has(plain(o.country))) continue;
    const here = landings.filter(f => country(f) === plain(o.country));
    if (!here.length) continue;
    const lon = here.reduce((s, f) => s + f.geometry.coordinates[0], 0) / here.length, lat = here.reduce((s, f) => s + f.geometry.coordinates[1], 0) / here.length;
    const c = el("circle", { class: "ring", cx: X(lon), cy: Y(lat), r: 5 });
    hint(c, o.country, signal(o.signal) + " down " + o.fall + "%");
    gRing.append(c);
  }
  map.append(gRing);

  /* the trace: observer, name servers, site */
  const places = [DATA.observer].concat(DATA.places || []).filter(p => p && p.lat != null);
  const groups = {};
  for (const p of places) (groups[p.lat + "," + p.lon] = groups[p.lat + "," + p.lon] || []).push(p);
  for (const g of Object.values(groups)) g.forEach((p, i) => {
    const a = i / g.length * 2 * Math.PI, r = g.length > 1 ? 9 : 0;
    p.x = X(p.lon) + r * Math.cos(a); p.y = Y(p.lat) + r * Math.sin(a);
  });
  const gTrace = el("g"), me = places.find(p => p.kind === "observer");
  for (const p of places) {
    if (!me || p === me) continue;
    const mx = (me.x + p.x) / 2, my = (me.y + p.y) / 2 - Math.hypot(p.x - me.x, p.y - me.y) * .22;
    gTrace.append(el("path", { class: "hop " + (p.kind === "site" ? "web" : "dns"), d: `M${me.x} ${me.y}Q${mx} ${my} ${p.x} ${p.y}` }));
  }
  for (const p of places) {
    const cls = p.kind === "observer" ? "observer" : p.kind === "site" ? "site" : "ns";
    const c = el("circle", { class: "pt " + cls, cx: p.x, cy: p.y, r: p.kind === "site" ? 4.5 : 3.5 });
    hint(c, p.name + (p.ip ? "  " + p.ip : ""), [p.kind, p.country, p.asn ? "AS" + p.asn + " " + p.holder : ""].filter(Boolean).join(" · "));
    gTrace.append(c);
  }

  /* BGP: the route collectors and the path each one has */
  const route = DATA.route || {}, routers = DATA.routers || { probes: [] };
  const target = places.find(p => p.kind === "site");
  const gBgp = el("g");
  for (const c of route.collectors || []) {
    if (c.lat == null) continue;
    const x = X(c.lon), y = Y(c.lat);
    if (target) gBgp.append(el("path", { class: "bgp", d: `M${x} ${y}Q${(x + target.x) / 2} ${(y + target.y) / 2 - Math.hypot(target.x - x, target.y - y) * .15} ${target.x} ${target.y}` }));
    const dot = el("circle", { class: "pt col", cx: x, cy: y, r: 2.6 });
    hint(dot, c.name + " " + c.city, c.peers + " peers · usual path AS" + c.path.split(" ").join(" → AS"));
    gBgp.append(dot);
  }
  /* routers: each probe's traceroute. A router whose name gives its city is placed there; the
     line joins those when a trace has any, and the register's places only when it has none. */
  const km = (a, b) => { const r = Math.PI / 180, h = Math.sin((b[1] - a[1]) * r / 2) ** 2 + Math.cos(a[1] * r) * Math.cos(b[1] * r) * Math.sin((b[0] - a[0]) * r / 2) ** 2; return 12742 * Math.asin(Math.sqrt(h)); };
  const near = (c, at, reach) => c.ends.some(e => km(e, at) < reach);
  const gRouters = el("g");
  for (const p of routers.probes || []) {
    const sure = p.hops.some(h => h.by === "name");
    /* this machine's own traceroute comes first; its place is the register's, so its first leg is not a sure one */
    p.label = p.local ? "This machine" + (p.country ? " in " + p.country : "") : "Probe " + p.id + " in " + p.country;
    p.stops = p.lat == null ? [] : [{ at: [p.lon, p.lat], label: p.local ? "this machine" : "probe in " + p.country, sure: !p.local }];
    for (const h of p.hops) {
      if (h.lat == null || (sure && h.by !== "name")) continue;
      const l = p.stops[p.stops.length - 1];
      if (!l || l.at[0] !== h.lon || l.at[1] !== h.lat) p.stops.push({ at: [h.lon, h.lat], label: h.city || h.country, sure: h.by === "name" });
    }
    /* a long jump between two sure places may cross a sea: the cables that land near both ends */
    p.seas = [];
    for (let i = 1; i < p.stops.length; i++) {
      const a = p.stops[i - 1], b = p.stops[i], far = km(a.at, b.at);
      if (!a.sure || !b.sure || far < 800) continue;
      const cables = cableList.filter(c => near(c, a.at, 700) && near(c, b.at, 700));
      if (cables.length) p.seas.push({ from: a.label, to: b.label, km: Math.round(far / 100) * 100, cables });
    }
    const cls = p.reached ? "" : " lost";
    p.line = p.stops.length > 1 ? el("path", { class: "rt" + cls, d: line(p.stops.map(s => s.at)) }) : null;
    if (p.line) gRouters.append(p.line);
    if (p.lat == null) continue;
    const dot = el("circle", { class: "pt probe" + cls, cx: X(p.lon), cy: Y(p.lat), r: p.local ? 4 : 3 });
    hint(dot, p.label, (p.reached ? "reached its target in " : "did not reach its target; stopped after ") + p.hops.length + " hops" + (p.stops.length > 1 ? " · via " + p.stops.slice(1).map(s => s.label).join(", ") : ""));
    gRouters.append(dot);
  }
  /* exchange buildings of the origin network */
  const gFac = el("g"), meet = DATA.peering || {};
  for (const f of meet.facilities || []) {
    const x = X(f.lon), y = Y(f.lat), d = el("path", { class: "fac", d: `M${x} ${y - 3}L${x + 3} ${y}L${x} ${y + 3}L${x - 3} ${y}Z` });
    hint(d, f.name, [f.city, f.country].filter(Boolean).join(", "));
    gFac.append(d);
  }
  map.append(gFac);
  map.append(gBgp, gRouters, gTrace);
  for (const [id, g] of [["l-cables", gCable], ["l-routers", gRouters], ["l-bgp", gBgp], ["l-fac", gFac]]) $(id).onchange = e => { g.style.display = e.target.checked ? "" : "none"; };

  /* zoom and pan on the viewBox */
  const HOME = [0, 14, 1000, 400]; let view = HOME.slice();
  const show = () => map.setAttribute("viewBox", view.join(" "));
  function zoom(f, cx, cy) {
    const w = Math.min(1000, Math.max(60, view[2] * f)), h = w * .4;
    cx = cx ?? view[0] + view[2] / 2; cy = cy ?? view[1] + view[3] / 2;
    view = [cx - (cx - view[0]) * w / view[2], cy - (cy - view[1]) * h / view[3], w, h];
    view[0] = Math.max(0, Math.min(1000 - w, view[0])); view[1] = Math.max(0, Math.min(500 - h, view[1])); show();
  }
  $("zin").onclick = () => zoom(.6); $("zout").onclick = () => zoom(1.6); $("zreset").onclick = () => { view = HOME.slice(); show(); };
  map.addEventListener("wheel", e => { e.preventDefault(); const b = map.getBoundingClientRect();
    zoom(e.deltaY < 0 ? .8 : 1.25, view[0] + (e.clientX - b.left) / b.width * view[2], view[1] + (e.clientY - b.top) / b.height * view[3]); }, { passive: false });
  let drag = null;
  map.addEventListener("pointerdown", e => { drag = [e.clientX, e.clientY, view[0], view[1]]; map.setPointerCapture(e.pointerId); });
  map.addEventListener("pointermove", e => { if (!drag) return; const b = map.getBoundingClientRect();
    view[0] = Math.max(0, Math.min(1000 - view[2], drag[2] - (e.clientX - drag[0]) / b.width * view[2]));
    view[1] = Math.max(0, Math.min(500 - view[3], drag[3] - (e.clientY - drag[1]) / b.height * view[3])); show(); });
  map.addEventListener("pointerup", () => { drag = null; });

  /* legend */
  const key = (shape, text) => el("span", {}, shape, text);
  const sw = (cls, kind) => { const s = el("svg", { viewBox: "0 0 26 10" });
    s.append(kind === "dot" ? el("circle", { class: cls, cx: 13, cy: 5, r: 4 }) : el("path", { class: cls, d: "M1 5H25" })); return s; };
  $("legend").append(
    key(sw("pt observer", "dot"), "where this was run from"), key(sw("pt ns", "dot"), "the domain's name servers"), key(sw("pt site", "dot"), "the site's address"),
    key(sw("hop dns", "l"), "DNS question"), key(sw("hop web", "l"), "HTTPS request"),
    key(sw("pt probe", "dot"), "probe whose traceroute arrived"), key(sw("pt probe lost", "dot"), "probe whose traceroute did not"),
    key(sw("pt col", "dot"), "BGP route collector"), key(sw("fac", "dot"), "building where the site's network meets others"),
    key(sw("cable cand", "l"), "cables a trace may ride (open a probe below)"),
    key(sw("cable", "l"), ((CABLES && CABLES.features) || []).length + " submarine cables"),
    key(sw("cable hit", "l"), marked + " landing where routes or probes are down " + STRONG + "% or more"));

  /* header and verdicts */
  $("title").replaceChildren("Trace of ", el("span", {}, DATA.domain));
  $("when").textContent = "asked " + DATA.time + (me ? " from " + me.ip + " (AS" + me.asn + " " + me.holder + ")" : "");
  $("when").append(" · ", el("a", { href: "?domain=" + encodeURIComponent(DATA.domain) + "&fresh=1" }, "survey again"));
  function verdict(tone, q, a, why) {
    $("verdicts").append(el("div", { class: "verdict " + tone }, el("div", { class: "q" }, q), el("div", { class: "a" }, el("i"), el("span", {}, a)), el("div", { class: "why" }, why)));
  }
  const site = DATA.site || {}, front = site.front || {};
  const last = (DATA.steps || []).slice(-1)[0];
  verdict(last && last.servers.length ? "good" : "bad", "Does the name resolve?", (DATA.places || []).some(p => p.kind === "site") ? "Yes" : "No address",
    (DATA.steps || []).map(s => s.zone).join(" → ") + ((DATA.aliases || []).length ? " → alias " + DATA.aliases.join(" → ") : ""));
  verdict(front.kind === "edge" ? "warn" : front.kind === "server" ? "good" : "bad", "Firewall or server?",
    front.kind === "edge" ? "Through an edge: " + front.name : front.kind === "server" ? "The server itself" + (front.name ? " (" + front.name + ")" : "") : "No answer",
    front.kind === "edge" ? "The answer carries the marks of a CDN, cache or web firewall in front of the server." : front.kind === "server" ? "No CDN, cache or firewall marked the answer." : site.note || "");
  verdict(site.up ? "good" : "bad", "Did the site answer?", site.up ? (site.status || "an error") + " in " + site.ms + " ms" : "No", site.up ? "one HTTPS request to https://" + DATA.domain + "/" : site.note || "");
  const tone = { whole: "good", "partly cut": "warn", cut: "bad" }[route.state] || "";
  verdict(tone, "Is the backbone route whole?", route.state === "whole" ? "Yes, whole" : route.state === "partly cut" ? "Partly cut" : route.state === "cut" ? "Cut" : "Not known",
    route.total ? route.seeing + " of " + route.total + " routers watching worldwide have a route to " + route.prefix : route.error || "no address to route to");

  const got = (routers.probes || []).filter(p => p.reached).length, sent = (routers.probes || []).length;
  const toNetwork = routers.kind === "network";
  verdict(!sent ? "" : got === sent ? "good" : got ? "warn" : "bad", toNetwork ? "Do routers reach its network?" : "Do routers get through?", sent ? got + " of " + sent + " probes arrived" : "Not measured",
    sent ? (toNetwork ? "nobody traces " + DATA.domain + " itself: these traceroutes go to " + routers.name + ", in the same network, from " : "traceroutes run by RIPE Atlas probes in ") + [...new Set(routers.probes.map(p => p.country))].join(", ") : routers.note || "");

  /* DNS chain */
  for (const s of DATA.steps || []) $("dns").append(el("li", {}, el("div", { class: "zone" }, s.zone === "." ? ". (root)" : s.zone),
    el("div", { class: "sub" }, s.servers.length ? s.servers.length + " name servers: " + s.servers.join(", ") : "not answered: " + (s.error || ""))));
  for (const p of (DATA.places || []).filter(p => p.kind === "site")) $("dns").append(el("li", { class: "end" }, el("div", { class: "zone" }, p.ip),
    el("div", { class: "sub" }, [p.country, p.asn ? "AS" + p.asn + " " + p.holder : "", p.prefix].filter(Boolean).join(" · "))));

  /* what answered */
  $("front").append(el("p", { style: "margin:0 0 8px" }, front.kind === "edge" ? "The request passed through " + front.name + " before reaching the server."
    : front.kind === "server" ? "The request was answered by the server itself" + (front.name ? ", which says it is " + front.name + "." : ".") : "Nothing answered: " + (site.note || "")));
  for (const e of front.evidence || []) $("front").append(el("div", { class: "mono" }, e));
  if ("hsts" in site && site.up) $("front").append(el("div", { class: site.hsts ? "mono" : "mono changed" }, site.hsts ? "strict-transport-security: " + site.hsts : "no strict-transport-security header"));
  (function () {
    const x = DATA.exposure;
    if (!x || !x.ip) { $("exposure").append(el("p", { class: "note", style: "margin-top:0" }, "Not looked up in this survey.")); return; }
    if (!x.known) { $("exposure").append(el("p", { class: "note", style: "margin-top:0" }, x.note || "Nothing known.")); return; }
    const row = (k, v, cls) => [el("dt", {}, k), el("dd", { class: cls || "" }, v)];
    $("exposure").append(el("dl", {},
      ...row("address", x.ip + (front.kind === "edge" ? " (the edge's, " + front.name + ")" : "")),
      ...row("open ports", x.ports.join(", ") || "none seen"),
      ...row("software", x.software.map(c => c.replace(/^cpe:\/[aoh]:/, "")).join(", ") || "none recognised"),
      ...row("vulnerabilities", x.vulns.join(", ") || "none known", x.vulns.length ? "no" : "ok"),
      ...(x.tags.length ? row("tags", x.tags.join(", ")) : [])),
      el("p", { class: "note" }, "From Shodan InternetDB, which scans the whole internet every week: the observatory sends nothing to the address. Vulnerabilities go by version numbers, so each one needs checking."));
  })();
  $("front").append(el("p", { class: "note" }, "This reads the headers of the answer. A firewall that only filters packets leaves no mark there, so it cannot be seen this way."));

  /* route */
  if (route.transits && route.transits.length) {
    const t = el("table", {}, el("tr", {}, el("th", {}, "network"), el("th", { class: "n" }, "share of " + route.paths + " paths")));
    for (const r of route.transits) { const pct = Math.round(r.paths * 100 / route.paths), bar = el("div", { class: "bar" }, el("b", { style: "width:" + pct + "%" }));
      t.append(el("tr", {}, el("td", {}, r.holder || "AS" + r.asn, el("div", { class: "sub mono" }, "AS" + r.asn), bar), el("td", { class: "n" }, pct + "%"))); }
    $("route").append(t, el("p", { class: "note" }, "Paths seen by route collectors around the world towards AS" + route.origin + " " + (route.origin_holder || "").replace(/\.$/, "") +
      ". These are the networks that carry the traffic; BGP names networks, not the cables they use."));
  } else $("route").append(el("p", { class: "note" }, route.error || "No route data."));

  /* router traces: hops, time and loss per hop, and the sea crossings */
  function latency(p) {
    const got = p.hops.filter(h => h.ms != null);
    if (got.length < 2) return null;
    const W = 420, H = 96, L = 34, B = 18, top = Math.max(...got.map(h => h.ms)) || 1, n = p.hops.length;
    const x = i => L + (W - L - 8) * (n > 1 ? i / (n - 1) : 0), y = v => 8 + (H - B - 8) * (1 - v / top);
    let jump = null;
    for (let i = 1; i < got.length; i++) { const d = got[i].ms - got[i - 1].ms; if (!jump || d > jump.d) jump = { d, hop: got[i] }; }
    const svg = el("svg", { class: "lat", viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Time to each hop" });
    svg.append(el("line", { x1: L, y1: H - B, x2: W - 8, y2: H - B }), el("line", { x1: L, y1: 8, x2: L, y2: H - B }));
    const label = (t, a) => { const e = el("text", a); e.textContent = t; return e; };
    svg.append(label(top.toFixed(0) + " ms", { x: L - 4, y: 12, "text-anchor": "end" }), label("0", { x: L - 4, y: H - B, "text-anchor": "end" }),
      label("hop 1", { x: L, y: H - 4 }), label("hop " + n, { x: W - 8, y: H - 4, "text-anchor": "end" }));
    svg.append(el("path", { d: "M" + got.map(h => x(p.hops.indexOf(h)).toFixed(1) + " " + y(h.ms).toFixed(1)).join("L") }));
    p.hops.forEach((h, i) => svg.append(el("circle", { class: h.ms == null ? "none" : jump && h === jump.hop && jump.d > 20 ? "jump" : "", cx: x(i), cy: h.ms == null ? H - B : y(h.ms), r: 2.6 })));
    return { svg, jump: jump && jump.d > 20 ? jump : null };
  }
  for (const p of routers.probes || []) {
    const t = el("table", {}, el("tr", {}, el("th", {}, "hop"), el("th", {}, "router"), el("th", { class: "n" }, "ms"), el("th", { class: "n" }, "lost")));
    for (const h of p.hops) {
      const where = h.by === "name" ? h.city + " (from its name)" : h.country ? h.country + " (register)" : "";
      t.append(el("tr", {}, el("td", {}, String(h.n)),
        el("td", {}, el("span", { class: "mono" }, h.host || h.ip || "no answer"), h.ip ? el("div", { class: "sub" }, [h.host ? h.ip : "", where, h.asn ? "AS" + h.asn + " " + h.holder : (h.lat == null && !h.host ? "private or unplaced" : "")].filter(Boolean).join(" · ")) : null),
        el("td", { class: "n" }, h.ms == null ? "" : h.ms.toFixed(1)), el("td", { class: "n" + (h.lost && h.lost < h.sent ? " changed" : "") }, h.sent ? h.lost + "/" + h.sent : "")));
    }
    const chart = latency(p), body = el("div", { class: "cols", style: "margin-top:8px" }), side = el("div", { style: "min-width:0" });
    if (chart) side.append(el("div", { class: "sub" }, "Time to each hop" + (chart.jump ? ". Biggest step: +" + chart.jump.d.toFixed(0) + " ms at hop " + chart.jump.hop.n + (chart.jump.hop.city ? " (" + chart.jump.hop.city + ")" : "") : "")), chart.svg);
    if (p.stops.length > 1) side.append(el("p", { class: "note" }, "Path: " + p.stops.map(s => s.label).join(" → ")));
    for (const sea of p.seas) side.append(el("p", { class: "note" }, sea.from + " → " + sea.to + ", about " + sea.km.toLocaleString("en") + " km. " + sea.cables.length +
      (sea.cables.length === 1 ? " cable lands" : " cables land") + " near both ends: " + sea.cables.slice(0, 8).map(c => c.name).join(", ") + (sea.cables.length > 8 ? " and " + (sea.cables.length - 8) + " more" : "") + "."));
    body.append(el("div", { class: "scroll", style: "min-width:0" }, t), side);
    const d = el("details", {}, el("summary", {}, p.label + " ", el("small", { class: p.reached ? "ok" : "no" }, p.reached ? "arrived in " + p.hops.length + " hops" : "did not arrive"),
      p.seas.length ? el("small", {}, " · " + p.seas.length + (p.seas.length === 1 ? " sea crossing" : " sea crossings")) : null), body);
    d.addEventListener("toggle", () => { if (p.line) p.line.classList.toggle("on", d.open); for (const sea of p.seas) for (const c of sea.cables) c.node.classList.toggle("cand", d.open); });
    $("routers").append(d);
  }
  $("routers").append(el("p", { class: "note" }, (routers.probes || []).length
    ? (toNetwork ? "Nobody is tracing " + DATA.domain + " itself, so these are the traceroutes RIPE Atlas probes run to " + routers.name + " (" + routers.target + "), another address of the same network, AS" + route.origin +
        ": they show the routers on the way into that network, and the last hops are that site's, not this one's. Measurement " + routers.measurement + ", the latest from each probe. "
      : "Traceroutes that RIPE Atlas probes run to " + (routers.target || DATA.domain) + " (measurement " + routers.measurement + "), the latest from each probe. ") + "A router is placed by the city code in its host name where it has one, and its line on the map joins those. " +
      "Where two such routers are far apart, the cables that land near both are named and lit on the map while the probe is open: candidates, since no trace says which cable carried it. A hop with no answer is a router that does not reply, or a firewall dropping the packets."
    : routers.note || "No router traces."));
  if (routers.note && (routers.probes || []).length) $("routers").append(el("p", { class: "note" }, routers.note));
  $("routers").append(el("p", { class: "note" }, routers.local_note || (routers.probes || []).some(p => p.local)
    ? routers.local_note || "The first trace is this machine's own, run with its " + routers.probes.find(p => p.local).tool + " command to the site's address: the real route from where the observatory runs."
    : ""));

  /* network path graph: the links the collectors' paths use most, origin on the right */
  (function () {
    const links = route.links || [], names = route.names || {};
    if (!links.length) { $("graph").append(el("p", { class: "note" }, "No path data.")); return; }
    const depth = { [route.origin]: 0 };
    for (let round = 0; round < 4; round++) for (const l of links) if (l.far in depth && !(l.near in depth)) depth[l.near] = depth[l.far] + 1;
    const kept = links.filter(l => l.near in depth && l.far in depth && depth[l.near] === depth[l.far] + 1 && depth[l.near] <= 3);
    const weight = {}; for (const l of kept) { weight[l.near] = (weight[l.near] || 0) + l.paths; weight[l.far] = Math.max(weight[l.far] || 0, 0); }
    const cols = [[], [], [], []]; for (const a of Object.keys(depth)) if (depth[a] <= 3 && (a === route.origin || a in weight)) cols[depth[a]].push(a);
    for (const c of cols) { c.sort((a, b) => (weight[b] || 0) - (weight[a] || 0)); c.length = Math.min(c.length, 8); }
    const shown = new Set(cols.flat()), drawn = kept.filter(l => shown.has(l.near) && shown.has(l.far));
    const used = cols.filter(c => c.length), rows = Math.max(...used.map(c => c.length)), W = 1000, bw = 190, bh = 30, H = rows * 40 + 24, pos = {};
    used.forEach((c, i) => c.forEach((a, j) => { pos[a] = [W - bw - 4 - i * ((W - bw - 8) / Math.max(1, used.length - 1)), 12 + (H - 24 - bh) * (c.length > 1 ? j / (c.length - 1) : .5)]; }));
    /* the risk each network carries: an outage signal, every path through it, an origin nobody authorised */
    const risk = {};
    for (const t of route.transits || []) if (route.paths >= 10 && t.paths === route.paths) risk[t.asn] = ["warn", "Every path seen goes through this network", "single point"];
    if ((route.upstreams || []).length === 1) risk[route.upstreams[0].asn] = ["warn", "The only upstream of the origin", "only upstream"];
    if (/invalid/.test(route.rpki || "")) risk[route.origin] = ["bad", "RPKI: " + route.rpki, "invalid"];
    else if (route.rpki === "unknown") risk[route.origin] = ["warn", "No ROA covers the prefix", "no ROA"];
    for (const o of DATA.path_outages || []) risk[String(o.code).replace(/^AS/i, "")] = ["bad", "Outage signal: " + o.signal + " fell " + o.fall + "%", "outage"];
    const svg = el("svg", { class: "graph", viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Networks and the links between them on the paths to the origin" });
    const most = Math.max(...drawn.map(l => l.paths));
    for (const l of drawn) { const a = pos[l.near], b = pos[l.far], x1 = a[0] + bw, y1 = a[1] + bh / 2, x2 = b[0], y2 = b[1] + bh / 2, e = el("path", { class: "edge", "stroke-width": (1 + 9 * l.paths / most).toFixed(1), d: `M${x1} ${y1}C${(x1 + x2) / 2} ${y1} ${(x1 + x2) / 2} ${y2} ${x2} ${y2}` });
      e.append(el("title", {}, "AS" + l.near + " hands to AS" + l.far + " in " + l.paths + " of " + route.paths + " paths")); svg.append(e); }
    for (const a in pos) { const [x, y] = pos[a], t1 = el("text", { x: x + 8, y: y + 13 }), t2 = el("text", { class: "dim", x: x + 8, y: y + 25 });
      t1.textContent = ((names[a] || "").split(" - ")[0].split(" ")[0] || "AS" + a).slice(0, 24); t2.textContent = "AS" + a + (a === route.origin ? " · origin" : "");
      const r = el("rect", { class: (a === route.origin ? "origin " : "") + (risk[a] ? "risk-" + risk[a][0] : ""), x, y, width: bw, height: bh, rx: 3 });
      if (risk[a]) { r.append(el("title", {}, risk[a][1])); t2.textContent += " · " + risk[a][2]; }
      svg.append(r, t1, t2); }
    $("graph").append(svg, el("p", { class: "note" }, "The " + drawn.length + " links most used by the " + route.paths + " paths the route collectors see. Traffic flows left to right, to the origin; a thicker link is on more paths. A network framed in red has an outage signal or an origin RPKI rejects; in amber, every path depends on it or its prefix has no ROA."));
  })();

  /* BGP: validity, upstreams, changes */
  if (route.prefix) {
    const dl = el("dl", {}, el("dt", {}, "prefix"), el("dd", { class: "mono" }, route.prefix), el("dt", {}, "origin"), el("dd", {}, "AS" + route.origin + " " + (route.origin_holder || "")),
      el("dt", {}, "RPKI"), el("dd", { class: route.rpki === "valid" ? "ok" : route.rpki === "unknown" ? "" : "no" },
        route.rpki === "valid" ? "valid: the origin is authorised to announce it" : route.rpki === "unknown" ? "no authorisation published" : route.rpki),
      el("dt", {}, "upstreams"), el("dd", {}, (route.upstreams || []).map(u => (u.holder || "").split(" ")[0] + " (AS" + u.asn + ")").join(", ") || "none seen"),
      el("dt", {}, "more specific"), el("dd", {}, route.more_specifics ? route.more_specifics + " smaller routes inside this prefix are announced too" : "none: nobody announces a part of this prefix"));
    const others = (route.history || []).filter(h => h.origin !== route.origin);
    dl.append(el("dt", {}, "announced by"), el("dd", { class: others.some(h => Date.parse(DATA.time) - Date.parse(h.last) < 365 * 864e5) ? "no" : "" },
      (route.history || []).map(h => "AS" + h.origin + " " + ((route.names || {})[h.origin] || "").split(" ")[0] + " " + h.first.slice(0, 10) + " to " + (h.origin === route.origin ? "now" : h.last.slice(0, 10))).join("; ") || "no history",
      el("div", { class: "sub" }, others.length ? "Another origin for the same prefix can be a hand-over, or a hijack." : "One origin for all of its history: no sign of a hijack.")));
    const len = Number((route.prefix || "").split("/")[1] || 0), foreign = (route.specifics || []).filter(m => m.origin !== route.origin);
    if ("roa_max" in route && route.rpki === "valid") dl.append(el("dt", {}, "ROA length"), el("dd", { class: route.roa_max > len ? "changed" : "ok" },
      route.roa_max > len ? "up to /" + route.roa_max + " allowed, /" + len + " announced: room for a forged more specific route" : "exactly what is announced, /" + len));
    if (foreign.length) dl.append(el("dt", {}, "from others"), el("dd", { class: "changed" }, foreign.slice(0, 6).map(m => m.prefix + " by AS" + m.origin).join("; ") + ": parts of this prefix announced by another network"));
    if ("irr" in route) dl.append(el("dt", {}, "route object"), el("dd", { class: route.irr.length ? "" : "changed" }, route.irr.length ? "in " + route.irr.join(", ") : "none in the routing registries"));
    $("bgp").append(dl);
    const ups = route.updates || [];
    if (ups.length) {
      const max = Math.max(1, ...ups.map(u => u.announced + u.withdrawn)), spark = el("div", { class: "spark" });
      for (const u of ups) { const col = el("div", { title: u.at.replace("T", " ").slice(0, 16) + ": " + u.announced + " announced, " + u.withdrawn + " withdrawn" },
        el("i", { style: "height:" + (u.announced / max * 56).toFixed(1) + "px" }), el("i", { class: "w", style: "height:" + (u.withdrawn / max * 56).toFixed(1) + "px" })); spark.append(col); }
      const sum = k => ups.reduce((n, u) => n + u[k], 0);
      $("bgp").append(el("div", { class: "sub" }, "Route changes, 6 hours per bar (tallest: " + max + ")"), spark,
        el("div", { class: "axis" }, el("span", {}, ups[0].at.slice(0, 10)), el("span", {}, ups[ups.length - 1].at.slice(0, 10))),
        el("p", { class: "note" }, sum("announced") + " announcements and " + sum("withdrawn") + " withdrawals (red) in this period. A burst of withdrawals is what a broken link looks like in BGP."));
    }
  } else $("bgp").append(el("p", { class: "note" }, route.error || "No route data."));

  /* outages */
  const outs = DATA.outages || [];
  if (outs.length) {
    const t = el("table", {}, el("tr", {}, el("th", {}, "country"), el("th", {}, "signal"), el("th", { class: "n" }, "down")));
    for (const o of outs) t.append(el("tr", {}, el("td", {}, o.country, el("div", { class: "bar out" }, el("b", { style: "width:" + Math.min(100, o.fall) + "%" }))),
      el("td", {}, signal(o.signal), el("div", { class: "sub" }, "since " + new Date(o.since * 1000).toISOString().slice(0, 16).replace("T", " ") + " UTC")), el("td", { class: "n" }, o.fall + "%")));
    $("outages").append(t);
  } else $("outages").append(el("p", { class: "note" }, "No country has an outage signal open."));
  $("outages").append(el("p", { class: "note" }, "No public feed reports cable faults. A cable is marked red when it lands in a country whose routes or probes are down " + STRONG +
    "% or more. That says where to look. It does not say the cable is cut. A fall in Google traffic alone tints the country and marks no cable."));

  const onPath = DATA.path_outages || [];
  $("outages").append(el("h2", { style: "margin-top:18px" }, "On the networks of the route"), onPath.length
    ? el("ul", { class: "plain" }, ...onPath.map(o => el("li", { class: "no" }, o.country + ": " + signal(o.signal) + " down " + o.fall + "%")))
    : el("p", { class: "note", style: "margin-top:0" }, "No outage signal on the origin or on any of the " + (route.transits || []).length + " transit networks."));

  /* alerts: what the survey found to look at, the most serious first */
  (function () {
    const list = DATA.alerts, count = level => list.filter(a => a.level === level).length;
    if (!list) { $("alerts").append(el("p", { class: "note", style: "margin-top:0" }, "This survey was kept before the observatory raised alerts. Survey again for them.")); return; }
    const marks = DATA.scores || [];
    if (marks.length) $("alerts").append(el("div", { class: "scores" }, ...marks.map(m => el("div", { class: "score " + (m.score >= 80 ? "good" : m.score >= 50 ? "warn" : "bad") },
      el("b", {}, String(m.score)), el("span", {}, m.block), el("small", {}, m.alerts ? m.alerts + (m.alerts === 1 ? " alert" : " alerts") : "nothing found")))));
    if (!list.length) { $("alerts").append(el("p", { class: "note", style: "margin-top:0" }, "Nothing to look at: no check of this survey raised an alert.")); return; }
    $("alerts-title").textContent = "Alerts: " + ["critical", "warning", "notice"].filter(count).map(l => count(l) + " " + l).join(", ");
    $("alerts").append(el("ul", { class: "alerts" }, ...list.map(a => el("li", { class: a.level },
      el("span", { class: "level" }, a.level), el("span", { class: "area" }, a.area),
      el("div", {}, a.text, el("div", { class: "sub" }, a.fix))))));
  })();

  /* DNS health and mail */
  (function () {
    const h = DATA.health || {}, subs = DATA.subdomains, rs = h.resolvers || [], same = rs.length > 1 && rs.every(r => r.addresses.slice().sort().join() === rs[0].addresses.slice().sort().join());
    const row = (k, v, cls) => [el("dt", {}, k), el("dd", { class: cls || "" }, v)];
    $("health").append(el("dl", {},
      ...row("DNSSEC", h.dnssec ? "signed, and the resolver checked it" : "not signed: answers cannot be checked for forgery", h.dnssec ? "ok" : "changed"),
      ...(h.dnssec && h.algorithm ? row("signature", "algorithm " + h.algorithm + (h.signed_until ? ", good until " + new Date(h.signed_until * 1000).toISOString().slice(0, 16).replace("T", " ") + " UTC" : ""), [1, 3, 5, 6, 7].includes(h.algorithm) ? "changed" : "") : []),
      ...row("kept for", h.ttl == null ? "unknown" : h.ttl + " s before a resolver asks again"),
      ...row("zone serial", h.serial || "not at this name"),
      ...row("resolvers", rs.map(r => r.name + ": " + (r.addresses.join(", ") || "none")).join(" · ")),
      ...row("", rs.length < 2 ? "" : same ? "Both give the same addresses." : "They differ. That is usual when a site answers by where the question comes from."),
      ...row("mail servers", (h.mx || []).join(", ") || "none: the domain takes no mail"),
      ...row("SPF", h.spf || "none: anyone can send mail as this domain", h.spf ? "" : "changed"),
      ...row("DMARC", h.dmarc || "none: receivers are not told what to do with forged mail", h.dmarc ? "" : "changed"),
      ...row("CAA", (h.caa || []).join(", ") || "none: any authority may issue its certificates"),
      ...((h.mx || []).length && "mta_sts" in h ? row("mail in transit", [h.mta_sts ? "MTA-STS" : "", h.tls_rpt ? "TLS reports" : "", h.dane ? "DANE" : ""].filter(Boolean).join(", ") || "no MTA-STS and no DANE: encryption can be stripped on the way", h.mta_sts || h.dane ? "" : "changed") : []),
      ...(subs ? row("subdomains", subs.checked ? subs.checked + " names of the certificate logs checked; " + (subs.hanging.length ? subs.hanging.map(x => x.name + " → " + x.target).join(", ") + " left hanging" : "none left hanging") : "none in the certificate logs", subs.hanging.length ? "no" : "") : [])),
      el("p", { class: "note" }, "A resolver cannot be made to ask each name server on its own over HTTPS, so two resolvers are compared here."));
  })();

  /* certificates */
  (function () {
    const cs = DATA.certificates || [];
    if (!cs.length) { $("certs").append(el("p", { class: "note", style: "margin-top:0" }, "None read from the logs.")); return; }
    const t = el("table", {}, el("tr", {}, el("th", {}, "issued by"), el("th", {}, "for"), el("th", { class: "n" }, "ends")));
    for (const c of cs) { const days = Math.round((Date.parse(c.ends + "Z") - Date.parse(DATA.time)) / 864e5);
      t.append(el("tr", {}, el("td", {}, c.issuer), el("td", { class: "mono" }, c.name), el("td", { class: "n" + (days < 15 ? " changed" : "") }, c.ends.slice(0, 10), el("div", { class: "sub" }, days + " days")))); }
    $("certs").append(t, el("p", { class: "note" }, "From certificate transparency (crt.sh): what authorities have issued for this name and is still in force, not necessarily the one the server sent."));
  })();

  /* IPv6 */
  (function () {
    const v = DATA.v6 || {}, r = v.route, ps = v.probes || [];
    if (!(v.addresses || []).length) { $("v6").append(el("p", { class: "note", style: "margin-top:0" }, v.note || "No IPv6 address.")); return; }
    const got = ps.filter(p => p.reached).length;
    $("v6").append(el("dl", {}, el("dt", {}, "address"), el("dd", { class: "mono" }, v.addresses.map(a => a.ip).join(", ")),
      el("dt", {}, "network"), el("dd", {}, v.addresses[0].asn ? "AS" + v.addresses[0].asn + " " + v.addresses[0].holder : "unknown"),
      el("dt", {}, "route"), el("dd", { class: r && r.state === "whole" ? "ok" : "no" }, r ? r.state + ": " + r.seeing + " of " + r.total + " routers have " + r.prefix : "unknown"),
      el("dt", {}, "RPKI"), el("dd", { class: r && r.rpki === "valid" ? "ok" : "" }, r ? r.rpki : "unknown"),
      el("dt", {}, "routers"), el("dd", { class: ps.length && got === ps.length ? "ok" : ps.length ? "no" : "" }, ps.length ? got + " of " + ps.length + " probes arrived (" + ps.map(p => (p.country || p.id) + " " + p.hops + " hops").join(", ") + ")" : v.note || "not measured")));
    if (v.note && ps.length) $("v6").append(el("p", { class: "note" }, v.note));
  })();

  /* exchange points */
  (function () {
    if (!meet.name) { $("peering").append(el("p", { class: "note", style: "margin-top:0" }, meet.error || "The origin network has no PeeringDB record.")); return; }
    $("peering").append(el("dl", {}, el("dt", {}, "network"), el("dd", {}, meet.name + " (AS" + route.origin + ")"), el("dt", {}, "peering"), el("dd", {}, [meet.policy, meet.scope, meet.traffic].filter(Boolean).join(" · ")),
      el("dt", {}, "exchanges"), el("dd", {}, (meet.exchanges || []).join(", ") || "none listed"),
      el("dt", {}, "buildings"), el("dd", {}, (meet.facilities || []).map(f => f.name).join("; ") || "none listed")),
      el("p", { class: "note" }, "From PeeringDB, as the network declares it. The buildings are on the map."));
  })();

  /* history: what changed since the run before, and the runs kept */
  (function () {
    const runs = DATA.history || [], prev = runs[0], rt = route, now = { address: (DATA.places || []).filter(p => p.kind === "site").map(p => p.ip)[0] || "", route: rt.state, seeing: rt.seeing, total: rt.total,
      arrived: got, probes: sent, status: site.status, ms: site.ms, front: front.name, rpki: rt.rpki, origin: rt.origin };
    if (!prev) { $("since").append(el("p", { class: "note", style: "margin-top:0" }, "This is the first run of " + DATA.domain + " kept here. The next one will say what changed.")); }
    else {
      const diff = [], say = (k, label) => { if (String(prev[k]) !== String(now[k])) diff.push(label + ": " + (prev[k] === "" ? "none" : prev[k]) + " → " + (now[k] === "" ? "none" : now[k])); };
      say("address", "address"); say("origin", "origin network"); say("route", "route"); say("rpki", "RPKI"); say("front", "in front of the site"); say("status", "HTTP status");
      if (prev.seeing !== now.seeing || prev.total !== now.total) diff.push("routers with the route: " + prev.seeing + "/" + prev.total + " → " + now.seeing + "/" + now.total);
      if (prev.arrived !== now.arrived || prev.probes !== now.probes) diff.push("probes that arrived: " + prev.arrived + "/" + prev.probes + " → " + now.arrived + "/" + now.probes);
      if (prev.ms > 0 && (now.ms > prev.ms * 1.5 || now.ms < prev.ms / 1.5)) diff.push("answer time: " + prev.ms + " ms → " + now.ms + " ms");
      $("since").append(diff.length ? el("ul", { class: "plain" }, ...diff.map(d => el("li", { class: "changed" }, d))) : el("p", { class: "note", style: "margin-top:0" }, "Nothing changed since " + prev.at.slice(0, 16).replace("T", " ") + " UTC."));
    }
    const seq = [Object.assign({ at: DATA.time }, now), ...runs], steps = [];
    for (let i = 0; i + 1 < seq.length; i++) for (const [k, label] of [["address", "address"], ["origin", "origin network"], ["route", "route"], ["rpki", "RPKI"], ["front", "in front of the site"], ["status", "HTTP status"]])
      if (String(seq[i][k]) !== String(seq[i + 1][k])) steps.push([seq[i].at, label + ": " + (seq[i + 1][k] === "" ? "none" : seq[i + 1][k]) + " → " + (seq[i][k] === "" ? "none" : seq[i][k]), k === "origin" || k === "rpki" || k === "route"]);
    if (runs.length) $("timeline").append(steps.length ? el("ol", { class: "timeline" }, ...steps.map(c => el("li", { class: c[2] ? "hot" : "" }, el("time", {}, String(c[0]).slice(0, 16).replace("T", " ")), el("span", {}, c[1]))))
      : el("p", { class: "note", style: "margin-top:0" }, "Nothing changed in the " + seq.length + " runs kept."));
    else $("timeline").append(el("p", { class: "note", style: "margin-top:0" }, "One run so far: a timeline needs two."));
    if (!runs.length) { $("history").append(el("p", { class: "note", style: "margin-top:0" }, "None yet. Runs are kept in observatory.db.")); return; }
    const t = el("table", {}, el("tr", {}, el("th", {}, "when (UTC)"), el("th", {}, "route"), el("th", { class: "n" }, "probes"), el("th", { class: "n" }, "HTTP"), el("th", { class: "n" }, "ms")));
    for (const r of runs) t.append(el("tr", {}, el("td", {}, r.at.slice(0, 16).replace("T", " ")), el("td", {}, r.route + " " + r.seeing + "/" + r.total), el("td", { class: "n" }, r.arrived + "/" + r.probes), el("td", { class: "n" }, String(r.status || "")), el("td", { class: "n" }, String(r.ms))));
    $("history").append(t);
  })();

  const src = (t, u) => el("a", { href: u }, t);
  $("foot").append("Sources, all asked when this page was made: DNS from Cloudflare's resolver over HTTPS; places, networks and routes from ", src("RIPEstat", "https://stat.ripe.net/"), "; router traces from ", src("RIPE Atlas", "https://atlas.ripe.net/"), "; exchange points from ", src("PeeringDB", "https://www.peeringdb.com/"), "; certificates from ", src("crt.sh", "https://crt.sh/"),
    "; outage signals from ", src("IODA", "https://ioda.inetintel.cc.gatech.edu/"), " (Georgia Tech); cables from the ", src("Submarine Cable Map", "https://www.submarinecablemap.com/"),
    " (TeleGeography); coastlines from Natural Earth. A place is the city in a router's name where it has one; otherwise it is the address register's, usually exact to the country only.");
}

(async function () {
  const $ = id => document.getElementById(id);
  const say = text => { $("status").textContent = text; $("status").hidden = !text; };
  $("report").hidden = true;
  const get = async url => {
    const r = await fetch(url);
    if (r.status === 204) return null;
    const body = await r.json().catch(() => null);
    if (!r.ok) throw new Error((body && body.error) || r.status + " " + r.statusText);
    return body;
  };
  /* what is typed becomes a domain name: an address pasted whole loses its scheme, path and port */
  const clean = text => text.trim().toLowerCase().replace(/^[a-z]+:\/\//, "").replace(/[\/?#:].*$/, "").replace(/\.$/, "");
  const isDomain = name => name.length <= 253 && /^([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$/.test(name);
  $("ask").addEventListener("submit", e => {
    e.preventDefault();
    const name = clean($("field").value);
    $("field").value = name;
    if (!isDomain(name)) { say("\"" + name + "\" is not a domain name. Type one like example.com."); $("field").focus(); return; }
    location.search = "?domain=" + encodeURIComponent(name);
  });
  const query = new URLSearchParams(location.search), domain = clean(query.get("domain") || "");
  if (!domain) {
    /* nothing asked: the domains surveyed here before */
    const runs = await get("api/runs").catch(() => []);
    if (runs && runs.length) {
      const list = document.createElement("ul");
      for (const r of runs) {
        const li = document.createElement("li"), a = document.createElement("a");
        a.href = "?domain=" + encodeURIComponent(r.domain); a.textContent = r.domain;
        li.append(a, " · route " + r.route + " · " + r.arrived + "/" + r.probes + " probes arrived · " + r.at.slice(0, 16).replace("T", " ") + " UTC");
        list.append(li);
      }
      $("recent").append("Surveyed here before:", list);
    }
    return;
  }
  $("field").value = domain;
  if (!isDomain(domain)) { say("\"" + domain + "\" is not a domain name. Type one like example.com."); return; }
  say("Surveying " + domain + ": DNS, routes, routers, certificates, outages. A new survey takes one to two minutes; one made in the last quarter of an hour comes at once.");
  try {
    const world = await get("world.json"), cables = await get("api/cables"), landings = await get("api/landings");
    const data = await get("api/trace/" + encodeURIComponent(domain) + (query.get("fresh") ? "?fresh=true" : ""));
    say(""); $("report").hidden = false;
    render(world, cables, landings, data);
  } catch (e) {
    say("The survey of " + domain + " failed: " + e.message);
  }
})();
