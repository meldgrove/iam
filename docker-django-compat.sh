#!/bin/sh
# Installed as `python`: runs what the compose files of the Django version
# (0.1.1 to 0.2, compose.yaml and compose.coolify.yaml) ask of the image. The
# updater installs images, not files, so a server it upgrades starts this
# image with the Django compose.yaml until the operator replaces it
# (docs/deployment.md, Upgrade from the Django version). Only those files'
# exact commands run: anything else is refused, since `manage.py migrate
# --plan`, for example, would migrate.
set -e
web_check="import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/-/healthz', timeout=3)"
worker_check="import os,sys,tempfile,time; sys.exit(time.time() - os.path.getmtime(os.path.join(tempfile.gettempdir(), 'iam-worker-alive')) > 60)"
if [ "$#" -eq 3 ] && [ "$1" = manage.py ] && [ "$2" = migrate ] && [ "$3" = --noinput ]; then
  exec docker-entrypoint.sh migrate
elif [ "$#" -eq 2 ] && [ "$1" = manage.py ] && [ "$2" = run_worker ]; then
  exec docker-entrypoint.sh worker
elif [ "$#" -eq 2 ] && [ "$1" = -c ] && [ "$2" = "$web_check" ]; then
  # The health checks of compose.yaml.
  exec node -e "fetch('http://127.0.0.1:8000/-/healthz',{signal:AbortSignal.timeout(3000)}).then((r)=>process.exit(r.ok?0:1),()=>process.exit(1))"
elif [ "$#" -eq 2 ] && [ "$1" = -c ] && [ "$2" = "$worker_check" ]; then
  exec node -e "process.exit(Date.now()-require('fs').statSync('/tmp/iam-worker-alive').mtimeMs>60000?1:0)"
fi
echo "python: IAM runs on Node.js since 0.3 and has no Python. Replace compose.yaml with the one of this version (docs/deployment.md, Upgrade from the Django version)." >&2
exit 127
