# TML NFT Watch

Suivi de 3 collections NFT officielles Tomorrowland — **Letter**, **Reflection**
et **Symbol** (Magic Eden), avec indicateurs, ATH/ATL depuis le contrat et
alertes Telegram — 100% gratuit, aucun serveur à gérer.

- **Collecteur** (`collector.py`) : interroge l'API publique Magic Eden +
  CoinGecko, calcule les indicateurs, le plus bas/plus haut historique
  (scan complet de l'historique on-chain au premier passage) et les 5
  dernières transactions par série, puis met à jour `docs/data.json`.
- **Dashboard** (`docs/index.html`) : page statique, thème clair, toggle
  SOL/EUR, qui lit `docs/data.json`.
- **Automatisation** : GitHub Actions exécute le collecteur toutes les
  15 minutes et publie le résultat.

Voir [`CHANGELOG.md`](CHANGELOG.md) pour l'historique des versions.

## Mise en place (10 minutes)

1. **Créer un repo GitHub** (public ou privé) et y pousser ces fichiers.

   ```bash
   cd tml-nft-bot
   git init
   git add .
   git commit -m "init"
   git branch -M main
   git remote add origin https://github.com/<votre-compte>/<votre-repo>.git
   git push -u origin main
   ```

2. **Activer GitHub Pages**
   Settings → Pages → Source : *Deploy from a branch* → branche `main`,
   dossier `/docs`. Votre dashboard sera disponible à
   `https://<votre-compte>.github.io/<votre-repo>/` après quelques minutes.

3. **(Optionnel) Créer le bot Telegram**
   - Parlez à [@BotFather](https://t.me/BotFather) sur Telegram, `/newbot`,
     récupérez le **token**.
   - Créez un groupe Telegram avec vos amis, ajoutez le bot dedans.
   - Pour récupérer le **chat_id** du groupe : envoyez un message dans le
     groupe, puis ouvrez dans un navigateur
     `https://api.telegram.org/bot<VOTRE_TOKEN>/getUpdates` et repérez
     `"chat":{"id": ...}` (un nombre négatif pour un groupe).

4. **Ajouter les secrets GitHub**
   Settings → Secrets and variables → Actions → *New repository secret* :
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`

   (Si vous ne configurez pas Telegram, le collecteur continue de tourner
   normalement, il logge juste les alertes au lieu de les envoyer.)

5. **Lancer un premier passage manuel**
   Onglet *Actions* → *Update TML NFT data* → *Run workflow*. Après ~30 sec,
   `docs/data.json` est mis à jour et le dashboard affiche les données.

Ensuite, ça tourne tout seul toutes les 15 minutes, gratuitement, sans rien
à maintenir de votre côté.

## Comment lire les signaux

- **SURVEILLER** : pas de mouvement net, situation stable.
- **SIGNAL ACHAT** : tendance courte terme haussière + activité soutenue
  et/ou rétention des détenteurs (moins d'annonces en vente).
- **PRUDENCE** : tendance baissière et/ou forte hausse des annonces en
  vente (signe possible de pression vendeuse).
- **COLLECTE EN COURS** : pas encore assez d'historique (les 5 premiers
  passages du collecteur, soit ~1h15).

Ce sont des heuristiques simples basées sur l'historique collecté depuis
la mise en route — **pas un conseil financier**. Le marché de ces NFT est
peu liquide (parfois quelques ventes par jour), donc les signaux peuvent
être bruités au début. On peut affiner les règles (RSI adapté, détection
de wallets "baleines", pondération par volume réel plutôt que nombre de
ventes...) une fois qu'on a quelques semaines de données réelles.

## Pistes d'évolution

- Suivi des wallets qui accumulent/liquident plusieurs medallions.
- Alerte spécifique si un ami du groupe liste un medallion (si vous
  suivez des wallets connus).
- Vue "budget objectif" : combien de SOL il faudrait pour garantir l'accès
  du groupe sur X années, comparé au floor actuel.
- Export CSV de l'historique pour analyse plus poussée.
- Toggle USD en plus de SOL/EUR.
- Historique visuel (graphe complet, pas juste sparkline) pour l'ATH/ATL.

## Sources

- Données de marché : [Magic Eden](https://magiceden.io) (API publique,
  utilisée aussi par `nft.hardy.se` et `kaemm.com/nft`).
- Taux de change : [CoinGecko](https://www.coingecko.com) (API publique).
