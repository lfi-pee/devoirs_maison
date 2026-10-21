# Changelog

## [Unreleased]

### Changed

- Passer l'interface à la typographie du kit graphique M27 : Union Gothic Condensed pour les titres (nom du site en 1000 avec l'effet isométrique du kit, nom de la zone, chiffre de tête et titres de fenêtre en 700) et Stack Sans Text pour tout le reste, boutons en Bold et en capitales. Public Sans et Gowun Batang ne sont plus chargées. Union Gothic est inlinée telle quelle dans la page (licence CC BY-NC-ND, voir `assets/fonts/LICENCE.md`), ce qui ajoute 210 Ko à `index.html`.
- Appliquer les couleurs du kit graphique M27 à tout le site : toutes viennent de `Light.tokens.json` (interface, choroplèthe, axe politique, note de priorité, séries socio, fond de carte). `validation_charte.py` bloque en CI toute couleur hors charte.
- Colorer la note de priorité sur les six niveaux de la légende de priorité (faible, secondaire, importante, haute, majeure, critique), sur la carte, dans la pastille de la fiche et dans la légende. Sur la carte, la note « Prioritaire » prend la couleur de son niveau, la même partout en France, au lieu de l'écart à la médiane des zones affichées.

### Internal

- Ajouter une image Atlas autonome et sa publication sur GHCR pour le déploiement authentifié par Canvass, avec un build indépendant du site GitHub Pages.
