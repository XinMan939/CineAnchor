FROM python:3.12-slim

WORKDIR /app

# 依赖层 (单独 COPY 利用构建缓存)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 应用: server 在根, 工具与前端在子目录
COPY server.py .
COPY tools/ tools/
COPY web/ web/

# config.json 由部署者提供 (真实 token 不进镜像):
#   docker compose 里挂载 ./config.json:/app/config.json
#   首次部署: cp tools/config.example.json config.json 后填入 token
EXPOSE 8787

# 数据卷挂载点: cache.db / index.db 建议都映射到宿主机
VOLUME ["/app/data"]

ENV CINEANCHOR_DATA_DIR=/app/data

CMD ["python", "server.py", "--host", "0.0.0.0", "--port", "8787"]
