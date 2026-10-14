# NAS（Synology / QNAP）或任何能跑 Docker 的主機使用
FROM python:3.12-slim
ENV PYTHONPATH=/app/src PYTHONUNBUFFERED=1 TZ=Asia/Taipei
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# 預設一次跑完：蒐集 → 萃取 → 等到 09:30 寄出報告
ENTRYPOINT ["python", "-m", "display_intel"]
CMD ["run"]
