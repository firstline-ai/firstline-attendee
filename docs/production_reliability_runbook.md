# Runbook de confiabilidade em produção

Este runbook descreve como publicar, validar e reverter uma release do
Attendee com gravação de áudio pós-reunião. Ele cobre somente o repositório e os
serviços do Attendee.

## Escopo e invariantes

Os serviços de aplicação que podem ser recriados por este procedimento são:

- `attendee-app`
- `attendee-worker`
- `attendee-launcher-worker`
- `attendee-webhook-worker`
- `attendee-scheduler`

Os serviços abaixo mantêm estado e **nunca** devem ser incluídos no comando de
deploy ou rollback:

- `postgres`
- `redis`
- `minio`

Também são invariantes:

- executar o deploy apenas com autorização explícita;
- não iniciar o deploy enquanto houver bot efêmero ativo;
- usar a mesma revisão Git na imagem, no diretório de código e nos bots
  efêmeros;
- manter configuração e código separados: `RELEASE_DIR` contém Compose e
  `.env*`; `CODE_DIR` contém somente o checkout, é `root:root` e não tem
  permissão de escrita;
- usar uma tag de imagem exclusiva por commit e nunca sobrescrevê-la;
- não executar migration sem plano, backup e autorização específicos;
- nunca usar `docker compose down`, `docker system prune`,
  `--renew-anon-volumes` ou `-V` durante deploy ou rollback;
- nunca apagar um arquivo não vazio do spool durante um incidente;
- preservar a release e a imagem anteriores até o piloto terminar;
- não usar `set -x` em comandos que carregam arquivos de ambiente.

O Compose de produção usa `network_mode: host`. PostgreSQL e Redis ficam em
`127.0.0.1`; o acesso externo deve continuar bloqueado por firewall.

### Risco conhecido de persistência do Redis

O Compose declara `redis_data:/data/redis`, mas a imagem Redis grava em `/data`
por padrão. Portanto, não presuma que o volume nomeado `redis_data` contém o
`dump.rdb`; a persistência real pode estar num volume anônimo montado em
`/data`.

Valide somente por leitura antes da janela:

```bash
docker inspect attendee-redis-1 \
  --format '{{range .Mounts}}{{.Type}}|{{.Name}}|{{.Source}}|{{.Destination}}{{println}}{{end}}'

REDIS_DIR_OUTPUT="$(
  docker exec attendee-redis-1 redis-cli --raw CONFIG GET dir
)"
printf '%s\n' "$REDIS_DIR_OUTPUT"
grep -Fxq '/data' <<<"$REDIS_DIR_OUTPUT"
```

Esta janela usa `--no-deps` e não corrige o layout. Nunca recrie Redis aqui. A
correção exige follow-up separado, com backup do volume efetivo, teste de
restore, alteração de `dir`/mount e migração controlada.

## Convenções

Defina os valores abaixo na estação operacional. Eles não são credenciais:

```bash
export ATTENDEE_HOST="root@<host>"
export ATTENDEE_SSH_KEY="<caminho-da-chave-ssh>"
export ATTENDEE_GIT_WORKTREE="<worktree-limpa-do-firstline-attendee>"
export COMMIT="<sha-completo-aprovado>"
export EXPECTED_PREVIOUS_COMMIT="<sha-esperado-da-release-atual>"
export SHORT_COMMIT="${COMMIT:0:12}"
export IMAGE="firstline-attendee:recording-only-${SHORT_COMMIT}"
export RELEASE_DIR="/home/deploy/apps/attendee-release-${SHORT_COMMIT}"
export CODE_DIR="/home/deploy/apps/attendee-code-${SHORT_COMMIT}"
```

No host, derive a release ativa dos labels reais dos cinco containers. Não
aceite `CURRENT_RELEASE_DIR` digitado manualmente:

```bash
set -euo pipefail

APPLICATION_CONTAINERS=(
  attendee-attendee-app-1
  attendee-attendee-worker-1
  attendee-attendee-launcher-worker-1
  attendee-attendee-webhook-worker-1
  attendee-attendee-scheduler-1
)

for container in "${APPLICATION_CONTAINERS[@]}"; do
  test "$(docker inspect "$container" \
    --format '{{.State.Running}}')" = "true"
done

CURRENT_WORKING_DIRS_OUTPUT="$(
  for container in "${APPLICATION_CONTAINERS[@]}"; do
    docker inspect "$container" \
      --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}'
  done
)"
mapfile -t CURRENT_WORKING_DIRS < <(
  sort -u <<<"$CURRENT_WORKING_DIRS_OUTPUT"
)
test "${#CURRENT_WORKING_DIRS[@]}" -eq 1
CURRENT_RELEASE_DIR="${CURRENT_WORKING_DIRS[0]}"

test -d "$CURRENT_RELEASE_DIR"
test -f "$CURRENT_RELEASE_DIR/docker-compose.prod.yml"
test -f "$CURRENT_RELEASE_DIR/.env.release"
test -e "$CURRENT_RELEASE_DIR/.env.prod"

CURRENT_CODE_DIR="$(awk -F= '$1 == "ATTENDEE_HOST_CODE_PATH" {
  print substr($0, index($0, "=") + 1)
}' "$CURRENT_RELEASE_DIR/.env.release")"
test -n "$CURRENT_CODE_DIR"

CURRENT_STATIC_SOURCE="$(docker inspect attendee-attendee-app-1 \
  --format '{{range .Mounts}}{{if eq .Destination "/attendee/staticfiles"}}{{.Source}}{{end}}{{end}}')"
test "$CURRENT_STATIC_SOURCE" = "$CURRENT_CODE_DIR/staticfiles"
```

Os blocos identificados como “no host” devem ser executados numa sessão SSH
como `root`, depois de repetir somente as variáveis operacionais não secretas:

```bash
ssh -i "$ATTENDEE_SSH_KEY" "$ATTENDEE_HOST"

set -euo pipefail
export COMMIT="<sha-completo-aprovado>"
export EXPECTED_PREVIOUS_COMMIT="<sha-esperado-da-release-atual>"
export SHORT_COMMIT="${COMMIT:0:12}"
export IMAGE="firstline-attendee:recording-only-${SHORT_COMMIT}"
export RELEASE_DIR="/home/deploy/apps/attendee-release-${SHORT_COMMIT}"
export CODE_DIR="/home/deploy/apps/attendee-code-${SHORT_COMMIT}"
```

Não exporte tokens ou credenciais manualmente. Eles permanecem somente no
arquivo `.env.prod` protegido.

`ATTENDEE_HOST_CODE_PATH` deve apontar para `CODE_DIR`, nunca para
`RELEASE_DIR`. Isso evita montar `.env.prod` dentro dos bots efêmeros.

### Arquivo canônico de secrets

Novas releases e rollbacks usam um único arquivo:
`/home/deploy/secrets/attendee/.env.prod`. Faça bootstrap idempotente a partir
da release ativa, sem imprimir seu conteúdo:

```bash
CANONICAL_ENV="/home/deploy/secrets/attendee/.env.prod"
install -d -m 0700 -o deploy -g deploy \
  /home/deploy/secrets/attendee

if test ! -e "$CANONICAL_ENV"; then
  install -m 0600 -o deploy -g deploy \
    "$CURRENT_RELEASE_DIR/.env.prod" \
    "$CANONICAL_ENV"
else
  test -f "$CANONICAL_ENV"
  test ! -L "$CANONICAL_ENV"
  test "$(stat -c '%U:%G' "$CANONICAL_ENV")" = 'deploy:deploy'
  test "$(stat -c '%a' "$CANONICAL_ENV")" = '600'
  cmp -s "$CURRENT_RELEASE_DIR/.env.prod" "$CANONICAL_ENV"
fi
```

Não crie outra cópia em `RELEASE_DIR`: use symlink absoluto para o canônico.
Não remova as cópias históricas neste runbook. Limpeza e rotação são uma fase
separada, explicitamente aprovada.

### Proveniência obrigatória do rollback

Antes de qualquer recriação, derive a imagem de rollback dos cinco containers
em execução. Não aceite uma tag informada manualmente:

```bash
PREVIOUS_COMMIT="$(cat "$CURRENT_RELEASE_DIR/.release-commit")"
test "$PREVIOUS_COMMIT" = "$EXPECTED_PREVIOUS_COMMIT"

PREVIOUS_IMAGE_REFS_OUTPUT="$(
  for container in "${APPLICATION_CONTAINERS[@]}"; do
    docker inspect "$container" --format '{{.Config.Image}}'
  done
)"
mapfile -t PREVIOUS_IMAGE_REFS < <(
  sort -u <<<"$PREVIOUS_IMAGE_REFS_OUTPUT"
)
test "${#PREVIOUS_IMAGE_REFS[@]}" -eq 1
PREVIOUS_IMAGE_REF="${PREVIOUS_IMAGE_REFS[0]}"

PREVIOUS_IMAGE_IDS_OUTPUT="$(
  for container in "${APPLICATION_CONTAINERS[@]}"; do
    docker inspect "$container" --format '{{.Image}}'
  done
)"
mapfile -t PREVIOUS_IMAGE_IDS < <(
  sort -u <<<"$PREVIOUS_IMAGE_IDS_OUTPUT"
)
test "${#PREVIOUS_IMAGE_IDS[@]}" -eq 1
PREVIOUS_IMAGE_ID="${PREVIOUS_IMAGE_IDS[0]}"

test "$(docker image inspect "$PREVIOUS_IMAGE_REF" \
  --format '{{.Id}}')" = "$PREVIOUS_IMAGE_ID"

PREVIOUS_IMAGE_REVISION="$(docker image inspect "$PREVIOUS_IMAGE_ID" \
  --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')"

test -n "$PREVIOUS_IMAGE_REVISION"
test "$PREVIOUS_IMAGE_REVISION" != '<no value>'
test "$PREVIOUS_IMAGE_REVISION" = "$PREVIOUS_COMMIT"

ROLLBACK_IMAGE="$PREVIOUS_IMAGE_REF"
ROLLBACK_IMAGE_ID="$PREVIOUS_IMAGE_ID"
```

Se a imagem ativa não tiver o label de revisão ou ele divergir de
`.release-commit`, interrompa a release. Uma imagem reconstruída ou escolhida
manualmente não substitui essa cadeia de proveniência; faça primeiro um
bootstrap controlado e revisado.

## 1. Gate de código

Operações GitHub em repositório privado devem rodar como o usuário `codex`.
Parta de uma worktree limpa da revisão já aprovada:

```bash
sudo -H -u codex bash -lc '
set -euo pipefail
cd "'"$ATTENDEE_GIT_WORKTREE"'"
test "$(git rev-parse HEAD)" = "'"$COMMIT"'"
git diff --quiet
git diff --cached --quiet
test -z "$(git status --porcelain)"
git status --short --branch
'
```

Revise os arquivos alterados e procure migrations:

```bash
sudo -H -u codex bash -lc '
set -euo pipefail
cd "'"$ATTENDEE_GIT_WORKTREE"'"
git diff --name-status "'"$EXPECTED_PREVIOUS_COMMIT..$COMMIT"'"
git diff --name-only "'"$EXPECTED_PREVIOUS_COMMIT..$COMMIT"'" -- "*/migrations/*"
'
```

Se a segunda saída não for vazia, interrompa este runbook. Migration exige um
plano separado que documente compatibilidade de rollback e impacto estrutural.

A revisão também precisa aplicar a política genérica de ambiente no
`.dockerignore`:

```dockerignore
.env*
!.env.example
!.env.prod.example
```

Valide as três regras literalmente:

```bash
sudo -H -u codex bash -lc '
set -euo pipefail
cd "'"$ATTENDEE_GIT_WORKTREE"'"
grep -Fxq ".env*" .dockerignore
grep -Fxq "!.env.example" .dockerignore
grep -Fxq "!.env.prod.example" .dockerignore
'
```

Somente exemplos redigidos podem ser incluídos. Um novo arquivo como
`.env.qa`, `.env.local`, `.env.release` ou `.env.prod` deve ser excluído
automaticamente sem depender da atualização manual de uma lista.

Só considere testes aprovados quando os comandos tiverem sido executados
novamente na revisão exata que será publicada.

## 2. Gate operacional

Daqui até o deploy, mantenha a mesma sessão `root` no host. Adquira um lock
para impedir dois operadores simultâneos:

```bash
set -euo pipefail
exec 9>/run/lock/attendee-deploy.lock
flock -n 9
```

Confirme HTTP público, migrations e espaço em disco:

```bash
test "$(curl -fsS -o /dev/null -w '%{http_code}' \
  https://bots.firstlineai.com.br/health/)" = "200"

docker exec attendee-attendee-app-1 python manage.py migrate --plan

df -h /
df -Pk / | awk 'NR == 2 { exit !($4 > 10485760) }'
```

O readiness do Celery deve ler o hostname dentro de cada container, construir o
nodename `%n@%h` real e dirigir todas as inspeções a ele. Não derive `%h` do
host e não aceite ping broadcast. Separe readiness (`ping` + `active_queues`)
do gate pré-cutover (`active`, `reserved` e `scheduled` vazios). Capture a
saída completa antes de analisá-la: pipelines como
`celery ... | grep -q` podem encerrar o produtor com `SIGPIPE` sob
`set -o pipefail` e gerar falso negativo.

```bash
check_celery_readiness() {
  local container="$1"
  local node_prefix="$2"
  local queue="$3"
  local container_hostname
  local node
  local ping_output
  local queues_output

  if ! container_hostname="$(docker exec "$container" hostname 2>&1)"; then
    printf '%s\n' "$container_hostname"
    return 1
  fi
  if test -z "$container_hostname"; then
    echo "ERRO: hostname vazio em $container" >&2
    return 1
  fi
  node="${node_prefix}@${container_hostname}"

  if ! ping_output="$(docker exec "$container" \
    celery -A attendee inspect ping \
    --destination "$node" --timeout=5 2>&1)"; then
    printf '%s\n' "$ping_output"
    return 1
  fi
  printf '%s\n' "$ping_output"
  if ! grep -Fq "$node: OK" <<<"$ping_output"; then
    return 1
  fi
  if ! grep -Fq 'pong' <<<"$ping_output"; then
    return 1
  fi

  if ! queues_output="$(docker exec "$container" \
    celery -A attendee inspect active_queues \
    --destination "$node" --timeout=5 2>&1)"; then
    printf '%s\n' "$queues_output"
    return 1
  fi
  printf '%s\n' "$queues_output"
  if ! grep -Fq "$queue" <<<"$queues_output"; then
    return 1
  fi

  return 0
}

check_celery_idle_gate() {
  local container="$1"
  local node_prefix="$2"
  local queue="$3"
  local container_hostname
  local node
  local active_output
  local reserved_output
  local scheduled_output

  if ! check_celery_readiness "$container" "$node_prefix" "$queue"; then
    return 1
  fi

  if ! container_hostname="$(docker exec "$container" hostname 2>&1)"; then
    printf '%s\n' "$container_hostname"
    return 1
  fi
  if test -z "$container_hostname"; then
    return 1
  fi
  node="${node_prefix}@${container_hostname}"

  if ! active_output="$(docker exec "$container" \
    celery -A attendee inspect active \
    --destination "$node" --timeout=5 2>&1)"; then
    printf '%s\n' "$active_output"
    return 1
  fi
  printf '%s\n' "$active_output"
  if ! grep -Fq -- '- empty -' <<<"$active_output"; then
    return 1
  fi

  if ! reserved_output="$(docker exec "$container" \
    celery -A attendee inspect reserved \
    --destination "$node" --timeout=5 2>&1)"; then
    printf '%s\n' "$reserved_output"
    return 1
  fi
  printf '%s\n' "$reserved_output"
  if ! grep -Fq -- '- empty -' <<<"$reserved_output"; then
    return 1
  fi

  if ! scheduled_output="$(docker exec "$container" \
    celery -A attendee inspect scheduled \
    --destination "$node" --timeout=5 2>&1)"; then
    printf '%s\n' "$scheduled_output"
    return 1
  fi
  printf '%s\n' "$scheduled_output"
  if ! grep -Fq -- '- empty -' <<<"$scheduled_output"; then
    return 1
  fi

  return 0
}

check_celery_idle_gate \
  attendee-attendee-worker-1 \
  worker \
  celery

check_celery_idle_gate \
  attendee-attendee-launcher-worker-1 \
  launcher \
  bot_launcher_vm

check_celery_idle_gate \
  attendee-attendee-webhook-worker-1 \
  webhook \
  webhooks
```

