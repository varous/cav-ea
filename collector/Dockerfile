FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY collector.py drive_delivery.py cloud_runner.py verify_replay.py /app/
ENV COLLECTOR_RUNTIME=/tmp/ea-collector PYTHONUNBUFFERED=1
USER 65532:65532
ENTRYPOINT ["python", "cloud_runner.py"]
