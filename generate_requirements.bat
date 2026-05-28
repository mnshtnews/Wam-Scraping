@echo off
echo Generating locked requirements.txt...
docker run --rm python:3.12-slim bash -c "pip install --quiet --upgrade pip --root-user-action=ignore && pip install --quiet --root-user-action=ignore asyncio-throttle aiohttp aiofiles playwright beautifulsoup4 lxml pydantic pydantic-settings supabase redis[hiredis] aiogram openai apscheduler loguru python-dotenv tenacity httpx dateparser fake-useragent pytz fastapi uvicorn[standard] sentry-sdk pytest pytest-asyncio pytest-mock && pip freeze" > requirements_locked.txt
echo Done! Check requirements_locked.txt
