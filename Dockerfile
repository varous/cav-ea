FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py storage.py operating.py attachments.py ./
RUN python -c "import app"
ENV PYTHONUNBUFFERED=1
USER 65532:65532
CMD ["gunicorn","--bind","0.0.0.0:8080","--workers","1","--threads","4","--timeout","600","--access-logfile","-","app:app"]
