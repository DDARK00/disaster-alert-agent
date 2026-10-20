# 빌드 컨텍스트: 저장소 루트
FROM nginx:1.27-alpine
# 호스트 파일 권한(600 등)이 이미지로 그대로 복사되므로 읽기 권한을 고정한다 (T-019)
COPY --chmod=644 frontend/nginx.conf /etc/nginx/conf.d/default.conf
COPY --chmod=644 frontend/index.html /usr/share/nginx/html/index.html
EXPOSE 80
