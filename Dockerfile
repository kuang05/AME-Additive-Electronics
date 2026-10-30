# AME 增材电子一体化 —— 云上 Demo 镜像
#
# 构建：  docker build -t ame-cloud .
# 运行：  docker run -p 8000:8000 ame-cloud
# 接 RDS：docker run -p 8000:8000 -e AME_PG_DSN="postgresql://user:pwd@host:5432/ame" ame-cloud
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AME_DATA_DIR=/data \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# shapely 需要 GEOS，顶层包很小
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgeos-c1v5 && rm -rf /var/lib/apt/lists/*

COPY requirements-cloud.txt .
RUN pip install -r requirements-cloud.txt

COPY ame/ ./ame/
COPY samples/ ./samples/

# 数据目录（工程库与 blob）挂卷
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# 健康检查：/api/health 会报告用的哪种存储后端
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health',timeout=4).status==200 else 1)"

CMD ["python", "-m", "uvicorn", "ame.web.app:app", \
     "--host", "0.0.0.0", "--port", "8000"]