Meça `celery`, `webhooks` e `bot_launcher_vm` diretamente no broker, incluindo
todas as filas de prioridade do transporte Redis, sem consumir mensagens:

```bash
check_priority_queue_zero() {
  local queue="$1"
  local queue_output

  if ! queue_output="$(docker exec \
    -e QUEUE_NAME="$queue" \
    attendee-attendee-worker-1 \
    python -c '
import json
import os
from attendee.celery import app

queue = os.environ["QUEUE_NAME"]
with app.connection_for_read() as connection:
    channel = connection.channel()
    try:
        depths = {}
        for priority in channel.priority_steps:
            key = channel._q_for_pri(queue, priority)
            depths[key] = int(channel.client.llen(key))
    finally:
        channel.close()

total = sum(depths.values())
print(json.dumps({"queue": queue, "priorities": depths, "total": total}))
if total:
    raise SystemExit(2)
' 2>&1)"; then
    printf '%s\n' "$queue_output"
    return 1
  fi

  printf '%s\n' "$queue_output"
  if ! grep -Fq '"total": 0' <<<"$queue_output"; then
    return 1
  fi

  return 0
}

for queue in celery webhooks bot_launcher_vm; do
  check_priority_queue_zero "$queue"
done
```

Se qualquer prioridade tiver backlog, mantenha o gate fechado e deixe a
release anterior tratar mensagens legítimas sob observação. Mensagens
obsoletas exigem decisão explícita e cancelamento pela aplicação; nunca use
`DEL`, `LPOP` ou edição manual no Redis.

### Fechamento da corrida de novos bots

Uma checagem isolada de `docker ps` não fecha a corrida entre o preflight e o
`compose up`:

1. suspenda, no controle autorizado da operação, criação manual e agendada de
   novos bots;
2. comunique a janela aos operadores;
3. confirme que nenhuma requisição nova será disparada até a reabertura;
4. escolha uma janela sem bot agendado desde o início até pelo menos 20 minutos
   após o cutover.

Valide por leitura que não há bot `SCHEDULED` dentro da janela. Ajuste a duração
para cobrir build, cutover e o smoke forçado de até 15 minutos:

```bash
MAINTENANCE_WINDOW_MINUTES=45
test "$MAINTENANCE_WINDOW_MINUTES" -ge 30

check_scheduled_window_clear() {
  local scheduled_output

  if ! scheduled_output="$(docker exec \
    -e MAINTENANCE_WINDOW_MINUTES="$MAINTENANCE_WINDOW_MINUTES" \
    attendee-attendee-app-1 \
    python manage.py shell -c '
import os
from django.utils import timezone
from bots.models import Bot, BotStates

end = timezone.now() + timezone.timedelta(
    minutes=int(os.environ["MAINTENANCE_WINDOW_MINUTES"])
)
bots = list(
    Bot.objects.filter(
        state=BotStates.SCHEDULED,
        join_at__lte=end,
    ).values_list("id", "object_id", "join_at")
)
print({"scheduled_bots_in_window": bots})
if bots:
    raise SystemExit(1)
'
  )"; then
    printf '%s\n' "$scheduled_output"
    return 1
  fi

  printf '%s\n' "$scheduled_output"
  return 0
}

check_scheduled_window_clear
```

Se não for possível garantir o gate ou uma janela sem agendamentos, aborte.
Neste ponto launcher e scheduler continuam operando normalmente; eles só serão
parados no cutover mínimo da seção 6, já protegidos por trap.

Não avance se:

- houver bot ativo;
- `migrate --plan` listar uma operação;
- algum worker não responder;
- houver tarefa ativa ou reservada que não possa ser interrompida;
- o HTTP público não retornar `200`;
- houver menos de 10 GiB livres;
- o spool ou alguma fila estiver crescendo sem explicação.

## 3. Backup consistente

O manifesto de checksum deve ser gerado **depois** que o `pg_dump` terminar.
Um dump ainda em escrita pode ser legível parcialmente e produzir um
manifesto inválido.

No host:

```bash
set -euo pipefail
test -n "$CURRENT_RELEASE_DIR"
test "$CURRENT_RELEASE_DIR" = "${CURRENT_WORKING_DIRS[0]}"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_DIR="/home/deploy/backups/attendee/${TS}-pre-${SHORT_COMMIT}"

install -d -m 0700 "$BACKUP_DIR"

cp --preserve=mode,ownership \
  "$CURRENT_RELEASE_DIR/docker-compose.prod.yml" \
  "$CURRENT_RELEASE_DIR/.env.release" \
  "$CURRENT_RELEASE_DIR/.release-commit" \
  "$BACKUP_DIR/"

printf '%s\n' "$CANONICAL_ENV" > "$BACKUP_DIR/canonical-env.path"
sha256sum "$CANONICAL_ENV" > "$BACKUP_DIR/canonical-env.sha256"

docker ps --format '{{.Names}}|{{.Image}}|{{.ID}}|{{.Status}}' \
  > "$BACKUP_DIR/running-containers.txt"

docker inspect \
  attendee-postgres-1 attendee-redis-1 attendee-minio-1 \
  --format '{{.Name}}|{{.Id}}|{{.Image}}' \
  > "$BACKUP_DIR/dependency-container-ids.txt"

docker image inspect "$(docker inspect attendee-attendee-app-1 \
  --format '{{.Config.Image}}')" \
  --format '{{.Id}}|{{.Created}}|{{.Size}}' \
  > "$BACKUP_DIR/current-image.txt"

printf '%s\n' \
  "previous_commit=$PREVIOUS_COMMIT" \
  "previous_image_ref=$PREVIOUS_IMAGE_REF" \
  "previous_image_id=$PREVIOUS_IMAGE_ID" \
  "previous_image_revision=$PREVIOUS_IMAGE_REVISION" \
  > "$BACKUP_DIR/rollback-provenance.txt"

docker exec attendee-postgres-1 sh -lc \
  'exec pg_dump --format=custom --no-owner --no-acl \
    -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
  > "$BACKUP_DIR/attendee-prod.dump"

test -s "$BACKUP_DIR/attendee-prod.dump"

docker exec -i attendee-postgres-1 pg_restore --list \
  < "$BACKUP_DIR/attendee-prod.dump" \
  > "$BACKUP_DIR/attendee-prod.dump.list"

test -s "$BACKUP_DIR/attendee-prod.dump.list"

(
  cd "$BACKUP_DIR"
  sha256sum \
    attendee-prod.dump \
    attendee-prod.dump.list \
    docker-compose.prod.yml \
    .env.release \
    .release-commit \
    canonical-env.path \
    canonical-env.sha256 \
    rollback-provenance.txt \
    > SHA256SUMS
  sha256sum -c SHA256SUMS
)

chmod 0600 "$BACKUP_DIR/.env.release"
```

Todos os checksums precisam retornar `OK`. Não imprima `.env.prod`, não salve
`docker inspect` completo de containers e não cole credenciais no relatório.

Este backup protege contra um incidente independente. Um rollback de código
sem migration não deve restaurar o banco.

## 4. Construção imutável sem secrets

O contexto do `docker build` deve vir exclusivamente de `git archive`. Copiar
`.env.prod` para o diretório antes do build é proibido: `.dockerignore` não é
uma fronteira suficiente para proteger secrets presentes no contexto.

Antes de criar o contexto, falhe se a tag já existir. Não sobrescreva nem
“reconstrua” uma tag imutável:

```bash
if docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "ERRO: a tag imutável já existe: $IMAGE" >&2
  exit 1
fi
```

Crie diretórios parciais separados para código e configuração:

```bash
set -euo pipefail
CODE_STAGE="/home/deploy/apps/.attendee-code-${SHORT_COMMIT}.partial"
RELEASE_STAGE="/home/deploy/apps/.attendee-release-${SHORT_COMMIT}.partial"

test ! -e "$CODE_DIR"
test ! -e "$RELEASE_DIR"

if test ! -e "$CODE_STAGE" && test ! -e "$RELEASE_STAGE"; then
  install -d -m 0755 -o root -g root "$CODE_STAGE"
  install -d -m 0700 -o root -g root "$RELEASE_STAGE"
elif test -d "$CODE_STAGE" && test -d "$RELEASE_STAGE"; then
  test "$(stat -c '%U:%G' "$CODE_STAGE")" = 'root:root'
  test "$(stat -c '%a' "$CODE_STAGE")" = '755'
  test "$(stat -c '%U:%G' "$RELEASE_STAGE")" = 'root:root'
  test "$(stat -c '%a' "$RELEASE_STAGE")" = '700'
  test -z "$(find "$RELEASE_STAGE" -mindepth 1 -print -quit)"
else
  echo "ERRO: diretórios parciais assimétricos" >&2
  exit 1
fi
```

Na estação operacional, gere o tar uma única vez como `codex`, calcule seu hash
local e transfira **esse arquivo exato** para um temporário `0600` no host. Não
extraia um stream recebido diretamente:

```bash
[[ "$COMMIT" =~ ^[0-9a-f]{40}$ ]]
SHORT_COMMIT="${COMMIT:0:12}"
test -n "$ATTENDEE_GIT_WORKTREE"
test -n "$ATTENDEE_SSH_KEY"
test -n "$ATTENDEE_HOST"

LOCAL_CODE_ARCHIVE="$(sudo -H -u codex mktemp)"
REMOTE_CODE_ARCHIVE="/home/deploy/apps/.attendee-code-${SHORT_COMMIT}.tar.partial"

cleanup_local_code_archive() {
  sudo -H -u codex rm -f "$LOCAL_CODE_ARCHIVE"
}
trap cleanup_local_code_archive EXIT

sudo -H -u codex bash -lc '
set -euo pipefail
cd "'"$ATTENDEE_GIT_WORKTREE"'"
test "$(git rev-parse HEAD)" = "'"$COMMIT"'"
git archive --format=tar "'"$COMMIT"'" > "'"$LOCAL_CODE_ARCHIVE"'"
'

EXPECTED_CODE_ARCHIVE_SHA256="$(
  sha256sum "$LOCAL_CODE_ARCHIVE" | awk '{print $1}'
)"
[[ "$EXPECTED_CODE_ARCHIVE_SHA256" =~ ^[0-9a-f]{64}$ ]]

set +e
ssh -i "$ATTENDEE_SSH_KEY" "$ATTENDEE_HOST" \
  "if test -f '$REMOTE_CODE_ARCHIVE' &&
       test -f '$REMOTE_CODE_ARCHIVE.sha256.expected'; then
     exit 0;
   elif test ! -e '$REMOTE_CODE_ARCHIVE' &&
        test ! -e '$REMOTE_CODE_ARCHIVE.sha256.expected'; then
     exit 10;
   else
     exit 11;
   fi"
REMOTE_CODE_ARCHIVE_STATE=$?
set -e

case "$REMOTE_CODE_ARCHIVE_STATE" in
  0)
    ssh -i "$ATTENDEE_SSH_KEY" "$ATTENDEE_HOST" \
      "set -e;
       test \$(stat -c '%U:%G' '$REMOTE_CODE_ARCHIVE') = root:root;
       test \$(stat -c '%a' '$REMOTE_CODE_ARCHIVE') = 600;
       test \$(stat -c '%U:%G' \
         '$REMOTE_CODE_ARCHIVE.sha256.expected') = root:root;
       test \$(stat -c '%a' \
         '$REMOTE_CODE_ARCHIVE.sha256.expected') = 600;
       test \$(cat '$REMOTE_CODE_ARCHIVE.sha256.expected') =
         '$EXPECTED_CODE_ARCHIVE_SHA256';
       test \$(sha256sum '$REMOTE_CODE_ARCHIVE' |
         awk '{print \$1}') = '$EXPECTED_CODE_ARCHIVE_SHA256'"
    ;;
  10)
    ssh -i "$ATTENDEE_SSH_KEY" "$ATTENDEE_HOST" \
      "set -e; umask 077;
       install -m 0600 /dev/null '$REMOTE_CODE_ARCHIVE'"
    scp -i "$ATTENDEE_SSH_KEY" \
      "$LOCAL_CODE_ARCHIVE" \
      "$ATTENDEE_HOST:$REMOTE_CODE_ARCHIVE"
    ssh -i "$ATTENDEE_SSH_KEY" "$ATTENDEE_HOST" \
      "set -e;
       test \$(stat -c '%a' '$REMOTE_CODE_ARCHIVE') = 600;
       umask 077;
       printf '%s\\n' '$EXPECTED_CODE_ARCHIVE_SHA256' >
         '$REMOTE_CODE_ARCHIVE.sha256.expected'"
    ;;
  11)
    echo "ERRO: tar da release parcial ou ambíguo no host" >&2
    exit 1
    ;;
  *)
    echo "ERRO: falha SSH ao inspecionar tar da release" >&2
    exit 1
    ;;
esac

cleanup_local_code_archive
trap - EXIT
```

No host, compare os bytes recebidos com o hash calculado localmente, valide
todas as entradas antes de extrair e mantenha o tar até a finalização da
release:

