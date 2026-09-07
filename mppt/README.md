# Ancien collecteur déplacé

Le collecteur Bluetooth et son image `ch.bus.victron-mqtt/mppt` ont été retirés.
La collecte Instant Readout appartient à `ch.bus.bluetooth-mqtt`, qui utilise
un scanner unique partagé avec les sondes de température.

Transférer `VICTRON_DEVICES` et les paramètres MQTT vers cette passerelle, arrêter
l'ancien conteneur `victron-ble`, puis démarrer la passerelle. Le projet présent
ne déploie plus que `api/`. Les topics et le contrat HTTP sont conservés.
Consulter `../ch.bus.bluetooth-mqtt/MIGRATION.md` depuis la racine du dépôt.
