"""Données administratives INSEE (recensement 2021) par commune, pour la « fiche
circonscription » de la prez (slides 25-28) ramenée à l'échelle communale :
pyramide des âges, statut d'occupation (propriétaires/locataires), déplacements
domicile-travail par mode, et — depuis le RNE — le maire en exercice.

Sources : bases infracommunales (IRIS) du recensement, agrégées à la commune via
la colonne COM, + Répertoire national des élus (data.gouv). On télécharge dans un
cache local ; chaque jeu est optionnel (téléchargement raté → colonnes absentes)."""

from __future__ import annotations

import zipfile
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

import prep_geo

BASE_IC = {  # jeu -> (id page INSEE, nom de fichier zip CSV)
    "pop": ("8268806", "base-ic-evol-struct-pop-2021_csv.zip"),
    "log": ("8268838", "base-ic-logement-2021_csv.zip"),
    "act": ("8268843", "base-ic-activite-residents-2021_csv.zip"),
}
INSEE = "https://www.insee.fr/fr/statistiques/fichier"

AGES = ["0014", "1529", "3044", "4559", "6074", "75P"]  # 6 tranches (slide 26)
# Tranches FINES de la même base IC, qui découpent la population à 18 ans pile. La
# pyramide des slides démarre à 15 ans : en déduire les majeur·es par « 15 et + moins un
# cinquième des 15-29 » sur-comptait les adultes dans 30 299 communes sur 34 970 (médiane
# +1,3 %, jusqu'à +2,5 %) et les sous-comptait de 4,1 % à Montpellier — le biais suit la
# part des jeunes (corrélation −0,53), donc il fabrique des non-inscrit·es fantômes dans
# les communes âgées. On somme donc les tranches exactes.
AGES_18P = ["1824", "2539", "4054", "5564", "6579", "80P"]
# NAT1 (« Population par sexe, âge et nationalité ») : la SEULE table INSEE qui croise
# nationalité et âge sous le département. Publiée à la commune et à l'arrondissement PLM,
# en quatre tranches seulement — 0-14 / 15-24 / 25-54 / 55 et + — d'où le rabattement
# ci-dessous des six tranches fines de la base IC sur les trois tranches adultes.
NAT1 = ("8202752", "TD_NAT1_2021_csv.zip")
NAT1_DE_AGE = {
    "1824": "15",
    "2539": "25",
    "4054": "25",
    "5564": "55",
    "6579": "55",
    "80P": "55",
}
TRANSPORTS = ["PAS", "MAR", "VELO", "2ROUESMOT", "VOIT", "TCOM"]  # slide 28
# IRAN (résidence un an avant) -> 5 catégories de la slide 25, dans l'ordre d'affichage :
# même logement / autre logement même commune / autre commune du dépt / hors dépt en
# France (autre dépt, hors région, DOM, COM) / à l'étranger (UE + hors UE). Z = sans objet.
IRAN_CAT = {"1": 0, "2": 1, "3": 2, "4": 3, "5": 3, "6": 3, "7": 3, "8": 4, "9": 4}
ANNEE_REF = 2026  # année de référence pour l'âge du maire (snapshot RNE 2026)


@dataclass(frozen=True)
class AdminTables:
    commune: pd.DataFrame  # une ligne / commune + une ligne code_commune="FRANCE"


def _lire_base_ic(zip_path: Path) -> pd.DataFrame:
    with zipfile.ZipFile(zip_path) as z:
        nom = next(
            n
            for n in z.namelist()
            if n.upper().endswith(".CSV") and not n.startswith("meta")
        )
        with z.open(nom) as f:
            return pd.read_csv(
                f, sep=";", dtype={"COM": str}, encoding="utf-8", low_memory=False
            )


def _telecharger_bases(cache: Path) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for jeu, (page, fichier) in BASE_IC.items():
        dest = cache / fichier
        if dest.exists() or prep_geo._telecharger(f"{INSEE}/{page}/{fichier}", dest):
            out[jeu] = dest
    return out