```bash
CODE_ARCHIVE="/home/deploy/apps/.attendee-code-${SHORT_COMMIT}.tar.partial"
test -f "$CODE_ARCHIVE"
test ! -L "$CODE_ARCHIVE"
test "$(stat -c '%U:%G' "$CODE_ARCHIVE")" = 'root:root'
test "$(stat -c '%a' "$CODE_ARCHIVE")" = '600'
test -f "$CODE_ARCHIVE.sha256.expected"
test "$(stat -c '%U:%G' \
  "$CODE_ARCHIVE.sha256.expected")" = 'root:root'
test "$(stat -c '%a' "$CODE_ARCHIVE.sha256.expected")" = '600'

EXPECTED_CODE_ARCHIVE_SHA256="$(cat "$CODE_ARCHIVE.sha256.expected")"
ACTUAL_CODE_ARCHIVE_SHA256="$(
  sha256sum "$CODE_ARCHIVE" | awk '{print $1}'
)"
[[ "$EXPECTED_CODE_ARCHIVE_SHA256" =~ ^[0-9a-f]{64}$ ]]
test "$ACTUAL_CODE_ARCHIVE_SHA256" = "$EXPECTED_CODE_ARCHIVE_SHA256"

python3 - "$CODE_ARCHIVE" <<'PY'
import pathlib
import sys
import tarfile

archive = sys.argv[1]
with tarfile.open(archive, mode="r:") as tar:
    members = tar.getmembers()
    if not members:
        raise SystemExit("archive vazio")
    for member in members:
        path = pathlib.PurePosixPath(member.name)
        if path.is_absolute() or ".." in path.parts:
            raise SystemExit(f"entrada insegura: {member.name!r}")
        if member.issym() or member.islnk():
            target = pathlib.PurePosixPath(member.linkname)
            if target.is_absolute() or ".." in target.parts:
                raise SystemExit(
                    f"link inseguro: {member.name!r} -> {member.linkname!r}"
                )
PY

tar -xf "$CODE_ARCHIVE" \
  --no-same-owner \
  -C "$CODE_STAGE"

verify_code_tree_against_archive() {
  python3 - "$1" "$2" <<'PY'
import hashlib
import os
import pathlib
import stat
import sys
import tarfile

archive_path = sys.argv[1]
root = pathlib.Path(sys.argv[2])

def ignored(path):
    return bool(path.parts) and path.parts[0] == "staticfiles"

def hash_stream(stream):
    digest = hashlib.sha256()
    while True:
        chunk = stream.read(1024 * 1024)
        if not chunk:
            return digest.hexdigest()
        digest.update(chunk)

expected = {}
with tarfile.open(archive_path, mode="r:") as tar:
    for member in tar.getmembers():
        relative = pathlib.PurePosixPath(member.name)
        if str(relative) in ("", ".") or ignored(relative) or member.isdir():
            continue
        if member.isfile():
            stream = tar.extractfile(member)
            if stream is None:
                raise SystemExit(f"arquivo sem conteúdo: {member.name!r}")
            expected[str(relative)] = (
                "file",
                hash_stream(stream),
                member.mode & 0o111,
            )
        elif member.issym():
            expected[str(relative)] = ("symlink", member.linkname)
        else:
            raise SystemExit(f"tipo não suportado: {member.name!r}")

actual = {}
for path in root.rglob("*"):
    relative = pathlib.PurePosixPath(path.relative_to(root).as_posix())
    if ignored(relative):
        continue
    metadata = path.lstat()
    if stat.S_ISDIR(metadata.st_mode):
        continue
    if stat.S_ISLNK(metadata.st_mode):
        actual[str(relative)] = ("symlink", os.readlink(path))
    elif stat.S_ISREG(metadata.st_mode):
        with path.open("rb") as stream:
            actual[str(relative)] = (
                "file",
                hash_stream(stream),
                metadata.st_mode & 0o111,
            )
    else:
        raise SystemExit(f"tipo local não suportado: {str(relative)!r}")

if actual != expected:
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    changed = sorted(
        key for key in set(actual) & set(expected)
        if actual[key] != expected[key]
    )
    raise SystemExit(
        f"árvore divergente; missing={missing}, extra={extra}, changed={changed}"
    )
PY
}

verify_code_tree_against_archive "$CODE_ARCHIVE" "$CODE_STAGE"
```

Execute então um probe genérico no contexto. Qualquer `.env*` que não seja um
dos dois exemplos redigidos bloqueia o build:

```bash
ENV_VIOLATIONS="$(
  find "$CODE_STAGE" -name '.env*' \
    ! -path "$CODE_STAGE/.env.example" \
    ! -path "$CODE_STAGE/.env.prod.example" \
    -print
)"

if test -n "$ENV_VIOLATIONS"; then
  printf 'ERRO: arquivos de ambiente no contexto:\n%s\n' \
    "$ENV_VIOLATIONS" >&2
  exit 1
fi

test -f "$CODE_STAGE/.env.example"
test -f "$CODE_STAGE/.env.prod.example"

grep -Fxq '.env*' "$CODE_STAGE/.dockerignore"
grep -Fxq '!.env.example' "$CODE_STAGE/.dockerignore"
grep -Fxq '!.env.prod.example' "$CODE_STAGE/.dockerignore"

compute_code_tree_sha256() {
  local root="$1"
  (
    cd "$root"
    find . -type f \
      ! -path './staticfiles/*' \
      -print0 |
      sort -z |
      xargs -0 -r sha256sum |
      sha256sum |
      awk '{print $1}'
  )
}

CODE_CONTEXT_SHA256="$(compute_code_tree_sha256 "$CODE_STAGE")"
test -n "$CODE_CONTEXT_SHA256"
```

Confirme novamente que a tag não apareceu desde o primeiro gate e construa:

```bash
if docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "ERRO: a tag imutável passou a existir: $IMAGE" >&2
  exit 1
fi

docker build \
  --label "org.opencontainers.image.revision=$COMMIT" \
  -t "$IMAGE" \
  "$CODE_STAGE"

test "$(docker image inspect "$IMAGE" \
  --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')" \
  = "$COMMIT"

NEW_IMAGE_ID="$(docker image inspect "$IMAGE" --format '{{.Id}}')"
test -n "$NEW_IMAGE_ID"

printf '%s\n' \
  "commit=$COMMIT" \
  "image_ref=$IMAGE" \
  "image_id=$NEW_IMAGE_ID" \
  "code_archive_sha256=$EXPECTED_CODE_ARCHIVE_SHA256" \
  "code_context_sha256=$CODE_CONTEXT_SHA256" \
  > "$RELEASE_STAGE/.build-provenance"
```

Faça o mesmo probe genérico dentro da imagem. O probe deve consumir toda a
saída e não imprimir conteúdo dos exemplos:

```bash
docker run --rm --entrypoint sh "$IMAGE" -c '
set -eu
violations="$(
  find /attendee -name ".env*" \
    ! -path "/attendee/.env.example" \
    ! -path "/attendee/.env.prod.example" \
    -print
)"
test -z "$violations"
test -f /attendee/.env.example
test -f /attendee/.env.prod.example
'

docker image inspect "$IMAGE" \
  --format 'tag={{index .RepoTags 0}} id={{.Id}} created={{.Created}} size={{.Size}}'
```

### Retomada após build parcial ou release finalizada

Uma tag existente continua bloqueando um novo build. Se a execução anterior
falhou depois de criar a imagem, aceite exatamente um de dois estados:

- parcial: `RELEASE_STAGE/.build-provenance` existe e `RELEASE_DIR` não;
- finalizado: `RELEASE_DIR/.build-provenance` existe, `RELEASE_STAGE` não e
  todos os vínculos de código, configuração e Compose continuam válidos.

Em uma sessão de host nova, redefina primeiro
`verify_code_tree_against_archive` executando a definição da seção 4. O gate
abaixo falha fechado se a função não estiver carregada.

```bash
set -euo pipefail

CODE_STAGE="/home/deploy/apps/.attendee-code-${SHORT_COMMIT}.partial"
RELEASE_STAGE="/home/deploy/apps/.attendee-release-${SHORT_COMMIT}.partial"
CODE_ARCHIVE="/home/deploy/apps/.attendee-code-${SHORT_COMMIT}.tar.partial"
CANONICAL_ENV="/home/deploy/secrets/attendee/.env.prod"

if test -f "$RELEASE_STAGE/.build-provenance" &&
  test ! -e "$RELEASE_DIR"; then
  RESUME_RELEASE_FINALIZED=0
  RESUME_PROVENANCE_FILE="$RELEASE_STAGE/.build-provenance"
elif test -f "$RELEASE_DIR/.build-provenance" &&
  test ! -e "$RELEASE_STAGE"; then
  RESUME_RELEASE_FINALIZED=1
  RESUME_PROVENANCE_FILE="$RELEASE_DIR/.build-provenance"
else
  echo "ERRO: estado de release ambíguo para retomada" >&2
  exit 1
fi

RESUME_COMMIT="$(awk -F= '$1 == "commit" {
  print substr($0, index($0, "=") + 1)
}' "$RESUME_PROVENANCE_FILE")"
RESUME_IMAGE_REF="$(awk -F= '$1 == "image_ref" {
  print substr($0, index($0, "=") + 1)
}' "$RESUME_PROVENANCE_FILE")"
RESUME_IMAGE_ID="$(awk -F= '$1 == "image_id" {
  print substr($0, index($0, "=") + 1)
}' "$RESUME_PROVENANCE_FILE")"
RESUME_ARCHIVE_SHA256="$(awk -F= '$1 == "code_archive_sha256" {
  print substr($0, index($0, "=") + 1)
}' "$RESUME_PROVENANCE_FILE")"
RESUME_CONTEXT_SHA256="$(awk -F= '$1 == "code_context_sha256" {
  print substr($0, index($0, "=") + 1)
}' "$RESUME_PROVENANCE_FILE")"

test "$RESUME_COMMIT" = "$COMMIT"
test "$RESUME_IMAGE_REF" = "$IMAGE"
[[ "$RESUME_ARCHIVE_SHA256" =~ ^[0-9a-f]{64}$ ]]
[[ "$RESUME_CONTEXT_SHA256" =~ ^[0-9a-f]{64}$ ]]
test "$(docker image inspect "$IMAGE" --format '{{.Id}}')" = "$RESUME_IMAGE_ID"
test "$(docker image inspect "$RESUME_IMAGE_ID" \
  --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')" \
  = "$COMMIT"
test -f "$CODE_ARCHIVE"
test -f "$CODE_ARCHIVE.sha256.expected"
test "$(stat -c '%U:%G' "$CODE_ARCHIVE")" = 'root:root'
test "$(stat -c '%U:%G' \
  "$CODE_ARCHIVE.sha256.expected")" = 'root:root'
test "$(stat -c '%a' "$CODE_ARCHIVE")" = '600'
test "$(stat -c '%a' "$CODE_ARCHIVE.sha256.expected")" = '600'
test "$(sha256sum "$CODE_ARCHIVE" | awk '{print $1}')" \
  = "$RESUME_ARCHIVE_SHA256"
test "$(cat "$CODE_ARCHIVE.sha256.expected")" \
  = "$RESUME_ARCHIVE_SHA256"

compute_code_tree_sha256() {
  local root="$1"
  (
    cd "$root"
    find . -type f \
      ! -path './staticfiles/*' \
      -print0 |
      sort -z |
      xargs -0 -r sha256sum |
      sha256sum |
      awk '{print $1}'
  )
}

if test -d "$CODE_STAGE" && test ! -e "$CODE_DIR"; then
  RESUME_CODE_ROOT="$CODE_STAGE"
elif test -d "$CODE_DIR" && test ! -e "$CODE_STAGE"; then
  RESUME_CODE_ROOT="$CODE_DIR"
  test "$(stat -c '%U:%G' "$CODE_DIR")" = 'root:root'
  test -z "$(find "$CODE_DIR" -perm /0222 -print -quit)"
else
  echo "ERRO: estado ambíguo entre CODE_STAGE e CODE_DIR" >&2
  exit 1
fi

if ! declare -F verify_code_tree_against_archive >/dev/null; then
  echo "ERRO: redefina verify_code_tree_against_archive da seção 4" >&2
  exit 1
fi
verify_code_tree_against_archive \
  "$CODE_ARCHIVE" \
  "$RESUME_CODE_ROOT"
test "$(compute_code_tree_sha256 "$RESUME_CODE_ROOT")" \
  = "$RESUME_CONTEXT_SHA256"

if test "$RESUME_RELEASE_FINALIZED" = "1"; then
  test "$RESUME_CODE_ROOT" = "$CODE_DIR"
  test "$(cat "$RELEASE_DIR/.release-commit")" = "$COMMIT"
  test -L "$RELEASE_DIR/.env.prod"
  test "$(readlink "$RELEASE_DIR/.env.prod")" = "$CANONICAL_ENV"
  test "$(readlink -f "$RELEASE_DIR/.env.prod")" = "$CANONICAL_ENV"
  test -f "$RELEASE_DIR/.env.release"
  test ! -d "$RELEASE_DIR/bots"
  test -z "$(find "$CODE_DIR" -name '.env*' \
    ! -path "$CODE_DIR/.env.example" \
    ! -path "$CODE_DIR/.env.prod.example" \
    -print -quit)"
  test "$(awk -F= '$1 == "ATTENDEE_RELEASE_IMAGE" {
    print substr($0, index($0, "=") + 1)
  }' "$RELEASE_DIR/.env.release")" = "$IMAGE"
  test "$(awk -F= '$1 == "ATTENDEE_HOST_CODE_PATH" {
    print substr($0, index($0, "=") + 1)
  }' "$RELEASE_DIR/.env.release")" = "$CODE_DIR"

  cd "$RELEASE_DIR"
  docker compose \
    --env-file .env.release \
    -p attendee \
    -f docker-compose.prod.yml \
    config -q
  RESUME_IMAGES_OUTPUT="$(
    docker compose \
      --env-file .env.release \
      -p attendee \
      -f docker-compose.prod.yml \
      config --images
  )"
  test "$(grep -Fxc "$IMAGE" <<<"$RESUME_IMAGES_OUTPUT" || true)" -eq 5
fi

NEW_IMAGE_ID="$RESUME_IMAGE_ID"
EXPECTED_CODE_ARCHIVE_SHA256="$RESUME_ARCHIVE_SHA256"
CODE_CONTEXT_SHA256="$RESUME_CONTEXT_SHA256"
```

Se `RESUME_CODE_ROOT=CODE_STAGE`, retome no probe da imagem e em
`collectstatic`. Se `RESUME_CODE_ROOT=CODE_DIR`, `collectstatic` e a
imutabilidade já foram concluídos: finalize `RELEASE_DIR` somente se
`RESUME_RELEASE_FINALIZED=0`. Quando `RESUME_RELEASE_FINALIZED=1`, pule a seção
5 e retome diretamente em 5.1. Não execute `docker build` novamente. Se o
arquivo de proveniência estiver ausente/divergente ou os diretórios forem
ambíguos, pare para auditoria. Não remova nem sobrescreva a tag para “tentar de
novo”.

## 5. Finalização da release

Gere `staticfiles` a partir da revisão nova, não copie os arquivos da release
anterior. As credenciais entram apenas no container temporário em runtime;
elas não fazem parte do build ou de `CODE_DIR`:

```bash
set -euo pipefail

install -d -m 0755 -o root -g root "$CODE_STAGE/staticfiles"

docker run --rm \
  --user 0:0 \
  --network none \
  --env-file "$CANONICAL_ENV" \
  -e DJANGO_SETTINGS_MODULE=attendee.settings.production \
  --mount \
    "type=bind,src=$CODE_STAGE/staticfiles,dst=/attendee/staticfiles" \
  --entrypoint python \
  "$IMAGE" \
  manage.py collectstatic --noinput

test "$(find "$CODE_STAGE/staticfiles" -type f | wc -l)" -gt 0
```

