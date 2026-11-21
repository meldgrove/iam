#!/bin/sh
# Installed as `python`: runs what the compose files of the Django version
# (0.1.1 to 0.2) ask of the image. The updater installs images, not files, so
# a server it upgrades starts this image with the Django compose.yaml until
# the operator replaces it (docs/deployment.md, Upgrade from the Django
# version). That file runs:
#   python manage.py migrate --noinput   service migrate
#   python manage.py run_worker          service worker
#   python -c "...healthz..."            health check of web
#   python -c "...iam-worker-alive..."   health check of worker
# The two checks are the ones of compose.yaml.
set -e
if [ "$1" = manage.py ]; then
  case "$2" in
    migrate) exec docker-entrypoint.sh migrate ;;
    run_worker) exec docker-entrypoint.sh worker ;;
  esac
elif [ "$1" = -c ]; then
  case "$2" in
    *iam-worker-alive*)
      exec node -e "process.exit(Date.now()-require('fs').statSync('/tmp/iam-worker-alive').mtimeMs>60000?1:0)"
      ;;
    *127.0.0.1:8000/-/healthz*)
      exec node -e "fetch('http://127.0.0.1:8000/-/healthz',{signal:AbortSignal.timeout(3000)}).then((r)=>process.exit(r.ok?0:1),()=>process.exit(1))"
      ;;
  esac
fi
echo "python: IAM runs on Node.js since 0.3 and has no Python. Replace compose.yaml with the one of this version (docs/deployment.md, Upgrade from the Django version)." >&2
exit 127
