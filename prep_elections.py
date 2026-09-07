"""Transforme les résultats électoraux bruts de hexagonal (un fichier parquet long
par scrutin, une ligne par candidat × bureau de vote) en tables compactes prêtes à
l'emploi, à toutes les échelles : bureau de vote, commune, département, région, France.

Indicateurs produits par (échelle × scrutin), comme demandé par la présentation :
- participation / abstention (% des inscrits)
- scores des 6 blocs de la « recomposition » (% des inscrits)
- scores des 3 blocs de la tripartition (% des inscrits)
- voix LFI / gauche (en valeur absolue, pour les réservoirs de voix)
"""

from __future__ import annotations

import collections
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import NamedTuple

import geopandas as gpd
import pandas as pd

from nuances import (
    BLOC6_ORDRE,
    FAMILLE_BLOC6,
    FAMILLE_TRIPARTITION,
    FAMILLES_GAUCHE,
    FAMILLES_LFI,
    SANS_NUANCE,
    TRIPARTITION_ORDRE,
    famille_de_liste,
    nuance_vers_famille,
)

FAMILLES = sorted(set(FAMILLE_BLOC6) | {"UDI"})
# colonnes pivotées : les familles ventilables + le fourre-tout « aucune nuance publiée »,
# qui sert à détecter les communes non ventilées et n'entre dans AUCUN bloc.
COLONNES_VOIX = [*FAMILLES, SANS_NUANCE]


PLM_COMMUNES = ("75056", "69123", "13055")

# Scrutins de liste 2026 (tour 1) pour lesquels la table des listes soutenues par LFI
# fait foi : la nuance du ministère sous-estime souvent l'implantation insoumise
# (une union FI–Écolos–PCF peut être étiquetée « LDVG »).
SCRUTINS_LISTES_LFI = ("2026-municipales-1", "2026-conseils-PLM-1")


def charger_listes_lfi(fichier: Path) -> set[tuple[str, int]]:
    """Clés (code_circonscription, numéro de panneau) des listes CONDUITES par LFI.

    On ne garde que les listes dont la tête de liste est étiquetée LFI
    (`étiquette_tdl == "LFI"`) : les listes d'union que LFI soutient sans les conduire
    (têtes DVG, PCF, PS, écolos…) relèvent de la gauche, pas du « vote LFI ».
    code_circonscription matche `code_commune` (communes) ou `code_secteur` (PLM,
    métropole de Lyon) ; le numéro de panneau identifie la liste au sein du scrutin.

    Les deux graphies des colonnes sont acceptées : hexagonal a livré cette table avec des
    en-têtes accentués (`numéro_panneau`, `étiquette_tdl`) puis sans. Exiger une seule des
    deux faisait échouer la lecture sur le millésime opposé, et l'échec ne se voyait qu'à
    l'exécution — un scrutin entier ventilé sans la requalification des listes LFI."""
    df = pd.read_parquet(fichier)
    df = df.rename(
        columns={"numero_panneau": "numéro_panneau", "etiquette_tdl": "étiquette_tdl"}
    )
    manque = {"code_circonscription", "numéro_panneau", "étiquette_tdl"} - set(
        df.columns
    )
    if manque:
        raise ValueError(f"{fichier} : colonnes manquantes {sorted(manque)}")
    df = df.dropna(subset=["numéro_panneau"])
    df = df[df["étiquette_tdl"] == "LFI"]
    return {
        (str(c), int(p))
        for c, p in zip(df["code_circonscription"], df["numéro_panneau"])
    }


def _canon_suffix(s: pd.Series) -> pd.Series:
    """Numéro de bureau canonique = zéro-padding sur 4 chiffres ('1' → '0001'), pour
    matcher les contours et homogénéiser les scrutins entre eux (certains fichiers du
    ministère paddent, d'autres non : sans ça le même bureau a deux clés)."""
    s = s.astype(str)
    return s.where(~s.str.fullmatch(r"\d+"), s.str.zfill(4))


# Codes départementaux d'avant la départementalisation, encore utilisés par les fichiers
# de 2012 et 2014 pour les DOM (ZA101 = Les Abymes, ZM514 = Ouangani). La lettre porte le
# département d'aujourd'hui, le chiffre qui suit celui d'alors.
DOM_LETTRE_DEP = {"ZA": "971", "ZB": "972", "ZC": "973", "ZD": "974", "ZM": "976"}


def _canon_commune(s: pd.Series) -> pd.Series:
    """Code commune canonique (INSEE, 5 caractères).

    Deux encodages hérités de l'outre-mer, l'un et l'autre bâtis sur le même principe —
    département actuel + chiffre du département d'alors + numéro de commune — et corrigés
    de la même façon : on retire le chiffre du milieu.

    1. Les européennes 2014 codent l'outre-mer sur SIX chiffres, d'où « 974411 » pour
       Saint-Denis de La Réunion (97411) ou « 976501 » pour Acoua (97601, Mayotte étant
       encore le 985 en 2014).
    2. La présidentielle 2012 et les municipales 2014 le codent par une LETTRE :
       « ZA101 » pour Les Abymes (97101), « ZM514 » pour Ouangani (97614). Ces codes
       ressemblent à ceux des Français·es de l'étranger (`ZZ…`) et du Pacifique, qui ne
       relèvent d'aucun département : les 129 communes des DOM tombaient donc hors des
       agrégats département et région (1,33 M d'inscrits, cinq régions entières absentes
       de ces quatre scrutins), et leur série s'ouvrait en 2017 faute de rejoindre le
       code INSEE des scrutins suivants.

    Aucune des deux réécritures n'entre en collision avec un code déjà présent : les
    fichiers concernés n'utilisent QUE la forme héritée pour l'outre-mer, et les codes
    dérivés recouvrent exactement les communes du COG (32 en Guadeloupe, 34 en
    Martinique, 22 en Guyane, 24 à La Réunion, 17 à Mayotte)."""
    s = s.astype(str)
    outremer = s.str.fullmatch(r"9[78]\d{4}")
    s = s.where(~outremer, s.str[:3] + s.str[4:])
    lettre = s.str.fullmatch(r"Z[ABCDM]\d{3}")
    return s.where(~lettre, s.str[:2].map(DOM_LETTRE_DEP) + s.str[3:])


