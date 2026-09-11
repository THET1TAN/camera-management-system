# r9 — essai B : transition par vitesse nulle

**Expérimental ; premier essai caméra positif, validation prolongée à poursuivre.** La r8 a été retirée de main après les
régressions terrain de l’[issue #7](https://github.com/THET1TAN/camera-management-system/issues/7).
Elle est conservée sans correctif dans la [PR #8](https://github.com/THET1TAN/camera-management-system/pull/8).
Main revient à la base publique précédant la PR #3, avec sa clé privée externalisée.
Cette PR #4 développe la r9 séparément ; elle ne valide ni ne réintègre la r8.

## Observation et recherches

Le 11 septembre, l’utilisateur observe une diagonale persistante après relâchement
d’un axe. Le journal montre pourtant l’entrée (0.5, 0, 0), puis plusieurs commandes
ContinuousMove sérialisées avec la verticale à zéro et une réponse HTTP 200.
Les réponses durent généralement 36 à 70 ms dans cet extrait ; le Stop final,
environ 236 ms. L’entrée est détectée mais l’observation mécanique ne suit pas
la commande. Cela ne prouve pas à lui seul la cause exacte dans le firmware.

[ONVIF PTZ §5.3.3](https://www.onvif.org/specs/srv/ptz/ONVIF-PTZ-Service-Spec.pdf)
définit une vitesse signée par axe : zéro doit arrêter l’axe concerné ; omettre
un groupe laisse son mouvement inchangé. Le joystick est cité comme usage de
ContinuousMove. RelativeMove indique une translation, pas une vitesse continue.
Le protocole ne garantit pas le délai de réponse mécanique des appareils.

[easy_onvif 3.1.3](https://pub.dev/documentation/easy_onvif/latest/index.html)
remplace son arrêt par ContinuousMove à vitesse nulle et rapporte une meilleure
compatibilité. La [documentation Milestone ONVIF](https://doc.milestonesys.com/mc/pdf/latest/en-US/Milestone_ONVIF_Driver_Documentation.pdf)
décrit aussi l’arrêt par vitesse nulle. Cela justifie un essai, sans démontrer que
les zéros partiels fonctionnent sur cette caméra. Les requêtes exactes de TinyCam
restent inconnues ; son zoom optique simultané est confirmé par l’utilisateur.

## Hypothèse de l’essai B

Un vecteur entièrement nul pourrait être accepté là où un zéro mélangé à des
axes actifs est ignoré. Aucun nom de fabricant ne sélectionne ce comportement.

- Lors du relâchement ou de l’inversion d’un axe, un vecteur entièrement nul est
  envoyé. Après sa réponse, le moteur relit l’entrée et réapplique ensemble tous
  les axes encore maintenus, y compris le zoom inchangé. Aucune reprise périmée.
- Les changements de vitesse de même signe et l’ajout d’un axe restent directs.
  L’entrée normalisée entre -1 et 1 et les plages ONVIF annoncées préparent les
  vitesses analogiques. Aucun contrôleur de jeu n’est implémenté ici.
- Les groupes annoncés ou déjà utilisés restent explicites, même à zéro. Aucun
  zoom non annoncé et jamais utilisé n’est ajouté.
- Les commandes identiques ne sont plus répétées toutes les 250 ms dans l’essai B.
  Le renouvellement se fait au tiers du délai natif : 20 s pour les 60 s annoncées
  ici. Sans délai exploitable, le secours reste à 250 ms. Les changements d’entrée
  passent dès la fin de la requête en cours, sans attendre cette échéance.
- Une seule requête à la fois, uniquement l’état désiré le plus récent. Le
  relâchement total, l’expiration du signal clavier, la perte de focus et la
  fermeture gardent un Stop explicite, même pendant la réponse au neutre.
  Une réponse perdue impose un Stop avant reprise. Les enfants ferment avec le Viewer.

Deux requêtes successives restent nécessaires au relâchement partiel : une pause
peut persister. La caméra peut aussi ignorer le vecteur entièrement nul. Aucun
succès HTTP ne vaut validation mécanique ni activation automatique du mode.
Réduire les répétitions teste aussi l’hypothèse qu’elles contribuent aux saccades
du zoom ; cette cause n’est pas encore établie.

## Lancement

Dans une installation de test avec sa copie privée de la base et de la clé :

```powershell
python camera_viewer_direct_test.py
```

Le titre affiche **r9 - Neutral transition test B**. Le lanceur active
CAMERA_PTZ_NEUTRAL_TRANSITIONS=1 uniquement pour son processus et ses enfants.
Les enfants utilisent l’interpréteur du Viewer, évitant le mélange Python 3.9 /
3.14 qui empêchait précédemment le PTZ de démarrer. Aucun panneau de diagnostic.
La base de test est distincte de la base restaurée à la racine.

Pour reproduire l’ancien essai direct dans un terminal de test séparé :

```powershell
$env:CAMERA_PTZ_NEUTRAL_TRANSITIONS = '0'
$env:CAMERA_PTZ_CONSERVATIVE_STOPS = '0'
python camera_viewer.py
```

Avec le neutre désactivé, CAMERA_PTZ_CONSERVATIVE_STOPS=1 garde l’ancien mode
Stop/reprise pour comparaison. Il conserve les défauts r8 et n’est pas une
solution validée. Fermer les autres contrôleurs avant chaque essai.

## Essai physique à effectuer

1. Maintenir S + A, relâcher S : seule la gauche doit continuer. Relâcher A.
   Répéter avec les quatre diagonales et chaque axe.
2. Maintenir une diagonale + Shift puis Ctrl au moins 5 secondes, avant la butée.
   Relâcher un axe, puis le zoom, puis le dernier axe. Répéter les diagonales.
3. Passer au zoom seul puis ajouter un déplacement. Alterner rapidement les
   directions et tester plusieurs vitesses M/N. Aucune direction ancienne ne
   doit reprendre après relâchement total.
4. Maintenir au-delà du renouvellement natif, puis vérifier perte de focus,
   Échap et fermeture du Viewer avec ses fenêtres enfants.

En cas de direction bloquée, relâcher toutes les touches pour le Stop explicite.
Noter séparément les axes, la pause et la continuité du zoom. GetStatus n’a pas
fourni de position exploitable précédemment ; l’image doit être observée.

## Vérifications réalisées

`python -m unittest discover -s tests` : **144 tests réussis**. Les nouveaux cas
couvrent les zéros partiels ignorés, la réapplication du zoom, les changements
pendant une réponse, le relâchement total, l’expiration, la fermeture, les erreurs,
les presets et les variations analogiques de même signe. Un test garde le cas
d’une caméra ignorant tous les zéros : il n’est pas présenté comme corrigé.

Les vraies bibliothèques ONVIF/Zeep/Requests ont transmis sur un serveur local
7 vecteurs complets avec espaces annoncés et Timeout PT1M, puis un Stop final.
Les changements pendant une réponse retardée utilisent le dernier état.
Le démarrage et la fermeture Tk masqués sont vérifiés. La connexion réelle à la
caméra atteint la création de la fenêtre, sans mouvement dans cette vérification.
Retour utilisateur du 11 septembre : relâchements corrects et déplacement avec
zoom simultané confirmés ; une pause au relâchement demeure. L’utilisateur décrit
l’essai B comme le meilleur candidat à ce stade. Les essais prolongés et sur
d’autres caméras restent nécessaires ; aucune intégration à main n’est autorisée.

Les journaux locaux bornés indiquent neutral_transitions, transition_neutral,
les vitesses et durées HTTP, sans identifiants, adresses ni corps SOAP.
La PR reste en brouillon jusqu’à validation des essais physiques.

## Mesures du premier essai B sur la caméra

Le journal confirme le mode B, le délai natif de 60 secondes et aucune erreur de
commande sur la session analysée. Les 11 réponses au vecteur nul prennent de
246 à 331 ms (médiane 272 ms) ; les 21 mouvements non nuls prennent de 30 à 89 ms
(médiane 57 ms). La dernière commande acceptée est un Stop.
Ces délais de réponse expliquent une attente dans l’enchaînement sérialisé ;
ils ne mesurent pas directement la pause mécanique. Le vecteur nul ne s’est donc
pas révélé aussi rapide que les mouvements ordinaires sur cet essai. Le prochain
travail sur la pause devra conserver les relâchements et le zoom maintenant
confirmés, sans conclure que des requêtes concurrentes seraient sûres.
