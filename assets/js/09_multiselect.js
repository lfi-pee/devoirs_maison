
// ============================================================================
// Sélection multiple de communes (retour Elia, point 4). En mode multi, cliquer une
// commune l'ajoute/retire d'une sélection ; la « fiche agrégée » somme les voix et
// recompose les pourcentages pondérés par les inscrits. Le SCORE MUNICIPALES est retiré
// de l'agrégat (incomparable d'une commune à l'autre : tête de liste LFI ou non).
// Pensée pour les GA à l'échelle interco / circo (territoires ruraux, beaucoup de communes).
// « + Circo » ajoute une circonscription entière : ses communes entières, ET le MORCEAU
// de chacune de ses communes partagées (values/_circo.json, baké bureau par bureau).
function clearSel(){ if(selCodes.size||selParts.size){ selCodes.clear(); selParts.clear(); }
  selBarSync(); }

function repaintSel(){ if(layer&&layer.eachLayer)layer.eachLayer(l=>l.feature&&layer.resetStyle(l)); }

// Un clic déselectionne ce qui est sélectionné, en partie comme en entier, et sélectionne
// en ENTIER ce qui ne l'est pas : la carte ne sait pas désigner un morceau de commune, et
// un troisième état n'aurait pas de geste pour en sortir.
function toggleSel(code){ if(selParts.has(code))selParts.delete(code);
  else if(selCodes.has(code))selCodes.delete(code); else selCodes.add(code);
  repaintSel(); selBarSync(); }

// Le tout absorbe la partie : une commune ajoutée en entier perd ses morceaux.
function selEntiere(code){ selParts.delete(code); selCodes.add(code); }

function selBarSync(){ const bar=$("selbar"); if(!bar)return;
  bar.style.display=multiSel?"flex":"none";
  if(window.__syncLayout)window.__syncLayout();
  $("selcount").textContent=selLabel()||"Cliquez les communes à regrouper";
  const n=selCodes.size+selParts.size;
  $("selview").disabled=n<1; $("selclear").disabled=n<1;
  syncCircoSelect(); }

// Le décompte dit combien de communes ne sont là QUE pour leurs bureaux de la circo : sans
// ça, « 15 communes » et un total d'inscrit·es trois fois trop petit pour Montpellier se
// contrediraient sans que rien ne l'explique.
function selLabel(){ const n=selCodes.size+selParts.size, p=selParts.size; if(!n)return "";
  return `${n} commune${n>1?"s":""} sélectionnée${n>1?"s":""}`
    +(p?` (dont ${p} partielle${p>1?"s":""})`:""); }

// niveau commune-choroplèthe affiché = on est ENTRÉ dans un département (sommet de pile).
const auNiveauCommunes=()=>{ const t=stack[stack.length-1]; return !!t&&t.niveau==="departement"; };

// Sélecteur de circonscription : peuplé avec les circos du département courant
// (values/_circo.json, chargé paresseusement et mis en cache). Chaque circo y porte `c`,
// ses communes ENTIÈRES, et `p`, les valeurs du MORCEAU de chacune de ses communes
// partagées — déjà sommées bureau par bureau côté pipeline.
let circoData=null, circoDep=null;
const circoLabel=c=>`${+c.split("-")[1]}ᵉ circonscription`;
async function syncCircoSelect(){ const sel=$("selcirco"), btn=$("seladdcirco"); if(!sel)return;
  const dep=(multiSel&&auNiveauCommunes())?stack[stack.length-1].code:null;
  const show=!!dep; sel.style.display=btn.style.display=show?"":"none";
  if(!show){ circoDep=null; return; }
  if(dep===circoDep)return; circoDep=dep;
  if(!circoData)circoData=await getJSON("values/_circo.json")||{};
  const circos=Object.keys(circoData).filter(c=>c.startsWith(dep+"-")).sort();
  sel.innerHTML=`<option value="">— circonscription —</option>`+
    circos.map(c=>`<option value="${c}">${circoLabel(c)}</option>`).join("");
  btn.disabled=!circos.length; }

