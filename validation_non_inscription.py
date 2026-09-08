#!/usr/bin/env python3
"""Confronte le RÉSERVOIR D'INSCRIPTION à une mesure INSEE indépendante, département par
département.

`resinsc` (cf. prep_bake._baker_carnet) est un solde : majeur·es de nationalité française
résidant dans la commune − inscrit·es. Rien, dans les données que consomme l'atlas, ne
permet de VOIR s'il est juste : la non-inscription n'est publiée à aucune échelle
infra-départementale, et le solde mélange trois populations (non-inscrit·es, résident·es
inscrit·es ailleurs, inscrit·es qui n'habitent plus là) qu'aucune source ne sépare.

Sauf par le haut. Deux contraintes existent, et elles suffisent à FALSIFIER l'estimateur :

1. **L'identité nationale.** Sommée sur toutes les communes, la mal-inscription s'annule
   exactement : chaque mal-inscrit·e est retranché·e là où il ou elle réside et ajouté·e là
   où il ou elle est inscrit·e. Le solde national DOIT donc valoir la non-inscription pure,
   que l'INSEE mesure à 2,9 M — 5,8 % des Français·es majeur·es (Insee Première n°1986,
   présidentielle 2022). Tout écart est de la contamination des listes : des inscrit·es qui
   ne sont dans la population résidente de personne — Français·es de l'étranger sur liste
   communale, radiations en retard.
2. **La forme départementale.** La même étude publie, figure 4, la part des personnes non
   inscrites dans leur commune de résidence principale, PAR DÉPARTEMENT DE RÉSIDENCE (le
   titre de la feuille le dit ; le sens de la ventilation n'est pas indifférent, il décide
   du signe attendu). Un département dont beaucoup de résident·es sont inscrit·es ailleurs
   doit afficher un solde plus positif. Le lien doit exister, être positif, et la droite
   doit couper l'axe vers +5,8 % — la non-inscription qui reste quand la mal-inscription
   est nulle.

Le test est mené sur les inscrit·es de la PRÉSIDENTIELLE 2022, scrutin de référence de
l'étude INSEE, quand le site publie sur le registre des européennes 2024 : on compare ce
qui est comparable, pas ce qui est affiché.

Sortie mesurée le 8 septembre 2026 (100 départements ; Mayotte est hors champ INSEE) :

    total national        2,25 M   contre 2,84 M attendus  ->  -21 %
    Spearman (métropole)   0,475
    pente 0,645   ordonnée à l'origine -0,063   (attendue ~ +0,058)
    dispersion des résidus 3,06 points

Autrement dit : **l'estimateur porte un vrai signal et reste faux d'un décalage.** Le lien
avec la mesure INSEE est net (p < 1e-6), donc le solde dit bien quelque chose du terrain ;
mais il manque 0,59 M au national et la droite passe 12 points sous là où la comptabilité
l'attend. C'est une seule et même chose, la contamination des listes, qui n'est pas
modélisée — voir EVOLUTIONS.md pour ce qui reste à faire.

Deux repères pour la suite, à ne pas perdre :

- **Seine-Saint-Denis** était le pire écart du pays (+8,6 points au-dessus de la droite)
  tant que la part de nationalité française était mesurée toutes générations confondues.
  Avec `part_fr18` (NAT1, cf. prep_admin._part_fr_18p) il tombe à +7,0 et cède la première
  place. C'est le seul des quatre grands écarts que la nationalité explique.
- **Les Ardennes** (+7,3 points) sont le département le MOINS mal-inscrit de France
  (10,8 %) et affichent pourtant un solde de 8,0 %. Rien dans le modèle actuel ne
  l'explique. Anomalie ouverte.
- **L'outre-mer casse la relation** (Guadeloupe : 23,6 % côté INSEE, −11,1 % côté solde) :
  ses listes portent une diaspora sans équivalent métropolitain. Les DOM sont donc mesurés
  et affichés, mais exclus de l'ajustement, qui porte sur la métropole.

    uv run --project ./hexagonal python validation_non_inscription.py
"""

from __future__ import annotations

import argparse
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

import prep_geo

# Insee Première n°1986, « Élection présidentielle 2022 : 16,5 % des électeurs inscrits
# l'étaient dans une autre commune que celle de leur résidence principale ».
INSEE_IP1986 = (
    "https://www.insee.fr/fr/statistiques/fichier/7766966/"
    "donnees_insee_premiere_n1986.xlsx"
)
FEUILLE = "Figure 4"
SCRUTIN = "2022-presidentielle-1"  # scrutin de référence de l'étude INSEE
CIBLE_NI = 0.058  # part de non-inscrit·es parmi les majeur·es français·es (INSEE)
# Paris/Lyon/Marseille : le recensement est ventilé par arrondissement, l'électoral porte
# sur le code INSEE agrégé. Même rabattement que prep_bake.PLM_AGG.
PLM = {"751": "75056", "6938": "69123", "132": "13055"}


