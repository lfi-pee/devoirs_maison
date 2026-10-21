# Changelog

## [Unreleased]

### Changed

- Passer l'interface à la typographie du kit graphique M27 : Union Gothic Condensed pour les titres (nom du site en 1000 avec l'effet isométrique du kit, nom de la zone, chiffre de tête et titres de fenêtre en 700) et Stack Sans Text pour tout le reste, boutons en Bold et en capitales. Public Sans et Gowun Batang ne sont plus chargées. Union Gothic est inlinée telle quelle dans la page (licence CC BY-NC-ND, voir `assets/fonts/LICENCE.md`), ce qui ajoute 210 Ko à `index.html`.
- Appliquer les couleurs du kit graphique M27 à tout le site : toutes viennent de `Light.tokens.json` (interface, choroplèthe, axe politique, note de priorité, séries socio, fond de carte). `validation_charte.py` bloque en CI toute couleur hors charte.
- Passer la heatmap de la carte, pour tous les indicateurs, sur le dégradé de la légende de priorité (jaune → bordeaux) au lieu du bleu → rouge. La couleur dit toujours l'écart à la médiane des zones affichées. La pastille de la note dans la fiche prend la couleur de son niveau, sur six niveaux (faible, secondaire, importante, haute, majeure, critique).

### Internal

- Ajouter une image Atlas autonome et sa publication sur GHCR pour le déploiement authentifié par Canvass, avec un build indépendant du site GitHub Pages.