def construire_crosswalk_plm(
    dossier_clean: Path, geo_dir: Path
) -> tuple[dict[str, str], frozenset[str]]:
    """Crosswalk {code_bv continu → code_bv local} pour Paris/Lyon/Marseille, ARRONDISSEMENT
    par arrondissement — et la liste de ceux qui sont recodés.

    Depuis 2024 le ministère numérote les bureaux de façon continue à l'intérieur d'un
    secteur (à Paris, les arr. 1-4 fusionnés en « Paris Centre » : arr. 2 commence à 11,
    arr. 3 à 21, arr. 4 à 36) au lieu de repartir de 01 à chaque arrondissement. Les
    contours et les scrutins ≤ 2022 utilisent la numérotation locale — vérifié fichier par
    fichier : le 4e arrondissement porte 0401…0414 dans les dix-huit scrutins de 2012 à
    2022, et 0436…0449 dans les six de 2024 à 2026. Sans remappage, les bureaux 2024+
    tombent sur des codes orphelins (« none » sur la carte).

    L'appariement passe par la MÊME règle que les communes renumérotées (`_reapparier`),
    à la maille de l'ARRONDISSEMENT : la renumérotation est propre à un arrondissement, et
    recoder la commune détacherait les 903 bureaux de Paris pour les quinze du 4e.

    La première version alignait par rang « uniquement là où les effectifs coïncident »,
    entendu comme un nombre de codes ÉGAL des deux côtés. C'était trop raide, et le prix
    en était payé au centre de Paris : le 4e arrondissement compte 14 contours
    (0401…0414) et QUINZE codes en 2024 — les quatorze habituels plus un 0499 de
    1 183 inscrit·es propre aux européennes (ni les législatives 2024 ni les municipales
    2026 ne le portent). 15 ≠ 14, donc abstention totale : les 14 bureaux du 4e,
    19 062 inscrit·es en plein Paris, restaient sans contour dans TOUS les scrutins
    récents, alors que leur alignement par rang est confirmé par les inscrits à 1,5 %
    d'écart médian — celui que le journal ci-dessous imprime. L'alignement ordonné place
    les quatorze et laisse 0499 de côté ; faute de contour à lui attribuer, celui-ci en
    est privé comme n'importe quelle création.

    Les arrondissements où la numérotation n'a PAS changé ne sont pas touchés : leurs codes
    coïncident, la couverture y est haute (Marseille 9e : 46 codes identiques sur 50, soit
    92 % de l'électorat) et les quatre codes restants sont de vraies créations, sans
    contour à retrouver. C'est le premier test de `_reapparier` qui les écarte."""
    locaux: dict[tuple[str, str], set[str]] = collections.defaultdict(set)
    for dep in ("75", "69", "13"):
        f = geo_dir / f"{dep}.geojson"
        if not f.exists():
            continue
        for code in gpd.read_file(f, ignore_geometry=True)["bureau"].astype(str):
            com, _, suf = code.partition("_")
            if com in PLM_COMMUNES and suf.isdigit():
                locaux[(com, suf[:2])].add(code)
    if not locaux:
        return {}, frozenset()
    insc_new = _inscrits_par_bureau(dossier_clean, CROSSWALK_REF_NOUVEAU)
    insc_old = _inscrits_reference_ancienne(dossier_clean)
    if not insc_new or not insc_old:
        print("  ⚠ crosswalk PLM : scrutins de référence absents — ignoré")
        return {}, frozenset()
    # Les deux premiers chiffres du numéro de bureau portent l'arrondissement des DEUX
    # côtés (0436 est un bureau du 4e), c'est ce qui rend le groupe lisible sans référentiel
    # extérieur. Le suffixe non numérique, lui, n'appartient à aucun arrondissement.
    nouveaux_par_arr: dict[tuple[str, str], list[str]] = collections.defaultdict(list)
    for code in insc_new:
        com, _, suf = code.partition("_")
        if com in PLM_COMMUNES and suf.isdigit():
            nouveaux_par_arr[(com, suf[:2])].append(code)

    crosswalk: dict[str, str] = {}
    recodes: set[str] = set()
    for cle in sorted(nouveaux_par_arr):
        com, arr = cle
        anciens = sorted(c for c in locaux.get(cle, ()) if c in insc_old)
        v = _reapparier(
            anciens,
            sorted(nouveaux_par_arr[cle]),
            locaux.get(cle, set()),
            insc_old,
            insc_new,
        )
        if not v.recode:
            continue
        crosswalk.update({b: a for a, b, _ in v.couples})
        recodes.add(f"{com}_{arr}")
        print(
            f"  ↻ {com} arr. {arr} : numérotation continue depuis 2024 — "
            f"{len(v.couples)} bureaux réappariés, électorat localisé {v.avant:.0%} → "
            f"{v.apres:.0%} (écart d'inscrits médian {v.ecart:.1%})"
        )
    return crosswalk, frozenset(recodes)


