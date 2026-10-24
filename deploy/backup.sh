#!/usr/bin/env bash
# Nightly Neo4j dump for the docker-compose server, e.g. from cron:
#   15 3 * * *  cd /opt/jejak/deploy && ./backup.sh >> backup.log 2>&1
# Neo4j Community can only dump a stopped database, so Neo4j pauses for the
# dump (usually seconds). Clients queue their saves meanwhile and resend them.
set -euo pipefail
cd "$(dirname "$0")"
KEEP_DAYS="${KEEP_DAYS:-14}"
mkdir -p backups
stamp=$(date +%Y%m%d-%H%M%S)
docker compose stop neo4j
trap 'docker compose start neo4j' EXIT
docker compose run --rm --no-deps -v "$PWD/backups:/backups" --entrypoint neo4j-admin neo4j \
  database dump neo4j --to-path=/backups --overwrite-destination=true
mv backups/neo4j.dump "backups/neo4j-$stamp.dump"
chmod 600 "backups/neo4j-$stamp.dump"
find backups -name 'neo4j-*.dump' -mtime +"$KEEP_DAYS" -delete
echo "backup ok: backups/neo4j-$stamp.dump"
