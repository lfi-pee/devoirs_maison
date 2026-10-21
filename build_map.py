"""Assemblage de la carte : `map.html` (squelette) + `assets/map.css` + `assets/js/*.js`.

La carte est servie en UNE seule page autoportante (pas de serveur d'assets), mais la
source est éclatée en fichiers par responsabilité pour permettre l'édition en parallèle. Les modules JS sont concaténés dans l'ordre des noms
(01_, 02_, …) : le résultat est identique au fichier monolithe d'origine.

**Une seule page, un seul score.** Le site a longtemps publié trois versions côte à côte
(`index.html`, `v2/`, `v3/`), qui ne différaient que par la définition des « voix à
conquérir » : objectif arithmétique, voix modélisées, rentabilité du porte-à-porte. La
comparaison ayant tranché, seule la RENTABILITÉ subsiste — voix gagnables par heure de
porte-à-porte — et elle est servie à la racine. Les deux autres définitions ne sont plus
calculées nulle part, ni côté carte ni dans les fiches.
"""

from __future__ import annotations

import base64
import pathlib
import re
from functools import cache

from prep_seed import amorce

ROOT = pathlib.Path(__file__).parent
ASSETS = ROOT / "assets"
CSS_MARK = "/*__CSS__*/"
JS_MARK = "/*__JS__*/"
# le marqueur INCLUT le `{}` par défaut : sans lui, l'injection laissait `={...}{};`
# derrière elle — une erreur de syntaxe, et l'amorce passait silencieusement à la trappe.
SEED_MARK = "/*__SEED__*/{}"
# La page n'a pas de serveur d'assets : les `url("fonts/…")` de map.css (chemins relatifs
# à assets/) sont remplacés par le fichier en `data:`. Ils restent dans un <style>, donc
# l'export PDF (10_export.js), qui recopie les <style>, emporte la police avec lui.
ASSET_URL = re.compile(r'url\("(fonts/[^"]+)"\)')
MIME = {".ttf": "font/ttf", ".woff2": "font/woff2"}


def _inline_assets(css: str) -> str:
    def data_uri(m: re.Match) -> str:
        f = ASSETS / m.group(1)
        b64 = base64.b64encode(f.read_bytes()).decode("ascii")
        return f'url("data:{MIME[f.suffix]};base64,{b64}")'

    return ASSET_URL.sub(data_uri, css)


# @cache : l'assemblage relit tous les modules et simplifie les contours de la vue France
# (~0,4 s). Payé une fois à la PUBLICATION (build_site.py), jamais par un visiteur.
@cache
def assemble_map(base: str | None = None, suggestion: str | None = None) -> str:
    css = _inline_assets((ASSETS / "map.css").read_text(encoding="utf-8"))
    parts = [
        f.read_text(encoding="utf-8") for f in sorted((ASSETS / "js").glob("*.js"))
    ]
    js = "".join(p if p.endswith("\n") else p + "\n" for p in parts)
    html = (ROOT / "map.html").read_text(encoding="utf-8")
    if CSS_MARK not in html or JS_MARK not in html:
        raise ValueError("map.html : marqueurs __CSS__ / __JS__ introuvables")
    # amorce AVANT le JS : le marqueur vit dans un <script> qui précède celui de la carte
    if SEED_MARK not in html:
        raise ValueError("map.html : marqueur __SEED__ introuvable")
    html = html.replace(SEED_MARK, amorce(ROOT / "data_app").json)
    html = html.replace(CSS_MARK, css).replace(JS_MARK, js)
    if base is not None:
        html = html.replace("__BASE__", base)
    if suggestion is not None:
        html = html.replace("__SUGGESTION__", suggestion)
    return html


if __name__ == "__main__":
    print(assemble_map())
