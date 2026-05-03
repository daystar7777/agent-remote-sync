FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml README.md README.ko.md LICENSE ./
COPY src ./src
COPY tools ./tools
COPY smoke.py ./

RUN python -m pip install --no-cache-dir -e .

CMD ["python", "-m", "agentremote", "--help"]
