> **Release update — September 12, 2026:** r9 B is now approved for main as
> v0.2.7 and is the default at normal startup. Its release pause remains. C and
> r8 remain withdrawn. The investigation and withdrawal notes below record the
> earlier test stages and decisions before this promotion.

# PTZ r8 retirée de main — 11 septembre 2026

Les essais en conditions réelles ont invalidé la validation initiale de la r8.
La r8 n'est plus une version recommandée.

- En maintenant bas + gauche puis en relâchant bas, le déplacement peut
  s'arrêter entièrement alors que gauche reste appuyée. Il faut réappuyer.
- Le zoom optique combiné au déplacement peut s'interrompre par saccades.

Signalement sur une VIKYLIN PTZ-4518X-IS2 via ONVIF. Le périmètre sur les autres
caméras reste inconnu. Les réponses HTTP réussies et les tests simulés ne
prouvent pas le comportement mécanique. Aucun correctif r8 n'est entrepris ici.

`main` revient au code public précédant la PR #3, commit `d0336f3`, avec sa clé
de chiffrement toujours chargée depuis un fichier privé. Cette base conserve
ses limites clavier historiques : ce retour ne constitue pas un nouveau
correctif des combinaisons PTZ, ni une certification de compatibilité.

La r8 est conservée comme candidate en brouillon. La r9 est une expérience
distincte dans la PR #4, également non validée. Son premier essai envoyait bien
zéro pour l'axe relâché, mais la diagonale continuait selon l'observation caméra.

Avant de changer de version, fermer les anciens contrôleurs. Conserver ensemble
`camera_credentials.db` et `.camera_encryption.key`, sans les publier. Une base
ancienne requiert sa clé existante : ne pas en générer une autre pour la remplacer.

Suivi : [régressions et critères de validation, issue #7](https://github.com/THET1TAN/camera-management-system/issues/7),
[expérience r9, PR #4](https://github.com/THET1TAN/camera-management-system/pull/4).
