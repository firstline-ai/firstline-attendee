import logging

from celery import shared_task

from bots.ephemeral_container_utils import terminate_ephemeral_docker_container
from bots.models import (
    Bot,
    BotEventManager,
    BotEventSubTypes,
    BotEventTypes,
    BotStates,
)

logger = logging.getLogger(__name__)


@shared_task(bind=True)
def kill_stuck_joining_bot(self, bot_id):
    """
    Watchdog que mata bots presos em JOINING.

    Agendado via apply_async(countdown=...) quando o bot transiciona para
    JOINING (hook em BotEventManager.create_event). Se ao acordar o bot
    ainda estiver em JOINING, dispara FATAL_ERROR para liberar o slot
    e propagar o webhook ao backend.
    """
    try:
        bot = Bot.objects.get(id=bot_id)
    except Bot.DoesNotExist:
        logger.info(f"Joining watchdog: bot {bot_id} não existe mais")
        return

    if bot.state != BotStates.JOINING:
        logger.info(
            f"Joining watchdog: bot {bot.object_id} ({bot_id}) já saiu do estado JOINING "
            f"(state atual={BotStates.state_to_api_code(bot.state)}); nada a fazer"
        )
        return

    logger.warning(
        f"Joining watchdog: bot {bot.object_id} ({bot_id}) preso em JOINING; forçando FATAL_ERROR"
    )
    try:
        BotEventManager.create_event(
            bot=bot,
            event_type=BotEventTypes.FATAL_ERROR,
            event_sub_type=BotEventSubTypes.FATAL_ERROR_BOT_NOT_LAUNCHED,
            event_metadata={"reason": "joining_watchdog_timeout"},
        )
    except Exception as e:
        logger.error(
            f"Joining watchdog: falha ao criar FATAL_ERROR para bot {bot_id}: {e}"
        )
        return

    terminate_ephemeral_docker_container(bot)
