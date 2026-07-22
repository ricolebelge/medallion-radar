# Changelog

## v1.2.0

**Ajouts**
- Signaux étendus à 5 niveaux : **ACHAT FORT**, SIGNAL ACHAT, SURVEILLER,
  PRUDENCE, **VENDRE** — désormais calculés aussi à partir de la position
  du floor par rapport à l'ATH/ATL (proche du plus bas → signal d'achat
  renforcé ; proche du plus haut → signal de vente), pas seulement des
  moyennes mobiles.
- Variations 7 jours / 30 jours affichées sur chaque carte.
- Graphique comparatif des 3 séries (variation % sur une fenêtre commune),
  inspiré de tml.guide/tools/nft-tracker.

## v1.1.0

**Ajouts**
- ATH / ATL par collection, calculés depuis l'historique complet des ventes
  on-chain (scan unique au premier passage, puis mise à jour incrémentale).
- Affichage des 5 dernières transactions (vente, mise en vente, retrait...)
  sous chaque carte.
- Toggle SOL / EUR sur tout le dashboard (floor, ATH/ATL, transactions).
- Renommage des séries pour plus de lisibilité : *A Letter from the Universe
  (Winter)* → **Letter**, *The Reflection of Love* → **Reflection**,
  *The Symbol of Love and Unity* → **Symbol**.

**Retraits**
- Arrêt du suivi de *The Golden Auric* (hors périmètre du groupe).

**Design**
- Nouveau thème clair (le thème sombre précédent manquait de lisibilité).

**Correctifs**
- Correction d'une erreur de syntaxe JavaScript (apostrophe mal échappée)
  qui bloquait totalement le rendu du dashboard v1.0.

## v1.0.0

- Premier déploiement : collecteur Magic Eden + CoinGecko, dashboard
  statique GitHub Pages, alertes Telegram, automatisation GitHub Actions
  toutes les 15 minutes. 4 collections suivies (Winter, Reflection, Symbol,
  Golden Auric).