Torne o código imutável no host antes de publicar o diretório:

```bash
chown -R root:root "$CODE_STAGE"
chmod -R a-w "$CODE_STAGE"
find "$CODE_STAGE" -type d -exec chmod 0555 {} +

test -z "$(find "$CODE_STAGE" -perm /0222 -print -quit)"
test "$(stat -c '%U:%G' "$CODE_STAGE")" = 'root:root'

mv "$CODE_STAGE" "$CODE_DIR"
```

Agora prepare `RELEASE_DIR`, que contém configuração e secrets, mas nenhum
checkout montado no bot:

```bash
cp "$CODE_DIR/docker-compose.prod.yml" \
  "$RELEASE_STAGE/docker-compose.prod.yml"
cp -p "$CURRENT_RELEASE_DIR/.env.release" \
  "$RELEASE_STAGE/.env.release"
ln -s "$CANONICAL_ENV" "$RELEASE_STAGE/.env.prod"

grep -q '^ATTENDEE_RELEASE_IMAGE=' "$RELEASE_STAGE/.env.release"
grep -q '^ATTENDEE_HOST_CODE_PATH=' "$RELEASE_STAGE/.env.release"

sed -i \
  -e "s|^ATTENDEE_RELEASE_IMAGE=.*$|ATTENDEE_RELEASE_IMAGE=$IMAGE|" \
  -e "s|^ATTENDEE_HOST_CODE_PATH=.*$|ATTENDEE_HOST_CODE_PATH=$CODE_DIR|" \
  "$RELEASE_STAGE/.env.release"

printf '%s\n' "$COMMIT" > "$RELEASE_STAGE/.release-commit"

chown root:root \
  "$RELEASE_STAGE" \
  "$RELEASE_STAGE/.env.release" \
  "$RELEASE_STAGE/docker-compose.prod.yml" \
  "$RELEASE_STAGE/.release-commit" \
  "$RELEASE_STAGE/.build-provenance"
chown -h root:root "$RELEASE_STAGE/.env.prod"
chmod 0700 "$RELEASE_STAGE"
chmod 0600 "$RELEASE_STAGE/.env.release"
chmod 0644 \
  "$RELEASE_STAGE/docker-compose.prod.yml" \
  "$RELEASE_STAGE/.release-commit" \
  "$RELEASE_STAGE/.build-provenance"

mv "$RELEASE_STAGE" "$RELEASE_DIR"
```

Valide a separação:

```bash
test -d "$CODE_DIR/staticfiles"
test -z "$(find "$CODE_DIR" \
  \( ! -user root -o ! -group root \) -print -quit)"
test -z "$(find "$CODE_DIR" -perm /0222 -print -quit)"
test "$(compute_code_tree_sha256 "$CODE_DIR")" = "$CODE_CONTEXT_SHA256"
test ! -e "$CODE_DIR/.env"
test ! -e "$CODE_DIR/.env.prod"
test ! -e "$CODE_DIR/.env.release"
test -L "$RELEASE_DIR/.env.prod"
test "$(readlink "$RELEASE_DIR/.env.prod")" = "$CANONICAL_ENV"
test "$(readlink -f "$RELEASE_DIR/.env.prod")" = "$CANONICAL_ENV"
test -f "$RELEASE_DIR/.env.release"
test ! -d "$RELEASE_DIR/bots"
test "$(awk -F= '$1 == "ATTENDEE_HOST_CODE_PATH" {
  print substr($0, index($0, "=") + 1)
}' "$RELEASE_DIR/.env.release")" = "$CODE_DIR"
```

Valide o Compose sem exibir seu ambiente:

```bash
cd "$RELEASE_DIR"

docker compose \
  --env-file .env.release \
  -p attendee \
  -f docker-compose.prod.yml \
  config -q

docker compose \
  --env-file .env.release \
  -p attendee \
  -f docker-compose.prod.yml \
  config --images
```

Valide por serviço que as cinco aplicações usam `$IMAGE`, que os bots montam
`CODE_DIR` e que as dependências mantêm imagens próprias. O parser consome o
JSON completo; não há `head`/`grep -q` encerrando o Compose por `SIGPIPE`:

```bash
docker compose \
  --env-file .env.release \
  -p attendee \
  -f docker-compose.prod.yml \
  config --format json |
EXPECTED_IMAGE="$IMAGE" EXPECTED_CODE_DIR="$CODE_DIR" python3 -c '
import json
import os
import sys

config = json.load(sys.stdin)
services = config["services"]
expected_image = os.environ["EXPECTED_IMAGE"]
expected_code_dir = os.environ["EXPECTED_CODE_DIR"]

application_services = [
    "attendee-app",
    "attendee-worker",
    "attendee-launcher-worker",
    "attendee-webhook-worker",
    "attendee-scheduler",
]
for name in application_services:
    actual = services[name]["image"]
    if actual != expected_image:
        raise SystemExit(f"{name}: imagem inesperada: {actual}")

for name in ["attendee-worker", "attendee-launcher-worker"]:
    environment = services[name]["environment"]
    if environment["BOT_CONTAINER_IMAGE"] != expected_image:
        raise SystemExit(f"{name}: BOT_CONTAINER_IMAGE divergente")
    if environment["BOT_HOST_CODE_PATH"] != expected_code_dir:
        raise SystemExit(f"{name}: BOT_HOST_CODE_PATH divergente")
'
```

Valide o artefato novo sem aplicar migration:

```bash
docker run --rm \
  --entrypoint python \
  "$IMAGE" \
  -m compileall -q /attendee/bots

docker run --rm \
  --network host \
  --env-file "$RELEASE_DIR/.env.prod" \
  -e DJANGO_SETTINGS_MODULE=attendee.settings.production \
  --entrypoint python \
  "$IMAGE" \
  manage.py check

docker run --rm \
  --network host \
  --env-file "$RELEASE_DIR/.env.prod" \
  -e DJANGO_SETTINGS_MODULE=attendee.settings.production \
  --entrypoint python \
  "$IMAGE" \
  manage.py migrate --plan
```

`migrate --plan` deve informar que não há operações.

Mantenha o tar verificado até o cutover terminar. Uma queda entre este ponto,
o gate de rollback e a seção 7 deve poder retomar a release sem rebuild. A
remoção limitada desse temporário ocorre somente ao fim da seção 7.

### 5.1 Gate de rollback sanitizado

Não publique a nova release sem preparar previamente um rollback que mantenha
secrets fora do código montado. Defina:

```bash
export PREVIOUS_SHORT="${PREVIOUS_COMMIT:0:12}"
export ROLLBACK_CODE_DIR="$(awk -F= \
  '$1 == "ATTENDEE_HOST_CODE_PATH" {
    print substr($0, index($0, "=") + 1)
  }' "$CURRENT_RELEASE_DIR/.env.release")"
export ROLLBACK_RELEASE_DIR="/home/deploy/apps/attendee-rollback-${PREVIOUS_SHORT}-sanitized"
export ROLLBACK_CODE_STAGE="${ROLLBACK_CODE_DIR}.partial"
export ROLLBACK_RELEASE_STAGE="${ROLLBACK_RELEASE_DIR}.partial"

test -n "$ROLLBACK_CODE_DIR"
test "$ROLLBACK_CODE_DIR" = "$CURRENT_CODE_DIR"
test "$PREVIOUS_COMMIT" = "$EXPECTED_PREVIOUS_COMMIT"
test ! -e "$ROLLBACK_CODE_STAGE"
test ! -e "$ROLLBACK_RELEASE_STAGE"

if test -e "$ROLLBACK_CODE_DIR" && test ! -d "$ROLLBACK_CODE_DIR"; then
  echo "ERRO: caminho de código do rollback não é diretório" >&2
  exit 1
fi
if test -e "$ROLLBACK_RELEASE_DIR" &&
  test ! -d "$ROLLBACK_RELEASE_DIR"; then
  echo "ERRO: caminho do wrapper de rollback não é diretório" >&2
  exit 1
fi
if test -e "$ROLLBACK_RELEASE_DIR" &&
  test ! -d "$ROLLBACK_CODE_DIR"; then
  echo "ERRO: wrapper de rollback existe sem código" >&2
  exit 1
fi
```

`ROLLBACK_IMAGE` e `ROLLBACK_IMAGE_ID` são obrigatoriamente os valores
capturados dos containers na seção de proveniência; não os redefina aqui.

Este procedimento exige que `CURRENT_CODE_DIR`, já montado pela release ativa,
seja o código sanitizado de rollback. Se ele contiver `.env*` além dos dois
exemplos, não for `root:root`, estiver gravável ou divergir do archive do
`PREVIOUS_COMMIT`, aborte. Corrigir uma release ativa legada exige bootstrap
separado e revisado; não crie um target alternativo nesta janela.

Na estação operacional, gere também o rollback em um tar local único, calcule
o hash desses bytes e transfira o arquivo sem extrair o stream:

```bash
[[ "$EXPECTED_PREVIOUS_COMMIT" =~ ^[0-9a-f]{40}$ ]]
PREVIOUS_SHORT="${EXPECTED_PREVIOUS_COMMIT:0:12}"
test "${#PREVIOUS_SHORT}" -eq 12
test -n "$ATTENDEE_GIT_WORKTREE"
test -n "$ATTENDEE_SSH_KEY"
test -n "$ATTENDEE_HOST"

LOCAL_ROLLBACK_ARCHIVE="$(sudo -H -u codex mktemp)"
REMOTE_ROLLBACK_ARCHIVE="/home/deploy/apps/.attendee-rollback-code-${PREVIOUS_SHORT}.tar.partial"

cleanup_local_rollback_archive() {
  sudo -H -u codex rm -f "$LOCAL_ROLLBACK_ARCHIVE"
}
trap cleanup_local_rollback_archive EXIT

sudo -H -u codex bash -lc '
set -euo pipefail
cd "'"$ATTENDEE_GIT_WORKTREE"'"
git cat-file -e "'"$EXPECTED_PREVIOUS_COMMIT"'^{commit}"
git archive --format=tar "'"$EXPECTED_PREVIOUS_COMMIT"'" \
  > "'"$LOCAL_ROLLBACK_ARCHIVE"'"
'

ROLLBACK_GIT_ARCHIVE_SHA256="$(
  sha256sum "$LOCAL_ROLLBACK_ARCHIVE" | awk '{print $1}'
)"
[[ "$ROLLBACK_GIT_ARCHIVE_SHA256" =~ ^[0-9a-f]{64}$ ]]

set +e
ssh -i "$ATTENDEE_SSH_KEY" "$ATTENDEE_HOST" \
  "if test -f '$REMOTE_ROLLBACK_ARCHIVE' &&
       test -f '$REMOTE_ROLLBACK_ARCHIVE.sha256.expected'; then
     exit 0;
   elif test ! -e '$REMOTE_ROLLBACK_ARCHIVE' &&
        test ! -e '$REMOTE_ROLLBACK_ARCHIVE.sha256.expected'; then
     exit 10;
   else
     exit 11;
   fi"
REMOTE_ROLLBACK_ARCHIVE_STATE=$?
set -e

case "$REMOTE_ROLLBACK_ARCHIVE_STATE" in
  0)
    ssh -i "$ATTENDEE_SSH_KEY" "$ATTENDEE_HOST" \
      "set -e;
       test \$(stat -c '%U:%G' '$REMOTE_ROLLBACK_ARCHIVE') = root:root;
       test \$(stat -c '%a' '$REMOTE_ROLLBACK_ARCHIVE') = 600;
       test \$(stat -c '%U:%G' \
         '$REMOTE_ROLLBACK_ARCHIVE.sha256.expected') = root:root;
       test \$(stat -c '%a' \
         '$REMOTE_ROLLBACK_ARCHIVE.sha256.expected') = 600;
       test \$(cat '$REMOTE_ROLLBACK_ARCHIVE.sha256.expected') =
         '$ROLLBACK_GIT_ARCHIVE_SHA256';
       test \$(sha256sum '$REMOTE_ROLLBACK_ARCHIVE' |
         awk '{print \$1}') = '$ROLLBACK_GIT_ARCHIVE_SHA256'"
    ;;
  10)
    ssh -i "$ATTENDEE_SSH_KEY" "$ATTENDEE_HOST" \
      "set -e; umask 077;
       install -m 0600 /dev/null '$REMOTE_ROLLBACK_ARCHIVE'"
    scp -i "$ATTENDEE_SSH_KEY" \
      "$LOCAL_ROLLBACK_ARCHIVE" \
      "$ATTENDEE_HOST:$REMOTE_ROLLBACK_ARCHIVE"
    ssh -i "$ATTENDEE_SSH_KEY" "$ATTENDEE_HOST" \
      "set -e;
       test \$(stat -c '%a' '$REMOTE_ROLLBACK_ARCHIVE') = 600;
       umask 077;
       printf '%s\\n' '$ROLLBACK_GIT_ARCHIVE_SHA256' >
         '$REMOTE_ROLLBACK_ARCHIVE.sha256.expected'"
    ;;
  11)
    echo "ERRO: tar de rollback parcial ou ambíguo no host" >&2
    exit 1
    ;;
  *)
    echo "ERRO: falha SSH ao inspecionar tar de rollback" >&2
    exit 1
    ;;
esac

cleanup_local_rollback_archive
trap - EXIT
```

No host, verifique o hash e as entradas antes de extrair no diretório parcial:

