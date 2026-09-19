# Lecture progressive, scrubbing et interface anglaise — PR #16

## État des preuves

Le retour utilisateur sur `4366478` valide **44 tests en 0,547 s**, avec Python
3.9.13. Pour C3 et la journée du 18 septembre 2026 : 101 retourne 50 puis 14
archives ; 103 retourne `trackID=101`, donc `track-mismatch` ; auto conserve les
64 archives et signale `tracks-partial`. Cela confirme ce défaut secondaire sur
cet appareil et ce jour, sans généralisation aux autres modèles.

Les modifications de démarrage, scrubbing et langue qui suivent restent à
qualifier. **Aucun test, lecteur, FFmpeg ni appel caméra n’a été lancé par l’agent.**
Les vérifications de l’agent sont la lecture du code, l’analyse syntaxique sous
Python 3.9 et la comparaison des sources. Les résultats ci-dessus restent ceux
de l’utilisateur, antérieurs à ces nouvelles modifications.

## Causes et comportement corrigé

- L’analyse du préfixe partageait l’obligation de durée globale du fichier complet
  et n’était tentée qu’une fois après 2 MiB, uniquement pour ISAPI. Elle est
  maintenant distincte, bornée à cinq tailles croissantes et applicable selon le
  conteneur reçu. Les erreurs HTML/XML/JSON sont refusées avant tout lecteur.
- Un préfixe incomplet peut être réanalysé. MPEG/TS ou MP4 avec métadonnées avant
  les données vidéo peuvent être préparés pendant réception. MP4 à métadonnées
  tardives, conteneur non pris en charge ou échec avant publication : attente
  complète avec motif. La validation complète reste obligatoire après réception.
  Ce sont des capacités à tester ; les formats des trois téléchargements réels
  de ce nouveau parcours ne sont pas encore mesurés.
- Le plafond fixe de 12 s reportait certains segments longs jusqu’à la fin.
  La première playlist prend une durée cible calculée sur les segments observés.
  Un segment ultérieur plus long entraîne une nouvelle URL et une reprise native,
  sans attendre le fichier entier. Le transfert et le producteur restent actifs.
  Une même URL conserve sa TARGETDURATION ; aucune coupe vidéo arbitraire ni
  déclaration d’indépendance des segments n’est ajoutée.
- Réserve initiale : deux secondes de visionnage, au minimum deux secondes média,
  soit huit secondes à 4×. Le préchargement de confort reste à 120 s de visionnage.
  Ces valeurs sont des choix à évaluer, pas une promesse de latence.
- La timeline transmet une cible toutes les 125 ms au maximum. Une seule demande
  attend ; les mouvements suivants la remplacent. Le transfert, le producteur,
  la playlist et le processus libVLC sont conservés pour un seek dans la plage
  préparée. La dernière cible de la même archive attend ses données si nécessaire.
- Un glissement hors de la source active affiche `Loading preview…` ; le passage
  à cette autre source est effectué au relâchement, une seule fois. Un saut froid
  lointain reste limité par la réception séquentielle. Aucune bascule RTSP/cache
  ni reprise Range n’est revendiquée.
- L’aperçu garde la vitesse choisie, coupe temporairement le son et se fige sur une
  nouvelle image confirmée. Au relâchement, une nouvelle demande finale doit être
  confirmée avant restauration du mode pause/lecture et de l’audio utilisateur.
  La confirmation exige une progression des deux compteurs depuis la demande et
  une position à moins d’une seconde ; ce n’est pas une précision à l’image.
- Les textes Playback sont centralisés en anglais : `Recordings`, calendrier,
  commandes, réglages, erreurs, infobulles et export. Les dates explicites, ID,
  fuseaux, DST et Bootstrap Icons sont conservés. Le redimensionnement garde le
  même support vidéo ; les textes de la timeline se replient sur plusieurs lignes.