def _dep(code: str) -> str:
    return code[:3] if code.startswith("97") else code[:2]


def _canon(code: str) -> str:
    for prefixe, agg in PLM.items():
        if code.startswith(prefixe) and code not in PLM.values():
            return agg
    return code


def _mal_inscription_insee(cache: Path) -> pd.DataFrame:
    """Figure 4 de l'étude : une part par département DE RÉSIDENCE, en pourcentage."""
    dest = cache / "donnees_insee_premiere_n1986.xlsx"
    if not dest.exists():
        prep_geo._telecharger(INSEE_IP1986, dest)
    fig = pd.read_excel(dest, sheet_name=FEUILLE, skiprows=2, dtype={0: str})
    fig = fig.iloc[:, :3].set_axis(["dep", "libelle", "mal_insc"], axis=1)
    fig = fig.dropna(subset=["dep", "mal_insc"])
    fig["mal_insc"] = pd.to_numeric(fig["mal_insc"], errors="coerce") / 100
    return fig.dropna(subset=["mal_insc"])


def _solde_par_departement(da: Path) -> tuple[pd.DataFrame, str]:
    """Le solde `maj − inscrits` agrégé au département, reconstruit exactement comme
    prep_bake._baker_carnet le construit à la commune."""
    adm = pd.read_parquet(da / "admin_commune.parquet")
    adm = adm[adm["code_commune"] != "FRANCE"].copy()
    cle = "part_fr18" if "part_fr18" in adm.columns and adm["part_fr18"].notna().any() else "part_fr"
    adm["maj"] = adm["pop18"] * adm[cle] / 100

    res = pd.read_parquet(da / "resultats_commune.parquet")
    ins = res.loc[res["scrutin"] == SCRUTIN, ["code", "inscrits"]]
    ins = ins.rename(columns={"code": "code_commune", "inscrits": "insc"})

    adm["code_commune"] = adm["code_commune"].astype(str).map(_canon)
    adm = adm.groupby("code_commune", as_index=False)["maj"].sum()
    m = adm.merge(ins, on="code_commune", how="inner")
    m["dep"] = m["code_commune"].map(_dep)
    d = m.groupby("dep", as_index=False).agg(maj=("maj", "sum"), insc=("insc", "sum"))
    d["solde_rel"] = (d["maj"] - d["insc"]) / d["maj"]
    return d, cle


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", type=Path, default=Path("data_app"))
    ap.add_argument("--cache", type=Path, default=Path("data_app/_insee_cache"))
    args = ap.parse_args()
    args.cache.mkdir(parents=True, exist_ok=True)

    d, cle = _solde_par_departement(args.data)
    j = d.merge(_mal_inscription_insee(args.cache), on="dep", how="inner")
    metro = j[~j["dep"].str.startswith("97")]
    national = (j["maj"].sum() - j["insc"].sum()) / 1e6
    attendu = CIBLE_NI * j["maj"].sum() / 1e6

    pente, origine = np.polyfit(metro["mal_insc"], metro["solde_rel"], 1)
    resid = metro["solde_rel"] - np.polyval((pente, origine), metro["mal_insc"])
    rang = metro["mal_insc"].rank().corr(metro["solde_rel"].rank())

    print(f"  part de nationalité française utilisée : {cle}")
    print(f"  {len(j)} départements appariés (Mayotte hors champ INSEE)\n")
    print(f"  total national         {national:5.2f} M   contre {attendu:.2f} M attendus"
          f"  ->  {100 * (national - attendu) / attendu:+.0f} %")
    print(f"  Spearman (métropole)   {rang:5.3f}")
    print(f"  pente {pente:.3f}   ordonnée à l'origine {origine:+.3f}"
          f"   (attendue ~ {CIBLE_NI:+.3f})")
    print(f"  dispersion des résidus {100 * resid.std():.2f} points\n")

    pires = metro.assign(ecart=100 * resid).reindex(
        resid.abs().sort_values(ascending=False).index
    )
    print("  départements les plus mal reproduits :")
    for r in pires.head(5).itertuples():
        print(f"    {r.libelle:<24} INSEE {100 * r.mal_insc:5.1f} %"
              f"   solde {100 * r.solde_rel:6.1f} %   écart {r.ecart:+5.1f} pts")


if __name__ == "__main__":
    main()