# ---------------------------------------------------------------------------------
# Renumérotation communale des bureaux de vote
# ---------------------------------------------------------------------------------
# Les contours data.gouv sont figés sur le REU du 1er juin 2022 : le fichier « latest »
# porte encore la numérotation de 2022. Une commune qui a renuméroté ses bureaux depuis
# (Bordeaux : 1101 → 1001, 1201 → 1021, 1301 → 1041…) voit donc ses scrutins 2024+ tomber
# sur des codes orphelins. Deux dégâts, dont le second est le pire :
#   1. les bureaux renumérotés disparaissent de la carte BV pour ces scrutins ;
#   2. les rares codes qui coïncident PAR ACCIDENT (18 sur 153 à Bordeaux) rattachent les
#      voix de 2024 au contour d'un AUTRE bureau — un chiffre faux, pas un chiffre absent.
# Et comme l'estimation par quartier exige que 90 % de l'électorat communal soit porté par
# des bureaux localisés (prep_iris_bv.ELEC_MIN), la commune entière sort de la carte IRIS :
# à Bordeaux, 12 % d'électorat localisable, donc 88 quartiers sans une seule valeur.
#
# On reconstruit l'appariement par ALIGNEMENT ORDONNÉ (Needleman-Wunsch) : la
# renumérotation préserve l'ordre des bureaux et se contente d'en intercaler de nouveaux,
# ce que l'alignement modélise exactement (les créations depuis 2022 restent non
# appariées). Le coût d'un couple est l'écart relatif d'INSCRITS entre le scrutin de
# référence ancien et le nouveau : deux fichiers indépendants des codes, donc un vrai
# témoin. À Bordeaux, l'alignement retrouve les 148 contours avec 2,1 % d'écart médian,
# là où l'appariement par code identique en affiche 63 à 74 % — la mesure même de sa
# fausseté.
#
# Quatre garde-fous, sur le principe « sinon on s'abstient ». Les trois premiers jugent
# l'alignement EN BLOC, le quatrième ses couples un par un (cf. `_reapparier`, qui porte la
# règle et la partage avec le crosswalk PLM) :
#   - on n'intervient QUE sur les communes dont l'appariement par code est douteux (couverture
#     sous le seuil d'estimation, ou identité démentie par les inscrits) : ailleurs, un
#     alignement même bon dégraderait un rattachement déjà juste ;
#   - l'écart médian d'inscrits doit rester sous CROSSWALK_ECART_MAX, calibré sur les communes
#     SAINES (appariement complet par code) : leur écart médian vaut 1,9 % en médiane et 6,0 %
#     au 95e centile — au-delà, l'alignement n'est plus aussi cohérent qu'un vrai appariement ;
#   - le rattachement doit progresser franchement, sinon on garde l'existant — 77296 le montre :
#     son alignement ne place que 89 % de l'électorat, exactement ce que l'appariement par code
#     obtenait déjà, et le réapparier ne ferait que déplacer le risque ;
#   - un couple dont l'écart d'inscrits dépasse ECART_BUREAU_MAX n'est pas retenu, même dans un
#     alignement cohérent : le coût d'un trou (CROSSWALK_COUT_TROU) autorise la programmation
#     dynamique à préférer un couple à ~70 % d'écart plutôt que deux orphelins, et 80 couples
#     (68 688 inscrit·es, 31 % d'écart médian) passaient ainsi. Le prix en est connu : sur les
#     communes réalignées, la couverture publiée tombe de 90,9 % à 79,5 % et onze d'entre elles
#     repassent sous le seuil d'estimation par quartier. C'est le bon sens de ce seuil : mieux
#     vaut ne pas estimer un quartier que l'estimer sur un électorat localisé de nom.
# 26 communes passent l'alignement, 507 bureaux réappariés — dont Bordeaux, qui repasse de
# 12 % à 92 % d'électorat localisé.
#
# Les couples que l'alignement place SUR EUX-MÊMES (a == b) sont retenus comme les autres.
# Ils étaient exclus du crosswalk, et comme _remapper détache tout code non placé d'un groupe
# recodé, ils perdaient leur contour : 218 bureaux (198 787 inscrit·es) dans les communes
# réalignées, à 1 ou 2 % d'écart d'inscrits, que l'alignement ENDOSSE. Six communes en
# étaient réduites à 0 % d'électorat localisé quand leur log annonçait 100 % — le journal
# comptait les couples, la carte ne comptait que ceux qui changeaient de code.
#
# ---------------------------------------------------------------------------------
# Communes REDÉCOUPÉES : celles dont l'appariement n'est pas réparable
# ---------------------------------------------------------------------------------
# La renumérotation n'est qu'une des deux façons de perdre l'appariement. L'autre est le
# REDÉCOUPAGE : une commune refond ses bureaux sans changer d'espace de codes — Dammarie-
# les-Lys passe de 17 bureaux à 11, Montceau-les-Mines de 14 à 10, Sarreguemines de 18 à 14,
# Cogolin de 7 à 12 — à électorat quasi constant. TOUS ses codes se retrouvent alors des
# deux côtés de la jointure, et pas un seul ne désigne le même territoire : le bureau 0001
# de 2024 couvre ce qui était deux bureaux en 2022. La couverture est aveugle à ce cas (elle
# vaut 100 %), et l'alignement ordonné n'y peut rien — on ne fait pas correspondre 11
# bureaux fusionnés à 17 polygones.
#
# Seul l'écart d'INSCRITS sur les codes appariés le révèle, et il faut DEUX statistiques
# pour le lire, parce qu'elles ne voient pas la même chose.
#
# 1. L'ÉCART MÉDIAN dit « la plupart des appariements sont faux ». Sur les communes dont
#    l'ensemble des codes est identique d'un millésime à l'autre — donc présumées intactes —
#    il vaut 1,9 % en médiane, 5,7 % au 95e centile et 10,5 % au 99e. Au-delà de
#    ECART_IDENTITE_MAX, la commune a été refondue en bloc : +20 % de bureaux en médiane
#    pour +1,8 % d'électorat, le nombre de bureaux bouge quand le corps électoral ne bouge
#    pas. C'est la signature d'un redécoupage, pas d'une croissance.
#
# 2. La PART DES BUREAUX FRANCHEMENT FAUX dit « une partie des appariements est fausse », ce
#    que la médiane, justement robuste, cache. Bordeaux le montre : ses 18 codes coïncidant
#    par accident ont des écarts de 0 %, 2 %, 3 %, 3 %, 5 %, 5 %, 5 %, 8 %, 11 %, 14 %, puis
#    18 %, 24 %, 27 %, 29 %, 30 %, 51 %, 97 %, 108 % — médiane 12,4 %, sous le seuil, alors
#    que huit bureaux sur dix-huit sont grossièrement faux. La moitié basse n'est pas un
#    signe de justesse : ses bureaux faisant tous entre 600 et 1 400 inscrit·es, un
#    appariement au hasard tombe juste une fois sur deux. On regarde donc la PART des
#    bureaux dont l'écart dépasse ECART_BUREAU_MAX. Sur les communes intactes, elle vaut
#    0,0 % jusqu'au 90e centile et 0,9 % au 95e : au-delà de FRAC_FAUX_MAX, soit vingt-cinq
#    fois ce 95e centile, l'appariement ne tient plus.
#
# Ces deux témoins servent à DÉCLENCHER l'examen, pas à condamner : l'alignement ordonné est
# tenté d'abord, et beaucoup de ces communes sont réparables — Port-de-Bouc passe de 13 à 14
# bureaux, ses six premiers codes concordent et seuls les suivants décalent, ce qui est la
# signature d'un bureau intercalé, pas d'une refonte. Ce n'est que lorsque l'alignement
# échoue ET que l'identité est démentie qu'on prive les bureaux de contour : mieux vaut
# perdre le détail infra-communal de 2024 et 2026 que le porter faux. Les totaux communaux,
# départementaux et régionaux, eux, ne bougent pas d'un iota (ils ne passent pas par le
# bureau) — vérifié : les quatre tables d'agrégats sont identiques, ligne pour ligne, avant
# et après ce traitement, et les scrutins de 2012 à 2022 le sont aussi au bureau près.
#
# L'examen ne s'arrête pas aux grandes communes. Le seuil d'alignement (CROSSWALK_BV_MIN)
# écartait les 32 896 communes de moins de cinq bureaux — 20,6 M d'inscrit·es — de tout
# examen, alors que l'alignement seul demande de la structure : le REDÉCOUPAGE d'une commune
# de quatre bureaux se lit aussi bien que celui d'une commune de quarante, sur la médiane des
# écarts d'inscrits (cf. CODES_MEDIANE_MIN). 147 communes s'y ajoutent, 400 codes appariés,
# 345 000 inscrit·es dont la carte des bureaux cesse d'être fausse : 38479 fusionne quatre
# bureaux en deux (256 % d'écart médian), 71118 six en quatre (65 %).
ECART_IDENTITE_MAX = 0.15  # 99e centile de l'écart médian des communes intactes
ECART_BUREAU_MAX = 0.20  # au-delà, l'écart d'UN bureau n'est plus une dérive de listes
FRAC_FAUX_MAX = 0.25  # 25 × le 95e centile de cette part sur les communes intactes
CODES_APPARIES_MIN = 5  # sous 5 codes, la PART de bureaux faux n'est pas fiable
# La MÉDIANE des écarts, elle, se lit dès DEUX codes appariés — et c'est ce qui permet
# d'examiner les communes de moins de cinq bureaux, que le seuil d'alignement
# (CROSSWALK_BV_MIN) laissait entièrement hors examen : 32 896 communes, 20,6 M
# d'inscrit·es, 40 311 bureaux. Le seuil y est calibré comme pour les grandes, sur la
# population présumée INTACTE — jeu de codes identique d'un millésime à l'autre ET nombre
# de bureaux inchangé, 4 471 communes : écart médian de 2,0 % en médiane, 5,8 % au
# 95e centile, 12,9 % au 99e. ECART_IDENTITE_MAX (15 %) est donc là aussi au-delà du
# 99e centile des saines. Il signale 147 communes (400 codes appariés, 345 000 inscrit·es)
# qui portent exactement la signature du redécoupage : +33 % de bureaux en médiane pour
# +1,7 % d'électorat, quand les intactes gagnent 1,2 % d'électorat à nombre de bureaux
# constant. Coût : 0,8 % de faux positifs sur la population intacte, soit le standard déjà
# retenu pour les grandes communes.
# La PART de bureaux faux ne suit PAS cette extension : à deux ou quatre codes appariés,
# un seul bureau en fait 25 à 50 %, et FRAC_FAUX_MAX condamnerait 1 % des communes
# intactes sur un unique écart. Les deux témoins n'ont pas la même portée, ils ne
# descendent donc pas à la même taille.
#
# Le seuil n'a pas à dépendre de la taille du groupe, et la mesure le dit : le 99e centile
# de l'écart médian des communes intactes est PLAT — 12,6 % à deux codes appariés, 14,2 % à
# trois, 12,2 % à quatre, 9,0 % à cinq, 11,0 % au-delà de huit — et la part qui dépasse 15 %
# y reste entre 0,66 % et 1,10 %. Un seuil unique porte donc partout le même risque.
#
# La médiane s'arrête en revanche à DEUX codes, et c'est mesuré : à UN seul code apparié
# (28 278 communes, dont 28 168 présumées intactes), un seuil à 15 % signalerait
# 274 communes pour environ 194 faux positifs attendus — moins d'une commune juste sur
# trois. Un bureau qui grandit et une commune redécoupée y sont indiscernables.
#
# Le seuil est un PLANCHER, pas une porte réservée aux petites communes : `_reapparier` le
# lit pour TOUT groupe, là où l'ancienne règle exigeait cinq codes appariés avant de rien
# conclure. Reste à dire de quel côté cela mord, et la mesure le dit :
#   - là où l'alignement demeure possible — min(anciens, nouveaux) ≥ CROSSWALK_BV_MIN, les
#     seuls groupes RÉAPPARIABLES —, aucun ne porte aujourd'hui entre deux et quatre codes
#     appariés, et chaque arrondissement de PLM en a zéro, ou bien dix et plus : de ce
#     côté-là l'extension est DORMANTE ;
#   - elle mord là où elle est faite pour mordre, sur les groupes que l'ancienne règle
#     écartait de tout examen. 39 d'entre eux comptent cinq bureaux ou plus en 2024 pour
#     deux à quatre codes appariés seulement (01262 : 4 → 5 bureaux, 4 appariés). Ceux-là
#     sont désormais JUGÉS sur la médiane sans pouvoir être réappariés — la dissymétrie
#     voulue, réapparier AFFIRME quand détacher ne fait que RETIRER.
# La médiane vaut ce qu'elle vaut au nombre de codes qu'on lui donne, quelle que soit la
# taille du groupe qui les porte : le 99e centile plat, ci-dessus, est l'argument.
CODES_MEDIANE_MIN = 2
CROSSWALK_REF_ANCIEN = ("2022-legislatives-1", "2022-presidentielle-1")
CROSSWALK_REF_NOUVEAU = "2024-europeenne"
# Année à partir de laquelle les fichiers portent la NOUVELLE numérotation. Le crosswalk
# ne doit surtout pas toucher aux scrutins antérieurs : à Bordeaux, ses clés (1101, 1201…)
# sont des codes 2022 parfaitement valides, qu'il renverrait sur le contour d'un voisin.
CROSSWALK_ANNEE_MIN = 2024
# Miroir de prep_iris_bv.ELEC_MIN : le seuil sous lequel une commune n'est plus estimée.
CROSSWALK_ELEC_MIN = 0.90
CROSSWALK_ECART_MAX = 0.06  # 95e centile de l'écart d'inscrits des communes saines
CROSSWALK_GAIN_MIN = 0.01
CROSSWALK_BV_MIN = 5  # sous 5 bureaux, l'alignement n'a plus de structure à exploiter
# Coût d'un bureau laissé non apparié. Au-dessus de l'écart d'inscrits typique d'un vrai
# couple (~2 %) et bien en-dessous de celui de deux bureaux distincts, il fait préférer
# l'appariement quand les effectifs concordent et la création quand ils ne concordent pas.
CROSSWALK_COUT_TROU = 0.35


def _inscrits_par_bureau(dossier_clean: Path, cle: str) -> dict[str, int]:
    """{code_bv → inscrits} d'un scrutin de référence, codes bâtis comme dans
    `_bureau_depuis_df` (canonisation comprise) pour être comparables aux contours."""
    src = dossier_clean / f"{cle}-bureau_de_vote.parquet"
    if not src.exists():
        return {}
    df = pd.read_parquet(src, columns=["code_commune", "bureau_de_vote", "inscrits"])
    codes = (
        _canon_commune(df["code_commune"])
        + "_"
        + _canon_suffix(df["bureau_de_vote"].astype(str))
    )
    return df.assign(code=codes).groupby("code")["inscrits"].max().to_dict()


