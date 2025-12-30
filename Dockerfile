FROM python:3.11-slim

WORKDIR /app

# 设置时区为中国东八区（Asia/Shanghai）
ENV TZ=Asia/Shanghai 

RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# 安装 git（用于 /upgrade），以及一些常见运行依赖
RUN apt-get update \
    && apt-get install -y --no-install-recommends git  vim ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 先拷贝依赖清单，利用缓存
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# 拷贝项目代码
COPY . /app

# 创建日志目录
RUN mkdir -p /app/log

EXPOSE 56578

CMD ["python3", "ncControl.py"] 
