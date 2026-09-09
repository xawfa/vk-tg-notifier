FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY vk_email_to_tg.py .
CMD ["python", "vk_email_to_tg.py"]
