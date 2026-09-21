#!/usr/bin/env python3
"""Valide le découpage des communes partagées entre plusieurs circonscriptions.

Une circonscription ne se somme pas à la commune : 127 communes en touchent plusieurs, et
elles portent 15 % du corps électoral. `prep_elections` les découpe donc par BUREAU DE VOTE
(niveau `circo_partiel`), et `prep_bake` sert le résultat dans `values/_circo.json`, où
chaque circonscription porte ses communes ENTIÈRES (`c`) et les valeurs de chacun de ses
MORCEAUX de commune partagée (`p`).

Quatre contrôles, dont aucun ne demande de source extérieure au pipeline — ce qu'il faut
vérifier ici n'est pas une mesure du monde mais une ARITHMÉTIQUE : ces morceaux ne sont
qu'une redistribution de bureaux déjà comptés ailleurs.

1. **Les morceaux d'une commune redonnent la commune**, effectif par effectif et scrutin
   par scrutin. C'est ce qui garantit qu'aucune voix n'est perdue ni comptée deux fois, et
   c'est la propriété que la répartition des bureaux non placés existe pour préserver
   (cf. prep_elections.morceaux_de_commune).

2. **Chaque morceau est exactement la somme de ses bureaux**, vérifié aux législatives
   2024 — le seul scrutin que la correspondance couvre à 100 %, donc le seul où le
   découpage se lit sans aucune répartition. Le contrôle repart de
   `resultats_bureau.parquet`, sans passer par la table des morceaux qu'il vérifie.

3. **Un département conserve son électorat.** La somme de ses circonscriptions doit être la
   somme de ses communes. C'est exactement la propriété qui était violée : les dix-huit
   circonscriptions de Paris servaient chacune la ville entière, et le département pesait
   dix-huit fois son poids.

4. **Aucune circonscription n'en double une autre.** Deux circonscriptions d'un même
   département ne doivent plus jamais servir le même total d'inscrit·es — la signature du
   défaut, et ce qu'on verrait en premier s'il revenait.

Sortie attendue (mesurée le 21 septembre 2026) :

    577 circonscriptions · 127 communes partagées · 309 morceaux
    1. Σ des morceaux = la commune : 127 sur 127 (écart max 0,01 %, lfiv_M26 à 06088)
    2. morceau = Σ de ses bureaux (législatives 2024) : 309 sur 309
    3. conservation par département : 107 sur 107
    4. circonscriptions en doublon : aucune

    uv run --project ./hexagonal python validation_circo.py
"""

from __future__ import annotations

import collections
import json
import sys
from pathlib import Path

import pandas as pd

from prep_elections import FICHIER_CIRCO_BUREAUX

DA = Path(__file__).parent / "data_app"
VALUES = DA / "values"
# Le registre sur lequel la fiche agrégée pondère ses pourcentages : c'est lui qu'un écart
# fausserait visiblement, et le seul servi à toutes les échelles.
CLE_REGISTRE = "insc_E24"
# Scrutin de contrôle du découpage : la correspondance porte SA numérotation, elle y place
# donc tous les bureaux et aucun n'a besoin d'être réparti.
SCRUTIN_EXACT = "2024-legislatives-1"
CLE_EXACTE = "insc_L24"
# Tolérance. Elle n'absorbe pas une erreur de découpage mais l'ARRONDI : les valeurs servies
# au client sont des entiers par morceau, et la répartition se fait en fractions.
ECART_MAX = 0.002


def _charger() -> tuple[pd.DataFrame, dict, dict[str, dict]]:
    f = DA / FICHIER_CIRCO_BUREAUX
    circo = VALUES / "_circo.json"
    if not f.exists() or not circo.exists():
        sys.exit(
            f"{f.name} ou values/_circo.json absent — lancer prepare_data.py + prep_bake.py"
        )
    com: dict[str, dict] = {}
    for p in sorted((VALUES / "commune").glob("*.json")):
        com |= json.loads(p.read_text())
    return pd.read_parquet(f), json.loads(circo.read_text()), com


def _total_servi(d: dict, com: dict[str, dict], cle: str = CLE_REGISTRE) -> int:
    """Ce que la fiche agrégée sommera pour cette circonscription : communes + morceaux."""
    return sum((com.get(c) or {}).get(cle, 0) for c in d.get("c") or []) + sum(
        (v.get(cle) or 0) for v in (d.get("p") or {}).values()
    )


