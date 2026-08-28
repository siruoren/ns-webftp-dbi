FROM python:3.11-slim

ARG VERSION=dev
LABEL maintainer="ns-webftp-dbi"
LABEL description="Switch DBI FTP Transfer Tool"
LABEL version="${VERSION}"

WORKDIR /app

# 安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制项目文件
COPY app.py .
COPY config.yml .
COPY templates/ templates/

# 默认配置
ENV FLASK_DEBUG=0
ENV HOST=0.0.0.0
ENV PORT=8090

EXPOSE 8090

# 挂载点：/games 用于扫描 Switch 安装包，/app/config.yml 用于持久化配置
VOLUME ["/games", "/app/config.yml"]

CMD ["python", "app.py"]
