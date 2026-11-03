FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 APP_ENV=production
WORKDIR /app/claw-and-order
COPY claw-and-order/requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir -r /tmp/requirements.lock
RUN useradd --create-home --uid 10001 claw
COPY --chown=10001:10001 claw-and-order/backend ./backend
COPY --chown=10001:10001 claw-and-order/config ./config
COPY --chown=10001:10001 claw-and-order/scripts ./scripts
COPY --chown=10001:10001 claw-and-order/reporter ./reporter
COPY --chown=10001:10001 claw-and-order/templates ./templates
USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
