import os
from aws_lambda_powertools import Logger
from src.shared.utils.response_handler import create_response, handle_exception

logger = Logger()


@logger.inject_lambda_context
def handler(event, context):
    try:
        logger.info("Environment info lambda executed")
        env_value = os.environ.get("ENV", "L")
        return create_response(
            status_code=200,
            message="Environment retrieved successfully",
            data={
                "env": env_value
            }
        )
    except Exception as e:
        return handle_exception(e)