```bash
PREVIOUS_SHORT="${PREVIOUS_COMMIT:0:12}"
test "$PREVIOUS_COMMIT" = "$EXPECTED_PREVIOUS_COMMIT"
test "${#PREVIOUS_SHORT}" -eq 12
ROLLBACK_ARCHIVE="/home/deploy/apps/.attendee-rollback-code-${PREVIOUS_SHORT}.tar.partial"
ROLLBACK_CODE_STAGE="${ROLLBACK_CODE_DIR}.partial"

test -f "$ROLLBACK_ARCHIVE"
test ! -L "$ROLLBACK_ARCHIVE"
test "$(stat -c '%U:%G' "$ROLLBACK_ARCHIVE")" = 'root:root'
test "$(stat -c '%a' "$ROLLBACK_ARCHIVE")" = '600'
test -f "$ROLLBACK_ARCHIVE.sha256.expected"
test "$(stat -c '%a' "$ROLLBACK_ARCHIVE.sha256.expected")" = '600'

EXPECTED_ROLLBACK_ARCHIVE_SHA256="$(
  cat "$ROLLBACK_ARCHIVE.sha256.expected"
)"
ACTUAL_ROLLBACK_ARCHIVE_SHA256="$(
  sha256sum "$ROLLBACK_ARCHIVE" | awk '{print $1}'
)"
[[ "$EXPECTED_ROLLBACK_ARCHIVE_SHA256" =~ ^[0-9a-f]{64}$ ]]
test "$ACTUAL_ROLLBACK_ARCHIVE_SHA256" = \
  "$EXPECTED_ROLLBACK_ARCHIVE_SHA256"
ROLLBACK_GIT_ARCHIVE_SHA256="$EXPECTED_ROLLBACK_ARCHIVE_SHA256"

python3 - "$ROLLBACK_ARCHIVE" <<'PY'
import pathlib
import sys
import tarfile

archive = sys.argv[1]
with tarfile.open(archive, mode="r:") as tar:
    members = tar.getmembers()
    if not members:
        raise SystemExit("archive vazio")
    for member in members:
        path = pathlib.PurePosixPath(member.name)
        if path.is_absolute() or ".." in path.parts:
            raise SystemExit(f"entrada insegura: {member.name!r}")
        if member.issym() or member.islnk():
            target = pathlib.PurePosixPath(member.linkname)
            if target.is_absolute() or ".." in target.parts:
                raise SystemExit(
                    f"link inseguro: {member.name!r} -> {member.linkname!r}"
                )
PY

if ! declare -F verify_code_tree_against_archive >/dev/null; then
  echo "ERRO: redefina verify_code_tree_against_archive da seção 4" >&2
  exit 1
fi

if test -d "$ROLLBACK_CODE_DIR"; then
  test ! -e "$ROLLBACK_CODE_STAGE"
  verify_code_tree_against_archive \
    "$ROLLBACK_ARCHIVE" \
    "$ROLLBACK_CODE_DIR"
  test "$(stat -c '%U:%G' "$ROLLBACK_CODE_DIR")" = 'root:root'
  test -z "$(find "$ROLLBACK_CODE_DIR" \
    \( ! -user root -o ! -group root \) -print -quit)"
  test -z "$(find "$ROLLBACK_CODE_DIR" -perm /0222 -print -quit)"
  ROLLBACK_ENV_VIOLATIONS="$(
    find "$ROLLBACK_CODE_DIR" -name '.env*' \
      ! -path "$ROLLBACK_CODE_DIR/.env.example" \
      ! -path "$ROLLBACK_CODE_DIR/.env.prod.example" \
      -print
  )"
  test -z "$ROLLBACK_ENV_VIOLATIONS"
  test -d "$ROLLBACK_CODE_DIR/staticfiles"
else
  test ! -e "$ROLLBACK_CODE_STAGE"
  install -d -m 0755 -o root -g root "$ROLLBACK_CODE_STAGE"
  tar -xf "$ROLLBACK_ARCHIVE" \
    --no-same-owner \
    -C "$ROLLBACK_CODE_STAGE"
  verify_code_tree_against_archive \
    "$ROLLBACK_ARCHIVE" \
    "$ROLLBACK_CODE_STAGE"

  ROLLBACK_ENV_VIOLATIONS="$(
    find "$ROLLBACK_CODE_STAGE" -name '.env*' \
      ! -path "$ROLLBACK_CODE_STAGE/.env.example" \
      ! -path "$ROLLBACK_CODE_STAGE/.env.prod.example" \
      -print
  )"
  test -z "$ROLLBACK_ENV_VIOLATIONS"

  install -d -m 0755 -o root -g root \
    "$ROLLBACK_CODE_STAGE/staticfiles"
  docker run --rm \
    --user 0:0 \
    --network none \
    --env-file "$CANONICAL_ENV" \
    -e DJANGO_SETTINGS_MODULE=attendee.settings.production \
    --mount \
      "type=bind,src=$ROLLBACK_CODE_STAGE/staticfiles,dst=/attendee/staticfiles" \
    --entrypoint python \
    "$ROLLBACK_IMAGE" \
    manage.py collectstatic --noinput
  test "$(find "$ROLLBACK_CODE_STAGE/staticfiles" -type f | wc -l)" -gt 0

  chown -R root:root "$ROLLBACK_CODE_STAGE"
  chmod -R a-w "$ROLLBACK_CODE_STAGE"
  find "$ROLLBACK_CODE_STAGE" -type d -exec chmod 0555 {} +
  test -z "$(find "$ROLLBACK_CODE_STAGE" -perm /0222 -print -quit)"
  mv "$ROLLBACK_CODE_STAGE" "$ROLLBACK_CODE_DIR"
fi
```

Calcule o hash da árvore validada. `staticfiles` é excluído porque foi gerado
pela imagem:

```bash
ROLLBACK_CODE_TREE_SHA256="$(
  compute_code_tree_sha256 "$ROLLBACK_CODE_DIR"
)"
test -n "$ROLLBACK_CODE_TREE_SHA256"
```

Se o wrapper sanitizado já existe junto do código validado, não o sobrescreva;
o gate abaixo comprovará todo o conteúdo. Se não existe, crie somente o wrapper
por diretório parcial e `mv` atômico:

```bash
if test -d "$ROLLBACK_RELEASE_DIR"; then
  test -d "$ROLLBACK_CODE_DIR"
  test ! -e "$ROLLBACK_RELEASE_STAGE"
else
  test ! -e "$ROLLBACK_RELEASE_STAGE"
  install -d -m 0700 -o root -g root "$ROLLBACK_RELEASE_STAGE"

  cp "$CURRENT_RELEASE_DIR/docker-compose.prod.yml" \
    "$ROLLBACK_RELEASE_STAGE/docker-compose.prod.yml"
  cp -p "$CURRENT_RELEASE_DIR/.env.release" \
    "$ROLLBACK_RELEASE_STAGE/.env.release"
  ln -s "$CANONICAL_ENV" "$ROLLBACK_RELEASE_STAGE/.env.prod"

  grep -q '^ATTENDEE_RELEASE_IMAGE=' \
    "$ROLLBACK_RELEASE_STAGE/.env.release"
  grep -q '^ATTENDEE_HOST_CODE_PATH=' \
    "$ROLLBACK_RELEASE_STAGE/.env.release"
  sed -i \
    -e "s|^ATTENDEE_RELEASE_IMAGE=.*$|ATTENDEE_RELEASE_IMAGE=$ROLLBACK_IMAGE|" \
    -e "s|^ATTENDEE_HOST_CODE_PATH=.*$|ATTENDEE_HOST_CODE_PATH=$ROLLBACK_CODE_DIR|" \
    "$ROLLBACK_RELEASE_STAGE/.env.release"

  printf '%s\n' "$PREVIOUS_COMMIT" \
    > "$ROLLBACK_RELEASE_STAGE/.release-commit"
  printf '%s\n' \
    "commit=$PREVIOUS_COMMIT" \
    "git_archive_sha256=$ROLLBACK_GIT_ARCHIVE_SHA256" \
    "code_tree_sha256=$ROLLBACK_CODE_TREE_SHA256" \
    > "$ROLLBACK_RELEASE_STAGE/.code-provenance"

  chown root:root \
    "$ROLLBACK_RELEASE_STAGE" \
    "$ROLLBACK_RELEASE_STAGE/docker-compose.prod.yml" \
    "$ROLLBACK_RELEASE_STAGE/.env.release" \
    "$ROLLBACK_RELEASE_STAGE/.release-commit" \
    "$ROLLBACK_RELEASE_STAGE/.code-provenance"
  chown -h root:root "$ROLLBACK_RELEASE_STAGE/.env.prod"
  chmod 0700 "$ROLLBACK_RELEASE_STAGE"
  chmod 0600 "$ROLLBACK_RELEASE_STAGE/.env.release"
  chmod 0644 \
    "$ROLLBACK_RELEASE_STAGE/docker-compose.prod.yml" \
    "$ROLLBACK_RELEASE_STAGE/.release-commit" \
    "$ROLLBACK_RELEASE_STAGE/.code-provenance"

  mv "$ROLLBACK_RELEASE_STAGE" "$ROLLBACK_RELEASE_DIR"
fi
```

O gate abaixo é obrigatório:

```bash
test -d "$ROLLBACK_CODE_DIR/staticfiles"
test -d "$ROLLBACK_RELEASE_DIR"
test "$(cat "$ROLLBACK_RELEASE_DIR/.release-commit")" = "$PREVIOUS_COMMIT"
test "$(stat -c '%U:%G' "$ROLLBACK_RELEASE_DIR")" = 'root:root'
test "$(stat -c '%a' "$ROLLBACK_RELEASE_DIR")" = '700'
test "$(stat -c '%U:%G' \
  "$ROLLBACK_RELEASE_DIR/.env.release")" = 'root:root'
test "$(stat -c '%a' "$ROLLBACK_RELEASE_DIR/.env.release")" = '600'
for artifact in \
  docker-compose.prod.yml \
  .release-commit \
  .code-provenance
do
  test "$(stat -c '%U:%G' \
    "$ROLLBACK_RELEASE_DIR/$artifact")" = 'root:root'
  test "$(stat -c '%a' "$ROLLBACK_RELEASE_DIR/$artifact")" = '644'
done
test "$(stat -c '%U:%G' "$ROLLBACK_CODE_DIR")" = 'root:root'
test -z "$(find "$ROLLBACK_CODE_DIR" \
  \( ! -user root -o ! -group root \) -print -quit)"
test -z "$(find "$ROLLBACK_CODE_DIR" -perm /0222 -print -quit)"

ROLLBACK_ENV_VIOLATIONS="$(
  find "$ROLLBACK_CODE_DIR" -name '.env*' \
    ! -path "$ROLLBACK_CODE_DIR/.env.example" \
    ! -path "$ROLLBACK_CODE_DIR/.env.prod.example" \
    -print
)"
test -z "$ROLLBACK_ENV_VIOLATIONS"

test -L "$ROLLBACK_RELEASE_DIR/.env.prod"
test "$(stat -c '%U:%G' \
  "$ROLLBACK_RELEASE_DIR/.env.prod")" = 'root:root'
test "$(readlink "$ROLLBACK_RELEASE_DIR/.env.prod")" = "$CANONICAL_ENV"
test "$(readlink -f "$ROLLBACK_RELEASE_DIR/.env.prod")" = "$CANONICAL_ENV"
test -f "$ROLLBACK_RELEASE_DIR/.env.release"
test -f "$ROLLBACK_RELEASE_DIR/.code-provenance"
test ! -d "$ROLLBACK_RELEASE_DIR/bots"
docker image inspect "$ROLLBACK_IMAGE" >/dev/null
test "$(docker image inspect "$ROLLBACK_IMAGE" --format '{{.Id}}')" \
  = "$ROLLBACK_IMAGE_ID"
test "$(docker image inspect "$ROLLBACK_IMAGE_ID" \
  --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')" \
  = "$PREVIOUS_COMMIT"

test "$(awk -F= '$1 == "commit" {
  print substr($0, index($0, "=") + 1)
}' "$ROLLBACK_RELEASE_DIR/.code-provenance")" = "$PREVIOUS_COMMIT"
test "$(awk -F= '$1 == "git_archive_sha256" {
  print substr($0, index($0, "=") + 1)
}' "$ROLLBACK_RELEASE_DIR/.code-provenance")" \
  = "$ROLLBACK_GIT_ARCHIVE_SHA256"
test "$(awk -F= '$1 == "code_tree_sha256" {
  print substr($0, index($0, "=") + 1)
}' "$ROLLBACK_RELEASE_DIR/.code-provenance")" \
  = "$(compute_code_tree_sha256 "$ROLLBACK_CODE_DIR")"

test "$(awk -F= '$1 == "ATTENDEE_RELEASE_IMAGE" {
  print substr($0, index($0, "=") + 1)
}' "$ROLLBACK_RELEASE_DIR/.env.release")" = "$ROLLBACK_IMAGE"

test "$(awk -F= '$1 == "ATTENDEE_HOST_CODE_PATH" {
  print substr($0, index($0, "=") + 1)
}' "$ROLLBACK_RELEASE_DIR/.env.release")" = "$ROLLBACK_CODE_DIR"
```

Prenda o manifesto do rollback ao backup validado da seção 3. Isso torna
auditáveis, no rollback autocontido, o commit, o hash do `git archive` e o hash
da árvore realmente montada:

```bash
install -m 0644 \
  "$ROLLBACK_RELEASE_DIR/.code-provenance" \
  "$BACKUP_DIR/rollback-code-provenance.txt"

(
  cd "$BACKUP_DIR"
  sha256sum rollback-code-provenance.txt >> SHA256SUMS
  sha256sum -c SHA256SUMS
)
```

Capture `compose config --images` por completo e exija cinco ocorrências da
imagem de rollback. Como a saída já foi capturada, o `grep` não causa SIGPIPE:

```bash
cd "$ROLLBACK_RELEASE_DIR"

ROLLBACK_IMAGES_OUTPUT="$(
  docker compose \
    --env-file .env.release \
    -p attendee \
    -f docker-compose.prod.yml \
    config --images
)"
printf '%s\n' "$ROLLBACK_IMAGES_OUTPUT"

ROLLBACK_APP_IMAGE_COUNT="$(
  grep -Fxc "$ROLLBACK_IMAGE" <<<"$ROLLBACK_IMAGES_OUTPUT" || true
)"
test "$ROLLBACK_APP_IMAGE_COUNT" -eq 5
```

Além da contagem, valide por nome de serviço e o caminho montado:

```bash
docker compose \
  --env-file .env.release \
  -p attendee \
  -f docker-compose.prod.yml \
  config --format json |
EXPECTED_IMAGE="$ROLLBACK_IMAGE" \
EXPECTED_CODE_DIR="$ROLLBACK_CODE_DIR" \
python3 -c '
import json
import os
import sys

config = json.load(sys.stdin)
services = config["services"]
expected_image = os.environ["EXPECTED_IMAGE"]
expected_code_dir = os.environ["EXPECTED_CODE_DIR"]
application_services = [
    "attendee-app",
    "attendee-worker",
    "attendee-launcher-worker",
    "attendee-webhook-worker",
    "attendee-scheduler",
]
for name in application_services:
    if services[name]["image"] != expected_image:
        raise SystemExit(f"{name}: imagem de rollback divergente")
for name in ["attendee-worker", "attendee-launcher-worker"]:
    environment = services[name]["environment"]
    if environment["BOT_CONTAINER_IMAGE"] != expected_image:
        raise SystemExit(f"{name}: BOT_CONTAINER_IMAGE divergente")
    if environment["BOT_HOST_CODE_PATH"] != expected_code_dir:
        raise SystemExit(f"{name}: BOT_HOST_CODE_PATH divergente")
'
```

Mantenha o tar de rollback e sua expectativa até o cutover terminar. Se a
janela for retomada antes disso, o bloco de transferência valida e reutiliza o
par; presença parcial ou hash divergente bloqueia a operação. A seção 7 remove
somente esses dois temporários depois de provar health e readiness.

Qualquer falha nesse gate bloqueia o deploy, mesmo que a imagem anterior ainda
exista localmente.

## 6. Deploy dos cinco serviços

O cutover é faseado para manter launcher e scheduler parados durante a troca.
Com o gate externo ainda fechado, revalide Celery, janela agendada, backlog e
containers. Instale o trap **antes** de parar qualquer serviço:

