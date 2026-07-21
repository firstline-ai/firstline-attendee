# Attendee: confiabilidade de pós-reunião em QA

## O que é durável

- O upload do MP3 para o storage externo é persistido como `PENDING`,
  `UPLOADING`, `COMPLETE` ou `FAILED`; o scheduler recupera tarefas
  interrompidas.
- O webhook de estado `ended`, que inicia a análise na FirstLine, é persistido
  antes da entrega. Ele recebe até 12 tentativas e o scheduler reinsere uma
  entrega falha ou presa por mais de cinco minutos, preservando o mesmo
  `idempotency_key`.
- Trechos de áudio possuem um claim de task; uma entrega duplicada do Celery
  não pode submetê-los duas vezes ao provedor de transcrição.

## Pré-requisitos de QA

1. Aplicar as migrations Django `0085` e `0086` antes de iniciar código novo.
2. Manter ativos o worker Celery e `python manage.py run_scheduler`.
3. Usar bucket, credenciais e retenção isolados de produção. A credencial R2
   precisa de leitura e escrita no objeto, pois o upload é confirmado por
   `HeadObject`.
4. Testar falha transitória de R2, indisponibilidade da FirstLine e restart de
   worker/scheduler. O resultado esperado é `COMPLETE`, sem análise duplicada.

## Limite antes de produção

Uma entrega que atingir o orçamento máximo fica registrada como `FAILED` para
intervenção e alerta operacional. Não promover enquanto existir qualquer
`FAILED` sem alerta e procedimento de reenvio confirmado.
