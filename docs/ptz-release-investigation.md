# Essai r9 : vitesses PTZ directes

La r8 valid�e reste sur `main` et dans `~v0.2.6`. Cette branche pr�pare un nouvel
essai demand� apr�s l'�tude th�orique. La fluidit� sur cam�ra reste � valider.

## Ce qui change

Le mode exp�rimental utilise `ContinuousMove`, avec des vitesses sign�es par axe,
et non des petits d�placements `RelativeMove` vers des positions successives.
Une diagonale avec zoom devient, au rel�chement de la verticale, une commande
contenant horizontal maintenu, verticale z�ro et zoom maintenu, sans Stop interm�diaire.

- Le d�lai ONVIF natif s�lectionn� en r8 est conserv� (60 secondes annonc�es sur
  la cam�ra essay�e). Il est ind�pendant du rafra�chissement des commandes.
- Les groupes annonc�s par la cam�ra figurent dans chaque commande, m�me � z�ro.
  Si la d�couverte est indisponible, un groupe reste inclus d�s sa premi�re
  utilisation. Un zoom non annonc� et jamais utilis� n'est pas ajout�.
- Les z�ros restent explicites dans les rafra�chissements suivants et apr�s une
  r�cup�ration d'erreur. Ils ne disparaissent plus apr�s une seule mise � jour.
- L'�tat complet est rafra�chi 250 ms apr�s la r�ponse pr�c�dente, ou plus t�t si
  le d�lai natif impose son renouvellement. Un changement de touche est envoy�
  d�s que la requ�te en cours termine, sans attendre ce rafra�chissement.
- Le moteur conserve seulement l'�tat le plus r�cent et s�rialise les requ�tes.
  Aucun Stop/reprise n'est envoy� en parall�le.
- Un rel�chement total, une perte de focus, la fermeture ou un �tat incertain
  apr�s erreur utilisent encore un Stop explicite. Les fen�tres enfants restent
  li�es � leur parent.

La r7 avait combin� des transitions directes avec un d�lai de mouvement court.
Cet essai repart de la s�lection de d�lai r8 et maintient les z�ros dans les
commandes suivantes. Cela fournit une autre exp�rience; ce n'est pas une preuve
de la cause de l'�chec pr�c�dent. Une cam�ra qui ignore syst�matiquement z�ro
ne sera pas corrig�e par la r�p�tition : conserver alors le mode de compatibilit�.

## Lancement

Dans une installation de test de cette branche avec sa base et sa cl� locales :

```powershell
python camera_viewer_direct_test.py
```

Le lanceur active les vitesses directes et la mesure HTTP pour son processus et
ses enfants uniquement. Le titre du PTZ affiche **r9 - Direct velocity test**.
Il n'ajoute aucun panneau de diagnostic. Les fichiers priv�s ne font pas partie
de la PR. La pr�paration locale utilise une copie priv�e de la configuration;
les changements faits dans son gestionnaire ne modifient pas l'installation stable.

Pour comparer au comportement r8, fermer toutes les fen�tres de test puis lancer
`python camera_viewer.py` dans un environnement sans `CAMERA_PTZ_CONSERVATIVE_STOPS=0`.
Le titre r9 affiche alors **Compatibility**. La racine de l'installation stable
continue � lancer la vraie r8 avec son `camera_viewer.py` habituel.

## Essai sur cam�ra

1. Fermer les anciens contr�leurs. Maintenir W + D pendant quelques secondes,
   rel�cher W, puis rel�cher D : la droite doit continuer seule, puis tout s'arr�ter.
2. Maintenir W + D + Shift pendant au moins 5 secondes, rel�cher W, puis Shift,
   puis D. R�p�ter avec Ctrl et dans les quatre diagonales.
3. Passer de d�placement + zoom au zoom seul, puis revenir au d�placement.
4. Alterner rapidement les directions et rel�cher toutes les touches. Aucune
   ancienne direction ne doit reprendre. V�rifier aussi M/N � plusieurs vitesses.
5. Tester perte de focus, �chap et fermeture du Viewer avec ses fen�tres enfants.

En cas de direction bloqu�e, rel�cher toutes les touches pour le Stop explicite,
puis fermer le test. Ne pas retenir le mode direct sur la seule base d'une r�ponse
HTTP r�ussie. Il n'y a pas de d�tection automatique de bonne ex�cution m�canique.

## Mesures

Les journaux locaux `ptz_control_*.log` indiquent r9, le mode, le d�lai natif,
les vitesses s�rialis�es et les dur�es HTTP. Pour une transition partielle r�ussie,
on attend un `soap_move` avec l'axe rel�ch� � z�ro et sans `stop_send` interm�diaire.
Un `stop_send` reste attendu au rel�chement complet.

`headers_seconds` mesure l'attente avant le hook de r�ponse; `after_headers_seconds`
mesure le temps restant jusqu'au retour du transport. Ces mesures incluent Requests,
la connexion et la r�ception; elles ne mesurent pas directement les moteurs.
Les mesures pr�c�dentes de Stop (r6) �taient de 220 � 270 ms.

Le transport de mesure conserve les r�ponses et erreurs. Les journaux sont born�s
et n'incluent pas les identifiants, adresses ou corps SOAP. Un �chec du journal ne
peut pas emp�cher l'arr�t. La mesure est �galement activable s�par�ment avec
`CAMERA_PTZ_TRACE_HTTP=1` en mode de compatibilit�.

## Validation et limites

Tests : `python -m unittest discover -s tests`.
Les simulations couvrent le maintien diagonal + zoom avec Timeout natif,
les z�ros persistants, les r�ponses lentes, les rel�chements, les erreurs, le
mode de compatibilit� et les cam�ras sans zoom annonc�. Une v�rification s�par�e
avec les vraies biblioth�ques ONVIF/Zeep/Requests et un serveur local contr�le le
XML envoy�. Ces v�rifications ne valident pas la fluidit� physique de la cam�ra.

TinyCam a confirm� d�placement lat�ral + zoom optique, mais ses requ�tes exactes
et le rel�chement d'un axe d'une diagonale n'ont pas �t� compar�s. GetStatus n'a
pas fourni de retour de position exploitable lors de l'observation pr�c�dente.

La PR reste en brouillon jusqu'� validation utilisateur. Aucun changement du
comportement par d�faut ni de la version stable n'est propos� sans cette validation.

R�f�rences : [ONVIF PTZ �5.3.3](https://www.onvif.org/specs/srv/ptz/ONVIF-PTZ-Service-Spec.pdf),
[Milestone ONVIF](https://doc.milestonesys.com/mc/pdf/latest/en-US/Milestone_ONVIF_Driver_Documentation.pdf),
[easy_onvif](https://pub.dev/documentation/easy_onvif/latest/index.html),
[hooks Requests](https://requests.readthedocs.io/en/latest/user/advanced/#event-hooks).
