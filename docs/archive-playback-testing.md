# Essais de Playback — à lancer par l’utilisateur

**Dernier essai utilisateur sous Python 3.9.13 : 43 tests réussis sur 44.**
Le seul échec intervient au nettoyage d’une base SQLite temporaire, après les
assertions. La connexion du montage de test est maintenant fermée explicitement ;
la relance après cette correction reste à effectuer par l’utilisateur. La première
trace Python 3.14 échouait à l’import, avant tout test.

Le diagnostic réel C3/101 du 18 septembre 2026 retourne 64 archives en deux pages,
avec une recherche complète et sans erreur. Il ne valide pas le téléchargement
ni la lecture. Ces résultats et les étapes restantes sont documentés dans le
[guide de diagnostic des blocages](archive-playback-diagnostics.md). L’agent
n’a lancé aucun test ni appel caméra. Les commandes ci-dessous sont à lancer
depuis le dossier `Camera`, quand le poste est disponible.
Ne démarrez pas deux essais vidéo simultanément. Fermez le premier lecteur et
attendez sa fermeture complète avant de passer à la deuxième caméra.

## Préparation et vérifications sans lecteur

Utiliser explicitement le Python 3.9 du Viewer. Dans PowerShell, définir cette
variable une fois par terminal, puis la réutiliser dans toutes les commandes :

```powershell
$CameraPython = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python39\python.exe'
& $CameraPython tools/diagnose_playback.py environment
```

Ne pas utiliser simplement `python`, qui peut désigner une autre installation.
Si le Viewer utilise ailleurs un chemin différent, adapter uniquement cette
variable. Les processus enfants utilisent ensuite le même interpréteur.
Le cache est local dans AppData, pas dans le dossier OneDrive des sources.
Installer les dépendances sur cet interpréteur si nécessaire ; `tzdata` est
la nouvelle dépendance :

```powershell
& $CameraPython -m pip install -r requirements.txt
```

Puis lancer uniquement les nouveaux tests synthétiques, sans caméra ni lecteur :

```powershell
& $CameraPython -m unittest discover -s tests -p "test_playback*.py" -v
```

Ils couvrent les fuseaux/DST, limites de jours, états d’index, pagination,
réponses CGI observées, chiffrement des locateurs, protection du cache/exports,
plages HTTP loopback et calculs de playlist. Les nouveaux cas couvrent aussi la
découverte complète à partir de structures XML expurgées, une seconde piste
refusée, les deux erreurs en mode auto et l’application du fuseau par ID.
Ils ne valident ni affichage ni son. Pour les blocages du banc actuel, commencer
par les [comparaisons caméra/journée/piste](archive-playback-diagnostics.md),
sans générer de mire ni lancer FFmpeg.

## Premier essai visuel sans caméra

Cette commande **crée des fichiers vidéo synthétiques avec une piste audio**,
sans ouvrir de lecteur ni diffuser de son. Elle appelle FFmpeg. Prévoir quelques
minutes selon le poste. Le dossier doit être nouveau :

```powershell
& $CameraPython tools/create_playback_fixture.py --directory playback-fixtures
```

Puis ouvrir volontairement le lecteur intégré sur ce banc :

```powershell
& $CameraPython camera_playback.py --fixture-directory playback-fixtures --camera 901
```

Le banc utilise sa propre clé, son propre index et son propre cache ; il ne lit
pas les identifiants réels et ne contacte pas les caméras. Deux archives de 24 s
commencent à 12:00:00 et 12:00:24 le jour de création ; une troisième commence à
12:01:10, après un trou de 22 s. C901 porte AAC, C902 µ-law. L’image est une mire
animée ; la piste audio est une tonalité. Le son initial est coupé.

1. Vérifier les badges C901/C902, les filtres et les deux pistes de timeline.
2. Saisir `12:00:00`, cliquer Aller. Vérifier la vidéo dans cette même fenêtre.
3. Activer le son uniquement quand souhaité ; tester pause et 0,5×/1×/2×/4×.
4. Tester ±10 s, zoom sans déplacement de la lecture, curseur, fin à 12:00:48
   et saut explicite à 12:01:10. Un trou ne doit pas rester masqué par une image.
5. Arrêter/fermer ce lecteur, puis relancer avec `--camera 902` pour µ-law.
6. Refaire, si souhaité, avec une fixture HEVC dans un **autre dossier** :

```powershell
& $CameraPython tools/create_playback_fixture.py --directory playback-fixtures-hevc --codec hevc
& $CameraPython camera_playback.py --fixture-directory playback-fixtures-hevc --camera 901
```

