import logging

from bots.models import Bot, BotStates

logger = logging.getLogger(__name__)


def skip_terminal_bot_launch(bot_id: int):
    bot = Bot.objects.filter(id=bot_id).only("id", "object_id", "state").first()

    if not bot:
        logger.warning("Skipping bot launch for missing bot %s", bot_id)
        return {"bot_id": bot_id, "status": "skipped", "reason": "bot_not_found"}

    if bot.state in BotStates.post_meeting_states():
        logger.warning("Skipping bot launch for terminal bot %s (%s) in state %s", bot.object_id, bot.id, BotStates.state_to_api_code(bot.state))
        return {"bot_id": bot_id, "status": "skipped", "reason": "bot_in_terminal_state"}

    return None
