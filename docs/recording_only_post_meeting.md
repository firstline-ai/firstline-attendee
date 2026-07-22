# Gravação de áudio para processamento pós-reunião

Este modo transforma o bot do Google Meet em um gravador de áudio. Ele não
ativa legendas, não cria utterances e não chama um provedor de transcrição
durante a reunião. O MP3 completo ou parcial é entregue ao armazenamento
primário e, quando configurado, ao bucket S3/R2 externo.

## Contrato de criação

Use explicitamente os dois campos abaixo. Bots existentes continuam usando o
fluxo anterior até que o cliente da API passe a enviar este contrato.

```json
{
  "recording_settings": {"format": "mp3"},
  "transcription_settings": {"none": {}},
  "external_media_storage_settings": {
    "bucket_name": "attendee-recordings-prod",
    "recording_file_name": "meetings/<meeting-id>.mp3"
  },
  "webhooks": [
    {
      "url": "https://example.com/webhooks/attendee",
      "triggers": ["bot.state_change", "recording.ready"]
    }
  ]
}
```

`recording.ready` é o único sinal de que o arquivo foi validado e está pronto
para ser enviado ao AssemblyAI. O evento contém `recording_id`, `is_partial`,
`duration_ms`, `file_size_bytes`, `sha256` e a chave do objeto. Ele não contém
credenciais nem URL pública. Ao receber o evento, o consumidor pode consultar o
bot na API do Attendee para obter uma URL primária assinada e atual, ou assinar a
chave do R2 com credenciais próprias. Não persista a URL temporária como se fosse
o identificador do ativo; use `recording_id`/`object_key`.

## Recuperação de falha

- O bot grava em `/attendee-recording-spool`, montado a partir do host.
- Um encerramento normal finaliza o FFmpeg e enfileira a entrega.
- SIGTERM, exceção interna ou recuperação pelo scheduler marca o áudio como
  parcial.
- Se o container morrer sem cleanup, o scheduler encontra o MP3 pelo par
  `bot_id`/`recording_id`, tenta repará-lo com FFmpeg e retoma a entrega.
- O arquivo local só é apagado depois da confirmação de tamanho no storage
  primário e no R2/S3 externo.
- Falhas persistentes mantêm o arquivo no spool e registram apenas dados de erro
  sanitizados no banco.

As credenciais S3/R2 precisam permitir `PutObject` e `HeadObject`: a segunda
permissão é usada para confirmar que o tamanho remoto corresponde ao arquivo
local antes de apagar o spool.

## Preparação do host

Esta release exige a migration `0086_recording_only_delivery` e um diretório
persistente. Criar o diretório antes de iniciar os containers:

```bash
sudo install -d -m 0770 -o 1000 -g 1000 /var/lib/attendee/recordings
```

Configuração esperada:

```dotenv
ATTENDEE_RECORDING_SPOOL_HOST_PATH=/var/lib/attendee/recordings
BOT_RECORDING_SPOOL_DIRECTORY=/attendee-recording-spool
RECORDING_DELIVERY_REENQUEUE_SECONDS=300
RECORDING_ORPHAN_RECOVERY_GRACE_SECONDS=120
```

Não ativar o novo contrato antes de aplicar a migration e confirmar o bind
mount com `docker compose config`. A migration somente adiciona colunas de
estado/metadados ao modelo `Recording` e amplia o enum de webhook; ela não
reescreve nem remove gravações existentes.
