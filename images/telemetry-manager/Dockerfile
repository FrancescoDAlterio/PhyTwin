FROM python:3.10-slim
WORKDIR /app
RUN pip install pyzmq
COPY app/main.py .
CMD ["python", "-u", "main.py"]