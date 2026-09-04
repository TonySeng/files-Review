# 前端镜像：nginx 直接托管本地已构建的 dist
# 构建上下文为项目根目录。
#
# 说明：本环境拉取 node 基础镜像不稳定，故采用「本地构建 + nginx 托管」模式：
#   部署前请先在前端目录执行 `npm run build` 生成 dist/（已生成）。
#   这样既能稳定出包，也避免把 node_modules 打进镜像。
FROM nginx:1.27-alpine

COPY frontend/nginx.conf /etc/nginx/conf.d/default.conf
COPY frontend/dist /usr/share/nginx/html

EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