```bash
set -euo pipefail

check_celery_idle_gate attendee-attendee-worker-1 worker celery
check_celery_idle_gate \
  attendee-attendee-launcher-worker-1 launcher bot_launcher_vm
check_celery_idle_gate attendee-attendee-webhook-worker-1 webhook webhooks
check_scheduled_window_clear
for queue in celery webhooks bot_launcher_vm; do
  check_priority_queue_zero "$queue"
done

test -z "$(docker ps -q --filter label=attendee.type=ephemeral-bot)"

PRE_DEPS="$(docker inspect \
  attendee-postgres-1 attendee-redis-1 attendee-minio-1 \
  --format '{{.Id}}' | paste -sd '|')"

DEPLOYED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

CUTOVER_FINISHED=0
CUTOVER_RECOVERY_RUNNING=0

restore_previous_release() {
  local exit_code="${1:-1}"
  local rollback_failed=0

  if test "$CUTOVER_FINISHED" = "1" ||
    test "$CUTOVER_RECOVERY_RUNNING" = "1"; then
    return
  fi

  CUTOVER_RECOVERY_RUNNING=1
  trap - ERR INT TERM HUP EXIT
  set +e

  if test "$exit_code" -eq 0; then
    exit_code=1
  fi

  if test -n "$(docker ps -q \
    --filter label=attendee.type=ephemeral-bot)"; then
    echo "ERRO: bot ativo; rollback automático bloqueado" >&2
    exit "$exit_code"
  fi

  echo "Cutover interrompido; restaurando os cinco serviços anteriores" >&2
  cd "$ROLLBACK_RELEASE_DIR" || exit "$exit_code"

  docker compose \
    --env-file .env.release \
    -p attendee \
    -f docker-compose.prod.yml \
    up -d \
    --no-deps \
    --force-recreate \
    attendee-app \
    attendee-worker \
    attendee-launcher-worker \
    attendee-webhook-worker \
    attendee-scheduler

  for container in \
    attendee-attendee-app-1 \
    attendee-attendee-worker-1 \
    attendee-attendee-launcher-worker-1 \
    attendee-attendee-webhook-worker-1 \
    attendee-attendee-scheduler-1
  do
    if test "$(docker inspect "$container" \
      --format '{{.State.Running}}')" != "true"; then
      echo "ERRO: serviço anterior não iniciou: $container" >&2
      rollback_failed=1
    fi
    if test "$(docker inspect "$container" --format '{{.Image}}')" \
      != "$ROLLBACK_IMAGE_ID"; then
      echo "ERRO: imagem anterior divergente: $container" >&2
      rollback_failed=1
    fi
  done

  if test "$rollback_failed" -ne 0; then
    echo "ERRO: rollback automático incompleto; mantenha o gate fechado" >&2
  fi

  local rollback_http_healthy=0
  for attempt in $(seq 1 30); do
    if test "$(curl -fsS -o /dev/null -w '%{http_code}' \
      https://bots.firstlineai.com.br/health/ || true)" = "200"; then
      rollback_http_healthy=1
      break
    fi
    sleep 2
  done
  if test "$rollback_http_healthy" != "1"; then
    echo "ERRO: HTTP da release anterior não estabilizou" >&2
    rollback_failed=1
  fi

  local readiness_ok
  local specification
  for specification in \
    'attendee-attendee-worker-1 worker celery' \
    'attendee-attendee-launcher-worker-1 launcher bot_launcher_vm' \
    'attendee-attendee-webhook-worker-1 webhook webhooks'
  do
    readiness_ok=0
    for attempt in $(seq 1 12); do
      if check_celery_readiness $specification; then
        readiness_ok=1
        break
      fi
      sleep 2
    done
    if test "$readiness_ok" != "1"; then
      echo "ERRO: readiness da release anterior falhou: $specification" >&2
      rollback_failed=1
    fi
  done

  local rollback_post_deps
  rollback_post_deps="$(docker inspect \
    attendee-postgres-1 attendee-redis-1 attendee-minio-1 \
    --format '{{.Id}}' | paste -sd '|')"
  if test "$rollback_post_deps" != "$PRE_DEPS"; then
    echo "ERRO: dependência stateful mudou durante rollback" >&2
    rollback_failed=1
  fi

  if test "$rollback_failed" -ne 0; then
    echo "ERRO: restauração anterior não ficou operacional" >&2
  fi

  exit "$exit_code"
}

trap 'restore_previous_release $?' ERR
trap 'restore_previous_release 130' INT
trap 'restore_previous_release 143' TERM
trap 'restore_previous_release 129' HUP
trap 'restore_previous_release $?' EXIT
```

No ponto mínimo de cutover, pare scheduler e launcher. O scheduler também
publica `launch_scheduled_bot`; parar somente o launcher não fecha a corrida:

```bash
docker stop --time 30 \
  attendee-attendee-scheduler-1 \
  attendee-attendee-launcher-worker-1

test "$(docker inspect attendee-attendee-scheduler-1 \
  --format '{{.State.Status}}')" = "exited"
test "$(docker inspect attendee-attendee-launcher-worker-1 \
  --format '{{.State.Status}}')" = "exited"

test -z "$(docker ps -q --filter label=attendee.type=ephemeral-bot)"
sleep 3
test -z "$(docker ps -q --filter label=attendee.type=ephemeral-bot)"

check_celery_idle_gate attendee-attendee-worker-1 worker celery
check_celery_idle_gate attendee-attendee-webhook-worker-1 webhook webhooks
for queue in celery webhooks bot_launcher_vm; do
  check_priority_queue_zero "$queue"
done
```

Se houver backlog em qualquer prioridade, o trap restaura a release anterior.
Mantenha o gate fechado e trate as mensagens explicitamente; não inicie o
launcher novo sobre uma fila antiga.

Recrie primeiro app, worker e webhook. Scheduler e launcher permanecem
parados:

```bash
cd "$RELEASE_DIR"

docker compose \
  --env-file .env.release \
  -p attendee \
  -f docker-compose.prod.yml \
  up -d \
  --no-deps \
  --force-recreate \
  attendee-app \
  attendee-worker \
  attendee-webhook-worker
```

Valide HTTP e os dois nós novos, depois prove novamente backlog zero:

```bash
healthy=0
for attempt in $(seq 1 30); do
  if test "$(curl -fsS -o /dev/null -w '%{http_code}' \
    https://bots.firstlineai.com.br/health/ || true)" = "200"; then
    healthy=1
    break
  fi
  sleep 2
done
test "$healthy" = "1"

check_celery_readiness attendee-attendee-worker-1 worker celery
check_celery_readiness attendee-attendee-webhook-worker-1 webhook webhooks
check_scheduled_window_clear
for queue in celery webhooks bot_launcher_vm; do
  check_priority_queue_zero "$queue"
done
```

Somente agora recrie launcher e scheduler novos. A janela de agendamentos e o
backlog acabaram de ser provados vazios:

```bash
docker compose \
  --env-file .env.release \
  -p attendee \
  -f docker-compose.prod.yml \
  up -d \
  --no-deps \
  --force-recreate \
  attendee-launcher-worker \
  attendee-scheduler

check_celery_readiness \
  attendee-attendee-launcher-worker-1 launcher bot_launcher_vm
```

Confirme que as dependências não foram recriadas:

```bash
POST_DEPS="$(docker inspect \
  attendee-postgres-1 attendee-redis-1 attendee-minio-1 \
  --format '{{.Id}}' | paste -sd '|')"

test "$PRE_DEPS" = "$POST_DEPS"
```

Não reabra a criação normal de bots ainda. Primeiro valide todos os health
checks e execute os smokes controlados.

## 7. Health checks pós-deploy

`docker ps` não basta. Verifique imagem, processo, HTTP, Celery, migrations e
logs.

```bash
for container in \
  attendee-attendee-app-1 \
  attendee-attendee-worker-1 \
  attendee-attendee-launcher-worker-1 \
  attendee-attendee-webhook-worker-1 \
  attendee-attendee-scheduler-1
do
  test "$(docker inspect "$container" --format '{{.Config.Image}}')" = "$IMAGE"
  test "$(docker inspect "$container" --format '{{.Image}}')" = "$NEW_IMAGE_ID"
  test "$(docker inspect "$container" --format '{{.State.Status}}')" = "running"
done
```

Aguarde no máximo 60 segundos pelo HTTP real:

```bash
healthy=0
for attempt in $(seq 1 30); do
  if test "$(curl -fsS -o /dev/null -w '%{http_code}' \
    https://bots.firstlineai.com.br/health/ || true)" = "200"; then
    healthy=1
    break
  fi
  sleep 2
done
test "$healthy" = "1"
```

Reutilize `check_celery_readiness` da seção 2 para validar nodename real e fila.
Depois do start, tarefas legítimas podem existir; não exija ociosidade global e
não substitua o readiness dirigido por um ping broadcast:

```bash
check_celery_readiness \
  attendee-attendee-worker-1 \
  worker \
  celery

check_celery_readiness \
  attendee-attendee-launcher-worker-1 \
  launcher \
  bot_launcher_vm

check_celery_readiness \
  attendee-attendee-webhook-worker-1 \
  webhook \
  webhooks
```

Confira que nenhuma migration apareceu e examine logs desde o deploy:

```bash
docker exec attendee-attendee-app-1 python manage.py migrate --plan

for container in \
  attendee-attendee-app-1 \
  attendee-attendee-worker-1 \
  attendee-attendee-launcher-worker-1 \
  attendee-attendee-webhook-worker-1 \
  attendee-attendee-scheduler-1
do
  echo "===== $container ====="
  docker logs --since "$DEPLOYED_AT" "$container" 2>&1 | tail -200
done
```

Procure especialmente por `Traceback`, `OperationalError`, `deadlock`,
`Recording delivery failed`, reinícios e desconexões do broker.

Se houver erro acionável, mantenha o trap armado e o gate fechado. Quando todos
os checks acima passarem, prove novamente que não existe bot efêmero e desarme
o trap **antes** de iniciar qualquer smoke:

```bash
test -z "$(docker ps -q --filter label=attendee.type=ephemeral-bot)"
sleep 3
test -z "$(docker ps -q --filter label=attendee.type=ephemeral-bot)"

CUTOVER_FINISHED=1
trap - ERR INT TERM HUP EXIT

CODE_ARCHIVE="/home/deploy/apps/.attendee-code-${SHORT_COMMIT}.tar.partial"
PROVENANCE_CODE_ARCHIVE_SHA256="$(awk -F= \
  '$1 == "code_archive_sha256" {
    print substr($0, index($0, "=") + 1)
  }' "$RELEASE_DIR/.build-provenance")"
test "$(sha256sum "$CODE_ARCHIVE" | awk '{print $1}')" = \
  "$PROVENANCE_CODE_ARCHIVE_SHA256"
test "$(cat "$CODE_ARCHIVE.sha256.expected")" = \
  "$PROVENANCE_CODE_ARCHIVE_SHA256"

rm -- "$CODE_ARCHIVE" "$CODE_ARCHIVE.sha256.expected"
test ! -e "$CODE_ARCHIVE"
test ! -e "$CODE_ARCHIVE.sha256.expected"

PREVIOUS_SHORT="${PREVIOUS_COMMIT:0:12}"
ROLLBACK_ARCHIVE="/home/deploy/apps/.attendee-rollback-code-${PREVIOUS_SHORT}.tar.partial"
PROVENANCE_ROLLBACK_ARCHIVE_SHA256="$(awk -F= \
  '$1 == "git_archive_sha256" {
    print substr($0, index($0, "=") + 1)
  }' "$ROLLBACK_RELEASE_DIR/.code-provenance")"
test "$(sha256sum "$ROLLBACK_ARCHIVE" | awk '{print $1}')" = \
  "$PROVENANCE_ROLLBACK_ARCHIVE_SHA256"
test "$(cat "$ROLLBACK_ARCHIVE.sha256.expected")" = \
  "$PROVENANCE_ROLLBACK_ARCHIVE_SHA256"

rm -- "$ROLLBACK_ARCHIVE" "$ROLLBACK_ARCHIVE.sha256.expected"
test ! -e "$ROLLBACK_ARCHIVE"
test ! -e "$ROLLBACK_ARCHIVE.sha256.expected"
```

O rollback automático protege somente o cutover sem bots. A partir deste ponto,
qualquer falha de smoke exige encerrar o bot, preservar a gravação e executar o
rollback autocontido da seção 11; nunca faça rollback com um bot ativo.

## 8. Smoke normal

Use um projeto e uma reunião destinados a testes. Nunca reutilize uma reunião
de cliente. O webhook deste smoke deve apontar para um coletor isolado e
controlado pela operação; **não use o webhook da FirstLine AI**, para que o
teste não crie análise nem altere status de usuário.

O bot de smoke deve:

- usar login Google já provisionado;
- gravar em MP3;
- definir `record_async_transcription_audio_chunks=false`;
- usar `transcription_settings: {"none": {}}`;
- apontar para uma chave exclusiva no storage externo;
- assinar `bot.state_change` e `recording.ready`;
- usar um endpoint de webhook controlado e idempotente.

Use este contrato, substituindo somente placeholders e mantendo o token fora do
payload e do histórico do shell:

```json
{
  "meeting_url": "<meet-exclusivo-de-smoke>",
  "bot_name": "Attendee reliability smoke",
  "recording_settings": {
    "format": "mp3",
    "record_async_transcription_audio_chunks": false
  },
  "transcription_settings": {
    "none": {}
  },
  "google_meet_settings": {
    "use_login": true,
    "login_mode": "always",
    "login_group_name": "<grupo-de-login-de-smoke>"
  },
  "external_media_storage_settings": {
    "bucket_name": "<bucket-configurado-no-projeto>",
    "recording_file_name": "reliability-smoke/<run-id-unico>.mp3"
  },
  "webhooks": [
    {
      "url": "https://<coletor-isolado-de-smoke>/attendee",
      "triggers": ["bot.state_change", "recording.ready"]
    }
  ]
}
```

Procedimento:

1. Mantendo o gate normal fechado, autorize somente uma criação direta do bot
   de smoke pela API, sem imprimir o token no terminal ou nos logs. A resposta
   da API retorna o identificador público `bot_...`; registre-o como
   `BOT_OBJECT_ID`.
2. Autorize sua entrada e fale continuamente por pelo menos 60 segundos.
3. Durante a chamada, confirme que o spool cresce e que não surgem utterances
   ou transcrições assíncronas.
4. Encerre a reunião normalmente.
5. Inicie um cronômetro no encerramento e aguarde no máximo **180 segundos**
   pelo `recording.ready`.
6. Dentro dos 180 segundos, confirme `Bot=Ended`, `Recording=Complete`,
   `delivery_state=Ready` e `is_partial=false`.
7. Confirme tamanho maior que zero, duração plausível e SHA-256 preenchido.
8. Faça `HEAD` no objeto primário e no objeto externo; ambos devem ter o
   tamanho esperado.
9. Baixe uma cópia de teste, execute `ffprobe` e ouça um trecho.
10. Confirme que o arquivo correspondente saiu do spool somente depois das
    duas validações de storage.
