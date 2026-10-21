#!/usr/bin/env python3
"""Vérifie que l'atlas n'affiche aucune couleur hors de la charte graphique.

La charte est l'export Figma `Light.tokens.json`, à la racine du dépôt. `assets/map.css`
en recopie chaque variable en token primitif (`--c-*`), et tout le reste de la feuille et
du JS ne fait que les référencer. Trois contrôles :

1. **Les primitifs sont la charte**, ni plus ni moins : chaque `--c-*` de map.css porte la
   valeur d'une variable du fichier, et chaque couleur du fichier a son primitif.

2. **Aucune couleur littérale hors charte** dans le code servi (`assets/`, `map.html`) :
   tout `#rvb`/`#rrvvbb` et tout `rgb()`/`rgba()` à composantes numériques doit être une
   couleur du fichier — les fonds translucides s'écrivent en `rgba()` d'un primitif. Un
   `color-mix()` fabrique une couleur que la charte n'a pas : il est refusé aussi. Les
   commentaires ne comptent pas : on y cite parfois une ancienne valeur.

3. **Les tokens sémantiques ne référencent que des primitifs** : un `var(--c-x)` vers un
   primitif qui n'existe pas tomberait silencieusement sur la valeur initiale.

Sortie non nulle au premier écart, avec le fichier et la ligne.
"""

from __future__ import annotations

import json
import pathlib
import re
import sys

RACINE = pathlib.Path(__file__).parent
CHARTE = RACINE / "Light.tokens.json"
CSS = RACINE / "assets" / "map.css"
SERVIS = [CSS, RACINE / "map.html", *sorted((RACINE / "assets" / "js").glob("*.js"))]
# La sentinelle du parseur de couleurs (01_config.js) n'est jamais peinte : elle repère
# ce que le navigateur refuse de lire comme une couleur.
HORS_PEINTURE = {"#fedcba"}
# La rampe de la note de priorité (--prio1..6 dans map.css) vient de la légende de
# priorité, livrée à part de l'export Figma : six couleurs, et seulement celles-là.
RAMPE_PRIORITE = {"#f4ed0f", "#f8c71f", "#ff7107", "#e83400", "#b31f00", "#790000"}

HEX = re.compile(r"#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b")
RGB = re.compile(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*[\d.]+\s*)?\)")
PRIMITIF = re.compile(r"--(c-[a-z0-9-]+)\s*:\s*(#[0-9a-fA-F]{6})\s*;")
REF = re.compile(r"var\(--(c-[a-z0-9-]+)\)")


def couleurs_charte() -> set[str]:
    out: set[str] = set()

    def descendre(noeud: object) -> None:
        if isinstance(noeud, dict):
            if noeud.get("$type") == "color":
                out.add(noeud["$value"]["hex"].lower())
                return
            for v in noeud.values():
                descendre(v)

    descendre(json.loads(CHARTE.read_text(encoding="utf-8")))
    return out


def sans_commentaires(texte: str, js: bool) -> str:
    """Blanchit les commentaires en gardant les sauts de ligne (numéros de ligne justes)."""
    blanc = lambda m: re.sub(r"[^\n]", " ", m.group(0))
    texte = re.sub(r"/\*.*?\*/", blanc, texte, flags=re.DOTALL)
    if js:
        # `//` hors URL (`https://…`) et hors chaîne n'apparaît qu'en tête de commentaire.
        texte = re.sub(r"(?<![:\"'`])//[^\n]*", blanc, texte)
    return texte


def hex6(h: str) -> str:
    h = h.lower()
    return "#" + "".join(c * 2 for c in h[1:]) if len(h) == 4 else h


def main() -> None:
    charte = couleurs_charte()
    ecarts: list[str] = []

    css = CSS.read_text(encoding="utf-8")
    primitifs = {nom: v.lower() for nom, v in PRIMITIF.findall(css)}
    for nom, v in primitifs.items():
        if v not in charte:
            ecarts.append(f"map.css : --{nom} = {v} n'est pas dans la charte")
    for v in sorted(charte - set(primitifs.values())):
        ecarts.append(f"map.css : {v} de la charte n'a pas de primitif --c-*")
    for nom in sorted(set(REF.findall(css)) - primitifs.keys()):
        ecarts.append(f"map.css : var(--{nom}) ne référence aucun primitif")

    for p in SERVIS:
        texte = sans_commentaires(p.read_text(encoding="utf-8"), js=p.suffix != ".css")
        for i, ligne in enumerate(texte.split("\n"), 1):
            for m in HEX.finditer(ligne):
                h = hex6(m.group(0))
                if h not in charte and h not in HORS_PEINTURE | RAMPE_PRIORITE:
                    ecarts.append(f"{p.relative_to(RACINE)}:{i} : {m.group(0)} hors charte")
            if "color-mix(" in ligne:
                ecarts.append(f"{p.relative_to(RACINE)}:{i} : color-mix() hors charte")
            for m in RGB.finditer(ligne):
                h = "#" + "".join(f"{int(x):02x}" for x in m.groups())
                if h not in charte:
                    ecarts.append(f"{p.relative_to(RACINE)}:{i} : {m.group(0)} hors charte")
        for nom in sorted(set(REF.findall(texte)) - primitifs.keys()):
            ecarts.append(f"{p.relative_to(RACINE)} : var(--{nom}) ne référence aucun primitif")

    if ecarts:
        print("\n".join(ecarts))
        sys.exit(1)
    print(f"{len(charte)} couleurs dans la charte, {len(primitifs)} primitifs, "
          f"{len(SERVIS)} fichiers servis : aucune couleur hors charte")


if __name__ == "__main__":
    main()