// Agrégat de la sélection : voix/effectifs sommés, pourcentages pondérés par les inscrits
// (registre `insc_E24`, reconstitué du stock d'abstention en repli). On exclut tout *_M26
// (score municipales retiré) et le contexte social (une médiane de médianes n'a pas de sens).
function aggregateSelection(){
  const os=[...selCodes].map(c=>curVals[c]).filter(Boolean);
  // Les morceaux entrent dans l'agrégat comme des zones à part entière : ils portent les
  // mêmes clés, sommées sur leurs seuls bureaux (cf. prep_bake._baker_circonscriptions).
  selParts.forEach(m=>m.forEach(v=>os.push(v)));
  if(!os.length)return null;
  const inscOf=inscRef;
  // Voix à conquérir 2027 : extensif (voix, conjoncturels, portes, heures, km) → somme ;
  // intensif (abstention prédite, plancher, gauche prédite) → moyenne pondérée par les
  // inscrits ; γ → moyenne pondérée par les CONJONCTURELS, sur lesquels il s'applique ;
  // part de portes en voiture → moyenne pondérée par les portes. Le rendement de la
  // version 3, lui, n'est jamais moyenné : il se recalcule en `mobn / mobh` sur l'agrégat.
  const MOB_CNT=new Set(["mob","mobc","mobn","mobp","mobpt","mobh","mobk"]);
  const MOB_POND={mobg:"mobc",mobv:"mobpt"};
  // `resinsc` est un solde SIGNÉ : le sommer sur une sélection donne le solde net du
  // territoire, où les communes d'origine des mal-inscrit·es compensent les villes qui les
  // accueillent. C'est la bonne agrégation, et elle n'était pas possible tant que les
  // écarts négatifs n'étaient pas servis. Tous les autres EFFECTIFS se somment aussi :
  // voix, registres et votant·es de chaque scrutin (`insc_`, `vot_` — ce sont eux qui
  // permettent de relire en personnes les pourcentages pondérés de la fiche agrégée),
  // population (`pop`) et corps électoral potentiel (`maj`).
  const isCount=k=>/^(lfiv_|gv_|insc_|vot_)/.test(k)||k==="abst"||k==="resinsc"
    ||k==="maj"||k==="pop"||MOB_CNT.has(k);
  const isPct=k=>/^(part|lfi|gauche|rn|em|lr)_/.test(k)||k==="moba"||k==="mobf"||k==="mobl";
  const agg={}, wsum={}, wnum={}; let inscTot=0;
  os.forEach(o=>{ const insc=inscOf(o); if(insc)inscTot+=insc;
    for(const k in o){ if(k.endsWith("_M26")||typeof o[k]!=="number")continue;
      const pk=MOB_POND[k], p=pk?o[pk]:null;
      if(isCount(k))agg[k]=(agg[k]||0)+o[k];
      else if(pk){ if(p){ wsum[k]=(wsum[k]||0)+o[k]*p; wnum[k]=(wnum[k]||0)+p; } }
      else if(isPct(k)&&insc){ wsum[k]=(wsum[k]||0)+o[k]*insc; wnum[k]=(wnum[k]||0)+insc; } } });
  for(const k in wsum)if(wnum[k])agg[k]=Math.round(wsum[k]/wnum[k]*10)/10;
  // Repli : là où aucune commune de la sélection ne porte `insc_E24`, le registre reste
  // reconstitué du stock d'abstention (cf. inscRef) — on le sert sous le même nom pour
  // que la fiche agrégée n'ait rien de particulier à savoir.
  if(inscTot&&agg[`insc_${SC_REGISTRE}`]==null)agg[`insc_${SC_REGISTRE}`]=inscTot;
  agg.reg=os[0].reg;
  return agg;
}

function openAggregate(){ const o=aggregateSelection(); if(!o)return;
  infoPanel(selLabel(),o,"multi",null); }

(function(){ const mt=$("multitoggle"); if(!mt)return;
  mt.onclick=()=>{ multiSel=!multiSel; mt.setAttribute("aria-pressed",String(multiSel));
    document.body.classList.toggle("multi",multiSel);
    if(!multiSel){ selCodes.clear(); selParts.clear(); }
    repaintSel(); selBarSync(); };
  $("selclear").onclick=()=>{ clearSel(); repaintSel(); };
  $("selview").onclick=openAggregate;
  $("selall").onclick=()=>{ if(!auNiveauCommunes()||!layer)return;
    layer.eachLayer(l=>l.feature&&selEntiere(l.feature.properties.__code));
    repaintSel(); selBarSync(); };
  // Une commune entière déjà sélectionnée le reste : elle contient son morceau. Une
  // commune dont on ajoute DEUX circos garde les deux morceaux, qui s'additionnent — et
  // les ajouter toutes redonne exactement la commune.
  $("seladdcirco").onclick=()=>{ const c=$("selcirco").value, d=circoData&&circoData[c];
    if(!d)return;
    (d.c||[]).forEach(code=>{ if(curVals[code])selEntiere(code); });
    for(const code in (d.p||{})){ if(selCodes.has(code)||!curVals[code])continue;
      let m=selParts.get(code); if(!m){ m=new Map(); selParts.set(code,m); }
      m.set(c,d.p[code]); }
    repaintSel(); selBarSync(); };
  selBarSync(); })();
