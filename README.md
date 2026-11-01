# Veille actu → Telegram

4 bots Telegram : 🏛️ politique, 📈 économie, ⚽ sport et 🎬 art (cinéma, musique, mode, arts visuels).

- **Alertes en direct.** Tu reçois un message dès qu'un nouveau sujet sort.
- **Hiérarchisation.** Les articles qui parlent du même sujet sont regroupés. Chaque sujet est classé selon le nombre de médias différents qui le couvrent.
- **Résumé quotidien à 20 h.** Les sujets du jour sont classés par importance, avec les liens vers chaque article.

Les sources couvrent 55 médias différents (environ 80 flux) : presse nationale de toutes sensibilités, médias spécialisés, presse internationale et institutions.

---

## Comment fonctionne la hiérarchie

| Niveau | Règle par défaut | Ce qui se passe |
|---|---|---|
| ⚪ Brève | 1 à 2 médias | Alerte simple quand le sujet apparaît |
| 🟠 Important | 3 ou 4 médias | Badge 🟠 sur l'alerte, sujet placé en haut du résumé |
| 🔴 Majeur | 5 médias ou plus | **Nouvelle alerte « SUJET MAJEUR »** avec les liens de tous les médias qui en parlent |

Certains mots donnent +1 au score d'un sujet : urgent, démission, attentat, 49.3, finale, etc. La liste est modifiable.

Un même sujet repris par 10 médias ne te fait pas recevoir 10 messages. Tu reçois un message quand il apparaît, puis un seul 🔴 s'il devient majeur.

## Le résumé quotidien

Chaque bot t'envoie son résumé à 20 h (heure de Paris). Il contient :
- **À la une** : les 10 sujets les plus importants. Pour chacun : niveau, titre, résumé, nombre de médias et **un lien vers l'article de chaque média**.
- **Aussi dans l'actu** : 12 sujets supplémentaires sur une ligne, avec leurs liens.
- Une alerte si une source ne répond plus depuis plus de 6 h.

**Option synthèse IA.** Si tu ajoutes une clé API Anthropic (secret `ANTHROPIC_API_KEY`), chaque sujet de la une reçoit une synthèse rédigée à partir de tous les médias, avec une introduction « l'essentiel du jour ». Cela coûte quelques centimes par jour. Sans clé, le résumé reprend le chapô du meilleur article.

---

## Installation

### 1. Créer les 4 bots
1. Dans Telegram, ouvre **@BotFather** et envoie `/newbot`.
2. Choisis un nom (par exemple « Veille Politique ») et un identifiant qui finit par `bot`.
3. Note le **token** qu'il te donne.
4. Recommence pour l'économie, le sport et l'art.
5. **Ouvre chacun des 4 bots et appuie sur Démarrer.**

### 2. Trouver ton CHAT_ID
Ouvre **@userinfobot** dans Telegram : il t'affiche ton `Id`.

### 3. Mettre le projet sur GitHub
1. Sur github.com, clique sur **New repository** (choisis **Private**), puis dépose tous les fichiers du dossier.
2. Va dans **Settings → Secrets and variables → Actions** et ajoute ces secrets :
   - `CHAT_ID`
   - `TOKEN_POLITIQUE`
   - `TOKEN_ECONOMIE`
   - `TOKEN_SPORT`
   - `TOKEN_ART`
   - `ANTHROPIC_API_KEY` (optionnel)
3. Dans l'onglet **Actions**, ouvre **Veille actu Telegram** et clique sur **Run workflow**.

Au premier passage, chaque bot envoie « Bot … activé » et mémorise les articles existants. Les alertes commencent ensuite.

---

## Personnaliser (fichier `sources.yaml`)

- **Ajouter un média** : `- {nom: Le Média, url: "adresse du flux RSS"}`
- **Donner plus de poids à une source de référence** : ajoute `poids: 1.5`. C'est déjà le cas pour L'Équipe.
- **Recevoir moins ou plus de messages** : change `alertes:` en `toutes`, `sujets` (par défaut) ou `importantes`. Le réglage se fait globalement ou par thème.
- **Rendre la hiérarchie plus ou moins stricte** : modifie `seuil_important` et `seuil_majeur`.
- **Filtrer** : ajoute `mots_cles: [PSG, Ligue 1]` ou `mots_exclus: [horoscope]` dans un thème.
- **Changer l'heure du résumé** : modifie `heure_resume`.

## Vérifier les sources

```bash
pip install -r requirements.txt
python veille.py --tester
```

Cette commande indique pour chaque flux s'il répond (OK ou ERREUR). Les sites changent parfois l'adresse de leur flux. Si une source est en erreur, cherche « nom du média flux RSS » et remplace l'adresse.

## Lancer sur ton ordinateur (optionnel, plus réactif)

```bash
cp .env.example .env     # puis remplis-le
python veille.py --boucle 2
python veille.py --resume   # pour voir le résumé tout de suite
```

## Bon à savoir

- Sur GitHub, il faut compter 5 à 15 minutes de délai entre la parution d'un article et l'alerte.
- Les médias en anglais (BBC, Guardian, Variety…) forment des sujets séparés des médias français. Ils ne sont pas fusionnés avec eux.
- Un flux en panne ne bloque pas les autres.
