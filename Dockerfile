FROM python:3.11-slim

# 设置工作目录
WORKDIR /app

# 设置时区为中国东八区（Asia/Shanghai）
ENV TZ=Asia/Shanghai
RUN apt-get update && apt-get install -y tzdata && apt-get clean
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone


# 安装系统依赖
RUN apt-get update && apt-get install -y \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖文件并安装Python包
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY ncControl.py .
COPY qb_client.py .
COPY qb_rss.py .
COPY logger.py .
COPY frontend .

# 暴露端口
EXPOSE 56578

# 设置启动命令

CMD ["python3", "ncControl.py"] 