def _inscrits_reference_ancienne(dossier_clean: Path) -> dict[str, int]:
    """{code_bv → inscrits} du millésime des CONTOURS, lu sur les scrutins de 2022.

    Deux scrutins, le second bouchant les trous du premier (un bureau créé entre les
    législatives et la présidentielle n'est pas dans les deux fichiers)."""
    insc: dict[str, int] = {}
    for cle in CROSSWALK_REF_ANCIEN:
        for code, v in _inscrits_par_bureau(dossier_clean, cle).items():
            insc.setdefault(code, v)
    return insc


def _aligner_bureaux(
    anciens: list[str],
    nouveaux: list[str],
    insc_ancien: dict[str, int],
    insc_nouveau: dict[str, int],
) -> list[tuple[str, str, float]]:
    """Alignement ordonné {ancien, nouveau, écart} entre deux listes de codes triées.

    Programmation dynamique classique : à chaque pas, on apparie les deux têtes de liste
    ou on en saute une (bureau supprimé d'un côté, créé de l'autre). Ce sont les seules
    opérations que la renumérotation produit — elle ne réordonne pas.

    Les inscrits des deux côtés viennent de DEUX tables distinctes, et non d'une table
    fusionnée : un code renuméroté désigne un bureau à l'ancienne date et un AUTRE à la
    nouvelle. Fusionner les deux revenait à lire l'effectif de 2024 des deux côtés du
    couple pour les 18 faux amis de Bordeaux — l'écart tombait à 0 % et le garde-fou
    validait sa propre erreur."""
    n, m = len(anciens), len(nouveaux)
    trou = CROSSWALK_COUT_TROU
    cout = [
        [
            abs(insc_ancien[a] - insc_nouveau[b]) / max(insc_ancien[a], 1)
            for b in nouveaux
        ]
        for a in anciens
    ]
    d = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        d[i][0] = i * trou
    for j in range(1, m + 1):
        d[0][j] = j * trou
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            d[i][j] = min(
                d[i - 1][j - 1] + cout[i - 1][j - 1],
                d[i - 1][j] + trou,
                d[i][j - 1] + trou,
            )
    i, j, couples = n, m, []
    while i > 0 and j > 0:
        if d[i][j] == d[i - 1][j - 1] + cout[i - 1][j - 1]:
            couples.append((anciens[i - 1], nouveaux[j - 1], cout[i - 1][j - 1]))
            i, j = i - 1, j - 1
        elif d[i][j] == d[i - 1][j] + trou:
            i -= 1
        else:
            j -= 1
    return couples[::-1]


