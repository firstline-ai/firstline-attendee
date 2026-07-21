# Release de estabilização do Attendee

Esta release trata somente o caminho de transcrição em tempo real do Attendee. Ela não envia mídia para R2, não ativa MP3 globalmente e não altera a FirstLine AI.

## O que ela protege

- Áudio `custom_async` é enviado ao serviço de transcrição em trechos de no máximo 25 segundos por padrão. As palavras do trecho seguinte recebem o deslocamento correto no timestamp final.
- Uma mesma utterance tem um claim atômico por tarefa. Entregas duplicadas do broker não submetem o mesmo áudio novamente enquanto o claim está ativo.
- O scheduler recupera, depois de 15 minutos, uma tarefa regular que perdeu a entrega no broker ou cujo worker morreu. Utterances de transcrição assíncrona em grupo são explicitamente excluídas dessa recuperação.
- Workers de bots, lançador e webhooks ficam separados. Todos os produtores direcionam callbacks para a fila `webhooks`; antes, apenas o consumidor estava configurado para ela.
- Bots efêmeros são removidos ao terminar. Logs seguem no container principal; habilitar retenção para depuração deve ser uma decisão temporária e explícita.

## Pré-requisito de deploy

Não fazer deploy desta branch sem uma aprovação explícita. Quando aprovada, construir uma imagem imutável e preparar um checkout do mesmo commit no host. A imagem da aplicação e o diretório montado nos bots efêmeros precisam apontar para o mesmo commit.

No host, antes de validar a configuração:

```bash
export ATTENDEE_RELEASE_IMAGE=firstline-attendee:<tag-imutavel>
export ATTENDEE_HOST_CODE_PATH=/home/deploy/apps/attendee-<commit>
export DOCKER_GID="$(stat -c %g /var/run/docker.sock)"
docker compose --env-file .env.prod -f docker-compose.prod.yml config
```

Configurações esperadas em `.env.prod`:

```dotenv
CUSTOM_ASYNC_TRANSCRIPTION_TIMEOUT=120
CUSTOM_ASYNC_MAX_CHUNK_DURATION_SECONDS=25
TRANSCRIPTION_CLAIM_STALE_SECONDS=900
```

Não colocar credenciais de R2 nem mudar `recording_format` nesta release.

## Piloto controlado

1. Executar uma reunião curta com fala contínua por mais de 30 segundos.
2. Confirmar nos logs que a utterance foi dividida e que os trechos foram aceitos pelo transcritor.
3. Confirmar um único callback de transcrição por utterance e o recebimento na FirstLine.
4. Confirmar que o bot sai da reunião e que não permanece container efêmero parado.
5. Acompanhar por 24 horas: nenhuma falha `524` ou `timed_out` em trechos curtos, nenhuma tarefa presa além de 15 minutos e nenhuma fila crescendo continuamente.

Se o piloto falhar, pausar a promoção e preservar somente os identificadores de bot/utterance e os logs necessários para diagnóstico. Não reativar retenção ilimitada de containers.
