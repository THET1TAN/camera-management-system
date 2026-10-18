# PR #16 — jonction native et départ progressif

> Historique de la révision précédente. Le [suivi consolidé](archive-playback-consolidated.md)
> contient les observations actuelles, les nouvelles fonctionnalités et les 114 tests préparés (non exécutés).

État : correctif candidat, non qualifié sur lecteur/caméra. Aucun test, lecteur,
FFmpeg, son ou vidéo n'a été lancé par l'agent. Les commandes ci-dessous sont à
exécuter au moment choisi par l'utilisateur, une caméra à la fois.

## Ce que les données conservées établissent

Au début de cette intervention, les 83 fichiers du manifeste local étaient
identiques à la PR au commit `e90002fcbbc06f02220e1ee6e807584521bf9073`.
La copie stable `~v0.2.10` est conservée. Le relevé des processus ne montrait plus
de processus Recordings autonome ; les processus caméra existants ont été laissés
en fonctionnement. Les anciens journaux n'ont pas d'empreinte de build : leur
instrumentation correspond aux dernières modifications, mais ne prouve pas le
commit chargé en mémoire. Les nouveaux démarrages enregistrent `runtime-build`,
une empreinte des modules Playback et un identifiant d'exécution.

La correspondance C1/C2 avec les deux caméras signalées a été vérifiée dans la
base locale en lecture seule, sans publier d'adresse, identifiant ou secret.
Les journaux et l'index disponibles ont été copiés dans le dossier privé
`.release-work/incident-20260919`. Originaux, segments, paramètres, clé et base
d'identifiants n'ont pas été modifiés pour reproduire l'incident.

Temps en secondes depuis la demande, relevés du journal existant :

| Événement | C1, MP4 CGI | C2, MPEG ISAPI |
|---|---:|---:|
| Premier octet | 0,532 | 1,453 |
| Préfixe suffisant / mode progressif | 1,578 | 1,610 |
| Premier segment publié | 2,282 | 1,844 |
| Position relative demandée | 572,697 / 600 | 6117,422 / 6951,356 |
| Ouverture native | 25,391 | 31,250 |
| Téléchargement terminé | 26,266 | 34,375 |
| Première image confirmée par compteurs | 27,953 | absente |
| Erreur terminale | aucune sur ce départ | `seek-unavailable` à 52,047 |

C1 : les deux MP4 complets ont réellement `ftyp`, puis `moov` de 1 112 146 octets,
puis `mdat`. Le repli « métadonnées MP4 en fin de fichier » n'est pas en cause
dans ces fichiers. C2 : le préfixe MPEG est accepté dès 256 Kio. Les deux chemins
produisent avant la fin du téléchargement. La demande éloignée doit attendre
les octets correspondants : catégorie **E**. C2 cumule ensuite **D**, lecteur
ouvert sans confirmation. Aucun événement F8 ne permet de dater une observation
réelle à l'écran. Un départ proche à froid n'est pas encore qualifié sur ces deux
caméras avec ce correctif.

La session C1 conservée gèle près de la jonction, puis deux reprises en cache
montrent un retour de l'horloge native près de zéro. Les compteurs continuent
d'augmenter : cela prouve une incohérence de temps rapporté, pas le contenu des
pixels. Le débit observé est alors **2×**. Il faut encore le témoin **1× sans
interaction**, demandé dans le retour terrain.

Lecture des en-têtes TS existants, sans décodage : vidéo de la première archive
C1 à 1,528 s au début, derniers PTS jusqu'à 601,938 s ; archive suivante à nouveau
à 1,528 s. Audio initial à 1,400 s dans les deux. Les sommes EXTINF sont 600,420
et 599,820 s, alors que les durées source inspectées antérieurement sont 600 et
599 s. Le code assemblait ces deux horloges remises à zéro avec une discontinuité,
puis interprétait `get_time()` comme une durée cumulée. Ce mécanisme est un
candidat étayé pour la panne de jonction ; son rôle exact et l'éventuel effet des
paquets source/audio restent à départager par A/B/C ci-dessous.

