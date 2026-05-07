from aws_lambda_powertools import Logger
from src.shared.utils.response_handler import create_response, handle_exception
from src.shared.infrastructure.database import get_platform_db

logger = Logger()


@logger.inject_lambda_context
def handler(event, context):
    try:
        logger.info("Health check lambda executed")

        db = get_platform_db()
        db.command('ping')

        return create_response(
            status_code=200,
            message="SIGA Backend is alive and DB is connected!",
            data={
                "status": "UP",
                "database": "CONNECTED",
                "service": "siga-backend"
            }
        )
    except Exception as e:
        return handle_exception(e)
