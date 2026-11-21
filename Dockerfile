FROM python:3.13-slim
WORKDIR /bot
COPY . .
ENTRYPOINT ["python3", "-u", "/bot/rooftop_ninja.py"]
