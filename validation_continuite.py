#!/usr/bin/env python3
"""Valide le crosswalk des bureaux de vote contre une source INDÉPENDANTE : Paris.

Le pipeline retrouve l'appariement bureau ↔ contour avec un seul témoin, l'écart d'INSCRITS
entre deux millésimes (cf. prep_elections). C'est un raisonnement, pas une observation : rien,
dans les données que consomme l'atlas, ne permet de VOIR qu'un code a changé de territoire.

Sauf à Paris. La Ville publie les SECTEURS OFFICIELS de ses bureaux — de vrais polygones
administratifs, et non les cellules de Voronoï approchées du jeu national — pour 2021, 2022
et 2026. C'est le seul territoire de France où un « avant » et un « après » géométriques
existent : le national n'a qu'un millésime (REU du 1er juin 2022) et sa source amont, le
fichier d'adresses d'électeurs de l'INSEE, est elle-même une extraction de septembre 2022.
Lyon publie un millésime courant sans « avant », Marseille rien depuis 2019.

On apparie donc les secteurs 2026 aux secteurs 2022 par RECOUVREMENT DE SURFACE, et on
compare le résultat à ce que le crosswalk déduit des inscrits. Sortie attendue (mesurée le
5 septembre 2026) :

    39 couples sur 39 confirmés · 859 des 864 autres bureaux confirmés à l'identique
    5 désaccords : 4 bureaux créés depuis 2022 (sans contour, donc déjà écartés)
    et 75056_1371, dont le secteur 2026 recouvre à 98 % celui de l'ancien 75056_1334

Ce dernier cas est un défaut RÉEL que le pipeline ne peut pas voir : dans un arrondissement
de 72 codes, un seul code faux ne déplace ni la médiane des écarts ni la part de bureaux
faux. Il est écrit dans DOCUMENTATION.md plutôt que laissé tacite.

    uv run --project ./hexagonal python validation_continuite.py
"""

from __future__ import annotations

import argparse
import urllib.request
from pathlib import Path

import geopandas as gpd

import prep_elections

SECTEURS = {
    2026: "secteurs-des-bureaux-de-vote-2026",
    2022: "secteurs-des-bureaux-de-vote",
}
URL = "https://opendata.paris.fr/api/explore/v2.1/catalog/datasets/{}/exports/geojson"
CACHE = Path("data_app") / "_paris_cache"
# Recouvrement minimal pour retenir un appariement géométrique. Les secteurs officiels d'une
# année à l'autre se superposent presque exactement là où rien n'a bougé (99,9 % de médiane) :
# un candidat sous ce seuil est un bureau CRÉÉ, dont le territoire a été prélevé sur
# plusieurs anciens, pas un bureau renuméroté.
RECOUVREMENT_MIN = 0.90


def _charger(annee: int) -> gpd.GeoDataFrame:
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / f"secteurs-{annee}.geojson"
    if not f.exists():
        url = URL.format(SECTEURS[annee])
        print(f"  ↓ {url}")
        urllib.request.urlretrieve(url, f)
    g = gpd.read_file(f).to_crs(2154)
    g["code"] = [
        f"75056_{int(a):02d}{int(n):02d}"
        for a, n in zip(g["arrondissement"], g["num_bv"])
    ]
    return g[["code", "geometry"]]


def valider(dossier_clean: Path, geo_dir: Path) -> dict[str, int]:
    a26, a22 = _charger(2026), _charger(2022)
    print(f"secteurs officiels : 2026 = {len(a26)} · 2022 = {len(a22)}")
    inter = gpd.overlay(
        a26, a22.rename(columns={"code": "code22"}), how="intersection",
        keep_geom_type=True,
    )
    inter["part"] = inter.geometry.area / inter["code"].map(
        dict(zip(a26["code"], a26.geometry.area))
    )
    meilleur = inter.sort_values("part").groupby("code").tail(1).set_index("code")
    geo_map = {
        c: r.code22 for c, r in meilleur.iterrows() if r.part >= RECOUVREMENT_MIN
    }
    crosswalk, _ = prep_elections.construire_crosswalk_plm(dossier_clean, geo_dir)
    crosswalk = {k: v for k, v in crosswalk.items() if k.startswith("75056_")}
    contours = set(
        gpd.read_file(geo_dir / "75.geojson", ignore_geometry=True)["bureau"].astype(str)
    )

    confirmes = sum(1 for k, v in crosswalk.items() if geo_map.get(k) == v)
    print(f"\ncrosswalk déduit des inscrits : {len(crosswalk)} couples")
    print(f"  confirmés par la géométrie officielle : {confirmes} / {len(crosswalk)}")
    for k, v in sorted(crosswalk.items()):
        if geo_map.get(k) != v:
            print(f"   DÉSACCORD {k} : pipeline → {v} · géométrie → {geo_map.get(k)}")

    autres = {k: v for k, v in geo_map.items() if k not in crosswalk}
    identiques = sum(1 for k, v in autres.items() if k == v)
    print(f"\nautres bureaux parisiens : {len(autres)}")
    print(f"  identité confirmée par la géométrie : {identiques}")
    faux = [(k, v) for k, v in autres.items() if k != v]
    for k, v in sorted(faux):
        peint = "PEINT SUR LE MAUVAIS CONTOUR" if k in contours else "sans contour (créé)"
        print(f"   {k} → géométrie dit {v} · {peint}")
    return {
        "couples": len(crosswalk),
        "confirmes": confirmes,
        "identite_confirmee": identiques,
        "desaccords": len(faux),
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--clean", type=Path, default=Path("hexagonal/data/02_clean/elections"))
    ap.add_argument("--geo", type=Path, default=Path("data_app/geo/bv"))
    a = ap.parse_args()
    valider(a.clean, a.geo)