Les fichiers de mire ne contiennent pas encore de compteur OSD absolu : ce banc
ne prouve donc pas l’absence d’image omise/dupliquée à une jonction. La qualification
des timestamps/compteurs reste à compléter avant de fermer l’issue.

## Essai réel, une caméra à la fois

Pour ouvrir **uniquement** Playback, sans le moniteur de santé/direct/PTZ :

```powershell
& $CameraPython camera_playback.py
```

Ou lancer `Lancer-Enregistrements.cmd`. Les deux lanceurs Windows utilisent
`%LOCALAPPDATA%\Programs\Python\Python39\python.exe` par défaut ; la variable
`CAMERA_PYTHON` permet de choisir explicitement un autre interpréteur.
Ils n’ajoutent pas le dossier de dépendances temporaire `.release-work/test-deps`.
Choisir ensuite une seule caméra dans les
filtres et dans la sélection active. En Réglages, confirmer son fuseau pour CGI.
Si l’identité ne peut pas être découverte, renseigner une révision locale.
Cliquer Appliquer : le logiciel arrête les anciennes recherches, enregistre le
réglage local, recharge la caméra par son ID et relance sa journée. Aucune
réouverture ni saisie de mot de passe n’est nécessaire pour ces réglages.

Choisir une archive **déjà ancienne**, dont l’OSD est connu, puis noter :

| Action | Résultat à relever |
|---|---|
| Recherche du jour/mois | Badges, plage, piste, statut complet/partiel, fraîcheur |
| Premier démarrage | Temps jusqu’à la première image réellement visible, son |
| Seek début/milieu/fin | Heure demandée / OSD visible / délai / cache chaud ou froid |
| Pause puis seek | Position, pause, vitesse et volume conservés |
| 0,5×/1×/2×/4× | Rythme réellement constaté et synchronisation audio |
| Deux fichiers successifs | Heure avant/après, répétition/trou, transition sonore |
| Trou réel | Message explicite, saut seulement sur commande |
| Caméra momentanément indisponible | Lecture des octets locaux ; statut distinct d’un jour vide |
| Original puis plage A–B | Fichiers distincts et sidecars ; aucune réécriture d’un fichier existant |
| Fermeture pendant chargement | Fenêtre fermable, aucun FFmpeg/libVLC/serveur restant de cet essai |

Ne modifier ni réseau global, ni horloge/codec/détection des caméras pour ces essais.
Ne publier ni vidéo réelle, ni identifiants, ni index privé sur GitHub. Les journaux
expurgés sont dans `%LOCALAPPDATA%\CameraManagementSystem\playback\events.jsonl`.
L’original et la clé restent privés, même si un test échoue.

## Interface et non-régression

Dans un créneau permettant l’ouverture des fenêtres, lancer le Viewer :

```powershell
& $CameraPython camera_viewer.py
```

Vérifier l’entrée globale et par caméra, une seule fenêtre Playback, le résumé
de disponibilité inchangé au survol, Play/PTZ/Manage Cameras, positions et fermeture
des enfants. Sur Playback, vérifier grande largeur, largeur moyenne, portrait et
faible hauteur ; commandes regroupées, panneau repliable, focus/infobulles,
timeline recalculée sans seek ni redémarrage vidéo. Relever les DPI réellement
essayés (100/125/150/200 %), la taille de fenêtre et la version Tk/Windows.

La suite existante ouvre des fenêtres Tk et peut lancer des processus de test :
la réserver au moment approprié. Elle ne constitue pas une validation physique
automatique des deux caméras :

```powershell
& $CameraPython -m unittest discover -s tests -v
```

Pour un retour utile, indiquer caméra/API, action exacte, résultat attendu/observé,
heure de l’essai, code d’erreur éventuel, vitesse et état froid/chaud du cache.
Joindre une capture uniquement quand vous le choisissez. Une observation visuelle
sera consignée comme telle, séparément des mesures de processus et hypothèses.

## Retour aux sources stables

La copie `~v0.2.10` reste inchangée. Pour un rollback complet, fermer d’abord les
fenêtres de l’application et restaurer les sources depuis ce dossier vers la
racine, en conservant `camera_credentials.db`, `.camera_encryption.key` et
`camera_window_positions.db` à la racine. Les nouvelles sources Playback peuvent
rester présentes sans être utilisées par l’ancien Viewer. Le cache Playback est
indépendant ; ne pas y placer d’exports à conserver. Aucun rollback n’est effectué
automatiquement et aucune commande destructive n’est lancée par ces scripts.
