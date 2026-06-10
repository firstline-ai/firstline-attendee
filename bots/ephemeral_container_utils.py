import logging

import docker

logger = logging.getLogger(__name__)


def terminate_ephemeral_docker_container(bot):
    """Remove the ephemeral Docker container for a bot without failing caller flow."""
    try:
        client = docker.from_env()
    except Exception as e:
        logger.warning(f"Cannot connect to Docker to terminate bot {bot.id}: {e}")
        return False

    container_name = bot.ephemeral_container_name()
    try:
        container = client.containers.get(container_name)
        container.remove(force=True)
        logger.info(f"Removed ephemeral container: {container_name}")
        return True
    except docker.errors.NotFound:
        logger.info(f"No ephemeral container found for bot: {container_name}")
        return False
    except Exception as e:
        logger.warning(f"Error removing container {container_name}: {e}")
        return False