C2 : après ouverture à 2×, les traces SEEKING passent de 325/93 à 1569/715 images
décodées/affichées, avec progression de la position. La règle de confirmation
fixe ±1 s peut manquer cette fenêtre. Les anciennes traces ne donnent ni la
validité ni la référence initiale des compteurs, donc leur cause exacte reste
partiellement indéterminée. Un changement de cible erroné, une reprise HLS ou un
reset des compteurs ne sont pas exclus par ces seules données.

## Modifications à vérifier

- Les positions des segments proviennent du premier PTS vidéo de chaque segment,
  avec gestion du bouclage 33 bits et conservation des écarts ; EXTINF reste la
  durée annoncée. Un trou réel n'est pas remplacé par une somme continue.
- Le serveur local applique une translation constante des PTS/DTS/PCR/OPCR par
  archive vers une horloge commune. Espacement des paquets et décalage audio/vidéo
  sont conservés. La transformation se fait dans la réponse HTTP, y compris pour
  les requêtes Range ; aucun fichier original ou segment conservé n'est réécrit.
  Les discontinuités entre fichiers restent signalées. Cette approche doit être
  qualifiée avec libVLC et les deux formats réels, notamment les petits
  chevauchements constatés ; elle ne prouve pas une continuité OSD exacte.
- La confirmation exige toujours une progression des deux compteurs. Elle suit
  une arrivée observée ou des positions successives compatibles avec le temps
  écoulé et la vitesse. Une position arbitraire, des compteurs inchangés ou
  invalides ne suffisent pas. Un reset établit une nouvelle référence. Le délai
  d'échec n'a pas été augmenté.
- Une réouverture distingue départ initial, changement de génération pour GOP
  et reprise ENDED. Les commandes de seek portent la génération native ; une
  confirmation retire la demande même si l'interface n'a pas encore interrogé
  son état. La reprise GOP conserve la position de lecture.
- Deux préparations au maximum peuvent se chevaucher. Le fichier suivant est
  anticipé à 180 secondes de lecture minimum, séparément des 2 secondes de
  réserve initiale et du réglage de confort. Sa publication attend la fin de
  préparation du précédent pour ne pas déplacer les positions déjà servies.
  Quota, annulation et pins de cache s'appliquent aux deux. Un EOF temporaire
  attend les segments locaux suivants avant la reprise, au lieu d'annoncer
  immédiatement une fin de couverture.
- Les journaux relient archive/session/playlist/génération/seek, origine,
  position absolue et relative, limites préparées, compteurs et validité,
  vitesse/pause, réponses HTTP locales et erreurs. Le résumé conserve désormais
  aussi `error`, y compris les erreurs historiques sans identifiant de session,
  dont l'attribution est explicitement marquée comme déduite.

Les fichiers à métadonnées finales gardent leur repli explicite. Aucun pont
RTSP/cache ni accès aléatoire à une position lointaine n'est ajouté. Le correctif
ne peut donc pas promettre un départ immédiat loin dans un téléchargement
séquentiel. Les réglages réseau, codecs et horloges des caméras sont inchangés.