def _agreger(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    """Somme des effectifs (estimés, flottants) par commune (COM) + total France."""
    g = df.groupby("COM")[cols].sum()
    g.loc["FRANCE"] = df[cols].sum()
    return g


def _pop_par_age(pop: pd.DataFrame) -> pd.DataFrame:
    """Pyramide des âges en parts (slide 26) + les deux effectifs dont dépend l'estimation
    du corps électoral potentiel : `pop18` (majeur·es, tranches exactes) et `part_fr` (part
    de nationalité française). Un·e résident·e étranger·e n'est PAS un·e non-inscrit·e :
    hors ressortissant·es de l'UE (liste complémentaire, marginale), elle ou il ne peut pas
    s'inscrire. Sans cette part, l'écart « population majeure − inscrits » comptait 43 484
    étranger·es de Montpellier comme un réservoir d'inscription."""
    cols = (
        [f"P21_{s}{a}" for s in ("H", "F") for a in AGES]
        + [f"P21_POP{a}" for a in AGES_18P]
        + ["P21_POP", "P21_POP_FR"]
    )
    g = _agreger(pop, [c for c in cols if c in pop.columns])
    out = pd.DataFrame(index=g.index)
    out["pop"] = g["P21_POP"].round().astype(int)
    base = g["P21_POP"].replace(0, float("nan"))
    for s in ("H", "F"):
        for i, a in enumerate(AGES):
            out[f"age{s}_{i}"] = (100 * g[f"P21_{s}{a}"] / base).round(2)
    fines = [f"P21_POP{a}" for a in AGES_18P if f"P21_POP{a}" in g.columns]
    if len(fines) == len(AGES_18P):
        out["pop18"] = g[fines].sum(axis=1).round().astype(int)
    if "P21_POP_FR" in g.columns:
        out["part_fr"] = (100 * g["P21_POP_FR"] / base).round(2)
    return out


def _part_fr_18p(cache: Path, pop: pd.DataFrame) -> pd.Series:
    """Part de nationalité française parmi les MAJEUR·ES, commune par commune.

    `part_fr` (cf. `_pop_par_age`) est mesurée sur TOUTE la population, mineur·es compris,
    et sert pourtant à ramener `pop18` au corps électoral potentiel. Or la structure par
    âge des deux populations diffère, et pas du même signe partout : là où les ménages
    étranger·es sont jeunes et avec enfants, la part d'étranger·es parmi les adultes est
    plus forte que dans l'ensemble, donc `part_fr` SUR-estime le corps électoral potentiel
    et fabrique du réservoir d'inscription ; ailleurs c'est l'inverse. Le biais n'est donc
    pas un niveau qu'on pourrait absorber dans un calage national — il est GÉOGRAPHIQUE,
    et c'est la géographie qui décide où on envoie une équipe.

    Mesure de ce que ça change (validation_non_inscription.py, 9 septembre 2026) : au
    national presque rien, 49,10 M de majeur·es français·es contre 49,01 M — 84 000
    personnes, 0,17 %. Mais en Seine-Saint-Denis, département le plus étranger de
    métropole, l'écart du résidu départemental à sa droite d'ajustement tombe de +8,6 à
    +6,9 points.
    C'est de loin le plus gros des quatre grands écarts déplacés : le Val-de-Marne recule
    de 0,6 point (+7,4 à +6,8), les Ardennes de 0,2 (+7,5 à +7,4) et les Hautes-Alpes de
    0,2 (−7,1 à −6,9) — ces trois-là ne viennent donc pas de la nationalité.
    EVOLUTIONS.md dit ce qui reste.

    NAT1 ne descend pas sous quatre tranches d'âge : la tranche « 15-24 » porte donc aussi
    les 15-17 ans, dont la part d'étranger·es n'est pas exactement celle des 18-24. Biais
    résiduel, sans commune mesure avec celui qu'il remplace.

    Une tranche muette ne doit pas rendre la commune muette. Deux garde-fous, parce que
    NAT1 se tait de deux façons différentes et qu'aucune ne veut dire « zéro Français·e » :

    - une tranche PEUPLÉE dont aucun·e habitant·e n'est français·e n'a pas de ligne
      `INATC == "1"` du tout. Le rapport y vaut zéro, pas NaN : d'où le `fillna(0)` sur le
      numérateur, appliqué APRÈS avoir vérifié que le dénominateur, lui, est bien peuplé.
    - une tranche ABSENTE de la table, ou peuplée de zéro personne, ne dit rien du tout.
      On ne l'invente pas : elle sort du numérateur ET du dénominateur, et la part est la
      moyenne des seules tranches renseignées.

    Sans ces deux garde-fous une seule tranche muette suffisait à faire retomber toute la
    commune sur `part_fr` : 234 communes en 2021, dont 13 seulement étaient réellement
    absentes de NAT1. Il en reste **19** : ces 13, plus les 6 villages de la Meuse détruits
    en 1914-1918 et jamais repeuplés, que NAT1 déclare à zéro habitant·e dans chacune des
    trois tranches adultes — d'eux, il n'y a rien à mesurer. Plus toutes les communes si le
    fichier n'est pas téléchargeable, la série renvoyée étant alors vide."""
    dest = cache / NAT1[1]
    if not dest.exists() and not prep_geo._telecharger(
        f"{INSEE}/{NAT1[0]}/{NAT1[1]}", dest
    ):
        return pd.Series(dtype=float)
    with zipfile.ZipFile(dest) as z:
        nom = next(n for n in z.namelist() if n.upper().endswith(".CSV"))
        with z.open(nom) as f:
            nat = pd.read_csv(
                f,
                sep=";",
                usecols=["NIVGEO", "CODGEO", "AGE4", "INATC", "NB"],
                dtype={"NIVGEO": str, "CODGEO": str, "AGE4": str, "INATC": str},
            )
    # ARM = arrondissements de Paris/Lyon/Marseille, dont la base IC porte aussi le code
    # (751xx…) : les prendre ensemble aligne les deux tables sans rabattement.
    nat = nat[nat["NIVGEO"].isin(("COM", "ARM"))]
    g = nat.groupby(["CODGEO", "AGE4", "INATC"])["NB"].sum().unstack("INATC")
    if "1" not in g.columns:
        return pd.Series(dtype=float)
    # Dénominateur d'abord : une tranche à zéro (ou absente) ne dit rien, et NaN la sort
    # du calcul. Le numérateur, lui, est légitimement vide quand la tranche est peuplée
    # sans aucun·e Français·e — d'où le zéro, jamais un NaN, une fois le peuplement acquis.
    total = g.sum(axis=1).replace(0, float("nan"))
    part = (g["1"].fillna(0).where(total.notna()) / total).unstack("AGE4")

    fines = [a for a in AGES_18P if f"P21_POP{a}" in pop.columns]
    if len(fines) != len(AGES_18P):
        return pd.Series(dtype=float)
    eff = pop.groupby("COM")[[f"P21_POP{a}" for a in fines]].sum()
    # Moyenne des seules tranches renseignées : une tranche muette sort des DEUX sommes,
    # au lieu de propager son NaN à toute la commune.
    num = den = 0.0
    for a in fines:
        p = part.get(NAT1_DE_AGE[a], pd.Series(dtype=float)).reindex(eff.index)
        e = eff[f"P21_POP{a}"].where(p.notna(), 0.0)
        num = num + e * p.fillna(0.0)
        den = den + e
    out = 100 * num / den.replace(0, float("nan"))
    # Ligne FRANCE : la moyenne des communes PONDÉRÉE par leurs majeur·es, jamais la
    # moyenne des parts — sinon 34 000 villages pèsent autant que Paris.
    m = num.notna() & den.notna()
    out.loc["FRANCE"] = 100 * num[m].sum() / den[m].sum()
    return out.round(2)


def _logement(log: pd.DataFrame) -> pd.DataFrame:
    cols = ["P21_RP", "P21_RP_PROP", "P21_RP_LOC", "P21_RP_LOCHLMV", "P21_RP_GRAT"]
    g = _agreger(log, cols)
    base = g["P21_RP"].replace(0, float("nan"))
    return pd.DataFrame(
        {
            "prop": (100 * g["P21_RP_PROP"] / base).round(1),
            "loc": (100 * g["P21_RP_LOC"] / base).round(1),
            "hlm": (100 * g["P21_RP_LOCHLMV"] / base).round(1),
            "grat": (100 * g["P21_RP_GRAT"] / base).round(1),
        },
        index=g.index,
    )


def _transport(act: pd.DataFrame) -> pd.DataFrame:
    cols = [f"C21_ACTOCC15P_{m}" for m in TRANSPORTS]
    g = _agreger(act, cols)
    base = g[cols].sum(axis=1).replace(0, float("nan"))
    out = pd.DataFrame(index=g.index)
    for i, m in enumerate(TRANSPORTS):
        out[f"tr_{i}"] = (100 * g[f"C21_ACTOCC15P_{m}"] / base).round(1)
    return out


def _migration_canton(cache: Path) -> pd.DataFrame:
    """Renouvellement de population (slide 25) : répartition des résidents selon leur
    lieu de résidence un an avant (IRAN), pondérée par IPONDI. Les fichiers détail
    « individus localisés » ne sont géographiés qu'au **canton-ou-ville** (CANTVILLE,
    confidentialité) : on agrège donc à ce grain (+ ligne FRANCE). Optionnel."""
    parts = sorted(cache.glob("ilc_*.zip"))
    if not parts:
        return pd.DataFrame()
    acc: dict[str, list[float]] = {}
    fr = [0.0] * 5
    for part in parts:
        with zipfile.ZipFile(part) as z:
            nom = next(n for n in z.namelist() if n.upper().endswith(".CSV"))
            for chunk in pd.read_csv(
                z.open(nom),
                sep=";",
                usecols=["CANTVILLE", "IRAN", "IPONDI"],
                dtype={"CANTVILLE": str, "IRAN": str},
                chunksize=2_000_000,
            ):
                chunk["cat"] = chunk["IRAN"].map(IRAN_CAT)
                chunk = chunk.dropna(subset=["cat"])
                for (cv, cat), poids in (
                    chunk.groupby(["CANTVILLE", "cat"])["IPONDI"].sum().items()
                ):
                    acc.setdefault(cv, [0.0] * 5)[int(cat)] += poids
                    fr[int(cat)] += poids
    acc["FRANCE"] = fr
    out = pd.DataFrame.from_dict(
        acc, orient="index", columns=[f"mig_{i}" for i in range(5)]
    )
    tot = out.sum(axis=1).replace(0, float("nan"))
    return out.div(tot, axis=0).mul(100).round(1)


def _migration_communes(canton: pd.DataFrame, communes: pd.DataFrame) -> pd.DataFrame:
    """Rabat le profil de renouvellement (canton-ou-ville) sur chaque commune via son
    canton COG ; repli sur le pseudo-canton « <dépt>ZZ » (Paris, Lyon, grandes villes
    d'outre-mer). Index = code_commune (+ ligne FRANCE)."""
    prof = canton.to_dict("index")
    cols = [f"mig_{i}" for i in range(5)]
    lignes: dict[str, dict] = {"FRANCE": prof["FRANCE"]}
    for code, canton_code, dep in zip(
        communes["code_commune"], communes["code_canton"], communes["code_departement"]
    ):
        p = prof.get(canton_code) or prof.get(f"{dep}ZZ")
        if p:
            lignes[str(code)] = p
    return pd.DataFrame.from_dict(lignes, orient="index")[cols].rename_axis("COM")


def _maires(cache: Path) -> pd.DataFrame:
    """Maire en exercice (nom + CSP) par commune, depuis le RNE."""
    dest = cache / "elus-maires.csv"
    url = (
        "https://static.data.gouv.fr/resources/repertoire-national-des-elus-1/"
        "20260505-152119/elus-maires-mai.csv"
    )
    if not dest.exists() and not prep_geo._telecharger(url, dest):
        return pd.DataFrame(columns=["maire", "maire_csp"]).rename_axis("COM")
    df = pd.read_csv(dest, sep=";", dtype=str)
    df = df.rename(
        columns={
            "Code de la commune": "COM",
            "Nom de l'élu": "nom",
            "Prénom de l'élu": "prenom",
            "Libellé de la catégorie socio-professionnelle": "maire_csp",
            "Date de naissance": "naissance",
        }
    )
    df["maire"] = (
        df["prenom"].str.strip() + " " + df["nom"].str.title().str.strip()
    ).str.strip()
    df["maire_naissance"] = pd.to_numeric(df["naissance"].str[:4], errors="coerce")
    return df.set_index("COM")[["maire", "maire_csp", "maire_naissance"]]


def champs_client(row: pd.Series) -> dict:
    """Champs administratifs (slides 25-28) prêts pour le client, à partir d'une ligne
    d'admin_commune (commune ou ligne « FRANCE »). Listes compactes + maire."""

    def g(c: str) -> float | None:
        return round(float(row[c]), 1) if c in row and pd.notna(row[c]) else None

    d: dict = {
        "ageh": [g(f"ageH_{i}") for i in range(6)],
        "agef": [g(f"ageF_{i}") for i in range(6)],
        "tr": [g(f"tr_{i}") for i in range(6)],
        "prop": g("prop"),
        "loc": g("loc"),
        "hlm": g("hlm"),
    }
    if "mig_0" in row and pd.notna(row.get("mig_0")):
        d["mig"] = [g(f"mig_{i}") for i in range(5)]
    if "maire" in row and pd.notna(row.get("maire")):
        d["maire"] = str(row["maire"])
        if pd.notna(row.get("maire_csp")):
            d["csp"] = str(row["maire_csp"])
        if "maire_naissance" in row and pd.notna(row.get("maire_naissance")):
            d["maire_age"] = ANNEE_REF - int(row["maire_naissance"])
    return d


def construire_admin(cache: Path, communes: pd.DataFrame) -> AdminTables:
    """`communes` : table COG (code_commune, code_canton, code_departement) servant à
    rabattre le renouvellement (grain canton-ou-ville) sur chaque commune."""
    cache.mkdir(parents=True, exist_ok=True)
    bases = _telecharger_bases(cache)
    morceaux: list[pd.DataFrame] = []
    if "pop" in bases:
        pop = _lire_base_ic(bases["pop"])
        morceaux.append(_pop_par_age(pop))
        part18 = _part_fr_18p(cache, pop)
        if not part18.empty:
            morceaux.append(part18.rename("part_fr18").to_frame())
    if "log" in bases:
        morceaux.append(_logement(_lire_base_ic(bases["log"])))
    if "act" in bases:
        morceaux.append(_transport(_lire_base_ic(bases["act"])))
    if not morceaux:
        return AdminTables(pd.DataFrame())
    commune = pd.concat(morceaux, axis=1)
    canton = _migration_canton(cache)
    if not canton.empty:
        commune = commune.join(_migration_communes(canton, communes), how="left")
    commune = commune.join(_maires(cache), how="left")
    commune = commune.reset_index().rename(
        columns={"COM": "code_commune", "index": "code_commune"}
    )
    return AdminTables(commune)
