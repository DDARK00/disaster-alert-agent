# 빌드 컨텍스트: 저장소 루트
FROM nginx:1.27-alpine
COPY frontend/nginx.conf /etc/nginx/conf.d/default.conf
COPY frontend/index.html /usr/share/nginx/html/index.html
EXPOSE 80