Les options HLS de référence sont décrites dans la
[documentation FFmpeg](https://ffmpeg.org/ffmpeg-formats.html#hls-2). La présence
d'une discontinuité dans une playlist ne suffit pas à valider sa lecture réelle.

## 1. Régressions sans média natif

PowerShell, uniquement quand disponible :

```powershell
Set-Location 'C:\Users\thetitan.TITANDARK\OneDrive\Documents\Informatique\Projet\Camera'
$Python = 'C:\Users\thetitan.TITANDARK\AppData\Local\Programs\Python\Python39\python.exe'
& $Python -m unittest discover -s tests -p 'test_playback*.py' -v
```

84 méthodes préparées au total, dont 15 nouvelles sur jonctions, horloges,
Range, confirmation, chronologie et anticipation concurrente. **Non exécutées
par l'agent.** Les 44 succès précédents ne qualifient ni ce correctif ni les 25
tests progressifs préparés lors de l'intervention précédente.

## 2. Deux archives C1 réellement déjà téléchargées

Le cache observé contient deux fichiers consécutifs C1. C2 ne contient qu'un
fichier téléchargé : aucune jonction C2 complète ne peut encore être déclarée
testable hors caméra. Le banc exige deux fichiers et refuse de les inventer.

Copier les deux originaux C1 dans un **nouveau** banc, puis préparer chacun
séparément (ces commandes utilisent le disque et FFmpeg/ffprobe, sans lecteur) :

```powershell
$Bench = Join-Path $env:LOCALAPPDATA ('CameraManagementSystem\boundary-C1-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
& $Python '.\tools\playback_boundary_bench.py' --directory $Bench --camera 1 --from-cache
& $Python '.\tools\playback_boundary_bench.py' --directory $Bench --camera 1 --warm
$Vlc = 'C:\Program Files\VideoLAN\VLC\vlc.exe'
```

**A — Chaque original seul.** Ces commandes ouvrent VLC ; les lancer uniquement
quand cela convient, une après l'autre, en fermant le lecteur entre les deux.
Le son est coupé au départ ; pour l'écoute, relancer volontairement la même
commande sans `--no-audio`. Observer OSD, dernières/premières images et éventuel trou réel.

```powershell
& $Vlc --no-one-instance --no-audio --rate=1 --start-time=580 (Join-Path $Bench 'archive-A.bin')
# Fermer A avant de lancer B.
& $Vlc --no-one-instance --no-audio --rate=1 (Join-Path $Bench 'archive-B.bin')
```

Lire `boundary-audit-*.json` dans le banc : durée, PTS source, première/dernière
position de segment et bornes préparées. Un `source_packet_audit_error` non vide
laisse l'audit des paquets source indéterminé ; ne pas déclarer l'alignement
qualifié dans ce cas. Un probe réussi ne remplace pas la vérification visuelle
des deux originaux.

**B — Les deux via le serveur HLS local, sans contrôleur intégré.**
Dans une première console, démarrer le serveur et laisser la commande tourner :

```powershell
& $Python '.\tools\playback_boundary_bench.py' --directory $Bench --camera 1 --serve --before 20
```

Le serveur affiche une URL locale opaque et la position de départ relative.
Dans une seconde console, affecter **l'URL affichée** à `$Url`, puis ouvrir
volontairement VLC. Pour les fichiers C1 présents, le départ est à 580 s :

```powershell
$Vlc = 'C:\Program Files\VideoLAN\VLC\vlc.exe'
$Url = Read-Host 'Coller uniquement l URL locale affichee par le banc'
& $Vlc --no-one-instance --no-audio --rate=1 --start-time=580 $Url
```

À 1×, laisser passer la jonction sans pause, seek ou changement de vitesse et
observer au moins 30 s après. Noter l'OSD réel et l'audio lorsqu'activé. Fermer VLC,
puis Ctrl+C dans la console du banc. Refaire B avec `--legacy-clocks` ajouté à
`--serve` : cela conserve les horloges TS initiales pour isoler l'effet de leur
translation. Ce témoin ne reproduit pas à lui seul tous les comportements de
l'ancien contrôleur. Ne pas partager l'URL opaque.

**C — Lecteur intégré, les deux archives préchauffées.** Après arrêt du serveur B :

```powershell
& $Python '.\camera_playback.py' --fixture-directory $Bench --camera 1 --date 2026-09-19 --day-only
```

Choisir **2026-09-19 11:59:40 America/Toronto**, vitesse **1×**, puis laisser
franchir **12:00:00** sans interaction jusqu'à **12:00:30** minimum. La copie du
cache conserve les dates des deux originaux. Si un autre couple a été sélectionné
dans un cache ultérieurement enrichi, utiliser les dates de `fixture.json` et
viser 20 secondes avant son second fichier. Une seule génération native est
attendue à cette jonction préchauffée ; noter toute réouverture et son origine.

Fermer Recordings puis extraire le rapport :

```powershell
& $Python '.\tools\summarize_playback_events.py' --log (Join-Path $Bench 'cache\events.jsonl') --camera 1 |
    Set-Content -LiteralPath (Join-Path $Bench 'report-C1.json') -Encoding UTF8
```

## 3. Transfert ralenti, puis caméras réelles

Créer un nouveau banc synthétique : deux archives natives A/B de 180 secondes
par caméra, compteurs incrustés, son de test présent mais jamais joué par le
générateur. Ce dernier peut utiliser CPU/disque : le lancer au moment choisi.

```powershell
$Slow = Join-Path $env:LOCALAPPDATA ('CameraManagementSystem\boundary-slow-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
& $Python '.\tools\create_progressive_fixture.py' --directory $Slow
& $Python '.\camera_playback.py' --fixture-directory $Slow --camera 901 --day-only
```

C901 MPEG-TS, C902 GOP longs, C903 MP4 métadonnées finales, C904 MP4 faststart.
Fermer chaque essai avant de passer au suivant. Pour le départ proche choisir
12:00:02 ; appuyer F8 à la première image réellement vue. Pour le départ lointain
choisir 12:02:40 dans un **autre banc neuf**, afin de garder les mesures à froid.
Observer la jonction à 12:03:00 à 1× sans geste. Le transfert ajoute 0,3 s par
256 Kio. Réutiliser `--warm` puis `--serve --before 20` pour refaire A/B/C sur
deux fichiers synthétiques préchauffés (départ VLC alors à 160 s).

Sur les vraies caméras, privilégier d'abord une position proche du début d'une
archive encore absente du cache. Ne pas effacer les archives d'incident pour
forcer le froid. Lancer séparément, à la date voulue :

```powershell
& $Python '.\camera_playback.py' --camera 1 --date 2026-09-19 --day-only
# Fermer la fenêtre précédente avant C2.
& $Python '.\camera_playback.py' --camera 2 --date 2026-09-19 --day-only
```

Ces commandes contactent les caméras lorsqu'une recherche ou un transfert est
demandé. Pour C2, préparer ensuite deux fichiers consécutifs avant le témoin
de jonction en cache. Répéter `--from-cache --camera 2` vers un banc neuf.

```powershell
$Log = Join-Path $env:LOCALAPPDATA 'CameraManagementSystem\playback\events.jsonl'
& $Python '.\tools\summarize_playback_events.py' --log $Log --camera 1
& $Python '.\tools\summarize_playback_events.py' --log $Log --camera 2
```

## Résultat attendu et comparaison

| Scénario | Avant (preuves disponibles) | Après |
|---|---|---|
| C1 distant MP4 | segment 2,28 s, image confirmée 27,95 s | à mesurer |
| C2 distant MPEG | segment 1,84 s, confirmation échouée | à mesurer |
| C1 jonction cache | gel / temps natif retournant près de zéro à 2× | A/B/C à 1× à mesurer |
| C2 deux fichiers en cache | second fichier absent | à préparer par l'utilisateur |
| Proche, transfert ralenti | aucun résultat de qualification fourni | à mesurer par format |

Succès de jonction : OSD et audio cohérents, pas de retour au début ni d'écran
d'erreur, positions absolues/relatives cohérentes, données suivantes publiées
avant la jonction, réponses locales 200/206, aucun seek utilisateur inventé.
S'il manque temporairement les données, l'état doit annoncer l'attente puis
reprendre à leur arrivée. L'étiquette de cache ne certifie jamais la préparation
du fichier suivant. La PR reste en brouillon et l'issue ouverte jusqu'à ces
vérifications ; aucun merge ni publication dans main n'est autorisé.