def somme_des_morceaux(data: dict, com: dict[str, dict]) -> tuple[int, int, float, str]:
    morceaux: dict[str, list[dict]] = collections.defaultdict(list)
    for d in data.values():
        for code, v in (d.get("p") or {}).items():
            morceaux[code].append(v)
    ok, pire, pire_cle = 0, 0.0, ""
    for code, ms in morceaux.items():
        base = com.get(code)
        if not base:
            continue
        juste = True
        for cle, attendu in base.items():
            # Effectifs seulement : un pourcentage ne s'additionne pas, et `resinsc` est un
            # solde dérivé (majeur·es − inscrit·es) qui suit mécaniquement ses deux termes.
            if not isinstance(attendu, int) or not (
                cle.startswith(("insc_", "vot_", "lfiv_", "gv_")) or cle in ("pop", "maj")
            ):
                continue
            ecart = abs(sum(m.get(cle, 0) for m in ms) - attendu) / (attendu or 1)
            if ecart > pire:
                pire, pire_cle = ecart, f"{cle} à {code}"
            juste &= ecart <= ECART_MAX
        ok += juste
    return ok, len(morceaux), pire, pire_cle


def morceau_contre_bureaux(
    bur: pd.DataFrame, data: dict
) -> tuple[int, int, list[tuple[str, str, int, int]]]:
    bv = pd.read_parquet(
        DA / "resultats_bureau.parquet", columns=["code", "scrutin", "inscrits"]
    )
    insc = bv[bv["scrutin"] == SCRUTIN_EXACT].set_index("code")["inscrits"]
    ref = (
        bur.assign(i=bur["code_bv"].map(insc).fillna(0))
        .groupby(["circonscription", "code_commune"])["i"]
        .sum()
        .to_dict()
    )
    ok, total, mauvais = 0, 0, []
    for circ, d in data.items():
        for code, v in (d.get("p") or {}).items():
            total += 1
            attendu, servi = ref.get((circ, code), 0), v.get(CLE_EXACTE, 0)
            if abs(servi - attendu) / (attendu or 1) <= ECART_MAX:
                ok += 1
            else:
                mauvais.append((circ, code, servi, round(attendu)))
    return ok, total, mauvais


def conservation_par_departement(
    bur: pd.DataFrame, data: dict, com: dict[str, dict]
) -> tuple[int, int, list[tuple[str, int, int]]]:
    servi: dict[str, int] = collections.defaultdict(int)
    for circ, d in data.items():
        servi[circ.split("-")[0]] += _total_servi(d, com)
    communes: dict[str, set[str]] = collections.defaultdict(set)
    for code, circ in zip(bur["code_commune"], bur["circonscription"]):
        communes[circ.split("-")[0]].add(code)
    ok, mauvais = 0, []
    for dep, codes in sorted(communes.items()):
        attendu = sum((com.get(c) or {}).get(CLE_REGISTRE, 0) for c in codes)
        if not attendu:
            continue
        if abs(servi[dep] - attendu) / attendu <= ECART_MAX:
            ok += 1
        else:
            mauvais.append((dep, servi[dep], attendu))
    return ok, len(communes), mauvais


def doublons(data: dict, com: dict[str, dict]) -> list[tuple[str, str, int]]:
    par_valeur: dict[tuple[str, int], list[str]] = collections.defaultdict(list)
    for circ, d in data.items():
        t = _total_servi(d, com)
        if t:
            par_valeur[(circ.split("-")[0], t)].append(circ)
    return [
        (dep, ", ".join(sorted(cs)), t)
        for (dep, t), cs in sorted(par_valeur.items())
        if len(cs) > 1
    ]


def main() -> int:
    bur, data, com = _charger()
    n_circo = bur.groupby("code_commune")["circonscription"].nunique()
    print(
        f"{len(data)} circonscriptions · {(n_circo > 1).sum()} communes partagées · "
        f"{sum(len(d.get('p') or {}) for d in data.values())} morceaux"
    )
    echec = False

    ok, total, pire, pire_cle = somme_des_morceaux(data, com)
    print(
        f"1. Σ des morceaux = la commune : {ok} sur {total} "
        f"(écart max {pire * 100:.2f} %, {pire_cle or '—'})"
    )
    echec |= ok < total

    ok, total, mauvais = morceau_contre_bureaux(bur, data)
    print(f"2. morceau = Σ de ses bureaux (législatives 2024) : {ok} sur {total}")
    for circ, code, servi, attendu in mauvais[:10]:
        print(f"   ✗ {circ} / {code} : {servi} servis pour {attendu} bureaux")
    echec |= bool(mauvais)

    ok, total, ecarts = conservation_par_departement(bur, data, com)
    print(f"3. conservation par département : {ok} sur {total}")
    for dep, servi, attendu in ecarts[:10]:
        print(f"   ✗ {dep} : {servi:,} servis pour {attendu:,}".replace(",", " "))
    echec |= bool(ecarts)

    dbl = doublons(data, com)
    print(f"4. circonscriptions en doublon : {'aucune' if not dbl else len(dbl)}")
    for dep, cs, t in dbl[:10]:
        print(f"   ✗ {dep} : {cs} servent {t:,} inscrit·es".replace(",", " "))
    echec |= bool(dbl)

    return 1 if echec else 0


if __name__ == "__main__":
    sys.exit(main())
