FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY bms_gateway ./bms_gateway
COPY schemas ./schemas
# Editable install keeps the package next to schemas/, where contract.py loads them from.
RUN pip install --no-cache-dir -e .

CMD ["python", "-m", "bms_gateway.simulator"]
