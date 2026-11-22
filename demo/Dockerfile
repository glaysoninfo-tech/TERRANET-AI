FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY pit_demo.py /app/pit_demo.py

USER 10001:10001

CMD ["python", "-B", "pit_demo.py"]