def _mediane(xs: list[float]) -> float:
    ys = sorted(xs)
    k = len(ys)
    return (
        1.0 if not k else (ys[k // 2] if k % 2 else (ys[k // 2 - 1] + ys[k // 2]) / 2)
    )


class _Verdict(NamedTuple):
    """Ce que vaut l'appariement par code sur un groupe de bureaux, et ce qu'on en fait."""

    couples: tuple[tuple[str, str, float], ...]  # (code contour, code récent, écart)
    recode: bool  # un code n'y désigne plus le contour qu'il nomme
    avant: float  # part de l'électorat récent portée par un contour, par code identique
    apres: float  # … après réappariement
    ecart: float  # écart d'inscrits médian de l'alignement
    ecart_identite: float | None  # … de l'appariement par code identique
    frac_faux: float | None  # part de ses codes dont l'écart dépasse ECART_BUREAU_MAX
    ecartes: int  # couples rejetés un par un, écart d'inscrits trop grand


_RIEN = _Verdict((), False, 1.0, 1.0, 0.0, None, None, 0)


def _reapparier(
    anciens: list[str],
    nouveaux: list[str],
    avec_contour: set[str],
    insc_old: dict[str, int],
    insc_new: dict[str, int],
) -> _Verdict:
    """Verdict sur UN groupe de bureaux : une commune, ou un arrondissement de PLM.

    Une seule règle pour les deux façons de perdre l'appariement (renumérotation
    communale, numérotation continue de PLM) et pour les deux issues (réapparier,
    détacher). Trois questions, dans cet ordre :

    1. **L'appariement par code est-il douteux ?** Il l'est si la couverture tombe sous le
       seuil d'estimation (CROSSWALK_ELEC_MIN) ou si les INSCRITS démentent l'identité des
       codes qui coïncident. Sinon on ne touche à rien : un alignement même bon dégraderait
       un rattachement déjà juste.
    2. **L'alignement ordonné tient-il ?** Il est jugé EN BLOC, sur son écart d'inscrits
       médian, comme un tout cohérent ou non (CROSSWALK_ECART_MAX).
    3. **Chaque couple est-il soutenu par les inscrits ?** L'alignement accepté, ses
       couples le sont un par un : le coût d'un trou (CROSSWALK_COUT_TROU, 0,35) autorise
       la programmation dynamique à retenir un couple jusqu'à ~70 % d'écart quand
       l'alternative est de laisser deux bureaux orphelins. 80 couples (68 688 inscrit·es,
       31 % d'écart médian) passent ainsi au-delà de ECART_BUREAU_MAX — le seuil où, du dire
       lui-même, l'écart d'un bureau n'est plus une dérive de listes. Ils sont écartés :
       dans un groupe recodé, un code non placé perd son contour, ce qui est le verdict
       juste pour un couple que le témoin dément.

    Le groupe est RECODÉ dès que l'un des deux verdicts tombe — réapparié (les codes non
    placés perdent leur contour) ou détaché (aucun ne le garde). Les couples retenus le
    sont TOUS, l'identité comprise (`a == b`) : ces couples-là étaient exclus du crosswalk,
    et `_remapper` détachait donc 218 bureaux (198 787 inscrit·es) dans les 30 communes que
    l'ancienne règle réalignait — des bureaux que l'alignement ENDOSSE, à 1 ou 2 % d'écart. Un
    couple identique et un couple décalé sont la même affirmation ; les traiter
    différemment n'avait aucune raison."""
    total = sum(insc_new[c] for c in nouveaux)
    if not anciens or not nouveaux or total <= 0:
        return _RIEN
    avant = sum(insc_new[c] for c in nouveaux if c in avec_contour) / total
    # Ce que VAUT l'appariement par code, mesuré sur les codes qu'il apparie : l'écart
    # d'inscrits entre les deux millésimes. C'est le seul témoin du redécoupage, que la
    # couverture ne voit pas (elle reste à 100 % quand l'espace de codes ne change pas).
    identiques = [c for c in nouveaux if c in avec_contour and c in insc_old]
    ecarts = [abs(insc_old[c] - insc_new[c]) / max(insc_old[c], 1) for c in identiques]
    ecart_identite = _mediane(ecarts) if len(ecarts) >= CODES_MEDIANE_MIN else None
    frac_faux = (
        sum(e > ECART_BUREAU_MAX for e in ecarts) / len(ecarts)
        if len(ecarts) >= CODES_APPARIES_MIN
        else None
    )
    identite_fausse = ecart_identite is not None and (
        ecart_identite > ECART_IDENTITE_MAX
        or (frac_faux is not None and frac_faux > FRAC_FAUX_MAX)
    )
    vu = _RIEN._replace(
        avant=avant, apres=avant, ecart_identite=ecart_identite, frac_faux=frac_faux
    )
    if avant >= CROSSWALK_ELEC_MIN and not identite_fausse:
        return vu  # appariement crédible : ne pas déranger un rattachement qui tient
    # L'alignement demande de la STRUCTURE (un ordre à exploiter) ; le détachement, non.
    # Sous CROSSWALK_BV_MIN bureaux, on ne réapparie donc pas — mais on continue de juger.
    # Les deux décisions n'ont pas la même charge de preuve : réapparier AFFIRME une
    # correspondance (qu'un alignement sur trois bureaux peut trouver par chance, leurs
    # effectifs tenant dans une bande étroite), détacher ne fait que RETIRER une
    # affirmation que le témoin dément.
    assez_de_structure = min(len(anciens), len(nouveaux)) >= CROSSWALK_BV_MIN
    couples = (
        _aligner_bureaux(anciens, nouveaux, insc_old, insc_new)
        if assez_de_structure
        else []
    )
    ecart = _mediane([e for _, _, e in couples]) if couples else 1.0
    if couples and ecart <= CROSSWALK_ECART_MAX:
        retenus = tuple(c for c in couples if c[2] <= ECART_BUREAU_MAX)
        apres = sum(insc_new[b] for _, b, _ in retenus) / total
        # Le gain de RATTACHEMENT ne peut pas être exigé quand l'identité est fausse : une
        # commune dont tous les codes coïncident est déjà à 100 % de couverture, et aucun
        # alignement, même parfait, ne la fera progresser. C'est le cas de Port-de-Bouc,
        # dont les six premiers codes concordent et dont seuls les suivants décalent d'un
        # cran. Là, la preuve n'est pas la couverture gagnée mais la COHÉRENCE de
        # l'alignement, et elle vaut mieux qu'un détachement qui jetterait un appariement
        # juste.
        if apres >= avant + CROSSWALK_GAIN_MIN or identite_fausse:
            return _Verdict(
                retenus,
                True,
                avant,
                apres,
                ecart,
                ecart_identite,
                frac_faux,
                len(couples) - len(retenus),
            )
    if identite_fausse:
        # Rien à réapparier, et l'identité est démentie : mieux vaut aucun contour qu'un
        # contour faux. Sans entrée de crosswalk, _remapper les détache tous.
        return vu._replace(recode=True, apres=0.0, ecart=ecart)
    return vu._replace(ecart=ecart)


def construire_crosswalk_renumerotation(
    dossier_clean: Path, geo_dir: Path
) -> tuple[dict[str, str], frozenset[str]]:
    """Crosswalk {code_bv 2024+ → code_bv des contours} pour les communes ayant
    renuméroté leurs bureaux depuis le REU de 2022 (cf. le commentaire ci-dessus).

    Renvoie aussi l'ensemble des communes RECODÉES — celles où un code de bureau ne
    désigne plus le contour qu'il nomme. Deux cas y tombent, traités par la même règle
    dans `_remapper` : la commune renumérotée, dont l'alignement place la plupart des
    bureaux (les autres perdent leur contour), et la commune redécoupée, où rien n'est
    plaçable et où tous le perdent.

    Le verdict est rendu commune par commune par `_reapparier`, la même fonction que pour
    les arrondissements de PLM : l'alignement n'est retenu que là où il est NÉCESSAIRE
    (appariement douteux), COHÉRENT (écart d'inscrits sous le seuil des communes saines) et
    UTILE (le rattachement progresse), et ses couples que là où les inscrits les soutiennent
    un par un. Là où il échoue mais où l'identité est démentie par les inscrits, la commune
    est recodée sans crosswalk : ses bureaux récents n'ont plus de contour.

    Paris, Lyon et Marseille sont écartés d'ici : leur renumérotation est propre à un
    ARRONDISSEMENT, maille à laquelle `construire_crosswalk_plm` la traite. Prise à la
    commune, elle détacherait les 903 bureaux de Paris pour les quinze du 4e."""
    contours: dict[str, set[str]] = collections.defaultdict(set)
    for f in sorted(geo_dir.glob("*.geojson")):
        for code in gpd.read_file(f, ignore_geometry=True)["bureau"].astype(str):
            com, _, _ = code.partition("_")
            if com and com not in PLM_COMMUNES:
                # 59 codes portent DEUX features (bureau au contour éclaté) : sans
                # dédoublonnage, l'alignement apparie deux bureaux 2024 au même polygone.
                contours[com].add(code)
    if not contours:
        return {}, frozenset()
    insc_new = _inscrits_par_bureau(dossier_clean, CROSSWALK_REF_NOUVEAU)
    insc_old = _inscrits_reference_ancienne(dossier_clean)
    if not insc_new or not insc_old:
        print("  ⚠ crosswalk renumérotation : scrutins de référence absents — ignoré")
        return {}, frozenset()

    nouveaux_par_com: dict[str, list[str]] = collections.defaultdict(list)
    for code in insc_new:
        nouveaux_par_com[code.partition("_")[0]].append(code)

    crosswalk: dict[str, str] = {}
    realignees: list[tuple[str, _Verdict]] = []
    detachees: list[tuple[str, int, int, _Verdict]] = []
    for com, nouveaux in nouveaux_par_com.items():
        anciens = sorted(c for c in contours.get(com, ()) if c in insc_old)
        nouveaux = sorted(nouveaux)
        v = _reapparier(
            anciens, nouveaux, set(contours.get(com, ())), insc_old, insc_new
        )
        if not v.recode:
            continue
        if v.couples:
            crosswalk.update({b: a for a, b, _ in v.couples})
            realignees.append((com, v))
        else:
            detachees.append((com, len(anciens), len(nouveaux), v))
    for com, v in sorted(realignees, key=lambda r: r[1].avant):
        ecartes = f", {v.ecartes} couple(s) écarté(s)" if v.ecartes else ""
        print(
            f"  ↻ {com} : bureaux renumérotés depuis 2022 — électorat localisé "
            f"{v.avant:.0%} → {v.apres:.0%} (écart d'inscrits médian {v.ecart:.1%}"
            f"{ecartes})"
        )
    if realignees:
        print(f"  ↻ crosswalk renumérotation : {len(crosswalk)} bureaux réappariés")
    if detachees:
        pires = sorted(detachees, key=lambda r: -r[3].ecart_identite)[:5]
        detail = ", ".join(
            f"{c} ({a}→{n} BV, écart d'inscrits médian {v.ecart_identite:.0%})"
            for c, a, n, v in pires
        )
        print(
            f"  ✂ {len(detachees)} commune(s) dont l'appariement par code est démenti par "
            f"les inscrits et que l'alignement ne répare pas "
            f"({sum(n for _, _, n, _ in detachees)} bureaux) : leurs bureaux récents "
            f"sont privés de contour. Pires cas : {detail}"
        )
    recodees = [com for com, _ in realignees] + [com for com, *_ in detachees]
    return crosswalk, frozenset(recodees)


@dataclass(frozen=True)
class Scrutin:
    cle: str  # ex: "2022-presidentielle-1"
    annee: int
    type: str  # presidentielle / legislatives / europeenne / municipales / ...
    tour: int | None
    fichier: Path

    @property
    def libelle(self) -> str:
        noms = {
            "presidentielle": "Présidentielle",
            "legislatives": "Législatives",
            "europeenne": "Européennes",
            "municipales": "Municipales",
            "departementales": "Départementales",
            "regionales": "Régionales",
            "referendum": "Référendum",
            "conseils-PLM": "Conseils de secteur (PLM)",
        }
        base = f"{noms.get(self.type, self.type.title())} {self.annee}"
        tours = {1: "1er tour", 2: "2e tour"}
        return (
            f"{base} ({tours.get(self.tour, f'tour {self.tour}')})"
            if self.tour
            else base
        )


def lister_scrutins(dossier_clean: Path) -> list[Scrutin]:
    scrutins: list[Scrutin] = []
    for f in sorted(dossier_clean.glob("*-bureau_de_vote.parquet")):
        parts = f.stem.replace("-bureau_de_vote", "").split("-")
        annee = int(parts[0])
        # Le tour est le DERNIER segment s'il est numérique, et le type tout ce qui reste :
        # « 2026-conseils-PLM-1 » a un type en deux mots. En lisant `parts[1:3]` on prenait
        # « PLM » pour un tour, donc pas de tour du tout — et les deux tours des conseils
        # PLM se retrouvaient avec le même libellé dans le sélecteur de scrutins.
        tour = int(parts[-1]) if len(parts) > 2 and parts[-1].isdigit() else None
        type_ = "-".join(parts[1:-1] if tour is not None else parts[1:])
        cle = "-".join(parts)
        scrutins.append(Scrutin(cle, annee, type_, tour, f))
    return scrutins


# Les fichiers du ministère repris par hexagonal stockent parfois les voix sur un entier
# 16 bits signé : au-delà de 32 767 le compte « déborde » et ressort négatif.
DEBORDEMENT_INT16 = 65536


def _reparer_voix_negatives(
    df: pd.DataFrame, scrutin: Scrutin
) -> tuple[pd.DataFrame, pd.Series]:
    """Répare les comptes de voix négatifs, impossibles par construction.

    Le fichier de la présidentielle 2012 en contient un : au 2e tour, le bureau
    ZZ006_0001 (Français·es de l'étranger, 107 077 inscrits) donne **−32 541** voix à
    Sarkozy — 32 995 tronqué sur 16 bits. Non corrigé, ce compte se SOUSTRAYAIT du bloc
    LR-DVD national et laissait 65 536 suffrages hors de tout bloc : c'était le seul
    scrutin dont la barre ne bouclait pas (99,87 % au lieu de 100 %).

    On ne rétablit le compte que s'il redonne EXACTEMENT les exprimés publiés du bureau
    (ici 32 995 + 19 978 = 52 973 ✓). Sinon on ne devine pas : les voix du bureau sont
    déclarées non ventilables, et sa commune bascule en « non mesuré ».

    Renvoie le tableau corrigé et le masque des lignes à ne pas ventiler."""
    negatif = df["voix"] < 0
    if not negatif.any():
        return df, negatif
    df = df.copy()
    df["voix"] = df["voix"] + negatif * DEBORDEMENT_INT16
    touche = df["code_bv"].isin(df.loc[negatif, "code_bv"])
    somme = df.groupby("code_bv")["voix"].transform("sum")
    exprimes = df.groupby("code_bv")["exprimes"].transform("max")
    echec = touche & (somme != exprimes)
    retablis = df.loc[touche & ~echec, "code_bv"].nunique()
    print(
        f"  ⚠ {scrutin.cle}: {int(negatif.sum())} compte(s) de voix négatif(s) "
        f"(débordement 16 bits) — {retablis} bureau(x) rétabli(s), "
        f"{df.loc[echec, 'code_bv'].nunique()} laissé(s) non ventilé(s)"
    )
    return df, echec


def est_recode(code_bv: str, recodes: frozenset[str] | set[str]) -> bool:
    """Le bureau `code_bv` appartient-il à un groupe RECODÉ (cf. FICHIER_RECODEES) ?

    Un groupe est une COMMUNE (« 77152 ») ou, à Paris/Lyon/Marseille, un ARRONDISSEMENT
    (« 75056_04 ») : la numérotation continue de 2024 y est propre à un arrondissement, et
    recoder la commune détacherait les 903 bureaux de Paris pour les quinze du 4e. Les deux
    formes cohabitent dans la même liste et se lisent par la même fonction — celle
    qu'applique aussi prep_mobilisation à la texture d'elections_predictions, qui porte le
    même faux appariement un cran plus haut."""
    com, _, suf = code_bv.partition("_")
    return com in recodes or f"{com}_{suf[:2]}" in recodes


def _remapper(
    codes: pd.Series, crosswalk: dict[str, str], recodees: frozenset[str]
) -> pd.Series:
    """Applique le crosswalk, puis prive de contour tout bureau qu'il ne place pas dans un
    groupe RECODÉ.

    Une seule règle couvre les trois façons de perdre l'appariement. Dans une commune
    RENUMÉROTÉE — et dans un arrondissement de PLM renuméroté en continu —, l'alignement
    place la plupart des bureaux ; ceux qui restent sont des créations postérieures aux
    contours, et le code qu'elles portent peut être celui d'un ANCIEN bureau. Dans une
    commune REDÉCOUPÉE, il n'y a rien à placer : aucun code ne désigne plus le même
    territoire, et tous sont détachés. Dans les deux cas, laisser le code tel quel
    dessinerait les voix sur le polygone d'un voisin.

    La règle vaut pour tous les scrutins récents, et pas seulement pour celui qui a servi
    de référence : les municipales 2026 ont créé à leur tour des bureaux que le crosswalk,
    bâti sur 2024, ne connaît pas.

    Le suffixe `+` ne fait que retirer le contour : prep_bake écarte de la carte tout code
    qui n'en a pas, et les voix continuent de compter dans les agrégats commune,
    département et région, qui ne passent pas par le bureau."""
    if not crosswalk and not recodees:
        return codes

    def un(c: str) -> str:
        vise = crosswalk.get(c)
        if vise is not None:
            return vise
        return f"{c}+" if est_recode(c, recodees) else c

    return codes.map(un)


def _bureau_depuis_df(
    df: pd.DataFrame,
    scrutin: Scrutin,
    crosswalk: dict[str, str],
    listes_lfi: set[tuple[str, int]],
    communes_recodees: frozenset[str] = frozenset(),
) -> pd.DataFrame:
    """Renvoie une ligne par bureau de vote, avec voix ventilées par famille."""
    df = df.copy()
    if "voix" not in df.columns:
        raise ValueError(f"{scrutin.cle}: colonnes manquantes {df.columns.tolist()}")
    if "code_commune" not in df.columns:
        if "code_secteur" not in df.columns:
            raise ValueError(f"{scrutin.cle}: ni code_commune ni code_secteur")
        # Paris/Lyon/Marseille : on rattache le secteur à sa commune principale.
        df["code_commune"] = df["code_secteur"].astype(str).str[:5]
    df["code_commune"] = _canon_commune(df["code_commune"])
    df["bureau_de_vote"] = df.get("bureau_de_vote", "")
    # Clé de CIRCONSCRIPTION du scrutin — le secteur là où il y en a un (conseils de
    # secteur PLM, métropole de Lyon) : elle n'identifie pas un bureau, elle sert à
    # retrouver une liste dans la table des listes LFI (cf. charger_listes_lfi).
    cle_circo = (
        df["code_secteur"].astype(str)
        if "code_secteur" in df.columns
        else df["code_commune"]
    )
    # Le code de bureau, lui, se bâtit TOUJOURS sur la commune. Bâti sur le secteur, il
    # devenait « 13055SR01_0101 » aux conseils de secteur de 2026 — un code que ni les
    # contours (« 13055_0101 ») ni le crosswalk PLM ne connaissent : les 1 714 bureaux de
    # ce scrutin, 2 281 337 inscrit·es, n'avaient AUCUN contour, donc ni frise de
    # recomposition au bureau ni estimation par quartier — sous la commune, ces deux tours
    # manquaient à la chronologie de toutes les fiches de Paris, Lyon et Marseille.
    # Le numéro de bureau porte déjà l'arrondissement : les 1 714 couples
    # (commune, bureau) du scrutin sont uniques — aucune collision entre les 34 secteurs —
    # et forment EXACTEMENT l'ensemble des bureaux de PLM aux municipales du même jour.
    df["code_bv"] = df["code_commune"] + "_" + _canon_suffix(df["bureau_de_vote"])
    df["code_bv"] = _remapper(df["code_bv"], crosswalk, communes_recodees)
    df, voix_perdues = _reparer_voix_negatives(df, scrutin)

    nuance = df["nuance"] if "nuance" in df.columns else pd.Series([None] * len(df))
    nom = df["nom"] if "nom" in df.columns else pd.Series([None] * len(df))
    # Le patronyme ne vaut nuance qu'à la présidentielle, où la table des candidat·es fait
    # foi ; ailleurs c'est un homonyme (cf. nuance_vers_famille).
    patronymes = scrutin.type == "presidentielle"
    df["famille"] = [nuance_vers_famille(n, m, patronymes) for n, m in zip(nuance, nom)]
    df.loc[voix_perdues, "famille"] = SANS_NUANCE
    # Européennes 2019 : le fichier ne porte ni nuance ni nom de candidat, seulement le
    # numéro de panneau et l'intitulé de la liste. Sans ce repli, tout le scrutin était
    # « non ventilé » (et, avant correction du mapping, entièrement versé dans « Autres »).
    if "numero_panneau" in df.columns:
        manque = df["famille"] == SANS_NUANCE
        if manque.any():
            depuis_liste = [
                famille_de_liste(scrutin.cle, p)
                for p in df.loc[manque, "numero_panneau"]
            ]
            df.loc[manque, "famille"] = [f or SANS_NUANCE for f in depuis_liste]
    if listes_lfi and scrutin.cle in SCRUTINS_LISTES_LFI and "numero_panneau" in df:
        est_lfi = [
            pd.notna(p) and (str(c), int(p)) in listes_lfi
            for c, p in zip(cle_circo, df["numero_panneau"])
        ]
        df.loc[est_lfi, "famille"] = "LFI"

    base_cols = ["code_bv", "code_commune", "bureau_de_vote"]
    base = df.groupby("code_bv", as_index=False)[
        ["inscrits", "votants", "exprimes"]
    ].max()
    meta = df.groupby("code_bv", as_index=False)[base_cols[1:]].first()
    base = base.merge(meta, on="code_bv")

    voix = df.pivot_table(
        index="code_bv", columns="famille", values="voix", aggfunc="sum", fill_value=0
    ).reset_index()
    out = base.merge(voix, on="code_bv", how="left")
    for fam in COLONNES_VOIX:
        if fam not in out.columns:
            out[fam] = 0
    return _neutraliser_non_ventile(out)


# Une commune dont les voix dépassent de 10 % ses exprimés vote au scrutin plurinominal.
# La séparation est franche — les communes de liste bouclent à 1,00 exactement, les
# communes à panachage sont entre 5 et 15 (autant que de sièges) : seules 8 communes sur
# 35 000 tombent entre les deux. Un seuil lâche (> exprimés tout court) ferait basculer
# des communes entières sur un bureau au dénombrement d'exprimés bancal — Tours en a un.
RATIO_PLURINOMINAL = 1.10
# Part des voix d'une commune sans aucune nuance publiée au-delà de laquelle la
# ventilation est déclarée inconnue. La distribution de cette part est franchement bimodale
# (le ministère publie les nuances de TOUTE la commune ou d'AUCUNE : aux municipales 2026,
# 31 554 communes sont à 100 % de voix SANS nuance et 3 282 à 0 % ; 3 communes seulement
# tombent entre les deux sur tout le corpus), le seuil est donc au milieu du vide.
SEUIL_SANS_NUANCE = 0.50


def _neutraliser_non_ventile(out: pd.DataFrame) -> pd.DataFrame:
    """Marque les communes où la ventilation par liste n'est pas mesurable.

    Deux régimes, un même verdict — la ventilation est INCONNUE (NaN), pas nulle :

    1. **Panachage** (municipales des communes de moins de 1 000 habitants). Le ministère
       y publie une ligne par CANDIDAT (un numéro de panneau chacun, pas de liste), et
       chaque électeur vote pour autant de noms qu'il y a de sièges — sommer ces voix par
       famille donnait 184 % des inscrits en 2014 et 135 % en 2020, contre 60 % et 43 %
       d'exprimés réels.
    2. **Nuance non publiée**. Le fichier des municipales 2026 ne gonfle plus les voix :
       le test de panachage ne se déclenchait donc plus, alors que la colonne `nuance` y
       est vide pour toutes les communes de moins de 1 000 habitants. Résultat, 24 816
       communes étaient servies avec « LFI 0 % · PS 0 % · RN 0 % » et 100 % du bloc
       « Autres » — des zéros affichés comme des mesures, là où 2020 disait « · ».

    `inscrits_nuances` (0 dans ces communes) sert de dénominateur aux blocs ; la
    participation et l'abstention, elles, restent mesurées et intactes. Les deux tests
    portent sur les totaux de la COMMUNE : la publication des nuances comme le panachage
    sont des régimes communaux, pas des accidents de bureau."""
    par_commune = (
        pd.DataFrame(
            {
                "code_commune": out["code_commune"],
                "voix": out[COLONNES_VOIX].sum(axis=1),
                "sans": out[SANS_NUANCE],
                "exp": out["exprimes"],
            }
        )
        .groupby("code_commune")[["voix", "sans", "exp"]]
        .sum()
    )
    panachage = par_commune["voix"] > RATIO_PLURINOMINAL * par_commune["exp"]
    sans_nuance = par_commune["sans"] > SEUIL_SANS_NUANCE * par_commune["voix"].clip(
        lower=1
    )
    inconnu = set(par_commune.index[panachage | sans_nuance])
    connu = ~out["code_commune"].isin(inconnu)
    out.loc[~connu, FAMILLES] = float("nan")
    out["inscrits_nuances"] = out["inscrits"].where(connu, 0)
    # Les EXPRIMÉS ventilables, à distinguer des inscrits ventilables : c'est le suffrage
    # qui manque à la barre de recomposition, pas le corps électoral. L'abstention de ces
    # communes est déjà comptée dans l'abstention générale.
    #
    # Ils se COMPTENT (somme des voix rangées dans une famille) au lieu de se déduire du
    # régime communal. Les deux tests ci-dessus sont communaux et binaires : une commune
    # est ventilée ou ne l'est pas. Poser alors `exprimes_nuances = exprimes` supposait
    # que toute voix d'une commune ventilée trouve sa famille — faux dès qu'une nuance
    # sort du mapping (`LNC` en Nouvelle-Calédonie, `LGJ` des gilets jaunes) ou qu'une
    # ligne n'en porte aucune : ces voix disparaissaient de la barre SANS entrer dans la
    # part non ventilée, qui restait à 0. La recomposition s'arrêtait à 62 % à La Foa et
    # dans 21 autres communes (54 bureaux, jusqu'à −40 points). Comptées, elles bouclent.
    ventiles = out[FAMILLES].sum(axis=1).where(connu, 0).clip(lower=0)
    # Le plafond doit être appliqué là où il EXISTE, et nulle part ailleurs.
    # `clip(upper=serie_nullable)` remplace en interne le NA par +inf, que pandas refuse
    # ensuite d'écrire dans un entier nullable : l'exception remontait jusqu'au garde-fou
    # « un scrutin atypique ne doit pas tout bloquer » de construire_resultats, et
    # 20 lignes sans exprimés à Montbéliard emportaient les 69 743 bureaux du
    # 1er tour des législatives 2024 — un des quatre scrutins servis par la carte,
    # disparu de la France entière à cause d'une commune.
    plafond = out["exprimes"]
    trop = plafond.notna() & (ventiles > plafond)
    out["exprimes_nuances"] = ventiles.where(~trop, plafond)
    return out


def _par_bureau(
    scrutin: Scrutin,
    crosswalk: dict[str, str],
    listes_lfi: set[tuple[str, int]],
    communes_recodees: frozenset[str] = frozenset(),
) -> list[tuple[Scrutin, pd.DataFrame]]:
    """Lit le fichier d'un scrutin et renvoie un (scrutin, table BV) par tour. Les fichiers
    legacy regroupant plusieurs tours (présidentielle 2012, municipales 2014) sont séparés
    en un scrutin par tour : sans cela, le pivot somme les voix des deux tours et double-compte."""
    df = pd.read_parquet(scrutin.fichier)
    if "numero_tour" in df.columns and df["numero_tour"].nunique(dropna=True) > 1:
        sorties = []
        for t, sub in df.groupby("numero_tour"):
            sc = replace(scrutin, cle=f"{scrutin.cle}-{int(t)}", tour=int(t))
            sorties.append(
                (
                    sc,
                    _bureau_depuis_df(
                        sub, sc, crosswalk, listes_lfi, communes_recodees
                    ),
                )
            )
        return sorties
    return [
        (
            scrutin,
            _bureau_depuis_df(df, scrutin, crosswalk, listes_lfi, communes_recodees),
        )
    ]


def _indicateurs(g: pd.DataFrame) -> dict:
    """Calcule les indicateurs d'un groupe (déjà agrégé en sommes).

    UN SEUL dénominateur, `inscrits`, pour tout ce qui est exprimé en pourcentage :
    participation, abstention, blocs, voix LFI/gauche. `inscrits_nuances` — les inscrits
    dont la ventilation par liste existe — ne sert qu'à décider si les blocs sont
    MESURÉS (cf. _neutraliser_non_ventile) : à 0, ils valent None et non zéro.

    Les blocs étaient auparavant rapportés à `inscrits_nuances`. À la commune les deux
    coïncident (une commune est ventilée ou ne l'est pas), mais dès qu'on agrège les
    deux populations divergent et les pourcentages cessaient d'être additionnables :
    la barre de recomposition des municipales 2026 totalisait 133 % en France. Le poids
    d'un bloc se lit désormais sur le corps électoral ENTIER, `non_ventile` portant les
    EXPRIMÉS que le ministère ne ventile pas (rapportés aux inscrits) — barre bouclée à
    100 % avec l'abstention et les blancs/nuls, échelles comparables entre elles, et pas
    de dénominateur qui change avec le territoire.

    Participation et abstention exigent en plus des comptages qui se tiennent
    (exprimés ≤ votants ≤ inscrits). Deux bureaux du fichier des municipales 2026 les
    contredisent — 212 votants pour 209 inscrits au Mesnil-sur-Bulles, 79 exprimés pour
    0 votant à Saint-Cyr-du-Gault : on préfère ne rien afficher à une participation de
    101 %."""
    inscrits = g["inscrits"]
    nuances_base = g["inscrits_nuances"]
    coherent = 0 <= g["exprimes"] <= g["votants"] <= inscrits
    res: dict = {
        "inscrits": int(inscrits),
        "votants": int(g["votants"]),
        "exprimes": int(g["exprimes"]),
        "inscrits_nuances": int(nuances_base),
        # publié pour que l'estimation par quartier puisse répartir et recaler la part
        # non ventilée comme n'importe quel comptage (cf. prep_iris_bv)
        "exprimes_nuances": int(g["exprimes_nuances"]),
        "participation": round(100 * g["votants"] / inscrits, 2)
        if inscrits and coherent
        else None,
        "abstention": round(100 * (1 - g["votants"] / inscrits), 2)
        if inscrits and coherent
        else None,
        "non_ventile": round(
            100 * (g["exprimes"] - g["exprimes_nuances"]) / inscrits, 2
        )
        if inscrits
        else None,
    }
    fam_voix = {fam: g.get(fam, 0) for fam in FAMILLES}
    mesure = bool(nuances_base) and bool(inscrits)
    for bloc in BLOC6_ORDRE:
        v = sum(fam_voix[f] for f in FAMILLES if FAMILLE_BLOC6.get(f) == bloc)
        res[f"b6_{bloc}"] = round(100 * v / inscrits, 2) if mesure else None
    for bloc in TRIPARTITION_ORDRE:
        v = sum(fam_voix[f] for f in FAMILLES if FAMILLE_TRIPARTITION.get(f) == bloc)
        res[f"tri_{bloc}"] = round(100 * v / inscrits, 2) if mesure else None
    lfi = sum(fam_voix[f] for f in FAMILLES_LFI)
    gauche = sum(fam_voix[f] for f in FAMILLES_GAUCHE)
    res["lfi_voix"] = int(lfi) if mesure else None
    res["gauche_voix"] = int(gauche) if mesure else None
    res["lfi_pct"] = round(100 * lfi / inscrits, 2) if mesure else None
    res["gauche_pct"] = round(100 * gauche / inscrits, 2) if mesure else None
    return res


def _agreger(
    bv: pd.DataFrame, cle_groupe: str, niveau: str, scrutin: Scrutin
) -> pd.DataFrame:
    cols_somme = [
        "inscrits",
        "votants",
        "exprimes",
        "inscrits_nuances",
        "exprimes_nuances",
        *FAMILLES,
    ]
    grp = bv.groupby(cle_groupe, as_index=False)[cols_somme].sum()
    lignes = [
        {
            "niveau": niveau,
            "code": row[cle_groupe],
            "scrutin": scrutin.cle,
            "scrutin_libelle": scrutin.libelle,
            "annee": scrutin.annee,
            "type": scrutin.type,
            "tour": scrutin.tour,
            **_indicateurs(row),
        }
        for _, row in grp.iterrows()
    ]
    return pd.DataFrame(lignes)


def _departement_du_code(code: str) -> str:
    """Le code INSEE d'une commune PORTE son département (2 caractères, 3 en outre-mer)."""
    code = str(code)
    return code[:3] if code.startswith("97") else code[:2]


def rattachement_communal(
    communes: pd.DataFrame,
) -> tuple[dict[str, str], dict[str, str]]:
    """{commune → département} et {département → région}, résistants au COG.

    Deux pièges, qui coûtaient 3 569 708 inscrits (7,2 % du corps électoral) aux niveaux
    département et région des européennes 2024 — le Maine-et-Loire y perdait 39 % de ses
    électeurs, la Seine-Saint-Denis toute la commune de Saint-Denis :

    1. Le COG liste une commune fusionnée DEUX fois : sous son nom actuel (avec son
       département) et sous son nom d'avant fusion (sans département, pour la recherche).
       `set_index(...).to_dict()` garde la DERNIÈRE ligne, donc la case vide. On ne garde
       donc que les lignes rattachées.
    2. Les scrutins anciens portent des codes de communes disparues, absents du COG. Le
       code INSEE porte son département : on le dérive, à condition qu'il désigne un
       département réel (sinon les codes « ZZ » des Français de l'étranger et « 98 » du
       Pacifique fabriqueraient des départements fantômes)."""
    rattachees = communes.dropna(subset=["code_departement"])
    com2dep = (
        rattachees.drop_duplicates("code_commune")
        .set_index("code_commune")["code_departement"]
        .to_dict()
    )
    dep2reg = (
        rattachees.dropna(subset=["code_region"])
        .drop_duplicates("code_departement")
        .set_index("code_departement")["code_region"]
        .to_dict()
    )
    return com2dep, dep2reg


def departements_de(
    codes: pd.Series, com2dep: dict[str, str], dep2reg: dict[str, str]
) -> pd.Series:
    """Département de chaque code commune : le COG d'abord, le préfixe du code ensuite."""
    depuis_code = codes.map(_departement_du_code)
    return codes.map(com2dep).fillna(depuis_code.where(depuis_code.isin(dep2reg)))


# Groupes de bureaux dont un code a changé de SENS entre le millésime des contours et les
# scrutins récents — commune renumérotée ou redécoupée, arrondissement de PLM renuméroté
# en continu (cf. construire_crosswalk_renumerotation, construire_crosswalk_plm,
# est_recode et _remapper). Une commune y est désignée par son code (« 77152 »), un
# arrondissement de PLM par commune + arrondissement (« 75056_04 ») : `est_recode` lit les
# deux. La liste est écrite dans
# data_app parce qu'elle ne concerne pas que ce module : dans ces groupes, un code de
# bureau ne désigne pas le même bureau selon la source qui le porte, et toute donnée
# ATTACHÉE AUX CODES venue d'ailleurs y est suspecte. prep_mobilisation s'en sert pour
# écarter la texture intra-communale d'elections_predictions, qui porte le même faux
# appariement un cran plus haut : ses 18 entrées bordelaises publient les inscrits des
# bureaux de 2024 sous des codes de contours de 2022.
FICHIER_RECODEES = "communes_recodees.json"


def construire_resultats(
    dossier_clean: Path,
    communes: pd.DataFrame,
    geo_dir: Path | None = None,
    listes_lfi_fichier: Path | None = None,
    sortie: Path | None = None,
) -> dict[str, pd.DataFrame]:
    """Construit un dict {niveau: DataFrame} agrégeant tous les scrutins.

    `sortie` (facultatif) : dossier data_app où déposer la liste des groupes recodés,
    à l'usage des étapes suivantes du pipeline."""
    # Les DEUX crosswalks ne valent que pour les scrutins qui portent la nouvelle
    # numérotation : appliqués aux scrutins antérieurs, leurs clés (des codes 2022 valides)
    # renverraient les voix d'un bureau sur le contour d'un autre. Vrai de la
    # renumérotation communale (à Bordeaux, 1101 est un bureau de 2022) comme de la
    # numérotation continue de PLM, qui apparaît en 2024 exactement — les dix-huit scrutins
    # de 2012 à 2022 numérotent le 4e arrondissement de Paris 0401…0414, les six de 2024 à
    # 2026 le numérotent 0436…0449. Aucune clé du crosswalk PLM n'existe dans les fichiers
    # de 2022, mais s'en remettre à cette vérification-là plutôt qu'à la règle serait
    # confier au hasard des millésimes ce que l'année tranche.
    plm, recodes_plm = (
        construire_crosswalk_plm(dossier_clean, geo_dir)
        if geo_dir
        else ({}, frozenset())
    )
    renum, recodes_com = (
        construire_crosswalk_renumerotation(dossier_clean, geo_dir)
        if geo_dir
        else ({}, frozenset())
    )
    crosswalk_recent = {**plm, **renum}
    recodees = frozenset(recodes_plm | recodes_com)
    if sortie is not None:
        (sortie / FICHIER_RECODEES).write_text(
            json.dumps(sorted(recodees), indent=1), encoding="utf-8"
        )
    listes_lfi = charger_listes_lfi(listes_lfi_fichier) if listes_lfi_fichier else set()
    com2dep, dep2reg = rattachement_communal(communes)
    accum: dict[str, list[pd.DataFrame]] = {
        n: [] for n in ("bureau", "commune", "departement", "region", "france")
    }
    for scrutin in lister_scrutins(dossier_clean):
        try:
            recent = scrutin.annee >= CROSSWALK_ANNEE_MIN
            bureaux = _par_bureau(
                scrutin,
                crosswalk_recent if recent else {},
                listes_lfi,
                recodees if recent else frozenset(),
            )
        except Exception as e:  # un scrutin atypique ne doit pas tout bloquer
            print(f"  ⚠ {scrutin.cle} ignoré : {e}")
            continue
        for sc, bv in bureaux:
            bv["code_departement"] = departements_de(
                bv["code_commune"], com2dep, dep2reg
            )
            bv["code_region"] = bv["code_departement"].map(dep2reg)
            bv["france"] = "FR"
            accum["bureau"].append(_agreger(bv, "code_bv", "bureau", sc))
            accum["commune"].append(_agreger(bv, "code_commune", "commune", sc))
            accum["departement"].append(
                _agreger(bv, "code_departement", "departement", sc)
            )
            accum["region"].append(_agreger(bv, "code_region", "region", sc))
            accum["france"].append(_agreger(bv, "france", "france", sc))
            print(f"  ✓ {sc.cle}: {len(bv)} bureaux")
    return {
        n: pd.concat(parts, ignore_index=True) for n, parts in accum.items() if parts
    }