11. Confirme um único processamento idempotente de `recording.ready`.
12. Por consulta read-only, confirme zero `AudioChunk`, zero `Utterance` e
    zero `AsyncTranscription` associados à gravação.

Valide estados e ausência do pipeline legado sem escrever no banco:

```bash
docker exec \
  -e BOT_OBJECT_ID="$BOT_OBJECT_ID" \
  attendee-attendee-app-1 \
  python manage.py shell -c '
import os
from bots.models import (
    Bot,
    BotStates,
    RecordingDeliveryStates,
    RecordingStates,
)

bot = Bot.objects.get(object_id=os.environ["BOT_OBJECT_ID"])
recording = bot.recordings.get(is_default_recording=True)
snapshot = {
    "bot_state": bot.get_state_display(),
    "recording_state": recording.get_state_display(),
    "delivery_state": recording.get_delivery_state_display(),
    "is_partial": recording.is_partial,
    "audio_chunks": recording.audio_chunks.count(),
    "utterances": recording.utterances.count(),
    "async_transcriptions": recording.async_transcriptions.count(),
}
print(snapshot)
assert bot.state == BotStates.ENDED
assert recording.state == RecordingStates.COMPLETE
assert recording.delivery_state == RecordingDeliveryStates.READY
assert recording.is_partial is False
assert recording.file_size_bytes and recording.file_size_bytes > 0
assert recording.duration_ms and recording.duration_ms > 0
assert recording.file_sha256 and len(recording.file_sha256) == 64
assert snapshot["audio_chunks"] == 0
assert snapshot["utterances"] == 0
assert snapshot["async_transcriptions"] == 0
'
```

Reprovar o smoke se houver recuperação manual, perda de áudio, objeto vazio,
webhook ausente/duplicado com efeito colateral ou arquivo local removido antes
da confirmação remota. Também reprove se exceder 180 segundos ou se qualquer
chunk/transcrição tiver sido criado.

## 9. Smoke de interrupção forçada

Este teste é destrutivo para o bot de teste. Exige autorização explícita e não
pode ser executado com reunião de cliente.

1. Crie outro bot de teste com o mesmo contrato recording-only da seção 8,
   outro `run-id`, chave de storage exclusiva e o mesmo coletor isolado. Não
   use webhook da FirstLine AI.
2. Autorize a entrada e fale por pelo menos 60 segundos.
3. Registre o `BOT_OBJECT_ID` público `bot_...` retornado pela API e derive o
   `Bot.id` interno por ORM read-only. O label Docker
   `attendee.bot_id` contém esse inteiro interno, não o `bot_...`:

   ```bash
   BOT_OBJECT_ID="<bot_...-retornado-pela-api>"
   BOT_INTERNAL_ID="$(
     docker exec \
       -e BOT_OBJECT_ID="$BOT_OBJECT_ID" \
       attendee-attendee-app-1 \
       python manage.py shell -c '
import os
from bots.models import Bot

print(Bot.objects.only("id").get(
    object_id=os.environ["BOT_OBJECT_ID"]
).id)
'
   )"
   [[ "$BOT_INTERNAL_ID" =~ ^[0-9]+$ ]]
   ```

4. Espere o MP3 do spool crescer entre duas leituras. Então resolva o container
   pelos labels e exija exatamente um resultado `running`:

   ```bash

   BOT_CONTAINER_MATCHES_OUTPUT="$(
     docker ps \
       --filter status=running \
       --filter label=attendee.type=ephemeral-bot \
       --filter label=attendee.bot_id="$BOT_INTERNAL_ID" \
       --format '{{.ID}}'
   )"
   mapfile -t BOT_CONTAINER_MATCHES < <(
     printf '%s' "$BOT_CONTAINER_MATCHES_OUTPUT"
   )

   test "${#BOT_CONTAINER_MATCHES[@]}" -eq 1
   BOT_CONTAINER_ID="${BOT_CONTAINER_MATCHES[0]}"

   test "$(docker inspect "$BOT_CONTAINER_ID" \
     --format '{{.State.Running}}')" = "true"
   test "$(docker inspect "$BOT_CONTAINER_ID" \
     --format '{{index .Config.Labels "attendee.type"}}')" = "ephemeral-bot"
   test "$(docker inspect "$BOT_CONTAINER_ID" \
     --format '{{index .Config.Labels "attendee.bot_id"}}')" \
     = "$BOT_INTERNAL_ID"
   test "$(docker inspect "$BOT_CONTAINER_ID" \
     --format '{{index .Config.Labels "attendee.durable_recording"}}')" = "true"
   ```

5. Imediatamente antes do sinal, repita a resolução e prove que o mesmo ID
   continua sendo o único container com os labels esperados.
6. Interrompa o ID derivado, nunca um ID digitado manualmente:

   ```bash
   REVALIDATED_MATCHES_OUTPUT="$(
     docker ps \
       --filter status=running \
       --filter label=attendee.type=ephemeral-bot \
       --filter label=attendee.bot_id="$BOT_INTERNAL_ID" \
       --format '{{.ID}}'
   )"
   mapfile -t REVALIDATED_MATCHES < <(
     printf '%s' "$REVALIDATED_MATCHES_OUTPUT"
   )

   test "${#REVALIDATED_MATCHES[@]}" -eq 1
   test "${REVALIDATED_MATCHES[0]}" = "$BOT_CONTAINER_ID"
   test "$(docker inspect "$BOT_CONTAINER_ID" \
     --format '{{index .Config.Labels "attendee.durable_recording"}}')" = "true"

   docker kill --signal=KILL "$BOT_CONTAINER_ID"
   ```

7. Não reenvie a tarefa manualmente durante a janela real de recuperação. O
   ciclo do scheduler somado à carência do órfão normalmente exige **12 a 13
   minutos**; aguarde 13 minutos completos.
8. Entre 12 e 13 minutos, confirme que o scheduler encontra o MP3, repara-o
   quando necessário e entrega `recording.ready` com `is_partial=true`.
   Esse é o tempo esperado, não o timeout.
9. Mantenha observação controlada até o timeout de **15 minutos**. Somente
   depois dele classifique a recuperação automática como falha.
10. Até 15 minutos após o `KILL`, confirme `Bot=Fatal Error`,
    `Recording=Complete`, `delivery_state=Ready` e `is_partial=true`.
11. Repita as validações de tamanho, duração, SHA-256, `HEAD`, `ffprobe`,
    idempotência, zero chunks/transcrições e limpeza do spool do smoke normal.

Para o smoke forçado, repita a consulta acima trocando somente as asserções de
estado por:

```python
assert bot.state == BotStates.FATAL_ERROR
assert recording.state == RecordingStates.COMPLETE
assert recording.delivery_state == RecordingDeliveryStates.READY
assert recording.is_partial is True
```

O teste passa quando o áudio acumulado antes da falha é entregue sem
intervenção manual, com os estados acima. O teste falha se o arquivo
desaparecer, permanecer preso por mais de 15 minutos, não terminar em
`fatal_error` ou gerar análise duplicada.

## 10. Resposta a gravação presa no spool

Considere uma gravação presa quando um MP3 não vazio permanece no spool por
mais de 15 minutos sem entrega `Ready`, ou quando o estado fica `STAGED`,
`UPLOADING` ou `FAILED` sem progresso.

### 10.1 Preservar evidência

```bash
set -euo pipefail

for container in \
  attendee-attendee-worker-1 \
  attendee-attendee-scheduler-1
do
  test "$(docker inspect "$container" \
    --format '{{.State.Running}}')" = "true"
done

INCIDENT_WORKING_DIRS_OUTPUT="$(
  docker inspect \
    attendee-attendee-worker-1 \
    attendee-attendee-scheduler-1 \
    --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}'
)"
mapfile -t INCIDENT_WORKING_DIRS < <(
  sort -u <<<"$INCIDENT_WORKING_DIRS_OUTPUT"
)
test "${#INCIDENT_WORKING_DIRS[@]}" -eq 1
CURRENT_RELEASE_DIR="${INCIDENT_WORKING_DIRS[0]}"
test -d "$CURRENT_RELEASE_DIR"
test -f "$CURRENT_RELEASE_DIR/.env.release"

SPOOL="$(awk -F= '$1 == "ATTENDEE_RECORDING_SPOOL_HOST_PATH" {
  print substr($0, index($0, "=") + 1)
}' "$CURRENT_RELEASE_DIR/.env.release")"
test -n "$SPOOL"
case "$SPOOL" in
  /*) ;;
  *) echo "ERRO: caminho de spool não absoluto" >&2; exit 1 ;;
esac
test -d "$SPOOL"

for container in \
  attendee-attendee-worker-1 \
  attendee-attendee-scheduler-1
do
  SPOOL_MOUNT_SOURCES_OUTPUT="$(
    docker inspect "$container" \
      --format '{{range .Mounts}}{{if eq .Destination "/attendee-recording-spool"}}{{.Source}}{{println}}{{end}}{{end}}'
  )"
  mapfile -t SPOOL_MOUNT_SOURCES < <(
    printf '%s' "$SPOOL_MOUNT_SOURCES_OUTPUT"
  )
  test "${#SPOOL_MOUNT_SOURCES[@]}" -eq 1
  test "${SPOOL_MOUNT_SOURCES[0]}" = "$SPOOL"
done

find "$SPOOL" -maxdepth 1 -type f \
  -printf '%TY-%Tm-%TdT%TH:%TM:%TS %s %f\n' | sort

df -h "$SPOOL"

incident_celery_readiness() {
  local container="$1"
  local node_prefix="$2"
  local expected_queue="$3"
  local container_hostname
  local node
  local output

  if ! container_hostname="$(docker exec "$container" hostname 2>&1)"; then
    printf '%s\n' "$container_hostname"
    return 1
  fi
  if test -z "$container_hostname"; then
    return 1
  fi
  node="${node_prefix}@${container_hostname}"

  if ! output="$(docker exec "$container" \
    celery -A attendee inspect ping \
    --destination "$node" --timeout=5 2>&1)"; then
    printf '%s\n' "$output"
    return 1
  fi
  printf '%s\n' "$output"
  if ! grep -Fq "$node: OK" <<<"$output"; then
    return 1
  fi
  if ! grep -Fq 'pong' <<<"$output"; then
    return 1
  fi

  if ! output="$(docker exec "$container" \
    celery -A attendee inspect active_queues \
    --destination "$node" --timeout=5 2>&1)"; then
    printf '%s\n' "$output"
    return 1
  fi
  printf '%s\n' "$output"
  if ! grep -Fq "$expected_queue" <<<"$output"; then
    return 1
  fi

  return 0
}
```

Não execute `rm`, não trunque o arquivo e não rode FFmpeg diretamente sobre o
original. Registre nome, tamanho, mtime, bot ID, recording ID e estado atual.

### 10.2 Inspecionar sem alterar

Use o shell Django somente para leitura:

```bash
RECORDING_ID="<id-interno-da-gravacao>"

docker exec \
  -e RECORDING_ID="$RECORDING_ID" \
  attendee-attendee-app-1 \
  python manage.py shell -c '
import os
from bots.models import Recording

r = Recording.objects.select_related("bot").get(
    id=int(os.environ["RECORDING_ID"])
)
print({
    "recording_id": r.id,
    "recording_object_id": r.object_id,
    "bot_id": r.bot_id,
    "bot_object_id": r.bot.object_id,
    "state": r.get_state_display(),
    "delivery_state": r.get_delivery_state_display(),
    "delivery_attempt_count": r.delivery_attempt_count,
    "local_file_path": r.local_file_path,
    "file_size_bytes": r.file_size_bytes,
    "is_partial": r.is_partial,
    "delivery_requested_at": r.delivery_requested_at,
    "delivery_started_at": r.delivery_started_at,
    "delivery_completed_at": r.delivery_completed_at,
})
'
```

Verifique também scheduler, worker e storage antes de intervir:

```bash
docker logs --since 30m attendee-attendee-scheduler-1 2>&1 | tail -300
docker logs --since 30m attendee-attendee-worker-1 2>&1 | tail -300
incident_celery_readiness attendee-attendee-worker-1 worker celery
```

### 10.3 Recuperar pela aplicação

Prefira aguardar a recuperação automática. Se a gravação continuar parada
depois de corrigida a dependência externa, uma reentrega manual pode ser
autorizada. Use a função idempotente da aplicação, nunca `UPDATE` manual:

```bash
RECORDING_ID="<id-interno-da-gravacao>"

incident_celery_readiness attendee-attendee-worker-1 worker celery

docker exec \
  -e RECORDING_ID="$RECORDING_ID" \
  attendee-attendee-app-1 \
  python manage.py shell -c '
import os
from bots.tasks.recording_delivery_task import enqueue_recording_delivery

print(enqueue_recording_delivery(int(os.environ["RECORDING_ID"])))
'
```

Depois, repita a inspeção e valide o storage. Não considere o incidente
resolvido somente porque a tarefa saiu da fila.

Arquivos de zero byte não são áudio recuperável. Depois que o PR #9 estiver
integrado **e a release que o contém estiver publicada**, a recuperação
automática:

- remove somente um arquivo vazio, regular e não symlink;
- exige o caminho determinístico esperado para aquela gravação;
- exige que o bot já esteja em estado pós-reunião;
- marca a entrega como `FAILED` terminal, com
  `RecordingSpoolEmpty`;
- leva `delivery_attempt_count` ao limite e limpa `local_file_path`, evitando
  novos ciclos inúteis.

Um symlink ou um arquivo vazio fora do caminho esperado nunca é removido
automaticamente. Antes de publicar uma release com o PR #9, mantenha o
procedimento conservador: não envie nem apague o arquivo vazio manualmente;
confirme que o bot nunca gravou e preserve a evidência.

Mesmo depois do PR #9, não execute `rm` manual. Confirme nos logs e no banco que
a limpeza automática aplicou todas as condições acima. A ausência do arquivo,
isoladamente, não prova sucesso de gravação.

### 10.4 Escalonamento

Interrompa novas ativações e escale quando:

- o spool atingir 75% do filesystem;
- duas ou mais gravações não vazias ficarem presas;
- o storage primário e o externo discordarem em tamanho;
- houver repetição de deadlocks após a política de retry;
- o scheduler ou o worker não permanecerem saudáveis;
- houver qualquer dúvida sobre a correspondência entre arquivo e gravação.

## 11. Rollback

O rollback troca somente os cinco serviços de aplicação e exige os artefatos
sanitizados aprovados na seção 5.1. Nunca use como
`ROLLBACK_CODE_DIR` um diretório que também contenha `.env.prod`.

Feche novamente a criação manual/agendada e encerre qualquer bot de smoke antes
de iniciar. O bloco abaixo é autocontido e não usa `docker exec` em app/worker
antes da restauração. O único input operacional é o diretório de backup
validado criado na seção 3:

