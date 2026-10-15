FROM nginx:1.27-alpine

RUN rm /etc/nginx/conf.d/default.conf
COPY nginx/nginx.conf /etc/nginx/nginx.conf
COPY nginx/app.conf /etc/nginx/conf.d/app.conf
COPY nginx/proxy_params.conf /etc/nginx/conf.d/proxy_params.conf
# También en la imagen, aunque el compose lo monte encima: sin él, un «docker
# run» de esta imagen suelta no arrancaría, porque app.conf lo incluye.
# En snippets y no en conf.d: nginx.conf incluye todo conf.d/*.conf en el
# bloque http, donde «return» no se admite, y con un certificado la redirección
# impedía arrancar a Nginx.
COPY nginx/tls_redirect.conf /etc/nginx/snippets/tls_redirect.conf

RUN mkdir -p /etc/nginx/certs

EXPOSE 80 443