La durée de segment dépend des images clés : voir [FFmpeg HLS](https://ffmpeg.org/ffmpeg-formats.html#hls).
L’immuabilité de la durée cible par playlist est définie dans [RFC 8216 §6.2.1](https://www.rfc-editor.org/rfc/rfc8216#section-6.2.1).

## 1. Tests synthétiques, sans lecteur

Depuis la racine `Camera`, au moment choisi :

```powershell
$CameraPython = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python39\python.exe'
& $CameraPython -m unittest discover -s tests -p "test_playback*.py" -v
```

Les 44 tests précédents sont conservés. Les nouveaux tests couvrent les préfixes
sans durée, réessais, MP4 non progressif, refus de réponses d’erreur, GOP longs,
réserve, conservation de session lors du seek, cible récente prioritaire,
attente des données, relâchement/pause/audio, compteurs périmés, cadence du geste,
langue et séparation des preuves natives/visuelles. Ils ne certifient pas la
fluidité du lecteur ni la compatibilité de toutes les dispositions MP4.

## 2. Banc froid et réception ralentie — ouvre ensuite une fenêtre

Cette première commande crée quatre vidéos synthétiques **avec FFmpeg** et une
piste sonore, sans ouvrir de lecteur. Choisir un moment approprié. Le dossier est
nouveau ; aucun cache ou média existant n’est effacé. Environ 180 secondes de vidéo
par source, débit cible 2 Mbit/s ; prévoir environ 200 Mo plus le cache de lecture.

```powershell
$CameraPython = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python39\python.exe'
$Bench = Join-Path $env:LOCALAPPDATA ('CameraManagementSystem\playback-bench-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
& $CameraPython tools/create_progressive_fixture.py --directory $Bench
```

Si la police proposée est absente, passer `--font-file` avec le chemin d’une
police TrueType installée. Ne pas relancer sur un dossier déjà créé après un
échec ; le conserver et choisir un nouveau dossier.

Ouvrir **uniquement C901** :

```powershell
& $CameraPython camera_playback.py --fixture-directory $Bench --camera 901 --day-only
```

1. Saisir **12:00:02**, puis `Go to time`. La réception est ralentie de 0,3 s par
   bloc de 256 KiB. La vidéo contient un temps relatif et un compteur d’images ;
   `00:00:02` correspond donc à 12:00:02 dans ce banc.
2. Lorsque la première nouvelle image est visible, appuyer sur **F8** avec le
   focus dans Playback. Cette annotation manuelle inclut le temps de réaction.
3. Vérifier visuellement que les images progressent tandis que la réception
   continue. Le journal permet de comparer première image native, annotation
   visuelle et fin de réception. Un compteur natif seul ne vaut pas observation.
4. Glisser pendant au moins cinq secondes à droite, à gauche, puis changer vite
   de direction dans les plages lisibles. Le compteur dans le viewer doit évoluer
   **avant le relâchement**. Noter latence, précision et sauts visibles ; la cadence
   d’envoi à 8 Hz ne garantit pas huit images affichées par seconde.
5. Refaire en pause, puis à 0.5×, 2× et 4×. Tester volume/mute avant le geste,
   silence pendant l’aperçu et restauration au relâchement. Tester une cible non
   reçue : distinguer `Requested` de `Preview`, puis attendre ses données.
6. Redimensionner, ouvrir les panneaux, parcourir toutes les infobulles/erreurs
   et boîtes d’export. Fermer pendant un geste, puis vérifier la fermeture complète.

Après fermeture, produire le rapport sans lancer de lecteur :

```powershell
& $CameraPython tools/summarize_playback_events.py --log (Join-Path $Bench 'cache\events.jsonl') --camera 901
```

Répéter ensuite **une source à la fois**, en fermant complètement la précédente :

```powershell
& $CameraPython camera_playback.py --fixture-directory $Bench --camera 902 --day-only
& $CameraPython tools/summarize_playback_events.py --log (Join-Path $Bench 'cache\events.jsonl') --camera 902
```

C902 a des GOP de 20 s : vérifier que le premier segment long se publie sans
attendre la réception entière, et lire `segment_duration` / `target_duration`.
Répéter les deux commandes avec **903** : MP4 à métadonnées finales, attente
complète expliquée. Puis **904** : MP4 faststart synthétique pour évaluer le chemin
progressif. Le faststart est ici une caractéristique du fichier source du banc,
pas une transformation prétendant accélérer une réception caméra déjà terminée.

Pour un nouvel essai froid de la même source, générer un nouveau banc daté plutôt
que supprimer ses preuves. Ne jamais exécuter simultanément deux fenêtres.

## 3. Caméra réelle — une à la fois

Après le banc, ouvrir volontairement C3 et la journée déjà recherchée si elle est
encore conservée sur la carte. Sinon choisir une journée récente avec archives.
Dans `Settings`, sélectionner `isapi` et `101`, puis `Apply` pour isoler la piste
qualifiée. Ne pas imposer ce réglage aux autres caméras.

```powershell
$CameraPython = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python39\python.exe'
& $CameraPython camera_playback.py --camera 3 --date 2026-09-18 --day-only
```

Choisir une cible proche du début d’une archive réellement listée. Une archive
entièrement en cache ne mesure pas le démarrage à froid ; sélectionner une autre
archive jamais lue, sans supprimer le cache existant. Appuyer sur F8 à la première
image et vérifier l’OSD. Faire ensuite un saut lointain puis un retour dans une
zone reçue. Fermer le lecteur avant de passer à une autre caméra.

```powershell
$PlaybackLog = Join-Path $env:LOCALAPPDATA 'CameraManagementSystem\playback\events.jsonl'
& $CameraPython tools/summarize_playback_events.py --log $PlaybackLog --camera 3
```

Pour les autres appareils, utiliser leurs **vrais ID de base**, leur backend
observé et leur fuseau confirmé. Les événements `container` et `mode-selected`
indiquent la décision réellement prise. Aucun réglage de caméra n’est modifié.

## Mesures et limites

Les événements expurgés utilisent l’horloge monotone : demande, premier bloc
HTTP reçu (`first-byte`, mesure par bloc et non par octet sur le câble), analyse
insuffisante/suffisante, mode/fallback, producteur, premier segment, cible
avec réserve, ouverture native, nouvelle image selon deltas libVLC, observation
F8, fin de téléchargement et changements de génération pour GOP long. La remise
à l’agent du signal du processus caméra ajoute un léger délai au premier bloc.

Un échec après publication arrête la session et conserve les octets déjà reçus ; un redémarrage
complet reste explicite. Des changements de GOP peuvent interrompre la lecture.
L’aperçu natif HLS, la copie HEVC, la disposition des MP4 réels, l’audio, les
frontières et la fermeture doivent être mesurés. Si le seek natif est trop lent,
un cache borné d’images d’aperçu sera à évaluer à partir de ces mesures ; il n’est
pas annoncé comme implémenté. La passerelle RTSP/cache reste absente.