```bash
set -euo pipefail

BACKUP_DIR="<backup-validado-da-secao-3>"
test -d "$BACKUP_DIR"
(
  cd "$BACKUP_DIR"
  sha256sum -c SHA256SUMS
)

exec 9>/run/lock/attendee-deploy.lock
flock -n 9

CANONICAL_ENV="$(cat "$BACKUP_DIR/canonical-env.path")"
test "$CANONICAL_ENV" = "/home/deploy/secrets/attendee/.env.prod"
sha256sum -c "$BACKUP_DIR/canonical-env.sha256"
test -f "$CANONICAL_ENV"
test ! -L "$CANONICAL_ENV"
test "$(stat -c '%U:%G' "$CANONICAL_ENV")" = 'deploy:deploy'
test "$(stat -c '%a' "$CANONICAL_ENV")" = '600'

PREVIOUS_COMMIT="$(awk -F= '$1 == "previous_commit" {
  print substr($0, index($0, "=") + 1)
}' "$BACKUP_DIR/rollback-provenance.txt")"
ROLLBACK_IMAGE="$(awk -F= '$1 == "previous_image_ref" {
  print substr($0, index($0, "=") + 1)
}' "$BACKUP_DIR/rollback-provenance.txt")"
ROLLBACK_IMAGE_ID="$(awk -F= '$1 == "previous_image_id" {
  print substr($0, index($0, "=") + 1)
}' "$BACKUP_DIR/rollback-provenance.txt")"
PREVIOUS_IMAGE_REVISION="$(awk -F= '$1 == "previous_image_revision" {
  print substr($0, index($0, "=") + 1)
}' "$BACKUP_DIR/rollback-provenance.txt")"

test "$PREVIOUS_IMAGE_REVISION" = "$PREVIOUS_COMMIT"
test "$(docker image inspect "$ROLLBACK_IMAGE" --format '{{.Id}}')" \
  = "$ROLLBACK_IMAGE_ID"
test "$(docker image inspect "$ROLLBACK_IMAGE_ID" \
  --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')" \
  = "$PREVIOUS_COMMIT"

PREVIOUS_SHORT="${PREVIOUS_COMMIT:0:12}"
ROLLBACK_RELEASE_DIR="/home/deploy/apps/attendee-rollback-${PREVIOUS_SHORT}-sanitized"
test -d "$ROLLBACK_RELEASE_DIR"
test -f "$ROLLBACK_RELEASE_DIR/.code-provenance"
test "$(cat "$ROLLBACK_RELEASE_DIR/.release-commit")" = "$PREVIOUS_COMMIT"
cmp -s \
  "$BACKUP_DIR/rollback-code-provenance.txt" \
  "$ROLLBACK_RELEASE_DIR/.code-provenance"
BACKUP_ROLLBACK_GIT_ARCHIVE_SHA256="$(awk -F= \
  '$1 == "git_archive_sha256" {
    print substr($0, index($0, "=") + 1)
  }' "$BACKUP_DIR/rollback-code-provenance.txt")"
[[ "$BACKUP_ROLLBACK_GIT_ARCHIVE_SHA256" =~ ^[0-9a-f]{64}$ ]]

ROLLBACK_CODE_DIR="$(awk -F= '$1 == "ATTENDEE_HOST_CODE_PATH" {
  print substr($0, index($0, "=") + 1)
}' "$ROLLBACK_RELEASE_DIR/.env.release")"
test -d "$ROLLBACK_CODE_DIR/staticfiles"
test "$(stat -c '%U:%G' "$ROLLBACK_CODE_DIR")" = 'root:root'
test -z "$(find "$ROLLBACK_CODE_DIR" \
  \( ! -user root -o ! -group root \) -print -quit)"
test -z "$(find "$ROLLBACK_CODE_DIR" -perm /0222 -print -quit)"

ROLLBACK_ENV_VIOLATIONS="$(
  find "$ROLLBACK_CODE_DIR" -name '.env*' \
    ! -path "$ROLLBACK_CODE_DIR/.env.example" \
    ! -path "$ROLLBACK_CODE_DIR/.env.prod.example" \
    -print
)"
test -z "$ROLLBACK_ENV_VIOLATIONS"

compute_code_tree_sha256() {
  local root="$1"
  (
    cd "$root"
    find . -type f \
      ! -path './staticfiles/*' \
      -print0 |
      sort -z |
      xargs -0 -r sha256sum |
      sha256sum |
      awk '{print $1}'
  )
}

test "$(awk -F= '$1 == "commit" {
  print substr($0, index($0, "=") + 1)
}' "$ROLLBACK_RELEASE_DIR/.code-provenance")" = "$PREVIOUS_COMMIT"
test "$(awk -F= '$1 == "git_archive_sha256" {
  print substr($0, index($0, "=") + 1)
}' "$ROLLBACK_RELEASE_DIR/.code-provenance")" \
  = "$BACKUP_ROLLBACK_GIT_ARCHIVE_SHA256"
test "$(awk -F= '$1 == "code_tree_sha256" {
  print substr($0, index($0, "=") + 1)
}' "$ROLLBACK_RELEASE_DIR/.code-provenance")" \
  = "$(compute_code_tree_sha256 "$ROLLBACK_CODE_DIR")"

test -L "$ROLLBACK_RELEASE_DIR/.env.prod"
test "$(readlink "$ROLLBACK_RELEASE_DIR/.env.prod")" = "$CANONICAL_ENV"
test "$(readlink -f "$ROLLBACK_RELEASE_DIR/.env.prod")" = "$CANONICAL_ENV"
test "$(awk -F= '$1 == "ATTENDEE_RELEASE_IMAGE" {
  print substr($0, index($0, "=") + 1)
}' "$ROLLBACK_RELEASE_DIR/.env.release")" = "$ROLLBACK_IMAGE"

cd "$ROLLBACK_RELEASE_DIR"
ROLLBACK_IMAGES_OUTPUT="$(
  docker compose \
    --env-file .env.release \
    -p attendee \
    -f docker-compose.prod.yml \
    config --images
)"
printf '%s\n' "$ROLLBACK_IMAGES_OUTPUT"
test "$(grep -Fxc "$ROLLBACK_IMAGE" \
  <<<"$ROLLBACK_IMAGES_OUTPUT" || true)" -eq 5

docker compose \
  --env-file .env.release \
  -p attendee \
  -f docker-compose.prod.yml \
  config --format json |
EXPECTED_IMAGE="$ROLLBACK_IMAGE" \
EXPECTED_CODE_DIR="$ROLLBACK_CODE_DIR" \
python3 -c '
import json
import os
import sys

services = json.load(sys.stdin)["services"]
expected_image = os.environ["EXPECTED_IMAGE"]
expected_code_dir = os.environ["EXPECTED_CODE_DIR"]
application_services = [
    "attendee-app",
    "attendee-worker",
    "attendee-launcher-worker",
    "attendee-webhook-worker",
    "attendee-scheduler",
]
for name in application_services:
    if services[name]["image"] != expected_image:
        raise SystemExit(f"{name}: imagem de rollback divergente")
for name in ["attendee-worker", "attendee-launcher-worker"]:
    environment = services[name]["environment"]
    if environment["BOT_CONTAINER_IMAGE"] != expected_image:
        raise SystemExit(f"{name}: BOT_CONTAINER_IMAGE divergente")
    if environment["BOT_HOST_CODE_PATH"] != expected_code_dir:
        raise SystemExit(f"{name}: BOT_HOST_CODE_PATH divergente")
'

test -z "$(docker ps -q --filter label=attendee.type=ephemeral-bot)"
sleep 3
test -z "$(docker ps -q --filter label=attendee.type=ephemeral-bot)"

rollback_celery_readiness() {
  local container="$1"
  local node_prefix="$2"
  local expected_queue="$3"
  local container_hostname
  local node
  local output

  if ! container_hostname="$(docker exec "$container" hostname 2>&1)"; then
    printf '%s\n' "$container_hostname"
    return 1
  fi
  if test -z "$container_hostname"; then
    return 1
  fi
  node="${node_prefix}@${container_hostname}"

  if ! output="$(docker exec "$container" \
    celery -A attendee inspect ping \
    --destination "$node" --timeout=5 2>&1)"; then
    printf '%s\n' "$output"
    return 1
  fi
  printf '%s\n' "$output"
  if ! grep -Fq "$node: OK" <<<"$output"; then
    return 1
  fi
  if ! grep -Fq 'pong' <<<"$output"; then
    return 1
  fi

  if ! output="$(docker exec "$container" \
    celery -A attendee inspect active_queues \
    --destination "$node" --timeout=5 2>&1)"; then
    printf '%s\n' "$output"
    return 1
  fi
  printf '%s\n' "$output"
  if ! grep -Fq "$expected_queue" <<<"$output"; then
    return 1
  fi

  return 0
}

ensure_rollback_release() {
  local rollback_failed=0
  local container
  local running
  local image_id
  local image_ref
  local http_healthy=0
  local readiness_ok
  local specification

  if ! cd "$ROLLBACK_RELEASE_DIR"; then
    echo "ERRO: diretório do rollback indisponível" >&2
    return 1
  fi

  if ! docker compose \
    --env-file .env.release \
    -p attendee \
    -f docker-compose.prod.yml \
    up -d \
    --no-deps \
    --force-recreate \
    attendee-app \
    attendee-worker \
    attendee-launcher-worker \
    attendee-webhook-worker \
    attendee-scheduler; then
    echo "ERRO: compose da release de rollback falhou" >&2
    rollback_failed=1
  fi

  for container in \
    attendee-attendee-app-1 \
    attendee-attendee-worker-1 \
    attendee-attendee-launcher-worker-1 \
    attendee-attendee-webhook-worker-1 \
    attendee-attendee-scheduler-1
  do
    running="$(docker inspect "$container" \
      --format '{{.State.Running}}' 2>/dev/null || true)"
    image_ref="$(docker inspect "$container" \
      --format '{{.Config.Image}}' 2>/dev/null || true)"
    image_id="$(docker inspect "$container" \
      --format '{{.Image}}' 2>/dev/null || true)"

    if test "$running" != "true"; then
      echo "ERRO: serviço de rollback não iniciou: $container" >&2
      rollback_failed=1
    fi
    if test "$image_ref" != "$ROLLBACK_IMAGE"; then
      echo "ERRO: referência de rollback divergente: $container" >&2
      rollback_failed=1
    fi
    if test "$image_id" != "$ROLLBACK_IMAGE_ID"; then
      echo "ERRO: ID da imagem de rollback divergente: $container" >&2
      rollback_failed=1
    fi
  done

  for attempt in $(seq 1 30); do
    if test "$(curl -fsS -o /dev/null -w '%{http_code}' \
      https://bots.firstlineai.com.br/health/ || true)" = "200"; then
      http_healthy=1
      break
    fi
    sleep 2
  done
  if test "$http_healthy" != "1"; then
    echo "ERRO: HTTP do rollback não estabilizou" >&2
    rollback_failed=1
  fi

  for specification in \
    'attendee-attendee-worker-1 worker celery' \
    'attendee-attendee-launcher-worker-1 launcher bot_launcher_vm' \
    'attendee-attendee-webhook-worker-1 webhook webhooks'
  do
    readiness_ok=0
    for attempt in $(seq 1 12); do
      if rollback_celery_readiness $specification; then
        readiness_ok=1
        break
      fi
      sleep 2
    done
    if test "$readiness_ok" != "1"; then
      echo "ERRO: readiness do rollback falhou: $specification" >&2
      rollback_failed=1
    fi
  done

  return "$rollback_failed"
}

ROLLBACK_FINISHED=0
ROLLBACK_RECOVERY_RUNNING=0

handle_rollback_interruption() {
  local exit_code="${1:-1}"

  if test "$ROLLBACK_FINISHED" = "1" ||
    test "$ROLLBACK_RECOVERY_RUNNING" = "1"; then
    return
  fi

  ROLLBACK_RECOVERY_RUNNING=1
  trap - ERR INT TERM HUP EXIT
  set +e

  if test "$exit_code" -eq 0; then
    exit_code=1
  fi

  if test -n "$(docker ps -q \
    --filter label=attendee.type=ephemeral-bot)"; then
    echo "ERRO: bot ativo; reaplicação automática bloqueada" >&2
    exit "$exit_code"
  fi

  echo "Rollback interrompido; reaplicando o rollback conhecido" >&2
  if ! ensure_rollback_release; then
    echo "ERRO: rollback conhecido não ficou operacional" >&2
  fi

  exit "$exit_code"
}

trap 'handle_rollback_interruption $?' ERR
trap 'handle_rollback_interruption 130' INT
trap 'handle_rollback_interruption 143' TERM
trap 'handle_rollback_interruption 129' HUP
trap 'handle_rollback_interruption $?' EXIT

PRE_DEPS="$(docker inspect \
  attendee-postgres-1 attendee-redis-1 attendee-minio-1 \
  --format '{{.Id}}' | paste -sd '|')"

docker stop --time 30 \
  attendee-attendee-scheduler-1 \
  attendee-attendee-launcher-worker-1

test -z "$(docker ps -q --filter label=attendee.type=ephemeral-bot)"

ensure_rollback_release

POST_DEPS="$(docker inspect \
  attendee-postgres-1 attendee-redis-1 attendee-minio-1 \
  --format '{{.Id}}' | paste -sd '|')"
test "$PRE_DEPS" = "$POST_DEPS"

ROLLBACK_FINISHED=1
trap - ERR INT TERM HUP EXIT
flock -u 9
```

Não reabra a criação de bots até o HTTP, os três nós Celery e um smoke normal
passarem. Não restaure o banco quando a release não aplicou migration. Não
remova a release que falhou até concluir a análise.

Execute rollback quando:

- o HTTP não estabilizar em 60 segundos;
- algum worker não responder;
- os serviços entrarem em loop de restart;
- o smoke normal não entregar áudio automaticamente;
- a recuperação parcial falhar;
- ocorrer perda, corrupção ou remoção prematura do arquivo local;
- surgir regressão que ameace novas gravações.

## 12. Encerramento da janela

Depois dos smokes, faça uma última validação dirigida e confirme que nenhum bot
de teste continua ativo. O trap do cutover já deve estar desarmado desde o fim
da seção 7; um erro de smoke deve ser tratado pelo rollback autocontido da
seção 11, somente depois de encerrar o bot de teste:

```bash
check_celery_readiness attendee-attendee-worker-1 worker celery
check_celery_readiness \
  attendee-attendee-launcher-worker-1 launcher bot_launcher_vm
check_celery_readiness attendee-attendee-webhook-worker-1 webhook webhooks
test -z "$(docker ps -q --filter label=attendee.type=ephemeral-bot)"

flock -u 9
```

Uma release só está concluída depois de:

- health checks reais aprovados;
- smoke normal aprovado;
- smoke forçado aprovado quando fizer parte da janela;
- nenhum bot ou gravação de teste preso;
- nenhuma fila crescendo;
- backup e checksums preservados;
- IDs da imagem e do commit registrados;
- rollback ainda executável;
- credenciais temporárias de teste revogadas;
- gate de criação de bots reaberto somente após aprovação do piloto.

Não remova imagens, releases ou backups durante o piloto. A retenção e a
limpeza devem seguir uma política separada e revisada.
